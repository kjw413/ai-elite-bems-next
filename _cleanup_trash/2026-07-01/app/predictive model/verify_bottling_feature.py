# 보틀링 피처 추가 후 v5.2 모델로 김해 5/12, 5/18, 5/22 재예측.
# 새 모델이 보틀링 신호를 반영해 over-band 가 줄어드는지 확인한다.
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
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY,
    WEATHER_COLS,
    classify_band,
    compute_bottling_aggregate,
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
TARGETS = {"전력": "전력량[kWh]", "연료": "연료량[N㎥]", "용수": "용수량[ton]"}
PKL_NEW = BASE_DIR / "energy usage" / "v5.2.pkl"
PKL_OLD = next(BASE_DIR.glob("energy usage/v5.2.pkl.bak_before_bottling_*"), None)
CHECK_DATES = [pd.Timestamp(d) for d in ["2026-05-12", "2026-05-18", "2026-05-22"]]


def build_feature_frame_for_plant() -> tuple[pd.DataFrame, set]:
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


def load_wip_for_codes(codes: list[str]) -> pd.DataFrame:
    sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, PLANT) or PLANT
    df = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
    df.columns = [str(c).strip() for c in df.columns]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"]).copy()
    out = df[["날짜"]].copy()
    for c in codes:
        col = c if c in df.columns else None
        out[f"wip_{c}"] = pd.to_numeric(df[col], errors="coerce").fillna(0.0) if col else 0.0
    return out


def predict_p_bands(spec: dict, X_full: pd.DataFrame) -> dict[str, np.ndarray]:
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
            cols = features_by_mtype[mtype]
            X_sub = X_full.reindex(columns=cols, fill_value=0.0)
            for _ in range(3):
                preds.append(np.expm1(models[idx].predict(X_sub)))
                idx += 1
        out[f"q{int(q*100):02d}"] = sum(w * p for w, p in zip(weights, preds))
    return out


def evaluate(pkl_path: Path, label: str) -> pd.DataFrame:
    pkl = joblib.load(pkl_path)
    spec_root = pkl.get(PLANT, pkl)  # 김해 단독 파일 vs 통합
    rows = []
    df_plant, hset = build_feature_frame_for_plant()
    for tgt_name, ycol in TARGETS.items():
        spec = spec_root.get(tgt_name) if isinstance(spec_root, dict) else None
        if spec is None or "models_by_q" not in spec:
            continue
        df_model = df_plant.copy()
        # WIP merge (모델 spec 의 raw 코드 + bottling EA)
        item_codes = list(spec.get("wip_item_codes") or [])
        bottling_codes = list(spec.get("wip_bottling_ea_codes") or [])
        load_codes = list(dict.fromkeys(item_codes + bottling_codes))
        if load_codes:
            wip = load_wip_for_codes(load_codes)
            df_model = df_model.merge(wip, on="날짜", how="left")
            for c in load_codes:
                col = f"wip_{c}"
                if col in df_model.columns:
                    df_model[col] = df_model[col].fillna(0.0)
        d_full = detect_special_events(make_features(df_model, ycol, hset))
        d = d_full[~d_full["is_special_event"]].copy().reset_index(drop=True)
        X_base = get_safe_features(d)
        # 보틀링 aggregate 컬럼 + wip raw 컬럼 추가
        wip_feature_cols = list(spec.get("wip_feature_cols") or [])
        wip_extra = pd.DataFrame(index=d.index)
        if BOTTLING_AGG_FEATURE in wip_feature_cols and bottling_codes:
            d[BOTTLING_AGG_FEATURE] = compute_bottling_aggregate(d, bottling_codes)
        for col in wip_feature_cols:
            if col not in d.columns:
                d[col] = 0.0
            wip_extra[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
        wip_extra.columns = sanitize_feature_columns(list(wip_extra.columns))
        X_all = pd.concat([X_base, wip_extra], axis=1)

        bands = predict_p_bands(spec, X_all)
        out = d[["date", ycol]].copy()
        out["target"] = tgt_name
        out["actual"] = d[ycol]
        out["p05"] = bands["q05"]
        out["p50"] = bands["q50"]
        out["p95"] = bands["q95"]
        out["band"] = [classify_band(a, lo, mid, hi)[0] for a, lo, mid, hi in zip(out["actual"], out["p05"], out["p50"], out["p95"])]
        rows.append(out)
    res = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    res["model"] = label
    return res


def main() -> None:
    print(f"OLD model: {PKL_OLD}")
    print(f"NEW model: {PKL_NEW}")

    new_df = evaluate(PKL_NEW, "NEW")
    if PKL_OLD and PKL_OLD.exists():
        old_df = evaluate(PKL_OLD, "OLD")
    else:
        old_df = pd.DataFrame()

    # 비교 대상일만 필터
    for d in CHECK_DATES:
        print(f"\n=== {d.date()} ===")
        if not old_df.empty:
            o = old_df[old_df["date"] == d]
            for _, r in o.iterrows():
                gap = (r["actual"] - r["p95"]) / r["p95"] * 100 if r["p95"] > 0 else 0.0
                print(f"  [OLD] {r['target']:<3s}  actual={r['actual']:>9.0f}  P95={r['p95']:>9.0f}  band={r['band']:<6s}  gap_p95={gap:+.2f}%")
        n = new_df[new_df["date"] == d]
        for _, r in n.iterrows():
            gap = (r["actual"] - r["p95"]) / r["p95"] * 100 if r["p95"] > 0 else 0.0
            print(f"  [NEW] {r['target']:<3s}  actual={r['actual']:>9.0f}  P95={r['p95']:>9.0f}  band={r['band']:<6s}  gap_p95={gap:+.2f}%")

    # 5월 전체 over-band 카운트 비교 (5/1~5/15 test 기간 기준)
    print("\n=== 2026-05-01 ~ 2026-05-15 over-band 카운트 비교 ===")
    may = (new_df["date"] >= pd.Timestamp("2026-05-01")) & (new_df["date"] <= pd.Timestamp("2026-05-15"))
    print("NEW over count:")
    print(new_df.loc[may].groupby("target")["band"].apply(lambda s: (s == "over").sum()).to_string())
    if not old_df.empty:
        may_o = (old_df["date"] >= pd.Timestamp("2026-05-01")) & (old_df["date"] <= pd.Timestamp("2026-05-15"))
        print("\nOLD over count:")
        print(old_df.loc[may_o].groupby("target")["band"].apply(lambda s: (s == "over").sum()).to_string())


if __name__ == "__main__":
    main()
