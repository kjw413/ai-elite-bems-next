# 두 모델(베이스라인 vs 현재)을 동일한 test 윈도우(2026-01-01~2026-05-15) 위에서 MAPE 비교.
# verify_bottling_feature.py 와 같은 inference 로직을 사용하지만 5개 공장 전체 + MAPE 까지 산출.
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
    BOTTLING_AGG_FEATURE,
    N_ACTIVE_SKUS_FEATURE,
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY,
    STATION_MAP,
    WEATHER_COLS,
    classify_band,
    compute_bottling_aggregate,
    detect_special_events,
    fill_weather_gaps,
    get_safe_features,
    is_workday,
    load_energy_sheet,
    load_holidays_excel,
    load_n_active_skus_series,
    load_weather_station_excel,
    make_features,
    resolve_plant_sheet,
    sanitize_feature_columns,
)

TARGETS = {"전력": "전력량[kWh]", "연료": "연료량[N㎥]", "용수": "용수량[ton]"}
PKL_NEW = BASE_DIR / "energy usage" / "v5.2.pkl"
PKL_OLD = BASE_DIR / "energy usage" / "v5.2.pkl.bak_before_bottling_20260526_170337"
TEST_START = pd.Timestamp("2026-04-01")
TEST_END = pd.Timestamp("2026-05-15")
PLANTS = ["남양주1", "남양주2", "김해", "광주", "논산"]


def mape(y_true, y_pred, eps=1.0):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100.0)


def build_feature_frame_for_plant(plant: str):
    holiday_set = load_holidays_excel()
    sheet = resolve_plant_sheet(PATH_ENERGY_SOURCE, plant) or plant
    df_e = load_energy_sheet(PATH_ENERGY_SOURCE, sheet)
    df_e["날짜"] = pd.to_datetime(df_e["날짜"], errors="coerce")
    df_e = df_e.dropna(subset=["날짜"]).copy()
    station = STATION_MAP.get(plant, plant)
    df_w = load_weather_station_excel(station)
    df = df_e.merge(df_w, on="날짜", how="left") if not df_w.empty else df_e.copy()
    df = fill_weather_gaps(df, WEATHER_COLS)
    df = df[df["날짜"].apply(lambda ts: is_workday(pd.Timestamp(ts), holiday_set))].copy()
    return df, holiday_set


def load_wip_for_codes(plant: str, codes: list[str]) -> pd.DataFrame:
    sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, plant) or plant
    df = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
    df.columns = [str(c).strip() for c in df.columns]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"]).copy()
    out = df[["날짜"]].copy()
    for c in codes:
        col = c if c in df.columns else None
        out[f"wip_{c}"] = pd.to_numeric(df[col], errors="coerce").fillna(0.0) if col else 0.0
    return out


def predict_p_bands(spec, X_full):
    models_by_q = spec["models_by_q"]
    weights_by_q = spec["weights_by_q"]
    quantiles = spec.get("quantiles", [0.05, 0.50, 0.95])
    features_by_mtype = spec["features"]
    out = {}
    for q in quantiles:
        models = models_by_q[q]
        weights = np.asarray(weights_by_q[q], dtype=float)
        preds = []
        idx = 0
        for mtype in ["M1", "M2", "M3", "M4"]:
            cols = features_by_mtype[mtype]
            X_sub = X_full.reindex(columns=cols, fill_value=0.0)
            for _ in range(3):
                preds.append(np.expm1(models[idx].predict(X_sub)))
                idx += 1
        out[f"q{int(q*100):02d}"] = sum(w * p for w, p in zip(weights, preds))
    return out


