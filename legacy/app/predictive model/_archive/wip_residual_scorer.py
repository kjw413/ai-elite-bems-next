# 이 파일은 외부 AI가 추천한 재공품 후보를 학습 구간 한정 mix-adjusted 잔차 상관으로 재평가합니다.
# 입력: 에너지예측_품목추천.xlsx (최종추천=True 후보)
# 출력: wip_residual_scores.csv — raw r vs residual r, 잔차 임계 통과 여부 포함
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import (
    fill_weather_gaps,
    is_workday,
    load_holidays_excel,
    load_weather_station_excel,
    resolve_plant_sheet,
    STATION_MAP,
    WEATHER_COLS,
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY,
)


# Wide format(행=항목, 열=날짜)을 long format으로 transpose 합니다.
def load_energy_long(plant: str) -> pd.DataFrame:
    sheet = resolve_plant_sheet(PATH_ENERGY_SOURCE, plant) or plant
    raw = pd.read_excel(PATH_ENERGY_SOURCE, sheet_name=sheet, header=None)
    if raw.empty:
        return pd.DataFrame()
    # 첫 컬럼 = 항목명, 첫 행 = 날짜 헤더
    cats = raw.iloc[1:, 0].astype(str).tolist()
    dates_raw = raw.iloc[0, 1:].tolist()
    values = raw.iloc[1:, 1:].to_numpy()
    long_df = pd.DataFrame(values.T, columns=cats)
    # 중복 컬럼명 처리 — 첫 등장만 보존
    long_df = long_df.loc[:, ~long_df.columns.duplicated()].copy()
    long_df.insert(0, "날짜", pd.to_datetime(dates_raw, errors="coerce"))
    long_df = long_df.dropna(subset=["날짜"]).reset_index(drop=True)
    for c in long_df.columns:
        if c == "날짜":
            continue
        long_df[c] = pd.to_numeric(long_df[c], errors="coerce")
    return long_df

PATH_RECOMMENDATIONS = BASE_DIR / "에너지예측_품목추천.xlsx"
OUTPUT_CSV = BASE_DIR / "wip_residual_scores.csv"
OUTPUT_SHORTLIST_CSV = BASE_DIR / "wip_residual_shortlist.csv"

TRAIN_START = pd.Timestamp("2023-01-01")  # WIP_AVAILABLE_START과 동일
TRAIN_END = pd.Timestamp("2024-12-31")

PLANTS = ["남양주1", "남양주2", "김해", "광주", "논산"]
TARGETS = {
    "전력": ("전력", "전력량[kWh]"),
    "연료": ("연료", "연료량[N㎥]"),
    "용수": ("용수", "용수량[ton]"),
}
TARGET_TO_SHEET_SUFFIX = {"전력": "전력", "연료": "연료", "용수": "용수"}

RESIDUAL_THRESHOLD = 0.20


# 추천 엑셀의 최종추천=True 후보 목록을 불러옵니다.
def load_final_recommendations() -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    xls = pd.ExcelFile(PATH_RECOMMENDATIONS)
    for plant in PLANTS:
        for target, _ in TARGETS.items():
            sheet = f"{plant}_{target}"
            if sheet not in xls.sheet_names:
                out[(plant, target)] = []
                continue
            df = pd.read_excel(PATH_RECOMMENDATIONS, sheet_name=sheet)
            if "최종추천" in df.columns:
                picked = df[df["최종추천"] == True]["품목"].astype(str).str.strip().tolist()
            elif "선택" in df.columns:
                picked = df[df["선택"] == True]["품목"].astype(str).str.strip().tolist()
            else:
                picked = df["품목"].astype(str).str.strip().tolist()
            out[(plant, target)] = picked
    return out


# 공장 단위 데이터(에너지+기상+WIP)를 학습 구간으로 로드합니다.
def load_plant_frame(plant: str, holiday_set: set) -> pd.DataFrame:
    df = load_energy_long(plant)
    if df.empty:
        return df

    station = STATION_MAP[plant]
    df_w = load_weather_station_excel(station)
    if not df_w.empty:
        df = df.merge(df_w, on="날짜", how="left")
    for col in WEATHER_COLS:
        if col not in df.columns:
            df[col] = np.nan
    df = fill_weather_gaps(df, WEATHER_COLS)

    df = df[df["날짜"].apply(lambda ts: is_workday(pd.Timestamp(ts), holiday_set))].copy()
    df = df[(df["날짜"] >= TRAIN_START) & (df["날짜"] <= TRAIN_END)].copy()
    df = df.sort_values("날짜").reset_index(drop=True)
    return df


# WIP 시트에서 지정 품목 컬럼을 가져옵니다.
def load_wip_columns(plant: str, item_codes: list[str]) -> pd.DataFrame:
    sheet_name = resolve_plant_sheet(PATH_WIP_SUMMARY, plant) or plant
    df = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet_name)
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"]).copy()

    df.columns = [str(c).strip() for c in df.columns]

    out_cols: dict[str, pd.Series] = {"날짜": df["날짜"]}
    for code in item_codes:
        if code in df.columns:
            out_cols[code] = pd.to_numeric(df[code], errors="coerce").fillna(0.0)
        else:
            out_cols[code] = pd.Series(0.0, index=df.index)
    return pd.DataFrame(out_cols)


