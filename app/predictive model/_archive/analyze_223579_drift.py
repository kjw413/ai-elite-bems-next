# 김해 신설 라인 223579 사후 보정(post-hoc calibration) 분석.
# 1) 저장된 김해 baseline 모델 로드 (modeling_v5.1_김해.py 의 v5.1_김해_단독.pkl)
# 2) test 구간(2026-01-01 ~ 2026-04-30) 예측
# 3) 잔차(residual = actual - pred) 와 223579 처리량 OLS 회귀로 단위 부하(α) 추정
# 4) 보정 전/후 MAPE 비교, 223579 활성 구간만 별도 분리해서 평가
# 재학습 없음 — 저장된 모델만 사용
from __future__ import annotations

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
    detect_special_events,
    fill_weather_gaps,
    get_safe_features,
    load_energy_sheet,
    make_features,
    resolve_plant_sheet,
    WEATHER_COLS,
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY,
)

PATH_HOLIDAY = BASE_DIR / "DB_holiday.xlsx"
PATH_WEATHER = BASE_DIR / "DB_weather.xlsx"
SAVED_PKL = BASE_DIR / "energy usage" / "v5.1_김해_단독.pkl"
OUTPUT_CSV = BASE_DIR / "energy usage" / "drift_calibration_223579.csv"

PLANT = "김해"
NEW_LINE_CODE = "223579"
TEST_START = pd.Timestamp("2026-01-01")
TEST_END = pd.Timestamp("2026-04-30")

TARGETS = {
    "전력": "전력량[kWh]",
    "연료": "연료량[N㎥]",
    "용수": "용수량[ton]",
}


def mape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = 1.0) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100.0)


def is_workday(ts: pd.Timestamp, hset: set) -> bool:
    return (ts.dayofweek < 5) and (ts.date() not in hset)


def load_holidays() -> set:
    if not PATH_HOLIDAY.exists():
        return set()
    h = pd.read_excel(PATH_HOLIDAY)
    if "날짜" not in h.columns:
        return set()
    h["날짜"] = pd.to_datetime(h["날짜"], errors="coerce")
    h = h.dropna(subset=["날짜"])
    return set(h["날짜"].dt.date.tolist())


def get_weather(station: str) -> pd.DataFrame:
    cols = ["날짜"] + WEATHER_COLS
    if not PATH_WEATHER.exists():
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_excel(PATH_WEATHER, sheet_name=station)
    except Exception:
        return pd.DataFrame(columns=cols)
    if df.empty or "일시" not in df.columns:
        return pd.DataFrame(columns=cols)
    df = df.copy()
    df["날짜"] = pd.to_datetime(df["일시"], errors="coerce")
    df = df.dropna(subset=["날짜"])
    col_map = {
        "평균기온(°C)": "평균기온",
        "일강수량(mm)": "일강수량",
        "평균 상대습도(%)": "상대습도",
        "합계 일사량(MJ/m2)": "일사량",
        "합계 일조시간(hr)": "일조시간",
    }
    df = df.rename(columns=col_map)
    for c in WEATHER_COLS:
        if c not in df.columns:
            df[c] = np.nan
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[cols]


def load_new_line_series() -> pd.DataFrame | None:
    sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, PLANT) or PLANT
    df = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
    df.columns = [str(c).strip() for c in df.columns]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"]).copy()
    if NEW_LINE_CODE not in df.columns:
        return None
    df[NEW_LINE_CODE] = pd.to_numeric(df[NEW_LINE_CODE], errors="coerce").fillna(0.0)
    return df[["날짜", NEW_LINE_CODE]].copy()