def evaluate_plant(pkl_path: Path, plant: str) -> pd.DataFrame:
    pkl = joblib.load(pkl_path)
    spec_root = pkl.get(plant, {})
    rows = []
    df_plant, hset = build_feature_frame_for_plant(plant)
    for tgt_name, ycol in TARGETS.items():
        spec = spec_root.get(tgt_name)
        if spec is None or "models_by_q" not in spec:
            continue
        df_model = df_plant.copy()
        item_codes = list(spec.get("wip_item_codes") or [])
        bottling_codes = list(spec.get("wip_bottling_ea_codes") or [])
        load_codes = list(dict.fromkeys(item_codes + bottling_codes))
        if load_codes:
            wip = load_wip_for_codes(plant, load_codes)
            df_model = df_model.merge(wip, on="날짜", how="left")
            for c in load_codes:
                col = f"wip_{c}"
                if col in df_model.columns:
                    df_model[col] = df_model[col].fillna(0.0)

        wip_feature_cols = list(spec.get("wip_feature_cols") or [])
        # n_active_skus 가 spec 에 있으면 시트 로드
        if N_ACTIVE_SKUS_FEATURE in wip_feature_cols:
            n_df = load_n_active_skus_series(PATH_WIP_SUMMARY, plant)
            if not n_df.empty:
                df_model = df_model.merge(n_df, on="날짜", how="left")
                df_model[N_ACTIVE_SKUS_FEATURE] = pd.to_numeric(
                    df_model[N_ACTIVE_SKUS_FEATURE], errors="coerce"
                ).fillna(0.0)

        d_full = detect_special_events(make_features(df_model, ycol, hset))
        d = d_full[~d_full["is_special_event"]].copy().reset_index(drop=True)

        # 보틀링 aggregate 컬럼 채우기
        if BOTTLING_AGG_FEATURE in wip_feature_cols and bottling_codes:
            d[BOTTLING_AGG_FEATURE] = compute_bottling_aggregate(d, bottling_codes)

        # test 윈도우 필터
        mask = (d["date"] >= TEST_START) & (d["date"] <= TEST_END)
        d_test = d[mask].copy().reset_index(drop=True)
        if d_test.empty:
            continue

        X_base = get_safe_features(d_test)
        wip_extra = pd.DataFrame(index=d_test.index)
        for col in wip_feature_cols:
            if col not in d_test.columns:
                d_test[col] = 0.0
            wip_extra[col] = pd.to_numeric(d_test[col], errors="coerce").fillna(0.0)
        wip_extra.columns = sanitize_feature_columns(list(wip_extra.columns))
        X_all = pd.concat([X_base, wip_extra], axis=1)

        bands = predict_p_bands(spec, X_all)
        y_actual = d_test[ycol].to_numpy(dtype=float)
        p50 = bands["q50"]
        p05 = bands["q05"]
        p95 = bands["q95"]
        # band 분류
        bands_str = [classify_band(a, lo, mid, hi)[0] for a, lo, mid, hi in zip(y_actual, p05, p50, p95)]
        over_count = sum(1 for b in bands_str if b == "over")
        under_count = sum(1 for b in bands_str if b == "under")

        rows.append({
            "plant": plant,
            "target": tgt_name,
            "n_days": len(y_actual),
            "MAPE_p50": mape(y_actual, p50),
            "over_count": over_count,
            "under_count": under_count,
            "inside_count": len(y_actual) - over_count - under_count,
        })
    return pd.DataFrame(rows)


def main():
    print(f"TEST window: {TEST_START.date()} ~ {TEST_END.date()}\n")
    print("=" * 75)
    print("OLD baseline (보틀링/n_active 없음)")
    print("=" * 75)
    old_rows = []
    for plant in PLANTS:
        old_rows.append(evaluate_plant(PKL_OLD, plant))
    old_df = pd.concat(old_rows, ignore_index=True)
    print(old_df.to_string(index=False))

    print("\n" + "=" * 75)
    print("NEW model (보틀링 + n_active_skus, 광주는 n_active_skus 제외)")
    print("=" * 75)
    new_rows = []
    for plant in PLANTS:
        new_rows.append(evaluate_plant(PKL_NEW, plant))
    new_df = pd.concat(new_rows, ignore_index=True)
    print(new_df.to_string(index=False))

    # 비교 표
    print("\n" + "=" * 75)
    print("Apples-to-apples MAPE 비교 (test=2026-01-01~05-15)")
    print("=" * 75)
    merged = old_df.merge(new_df, on=["plant", "target"], suffixes=("_OLD", "_NEW"))
    merged["delta_MAPE"] = merged["MAPE_p50_NEW"] - merged["MAPE_p50_OLD"]
    merged["delta_over"] = merged["over_count_NEW"] - merged["over_count_OLD"]
    print(merged[["plant", "target", "n_days_OLD", "MAPE_p50_OLD", "MAPE_p50_NEW", "delta_MAPE", "over_count_OLD", "over_count_NEW", "delta_over"]].to_string(index=False))


if __name__ == "__main__":
    main()