# OLS로 컨트롤 변수에 대한 잔차를 계산합니다.
def residualize(y: np.ndarray, X: np.ndarray) -> np.ndarray:
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    if mask.sum() < 30:
        return np.full(y.shape, np.nan, dtype=float)
    X_use = np.column_stack([np.ones(mask.sum()), X[mask]])
    beta, *_ = np.linalg.lstsq(X_use, y[mask], rcond=None)
    resid = np.full(y.shape, np.nan, dtype=float)
    resid[mask] = y[mask] - X_use @ beta
    return resid


# 안전한 Pearson 상관을 계산합니다.
def safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 30:
        return float("nan")
    if np.nanstd(a[mask]) == 0 or np.nanstd(b[mask]) == 0:
        return float("nan")
    return float(np.corrcoef(a[mask], b[mask])[0, 1])


# 단일 plant-target의 후보 품목들을 평가합니다.
def score_plant_target(
    plant: str,
    target: str,
    target_col: str,
    candidates: list[str],
    holiday_set: set,
) -> pd.DataFrame:
    if not candidates:
        return pd.DataFrame()

    df = load_plant_frame(plant, holiday_set)
    if df.empty:
        return pd.DataFrame()

    wip_df = load_wip_columns(plant, candidates)
    merged = df.merge(wip_df, on="날짜", how="left")
    for code in candidates:
        merged[code] = pd.to_numeric(merged[code], errors="coerce").fillna(0.0)

    # 컨트롤 변수: log_mix_ton, log_mix_ton_lag1, dow, month
    mix_kg = pd.to_numeric(merged.get("믹스생산량[kg]"), errors="coerce").fillna(0.0)
    log_mix = np.log1p(mix_kg / 1000.0).to_numpy(dtype=float)
    log_mix_lag1 = pd.Series(log_mix).shift(1).to_numpy(dtype=float)
    dow = merged["날짜"].dt.dayofweek.to_numpy(dtype=float)
    month = merged["날짜"].dt.month.to_numpy(dtype=float)
    controls = np.column_stack([log_mix, log_mix_lag1, dow, month])

    y_raw = pd.to_numeric(merged[target_col], errors="coerce").to_numpy(dtype=float)
    y_resid = residualize(y_raw, controls)

    rows: list[dict] = []
    for code in candidates:
        x_raw = merged[code].to_numpy(dtype=float)
        x_resid = residualize(x_raw, controls)

        r_raw = safe_corr(x_raw, y_raw)
        r_resid = safe_corr(x_resid, y_resid)
        nonzero = int(np.count_nonzero(x_raw))
        coverage = nonzero / max(1, len(x_raw))

        rows.append({
            "plant": plant,
            "target": target,
            "item_code": code,
            "n_obs": int(len(x_raw)),
            "nonzero_days": nonzero,
            "coverage": coverage,
            "r_raw_train": r_raw,
            "r_resid_train": r_resid,
            "abs_r_resid": abs(r_resid) if np.isfinite(r_resid) else float("nan"),
            "pass_threshold": bool(
                np.isfinite(r_resid)
                and abs(r_resid) >= RESIDUAL_THRESHOLD
                and nonzero >= 40
                and coverage >= 0.05
            ),
        })
    return pd.DataFrame(rows)


# 메인 실행 흐름입니다.
def main() -> None:
    if not PATH_RECOMMENDATIONS.exists():
        print(f"[ERROR] 추천 엑셀이 없습니다: {PATH_RECOMMENDATIONS}")
        return
    if not PATH_ENERGY_SOURCE.exists():
        print(f"[ERROR] 에너지 원본이 없습니다: {PATH_ENERGY_SOURCE}")
        return
    if not PATH_WIP_SUMMARY.exists():
        print(f"[ERROR] 재공품 원본이 없습니다: {PATH_WIP_SUMMARY}")
        return

    holiday_set = load_holidays_excel()
    recommendations = load_final_recommendations()

    all_rows: list[pd.DataFrame] = []
    for plant in PLANTS:
        for target, (_, target_col) in TARGETS.items():
            cands = recommendations.get((plant, target), [])
            if not cands:
                print(f"  - {plant}-{target}: 추천 없음, skip")
                continue
            df_score = score_plant_target(plant, target, target_col, cands, holiday_set)
            if df_score.empty:
                continue
            pass_count = int(df_score["pass_threshold"].sum())
            print(
                f"  - {plant}-{target}: 후보 {len(cands)}, 통과 {pass_count} | "
                f"max|r_resid|={df_score['abs_r_resid'].max():.3f}"
            )
            all_rows.append(df_score)

    if not all_rows:
        print("[ALERT] 결과가 비어 있습니다.")
        return

    full = pd.concat(all_rows, ignore_index=True)
    full = full.sort_values(
        ["plant", "target", "abs_r_resid"], ascending=[True, True, False]
    ).reset_index(drop=True)
    full.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n[OK] 전체 점수 저장: {OUTPUT_CSV}")

    shortlist = full[full["pass_threshold"]].copy()
    shortlist = shortlist.sort_values(
        ["plant", "target", "abs_r_resid"], ascending=[True, True, False]
    ).reset_index(drop=True)
    shortlist.to_csv(OUTPUT_SHORTLIST_CSV, index=False, encoding="utf-8-sig")
    print(f"[OK] 통과 후보 저장: {OUTPUT_SHORTLIST_CSV}")
    print(f"     총 통과: {len(shortlist)} / {len(full)}")


if __name__ == "__main__":
    main()
