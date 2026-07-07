# 김해 2026-05 이상치(5/12, 5/18, 5/22) 원인 품목 분석.
#
# 목적:
#   - 정상 생산일임에도 전력/연료/용수가 UCL(P95)을 초과하는 날의 공통 품목 식별.
#   - 모델은 baseline MAPE ~7-12% 수준이지만, 사용자 보고에 따르면 위 3개 날짜에서
#     UCL 초과 패턴이 반복됨.
# 절차:
#   1) v5.2 분위수 모델로 5월 일자별 P05/P50/P95 산출
#   2) actual > P95 인 날을 over-band로 분류
#   3) 김해 WIP 시트에서 동일 날짜의 품목별 처리량 추출
#   4) over-band 날의 품목별 평균 vs 정상일 평균 비교 (z-score, ratio)
#   5) 기존 보정(223579)과 추천 shortlist(260015 농축유, 250071 더위사냥 등) 이외 신규 후보 식별
from __future__ import annotations

import re
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.services.v5_common import (
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY,
    WEATHER_COLS,
    classify_band,
    detect_special_events,
    fill_weather_gaps,
    get_safe_features,
    is_workday,
    load_energy_sheet,
    load_holidays_excel,
    load_weather_station_excel,
    make_features,
    resolve_plant_sheet,
    sanitize_feature_columns,
)

PLANT = "김해"
STATION = "김해"
TARGETS = {
    "전력": "전력량[kWh]",
    "연료": "연료량[N㎥]",
    "용수": "용수량[ton]",
}
MODEL_PKL = BASE_DIR / "energy usage" / "v5.2.pkl"
ITEM_MASTER = BASE_DIR / "wip_analysis" / "wip_item_master.csv"
ANALYSIS_START = pd.Timestamp("2026-05-01")
ANALYSIS_END = pd.Timestamp("2026-05-26")
COMPARE_BASE_START = pd.Timestamp("2026-04-01")  # 정상기간 baseline (드리프트 보정 이후)


def load_item_master() -> dict[str, str]:
    if not ITEM_MASTER.exists():
        return {}
    df = pd.read_csv(ITEM_MASTER, dtype={"ItemCode": str})
    return dict(zip(df["ItemCode"].astype(str), df["item_name"].astype(str)))


def load_wip_long() -> pd.DataFrame:
    sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, PLANT) or PLANT
    df = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
    df.columns = [str(c).strip() for c in df.columns]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"]).copy()
    code_cols = [c for c in df.columns if re.fullmatch(r"\d{6}", str(c))]
    for c in code_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df = df[["날짜"] + code_cols].copy()
    return df


def predict_p_bands(spec: dict, X_full: pd.DataFrame) -> dict[str, np.ndarray]:
    """v5.2 spec → row별 P05/P50/P95 (드리프트 보정 전 raw)."""
    models_by_q = spec["models_by_q"]
    weights_by_q = spec["weights_by_q"]
    quantiles = spec.get("quantiles", [0.05, 0.50, 0.95])
    features_by_mtype = spec["features"]

    out: dict[str, np.ndarray] = {}
    for q in quantiles:
        models = models_by_q[q]
        weights = np.asarray(weights_by_q[q], dtype=float)
        preds: list[np.ndarray] = []
        idx = 0
        for mtype in ["M1", "M2", "M3", "M4"]:
            cols_used = features_by_mtype[mtype]
            X_sub = X_full.reindex(columns=cols_used, fill_value=0.0)
            for _ in range(3):
                preds.append(np.expm1(models[idx].predict(X_sub)))
                idx += 1
        agg = sum(w * p for w, p in zip(weights, preds))
        out[f"q{int(q*100):02d}"] = np.asarray(agg, dtype=float)
    return out


def build_features_for_plant() -> pd.DataFrame:
    holiday_set = load_holidays_excel()
    sheet = resolve_plant_sheet(PATH_ENERGY_SOURCE, PLANT) or PLANT
    df_e = load_energy_sheet(PATH_ENERGY_SOURCE, sheet)
    df_e["날짜"] = pd.to_datetime(df_e["날짜"], errors="coerce")
    df_e = df_e.dropna(subset=["날짜"]).copy()
    df_w = load_weather_station_excel(STATION)
    df = df_e.merge(df_w, on="날짜", how="left") if not df_w.empty else df_e.copy()
    df = fill_weather_gaps(df, WEATHER_COLS)
    df = df[df["날짜"].apply(lambda ts: is_workday(pd.Timestamp(ts), holiday_set))].copy()
    return df, holiday_set


