"""
Item × Energy Impact Analysis
=============================
이 스크립트는 2024년부터 적재된 일별 품목 생산실적(`production_daily`)과
일별 에너지 사용량(`energy_daily`)을 결합하여, **품목/제품유형이 에너지 소비에
미치는 영향**을 다층적으로 정량화합니다.

목적
----
1. 어떤 품목이 어떤 에너지원(전력/연료/용수) 소비를 가장 많이 끌어올리는지 식별
2. 신규 품목 도입이나 특정 품목 생산량 증감이 가져올 에너지 변동을 예측할 수 있도록
   "kg 당 에너지 소비량" 계수(category × factory × target) 산출
3. AI 보고서가 What-if 질문에 답할 수 있도록 lookup JSON 저장

산출물 (analysis_results/ 폴더)
-------------------------------
* item_energy_correlations.csv      — (factory, target, item_code, item_name)별 Pearson/Spearman/mix-adjusted 상관
* category_energy_intensity.csv     — (factory, target, category2)별 회귀 계수 (kWh/kg, Nm³/kg, ton/kg)
* item_energy_intensity_top.csv     — Lasso 기반 sparse 회귀로 추출한 상위 영향 품목
* item_energy_impact_report.md      — 한국어 요약 리포트 (AI/엔지니어 양쪽 활용)
* item_energy_impact_lookup.json    — `app/services/item_energy_impact_service.py` 가 사용하는 룩업

분석 기법
---------
* Pearson / Spearman: 단일 품목 vs 일별 에너지 사용량 상관 (선형/순위)
* Mix-adjusted: 일별 총 mix_prod_kg 의 영향을 제거한 후 잔차 상관 (체적 효과 분리)
* Lag (t, t+1): 같은 날 / 다음 날 에너지 신호 (공정 지연 검증)
* Ridge / Lasso 회귀: category2 또는 item 수준에서 단위(kg) 당 평균 에너지 소비
* Bootstrap 95% CI: 회귀 계수 안정성

사용법
-----
    py -3 tools/analyze_item_energy_impact.py
    py -3 tools/analyze_item_energy_impact.py --year-from 2024 --year-to 2026
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Windows cp949 콘솔 한글 출력
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from app.database.db_connection import get_connection  # noqa: E402

logger = logging.getLogger(__name__)

OUTPUT_DIR = ROOT / "analysis_results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 분석 대상 ────────────────────────────────────────────────
TARGETS: dict[str, dict] = {
    "전력": {"col": "total_power_kwh", "unit": "kWh"},
    "연료": {"col": "fuel_nm3",       "unit": "Nm³"},
    "용수": {"col": "water_ton",      "unit": "ton"},
}
# energy_daily 의 factory 코드 vs production_daily 의 factory 매핑
# 남양주 은 production_daily 에 합산되어 있음 → energy 측 남양주1+남양주2 합으로 비교
FACTORY_PAIRS: list[tuple[str, list[str]]] = [
    ("남양주", ["남양주1", "남양주2"]),
    ("김해", ["김해"]),
    ("광주", ["광주"]),
    ("논산", ["논산"]),
]

# 분석에 사용하기에 너무 희소하면 노이즈가 됨 → 최소 발생일 / 최소 비중
MIN_NONZERO_DAYS = 30
MIN_TOTAL_KG_RATIO = 0.001  # factory 전체 생산의 0.1% 이상


# ── 데이터 로딩 ──────────────────────────────────────────────
def load_data(year_from: int, year_to: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """production_daily, energy_daily 를 한꺼번에 로드."""
    conn = get_connection()
    try:
        prod = pd.read_sql_query(
            """
            SELECT date, item_code, item_name, factory, category1, category2, actual_qty
            FROM production_daily
            WHERE date BETWEEN %s AND %s
            """,
            conn, params=(f"{year_from}-01-01", f"{year_to}-12-31"),
        )
        energy = pd.read_sql_query(
            """
            SELECT date, factory, mix_prod_kg, total_power_kwh, fuel_nm3, water_ton
            FROM energy_daily
            WHERE date BETWEEN %s AND %s
            """,
            conn, params=(f"{year_from}-01-01", f"{year_to}-12-31"),
        )
    finally:
        conn.close()

    prod["date"] = pd.to_datetime(prod["date"])
    energy["date"] = pd.to_datetime(energy["date"])
    prod["actual_qty"] = pd.to_numeric(prod["actual_qty"], errors="coerce").fillna(0.0)
    return prod, energy


def aggregate_factory_energy(energy: pd.DataFrame, energy_codes: list[str]) -> pd.DataFrame:
    """단일/다중 energy factory 코드 합산해서 일별 에너지 시계열 반환."""
    df = energy[energy["factory"].isin(energy_codes)].copy()
    if df.empty:
        return pd.DataFrame(columns=["date", "mix_prod_kg", "total_power_kwh", "fuel_nm3", "water_ton"])
    g = df.groupby("date", as_index=False).agg(
        mix_prod_kg=("mix_prod_kg", "sum"),
        total_power_kwh=("total_power_kwh", "sum"),
        fuel_nm3=("fuel_nm3", "sum"),
        water_ton=("water_ton", "sum"),
    )
    return g.sort_values("date").reset_index(drop=True)


def pivot_factory_items(prod: pd.DataFrame, prod_factory: str) -> pd.DataFrame:
    """공장별 품목 일별 생산량을 wide 포맷으로 변환.

    columns: date, item_<code>, ...
    """
    sub = prod[prod["factory"] == prod_factory].copy()
    if sub.empty:
        return pd.DataFrame(columns=["date"])
    # 날짜 × item_code 피벗
    p = sub.pivot_table(
        index="date", columns="item_code", values="actual_qty",
        aggfunc="sum", fill_value=0.0,
    )
    p.columns = [f"item_{c}" for c in p.columns]
    p = p.reset_index()
    return p


def get_item_metadata(prod: pd.DataFrame) -> pd.DataFrame:
    """품목 메타 — item_code → name, category1, category2, factory"""
    m = (
        prod.dropna(subset=["item_code"])
        .groupby(["factory", "item_code"], as_index=False)
        .agg(item_name=("item_name", "first"),
             category1=("category1", "first"),
             category2=("category2", "first"),
             total_actual_qty=("actual_qty", "sum"))
    )
    return m


# ── 분석 루틴 ────────────────────────────────────────────────
def _safe_corr(x: np.ndarray, y: np.ndarray, method: str = "pearson") -> float:
    if len(x) < 5 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    if method == "pearson":
        return float(np.corrcoef(x, y)[0, 1])
    # Spearman = pearson on ranks
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    return float(np.corrcoef(rx, ry)[0, 1])


def _mix_adjusted_corr(item_qty: np.ndarray, target: np.ndarray, mix: np.ndarray) -> float:
    """믹스생산량 영향을 제거한 잔차 상관.

    target_resid = target - β·mix    (β = OLS 1d 회귀)
    item_resid   = item_qty - β·mix
    return corr(item_resid, target_resid)
    """
    if len(item_qty) < 5:
        return float("nan")
    if np.std(mix) == 0:
        return _safe_corr(item_qty, target)
    beta_t, _ = np.polyfit(mix, target, 1)
    beta_i, _ = np.polyfit(mix, item_qty, 1)
    target_resid = target - beta_t * mix
    item_resid = item_qty - beta_i * mix
    return _safe_corr(item_resid, target_resid)


def correlate_items(
    prod_pivot: pd.DataFrame,
    energy_daily: pd.DataFrame,
    factory: str,
) -> pd.DataFrame:
    """단일 공장의 품목 vs 에너지 상관표 반환."""
    if prod_pivot.empty or energy_daily.empty:
        return pd.DataFrame()

    df = prod_pivot.merge(energy_daily, on="date", how="inner")
    if df.empty:
        return pd.DataFrame()

    item_cols = [c for c in df.columns if c.startswith("item_")]
    rows: list[dict] = []
    mix = df["mix_prod_kg"].to_numpy(dtype=float)

    for tname, spec in TARGETS.items():
        y = df[spec["col"]].to_numpy(dtype=float)
        for ic in item_cols:
            x = df[ic].to_numpy(dtype=float)
            nonzero = int((x > 0).sum())
            if nonzero < MIN_NONZERO_DAYS:
                continue
            total = float(x.sum())
            row = {
                "factory": factory,
                "target": tname,
                "item_code": ic.replace("item_", ""),
                "nonzero_days": nonzero,
                "total_kg": total,
                "pearson": _safe_corr(x, y, "pearson"),
                "spearman": _safe_corr(x, y, "spearman"),
                "mix_adjusted": _mix_adjusted_corr(x, y, mix),
                # +1 일 lag (당일 생산 → 다음날 에너지)
                "pearson_lag1": _safe_corr(x[:-1], y[1:], "pearson"),
            }
            rows.append(row)
    return pd.DataFrame(rows)


def regress_category_intensity(
    prod: pd.DataFrame,
    energy_daily: pd.DataFrame,
    factory: str,
    energy_codes: list[str],
) -> pd.DataFrame:
    """(category1, category2) 단위 일별 생산량 → 에너지에 대한 OLS / Ridge 회귀.

    energy_target ≈ Σ β_c × cat_qty_c + α  (intercept)
    β_c 는 "category2 에서 1 kg 추가 생산 시 평균적으로 추가되는 에너지" 추정값.
    """
    sub = prod[prod["factory"] == factory].copy()
    if sub.empty:
        return pd.DataFrame()
    sub["cat_key"] = (
        sub["category1"].fillna("미분류")
        + "/" + sub["category2"].fillna("(미분류)")
    )
    pivot = sub.pivot_table(
        index="date", columns="cat_key", values="actual_qty",
        aggfunc="sum", fill_value=0.0,
    ).reset_index()

    df = pivot.merge(energy_daily, on="date", how="inner")
    if df.empty or len(df) < 30:
        return pd.DataFrame()

    cat_cols = [c for c in pivot.columns if c != "date"]
    X = df[cat_cols].to_numpy(dtype=float)

    rows: list[dict] = []
    for tname, spec in TARGETS.items():
        y = df[spec["col"]].to_numpy(dtype=float)
        if np.std(y) == 0:
            continue

        # Ridge 회귀 (closed-form, 의존성 최소화)
        n_features = X.shape[1]
        # 표준화
        x_mean = X.mean(axis=0)
        x_std = np.where(X.std(axis=0) > 1e-9, X.std(axis=0), 1.0)
        X_std = (X - x_mean) / x_std
        y_mean = y.mean()
        y_centered = y - y_mean
        # Ridge with λ=1
        lam = 1.0
        XtX = X_std.T @ X_std + lam * np.eye(n_features)
        Xty = X_std.T @ y_centered
        beta_std = np.linalg.solve(XtX, Xty)
        beta_orig = beta_std / x_std  # 원래 단위로 환원 (kg → energy unit)
        intercept = y_mean - (beta_orig * x_mean).sum()

        # R² (in-sample, 참고용)
        y_hat = X @ beta_orig + intercept
        ss_res = ((y - y_hat) ** 2).sum()
        ss_tot = ((y - y_mean) ** 2).sum()
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        # bootstrap 95% CI for β
        rng = np.random.default_rng(0)
        n_boot = 200
        boot_betas = np.zeros((n_boot, n_features))
        for b in range(n_boot):
            idx = rng.integers(0, len(X), size=len(X))
            Xb = X_std[idx]
            yb = y_centered[idx]
            try:
                bb_std = np.linalg.solve(Xb.T @ Xb + lam * np.eye(n_features), Xb.T @ yb)
                boot_betas[b] = bb_std / x_std
            except np.linalg.LinAlgError:
                boot_betas[b] = beta_orig
        ci_lo = np.percentile(boot_betas, 2.5, axis=0)
        ci_hi = np.percentile(boot_betas, 97.5, axis=0)

        for j, c in enumerate(cat_cols):
            cat1, cat2 = c.split("/", 1) if "/" in c else (c, "")
            rows.append({
                "factory": factory,
                "target": tname,
                "unit": spec["unit"],
                "category1": cat1,
                "category2": cat2,
                "intensity_per_kg": float(beta_orig[j]),  # energy_unit per kg
                "ci_lo": float(ci_lo[j]),
                "ci_hi": float(ci_hi[j]),
                "share_of_kg": float(X[:, j].sum() / X.sum()) if X.sum() > 0 else 0.0,
                "r2_model": float(r2),
                "intercept": float(intercept),
                "samples": int(len(df)),
            })

    return pd.DataFrame(rows)


def lasso_top_items(
    prod_pivot: pd.DataFrame,
    energy_daily: pd.DataFrame,
    factory: str,
    top_n: int = 10,
) -> pd.DataFrame:
    """품목 수준에서 Lasso 회귀로 가장 큰 양의 영향을 보이는 품목 추출.

    주의: scikit-learn 의존성을 피하기 위해 coordinate-descent 직접 구현.
    """
    if prod_pivot.empty or energy_daily.empty:
        return pd.DataFrame()
    df = prod_pivot.merge(energy_daily, on="date", how="inner")
    if df.empty or len(df) < 60:
        return pd.DataFrame()

    item_cols = [c for c in df.columns if c.startswith("item_")]
    if not item_cols:
        return pd.DataFrame()

    # 너무 희소한 컬럼 제외
    keep = [c for c in item_cols if (df[c] > 0).sum() >= MIN_NONZERO_DAYS]
    if not keep:
        return pd.DataFrame()
    X = df[keep].to_numpy(dtype=float)
    n, p = X.shape
    # 표준화
    mu = X.mean(axis=0); sd = X.std(axis=0); sd[sd < 1e-9] = 1.0
    Xs = (X - mu) / sd

    rows: list[dict] = []
    for tname, spec in TARGETS.items():
        y = df[spec["col"]].to_numpy(dtype=float)
        if np.std(y) == 0:
            continue
        ymu = y.mean()
        yc = y - ymu

        # coordinate descent Lasso
        beta = np.zeros(p)
        # alpha 자동 결정 (max correlation 의 0.05)
        alpha = 0.05 * np.max(np.abs(Xs.T @ yc) / n)
        for _ in range(300):
            beta_old = beta.copy()
            for j in range(p):
                rj = yc - Xs @ beta + Xs[:, j] * beta[j]
                rho = (Xs[:, j] @ rj) / n
                if rho > alpha:
                    beta[j] = rho - alpha
                elif rho < -alpha:
                    beta[j] = rho + alpha
                else:
                    beta[j] = 0
            # 수렴 체크
            if np.max(np.abs(beta - beta_old)) < 1e-6:
                break

        # 원래 스케일로 환원
        beta_orig = beta / sd
        # 정렬: 큰 양의 영향
        order = np.argsort(-np.abs(beta_orig))[:top_n]
        for k, j in enumerate(order):
            if beta_orig[j] == 0:
                continue
            rows.append({
                "factory": factory,
                "target": tname,
                "unit": spec["unit"],
                "rank": k + 1,
                "item_code": keep[j].replace("item_", ""),
                "intensity_per_kg": float(beta_orig[j]),
                "total_kg": float(X[:, j].sum()),
                "nonzero_days": int((X[:, j] > 0).sum()),
                "alpha": float(alpha),
            })
    return pd.DataFrame(rows)


# ── 메인 분석 파이프라인 ──────────────────────────────────────
def run_full_analysis(year_from: int, year_to: int) -> dict[str, Path]:
    t0 = time.time()
    print(f"[1/5] 데이터 로드 중 ({year_from} ~ {year_to})...")
    prod, energy = load_data(year_from, year_to)
    print(f"   production_daily: {len(prod):,}행, energy_daily: {len(energy):,}행")

    item_meta = get_item_metadata(prod)

    all_corr: list[pd.DataFrame] = []
    all_cat: list[pd.DataFrame] = []
    all_lasso: list[pd.DataFrame] = []

    print("[2/5] 공장별 분석 시작...")
    for prod_factory, energy_codes in FACTORY_PAIRS:
        print(f"   - {prod_factory} (energy={energy_codes})")
        ed = aggregate_factory_energy(energy, energy_codes)
        if ed.empty:
            print(f"     · energy 없음 — skip")
            continue
        # 품목 wide
        pp = pivot_factory_items(prod, prod_factory)
        if pp.empty:
            print(f"     · production_daily 없음 — skip")
            continue

        # 1) 품목 상관
        c = correlate_items(pp, ed, prod_factory)
        if not c.empty:
            all_corr.append(c)
            print(f"     · 품목 상관 행 {len(c):,}")

        # 2) 카테고리 ridge
        cat = regress_category_intensity(prod, ed, prod_factory, energy_codes)
        if not cat.empty:
            all_cat.append(cat)
            print(f"     · category 회귀 행 {len(cat):,}")

        # 3) Lasso top N
        ls = lasso_top_items(pp, ed, prod_factory)
        if not ls.empty:
            all_lasso.append(ls)
            print(f"     · Lasso top 행 {len(ls):,}")

    print("[3/5] 결과 집계 / 메타 결합...")
    df_corr = pd.concat(all_corr, ignore_index=True) if all_corr else pd.DataFrame()
    df_cat = pd.concat(all_cat, ignore_index=True) if all_cat else pd.DataFrame()
    df_lasso = pd.concat(all_lasso, ignore_index=True) if all_lasso else pd.DataFrame()

    if not df_corr.empty:
        df_corr = df_corr.merge(
            item_meta[["factory", "item_code", "item_name", "category1", "category2"]],
            on=["factory", "item_code"], how="left",
        )
        df_corr = df_corr.sort_values(
            ["factory", "target", "mix_adjusted"],
            key=lambda s: s.abs() if s.name == "mix_adjusted" else s,
            ascending=[True, True, False],
        )
    if not df_lasso.empty:
        df_lasso = df_lasso.merge(
            item_meta[["factory", "item_code", "item_name", "category1", "category2"]],
            on=["factory", "item_code"], how="left",
        )

    print("[4/5] 파일 저장...")
    paths: dict[str, Path] = {}
    if not df_corr.empty:
        p = OUTPUT_DIR / "item_energy_correlations.csv"
        df_corr.to_csv(p, index=False, encoding="utf-8-sig")
        paths["correlations"] = p

    if not df_cat.empty:
        p = OUTPUT_DIR / "category_energy_intensity.csv"
        df_cat.to_csv(p, index=False, encoding="utf-8-sig")
        paths["category_intensity"] = p

    if not df_lasso.empty:
        p = OUTPUT_DIR / "item_energy_intensity_top.csv"
        df_lasso.to_csv(p, index=False, encoding="utf-8-sig")
        paths["lasso_top"] = p

    # JSON lookup
    lookup = build_impact_lookup(df_cat, df_lasso, item_meta)
    p = OUTPUT_DIR / "item_energy_impact_lookup.json"
    p.write_text(json.dumps(lookup, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["lookup"] = p

    # MD 리포트
    md = build_markdown_report(df_corr, df_cat, df_lasso, year_from, year_to)
    p = OUTPUT_DIR / "item_energy_impact_report.md"
    p.write_text(md, encoding="utf-8")
    paths["report"] = p

    dt = time.time() - t0
    print(f"[5/5] 완료 ({dt:.1f}s) — {len(paths)}개 파일 저장")
    for name, path in paths.items():
        print(f"   · {name:20s} → {path}")
    return paths


# ── lookup JSON 구성 ────────────────────────────────────────
def build_impact_lookup(
    df_cat: pd.DataFrame, df_lasso: pd.DataFrame, item_meta: pd.DataFrame,
) -> dict:
    """AI / 서비스 모듈에서 'kg → 에너지' 추정에 사용할 룩업.

    구조:
      {
        "by_category": {
          "광주": {
            "전력": [{"category1": "냉장", "category2": "FM",
                      "intensity_per_kg": 0.234, "unit": "kWh", "ci_lo":..., "ci_hi":..., ...}, ...]
            ...
          }
        },
        "by_item_top": {
          "광주": {"전력": [{"item_code": "...", "intensity_per_kg":..., "item_name":...}, ...]}
        },
        "factories": ["남양주", ...],
        "targets": ["전력","연료","용수"],
        "generated_at": "..."
      }
    """
    from datetime import datetime

    lookup: dict = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "factories": list({r[0] for r in FACTORY_PAIRS}),
        "targets": list(TARGETS.keys()),
        "by_category": {},
        "by_item_top": {},
    }
    if not df_cat.empty:
        for f in df_cat["factory"].unique():
            lookup["by_category"][f] = {}
            for t in df_cat[df_cat["factory"] == f]["target"].unique():
                rows = df_cat[(df_cat["factory"] == f) & (df_cat["target"] == t)].copy()
                rows = rows.sort_values("intensity_per_kg", ascending=False)
                lookup["by_category"][f][t] = rows.to_dict(orient="records")

    if not df_lasso.empty:
        for f in df_lasso["factory"].unique():
            lookup["by_item_top"][f] = {}
            for t in df_lasso[df_lasso["factory"] == f]["target"].unique():
                rows = df_lasso[(df_lasso["factory"] == f) & (df_lasso["target"] == t)].copy()
                lookup["by_item_top"][f][t] = rows.to_dict(orient="records")

    return lookup


# ── Markdown 리포트 ──────────────────────────────────────────
def build_markdown_report(
    df_corr: pd.DataFrame, df_cat: pd.DataFrame, df_lasso: pd.DataFrame,
    y0: int, y1: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# 품목 × 에너지 영향 분석 리포트")
    lines.append("")
    lines.append(f"**분석 기간**: {y0}-01-01 ~ {y1}-12-31")
    lines.append(f"**대상 테이블**: `production_daily` × `energy_daily`")
    lines.append(f"**대상 에너지**: 전력 (`total_power_kwh`), 연료 (`fuel_nm3`), 용수 (`water_ton`)")
    lines.append("")

    lines.append("## 1. 분석 방법")
    lines.append("")
    lines.append("- **Pearson / Spearman 상관**: 품목별 일별 생산량 vs 일별 에너지 사용량")
    lines.append("- **Mix-adjusted 잔차 상관**: `mix_prod_kg` 영향을 회귀로 제거한 후 남는 상관")
    lines.append("  → '체적 효과'를 제거하고 품목 고유의 부하 패턴만 본다")
    lines.append("- **Lag(t+1) 상관**: 당일 생산 → 다음날 에너지 (지연 부하)")
    lines.append("- **Ridge 회귀 (category 단위)**: 일별 카테고리 합계 → 에너지. 계수 = kg 당 에너지 사용량")
    lines.append("- **Lasso 회귀 (item 단위)**: 200+ 품목 중 sparse 하게 영향 큰 품목만 선택")
    lines.append("- **Bootstrap 95% CI**: 200회 리샘플로 회귀 계수 신뢰구간")
    lines.append("")

    lines.append("## 2. category 단위 에너지 집약도 (Ridge)")
    lines.append("")
    if df_cat.empty:
        lines.append("(데이터 부족 — 결과 없음)")
    else:
        for f in sorted(df_cat["factory"].unique()):
            lines.append(f"### {f}")
            for t in df_cat[df_cat["factory"] == f]["target"].unique():
                sub = df_cat[(df_cat["factory"] == f) & (df_cat["target"] == t)].copy()
                sub = sub.sort_values("intensity_per_kg", ascending=False)
                unit = sub["unit"].iloc[0]
                r2 = sub["r2_model"].iloc[0]
                lines.append(f"- **{t}** (R²={r2:.2f}, n={int(sub['samples'].iloc[0])}일)")
                for _, r in sub.iterrows():
                    lines.append(
                        f"  - {r['category1']}/{r['category2'] or '(미분류)'}: "
                        f"`{r['intensity_per_kg']:.4f}` {unit}/kg  "
                        f"(95% CI [{r['ci_lo']:.4f}, {r['ci_hi']:.4f}])  "
                        f"비중 {r['share_of_kg']*100:.1f}%"
                    )
            lines.append("")

    lines.append("## 3. Lasso top 품목 (factory × target)")
    lines.append("")
    lines.append("> 양의 큰 계수 = 해당 품목이 1 kg 추가될 때 에너지가 평균적으로 더 소비되는 신호.")
    lines.append("> 음수 = 같은 라인을 점유해 다른 고집약 품목 생산을 밀어내는 대체효과로 볼 수 있음.")
    lines.append("")
    if df_lasso.empty:
        lines.append("(데이터 부족 — 결과 없음)")
    else:
        for f in sorted(df_lasso["factory"].unique()):
            lines.append(f"### {f}")
            for t in df_lasso[df_lasso["factory"] == f]["target"].unique():
                sub = df_lasso[(df_lasso["factory"] == f) & (df_lasso["target"] == t)].copy()
                unit = sub["unit"].iloc[0]
                lines.append(f"- **{t}** (단위 {unit}/kg)")
                for _, r in sub.iterrows():
                    name = r.get("item_name") or "-"
                    cat = r.get("category2") or "-"
                    sign = "+" if r["intensity_per_kg"] >= 0 else ""
                    lines.append(
                        f"  - rank {r['rank']:>2} · {r['item_code']} {name[:28]} ({cat}): "
                        f"`{sign}{r['intensity_per_kg']:.4f}` {unit}/kg  "
                        f"(생산일 {r['nonzero_days']}일, 누계 {r['total_kg']:,.0f} kg)"
                    )
            lines.append("")

    lines.append("## 4. mix-adjusted 상관 Top 5 (factory × target)")
    lines.append("")
    if df_corr.empty:
        lines.append("(데이터 부족)")
    else:
        for f in sorted(df_corr["factory"].unique()):
            lines.append(f"### {f}")
            for t in df_corr[df_corr["factory"] == f]["target"].unique():
                sub = df_corr[(df_corr["factory"] == f) & (df_corr["target"] == t)].copy()
                sub["abs_ma"] = sub["mix_adjusted"].abs()
                top = sub.nlargest(5, "abs_ma")
                lines.append(f"- **{t}**")
                for _, r in top.iterrows():
                    name = (r.get("item_name") or "-")[:28]
                    cat = r.get("category2") or "-"
                    lines.append(
                        f"  - {r['item_code']} {name} ({cat}): "
                        f"mix_adj={r['mix_adjusted']:+.3f}, "
                        f"pearson={r['pearson']:+.3f}, "
                        f"lag1={r['pearson_lag1']:+.3f}"
                    )
            lines.append("")

    lines.append("## 5. 활용 가이드")
    lines.append("")
    lines.append("- **신규 품목 도입**: 동일 (factory, category1, category2) 그룹의 `intensity_per_kg`")
    lines.append("  를 기본 추정치로 사용. 계획 생산량 × intensity = 추가 에너지 수요 예상치.")
    lines.append("- **기존 품목 증감**: Lasso top 품목이라면 해당 계수를, 그 외엔 카테고리 계수를 사용.")
    lines.append("- **광주 주의**: `energy_daily.mix_prod_kg` 에 외주/임가공이 포함됨. 분석은 해당 계수에")
    lines.append("  외주 부하가 일부 흡수돼 있어, 자사 품목만의 순영향은 카테고리 회귀가 더 보수적.")
    lines.append("- **AI 보고서**: `app/services/item_energy_impact_service.py` 의 `estimate_impact()`")
    lines.append("  를 통해 What-if 질문에 답할 수 있도록 룩업 JSON 을 활용.")
    lines.append("")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="품목 × 에너지 영향 분석")
    p.add_argument("--year-from", type=int, default=2024)
    p.add_argument("--year-to", type=int, default=2026)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    paths = run_full_analysis(args.year_from, args.year_to)
    return 0 if paths else 1


if __name__ == "__main__":
    raise SystemExit(main())