def main() -> None:
    if not SAVED_PKL.exists():
        print(f"[ERROR] 모델 PKL 없음: {SAVED_PKL}")
        print("  먼저 modeling_v5.1_김해.py 실행해서 baseline 모델 만드세요.")
        return
    saved = joblib.load(SAVED_PKL)

    # 223579 데이터 확인
    nq = load_new_line_series()
    if nq is None or (nq[NEW_LINE_CODE] > 0).sum() == 0:
        print(f"[ERROR] {NEW_LINE_CODE} 데이터 없거나 전부 0")
        return
    first_appearance = nq.loc[nq[NEW_LINE_CODE] > 0, "날짜"].min()
    nz_days = int((nq[NEW_LINE_CODE] > 0).sum())
    mean_nz = float(nq.loc[nq[NEW_LINE_CODE] > 0, NEW_LINE_CODE].mean())
    max_v = float(nq[NEW_LINE_CODE].max())
    print(f"[INFO] {NEW_LINE_CODE} 첫 등장: {first_appearance.date()}")
    print(f"[INFO] 비제로 일수: {nz_days}  | 평균(nz): {mean_nz:.0f}  | 최대: {max_v:.0f}")

    # 김해 데이터 로드
    holiday_set = load_holidays()
    sheet = resolve_plant_sheet(PATH_ENERGY_SOURCE, PLANT) or PLANT
    df_raw = load_energy_sheet(PATH_ENERGY_SOURCE, sheet)
    df_raw["날짜"] = pd.to_datetime(df_raw["날짜"], errors="coerce")
    df_raw = df_raw.dropna(subset=["날짜"]).copy()
    df_w = get_weather(PLANT)
    df = df_raw.merge(df_w, on="날짜", how="left") if not df_w.empty else df_raw.copy()
    for c in WEATHER_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = fill_weather_gaps(df, WEATHER_COLS)
    df = df[df["날짜"].apply(lambda ts: is_workday(pd.Timestamp(ts), holiday_set))].copy()

    rows: list[dict] = []
    for target_name, target_col in TARGETS.items():
        if target_name not in saved or saved[target_name].get("baseline") is None:
            print(f"\n--- {target_name}: baseline 없음, skip ---")
            continue
        base = saved[target_name]["baseline"]
        models = base["models"]
        weights = np.asarray(base["weights"])
        features_used = base["features"]  # {M1: [...], M2: [...], M3: [...], M4: [...]}

        # 피처 매트릭스 재구성 (baseline은 WIP 없음 → get_safe_features만 사용)
        d_full = detect_special_events(make_features(df, target_col, holiday_set))
        d = d_full[~d_full["is_special_event"]].copy()
        mte = (d["date"] >= TEST_START) & (d["date"] <= TEST_END)
        d_test = d[mte].copy().reset_index(drop=True)
        if d_test.empty:
            print(f"\n--- {target_name}: test 데이터 비어 있음 ---")
            continue

        y_actual = d_test[target_col].to_numpy(dtype=float)
        X_all = get_safe_features(d_test)

        # 12개 모델 예측 → 가중 평균
        preds: list[np.ndarray] = []
        idx = 0
        for mtype in ["M1", "M2", "M3", "M4"]:
            cols_used = features_used[mtype]
            X_sub = X_all.reindex(columns=cols_used, fill_value=0.0)
            for _ in range(3):
                preds.append(np.expm1(models[idx].predict(X_sub)))
                idx += 1
        y_pred = sum(w * p for w, p in zip(weights, preds))
        residual = y_actual - y_pred

        # 223579 매칭
        merged = d_test[["date"]].merge(nq, left_on="date", right_on="날짜", how="left")
        qty = merged[NEW_LINE_CODE].fillna(0.0).to_numpy(dtype=float)
        active = qty > 0
        n_active = int(active.sum())
        if n_active < 5:
            print(f"\n--- {target_name}: 223579 활성 일수 {n_active} → 회귀 불가, baseline만 표시 ---")
            print(f"  baseline test MAPE: {mape(y_actual, y_pred):.3f}%")
            continue

        # OLS (intercept 0 강제): residual = α × qty
        X = qty[active]
        y_r = residual[active]
        alpha = float(np.sum(X * y_r) / np.sum(X * X))

        y_pred_corr = y_pred + alpha * qty

        base_mape_all = mape(y_actual, y_pred)
        corr_mape_all = mape(y_actual, y_pred_corr)
        before = ~active
        base_mape_before = mape(y_actual[before], y_pred[before]) if before.sum() > 0 else float("nan")
        base_mape_after = mape(y_actual[active], y_pred[active])
        corr_mape_after = mape(y_actual[active], y_pred_corr[active])

        # 단위 환산 (1만개당, 가독성)
        alpha_per_10k = alpha * 10000.0
        unit_str = {
            "전력": "kWh / 1만개",
            "연료": "N㎥ / 1만개",
            "용수": "ton / 1만개",
        }.get(target_name, "/ 1만개")

        print(f"\n--- {target_name} ---")
        print(f"  test 일수 {len(y_actual)}, 그 중 223579 활성 {n_active}일 (~{n_active/len(y_actual)*100:.0f}%)")
        print(f"  baseline MAPE  (전체):                {base_mape_all:7.3f}%")
        print(f"  baseline MAPE  (223579 비활성 구간):  {base_mape_before:7.3f}%")
        print(f"  baseline MAPE  (223579 활성 구간):    {base_mape_after:7.3f}%  ← drift 영향")
        print(f"  추정 α (단위 부하):                    {alpha:.4g}  ({alpha_per_10k:.1f} {unit_str})")
        print(f"  보정 후 MAPE   (전체):                {corr_mape_all:7.3f}%")
        print(f"  보정 후 MAPE   (223579 활성 구간):    {corr_mape_after:7.3f}%")
        delta_all = corr_mape_all - base_mape_all
        delta_after = corr_mape_after - base_mape_after
        print(f"  Δ 전체:        {delta_all:+.3f}%p")
        print(f"  Δ 활성 구간:   {delta_after:+.3f}%p")
        rows.append({
            "target": target_name,
            "n_test_days": len(y_actual),
            "n_active_days": n_active,
            "alpha_unit_load": alpha,
            "alpha_per_10k_units": alpha_per_10k,
            "baseline_mape_all": base_mape_all,
            "baseline_mape_before": base_mape_before,
            "baseline_mape_after": base_mape_after,
            "corrected_mape_all": corr_mape_all,
            "corrected_mape_after": corr_mape_after,
            "delta_all": delta_all,
            "delta_after": delta_after,
        })

    if not rows:
        print("\n[ALERT] 분석 결과 없음")
        return

    df_out = pd.DataFrame(rows)
    df_out.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n[OK] 결과 CSV: {OUTPUT_CSV}")
    print("\n=== 요약 ===")
    print(df_out.to_string(index=False))


if __name__ == "__main__":
    main()