def compute_band_table() -> pd.DataFrame:
    if not MODEL_PKL.exists():
        raise FileNotFoundError(f"v5.2 모델 없음: {MODEL_PKL}")
    pkl = joblib.load(MODEL_PKL)
    df_plant, holiday_set = build_features_for_plant()

    rows: list[dict] = []
    for tgt_name, tgt_col in TARGETS.items():
        spec = pkl.get(PLANT, {}).get(tgt_name)
        if spec is None:
            # 김해 단독 모델은 plant 키가 직접 없을 수도 있음
            spec = pkl.get(tgt_name)
        if spec is None or "models_by_q" not in spec:
            print(f"[WARN] {tgt_name} spec 없음 또는 분위수 모델 아님 — skip")
            continue
        d_full = detect_special_events(make_features(df_plant, tgt_col, holiday_set))
        d = d_full[~d_full["is_special_event"]].copy()
        d = d[(d["date"] >= ANALYSIS_START - pd.Timedelta(days=60))].copy().reset_index(drop=True)
        if d.empty:
            continue
        X_all = get_safe_features(d)
        bands = predict_p_bands(spec, X_all)
        d_out = d[["date", tgt_col, "mix_ton"]].copy()
        d_out["target"] = tgt_name
        d_out["actual"] = d[tgt_col]
        d_out["p05"] = bands["q05"]
        d_out["p50"] = bands["q50"]
        d_out["p95"] = bands["q95"]
        d_out["band"] = [
            classify_band(a, lo, mid, hi)[0]
            for a, lo, mid, hi in zip(
                d_out["actual"], d_out["p05"], d_out["p50"], d_out["p95"]
            )
        ]
        d_out["over_ratio"] = (d_out["actual"] - d_out["p95"]) / d_out["p95"].replace(0, np.nan)
        rows.append(
            d_out[
                [
                    "date", "target", "actual", "mix_ton",
                    "p05", "p50", "p95", "band", "over_ratio",
                ]
            ]
        )
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def diagnose_wip_drivers(band_df: pd.DataFrame, wip_df: pd.DataFrame, item_master: dict) -> pd.DataFrame:
    """over-band 발생일과 정상일의 품목별 처리량 차이 분석."""
    # 분석 대상: 2026-04-01 ~ 2026-05-26 (드리프트 보정 이후 + 5월 분석 구간)
    wip = wip_df[
        (wip_df["날짜"] >= COMPARE_BASE_START) & (wip_df["날짜"] <= ANALYSIS_END)
    ].copy().reset_index(drop=True)

    # over band 날짜 집합 (3개 타겟 union)
    band_sub = band_df[
        (band_df["date"] >= ANALYSIS_START) & (band_df["date"] <= ANALYSIS_END)
    ].copy()
    over_dates_by_tgt = {
        tgt: set(band_sub.loc[(band_sub["target"] == tgt) & (band_sub["band"] == "over"), "date"].dt.date)
        for tgt in TARGETS.keys()
    }
    over_dates_any = set().union(*over_dates_by_tgt.values())
    normal_dates = set(wip["날짜"].dt.date) - over_dates_any

    code_cols = [c for c in wip.columns if re.fullmatch(r"\d{6}", str(c))]
    summary: list[dict] = []

    for code in code_cols:
        series = wip.set_index("날짜")[code]
        over_vals = series[series.index.normalize().date.astype(object).reshape(-1) != None]  # noop sanity
        over_vals = series.loc[[d for d in series.index if d.date() in over_dates_any]]
        norm_vals = series.loc[[d for d in series.index if d.date() in normal_dates]]
        if over_vals.empty:
            continue
        over_mean = float(over_vals.mean())
        norm_mean = float(norm_vals.mean()) if not norm_vals.empty else 0.0
        norm_std = float(norm_vals.std(ddof=0)) if len(norm_vals) > 1 else 0.0
        nz_over = int((over_vals > 0).sum())
        nz_norm = int((norm_vals > 0).sum())
        ratio = over_mean / norm_mean if norm_mean > 0 else (np.inf if over_mean > 0 else 0.0)
        z = (over_mean - norm_mean) / norm_std if norm_std > 0 else np.nan

        # 각 over 날짜에서 z-score
        date_zs: dict = {}
        for d in sorted(over_dates_any):
            v = float(series.loc[pd.Timestamp(d)]) if pd.Timestamp(d) in series.index else 0.0
            zi = (v - norm_mean) / norm_std if norm_std > 0 else np.nan
            date_zs[str(d)] = round(zi, 2) if not np.isnan(zi) else None

        summary.append({
            "item_code": code,
            "item_name": item_master.get(code, ""),
            "over_mean": round(over_mean, 1),
            "normal_mean": round(norm_mean, 1),
            "ratio_over_to_normal": round(ratio, 2) if np.isfinite(ratio) else float("inf"),
            "z_over_vs_normal": round(z, 2) if not np.isnan(z) else None,
            "nz_days_over": nz_over,
            "nz_days_normal": nz_norm,
            "max_over_value": round(float(over_vals.max()), 1),
            "max_normal_value": round(float(norm_vals.max()) if not norm_vals.empty else 0.0, 1),
            **{f"z_{k}": v for k, v in date_zs.items()},
        })

    out = pd.DataFrame(summary)
    return out


def main() -> None:
    print(f"[INFO] 분석 구간: {ANALYSIS_START.date()} ~ {ANALYSIS_END.date()}")
    print(f"[INFO] 정상비교 baseline: {COMPARE_BASE_START.date()} ~ {ANALYSIS_END.date()}")
    print(f"[INFO] 모델: {MODEL_PKL}")
    item_master = load_item_master()
    wip_df = load_wip_long()
    print(f"[INFO] 김해 WIP 컬럼 수: {wip_df.shape[1]-1}  /  행수: {len(wip_df)}")

    band_df = compute_band_table()
    if band_df.empty:
        print("[ERROR] 밴드 계산 결과 없음")
        return

    # 5월 일자별 P95 초과 요약
    band_may = band_df[(band_df["date"] >= ANALYSIS_START) & (band_df["date"] <= ANALYSIS_END)].copy()
    print("\n=== 5월 일자별 band 결과 (over만 표시) ===")
    over_only = band_may[band_may["band"] == "over"].copy()
    over_only["date"] = over_only["date"].dt.strftime("%Y-%m-%d")
    print(over_only[["date", "target", "actual", "p50", "p95", "over_ratio", "mix_ton"]].to_string(index=False))

    band_may.to_csv(BASE_DIR / "energy usage" / "gimhae_may_band_results.csv", index=False, encoding="utf-8-sig")

    # 품목 드라이버 분석
    drivers = diagnose_wip_drivers(band_df, wip_df, item_master)
    drivers_sorted = drivers.sort_values(["z_over_vs_normal", "ratio_over_to_normal"], ascending=[False, False])

    # over에서만 활성(>0)이고 normal에서는 거의 0인 품목 = "신규 등장 라인"
    suspicious_new = drivers[(drivers["nz_days_over"] >= 1) & (drivers["nz_days_normal"] <= 1) & (drivers["over_mean"] > 0)].copy()
    suspicious_new = suspicious_new.sort_values("over_mean", ascending=False)

    # 비율이 큰 상위 — 정상일 대비 over 일에 처리량이 점프
    top_ratio = drivers[(drivers["over_mean"] > 0) & (drivers["normal_mean"] > 0)].copy()
    top_ratio = top_ratio.sort_values("ratio_over_to_normal", ascending=False).head(15)

    top_z = drivers[drivers["z_over_vs_normal"].notna()].copy()
    top_z = top_z[top_z["z_over_vs_normal"] >= 1.0].sort_values("z_over_vs_normal", ascending=False).head(20)

    out_path = BASE_DIR / "energy usage" / "gimhae_may_wip_drivers.csv"
    drivers_sorted.to_csv(out_path, index=False, encoding="utf-8-sig")

    print("\n=== [A] over 발생일에 신규로 등장(정상일 활성<=1) - 신설/리뉴얼 라인 의심 ===")
    if suspicious_new.empty:
        print("  (해당 없음)")
    else:
        print(suspicious_new[["item_code", "item_name", "over_mean", "nz_days_over", "nz_days_normal", "max_over_value"]].to_string(index=False))

    print("\n=== [B] over일 / 정상일 평균 비율 상위 (정상일도 가동했지만 over일에 점프) ===")
    print(top_ratio[["item_code", "item_name", "over_mean", "normal_mean", "ratio_over_to_normal", "z_over_vs_normal"]].to_string(index=False))

    print("\n=== [C] z-score >= 1.0 상위 - 정상분포 대비 통계적으로 튐 ===")
    if top_z.empty:
        print("  (해당 없음)")
    else:
        z_date_cols = [c for c in top_z.columns if c.startswith("z_2026-")]
        print(top_z[["item_code", "item_name", "over_mean", "normal_mean", "z_over_vs_normal", *z_date_cols]].to_string(index=False))

    print(f"\n[OK] 전체 결과 CSV: {out_path}")
    print(f"[OK] 5월 band 결과: {BASE_DIR / 'energy usage' / 'gimhae_may_band_results.csv'}")


if __name__ == "__main__":
    main()
