# 이 파일은 plant-target 단위로 baseline vs +WIP 학습을 돌려 검증/테스트 MAPE를 비교합니다.
# 두 조건 모두 동일한 effective_train_start(2023-01-01)을 사용해 데이터 길이 효과를 통제합니다.
# 입력: wip_residual_shortlist.csv (residual scorer 출력)
# 출력: wip_ablation_results.csv + 콘솔 요약
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from lightgbm import LGBMRegressor

from app.services.v5_common import (
    detect_special_events,
    fill_weather_gaps,
    get_safe_features,
    is_workday,
    load_holidays_excel,
    load_weather_station_excel,
    make_features,
    resolve_plant_sheet,
    sanitize_feature_columns,
    STATION_MAP,
    WEATHER_COLS,
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY,
)
from wip_residual_scorer import load_energy_long

PATH_SHORTLIST = BASE_DIR / "wip_residual_shortlist.csv"
OUTPUT_CSV = BASE_DIR / "wip_ablation_results.csv"

# 공병 강제 포함 후보 (mix_prod_kg 미포함, 별도 전력 부하 — 도메인 지식)
# 사용자 지정: 욥닥터병(220032), 바나나병 상/하(220067/068)만 에너지 소모 높음
# WIP 시트 실측: 김해에 3개 모두 존재, 광주에 220067/068 존재. 나머지 공장 미존재.
EMPTY_CONTAINER_CODES: dict[str, list[str]] = {
    "남양주1": [],
    "남양주2": [],
    "김해": ["220032", "220067", "220068"],
    "광주": ["220067", "220068"],
    "논산": [],
}

# 데이터 길이 효과를 통제하기 위해 두 조건 모두 2023-01-01부터 학습
EFFECTIVE_TRAIN_START = pd.Timestamp("2023-01-01")
TRAIN_END = pd.Timestamp("2024-12-31")
VALID_START = pd.Timestamp("2025-01-01")
VALID_END = pd.Timestamp("2025-12-31")
TEST_START = pd.Timestamp("2026-01-01")
TEST_END = pd.Timestamp("2026-03-31")

PLANTS = ["남양주1", "남양주2", "김해", "광주", "논산"]
TARGETS = {
    "전력": "전력량[kWh]",
    "연료": "연료량[N㎥]",
    "용수": "용수량[ton]",
}

# 학습 속도를 위해 단일 LightGBM, 적당한 iter. ablation은 상대 비교가 목적.
N_ESTIMATORS = 1500
MAPE_IMPROVEMENT_MIN = 0.30  # val MAPE가 최소 이 %p만큼 개선되어야 채택


# MAPE 계산.
def mape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = 1.0) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100.0)


# residual shortlist를 plant-target별 dict로 변환합니다.
def load_residual_shortlist() -> dict[tuple[str, str], list[str]]:
    if not PATH_SHORTLIST.exists():
        raise FileNotFoundError(f"shortlist not found: {PATH_SHORTLIST}")
    df = pd.read_csv(PATH_SHORTLIST, dtype={"item_code": str})
    out: dict[tuple[str, str], list[str]] = {}
    for (plant, target), grp in df.groupby(["plant", "target"]):
        out[(plant, target)] = grp["item_code"].astype(str).tolist()
    return out


# 공장 단위 base frame(에너지+기상)을 학습 구간으로 로드합니다.
def load_plant_base_frame(plant: str, holiday_set: set) -> pd.DataFrame:
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
    df = df[(df["날짜"] >= EFFECTIVE_TRAIN_START) & (df["날짜"] <= TEST_END)].copy()
    df = df.sort_values("날짜").reset_index(drop=True)
    return df


# WIP 컬럼들을 base frame에 합칩니다.
def attach_wip(df_base: pd.DataFrame, plant: str, item_codes: list[str]) -> tuple[pd.DataFrame, list[str]]:
    if not item_codes:
        return df_base.copy(), []

    sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, plant) or plant
    df_wip = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
    df_wip.columns = [str(c).strip() for c in df_wip.columns]
    df_wip["날짜"] = pd.to_datetime(df_wip["날짜"], errors="coerce")
    df_wip = df_wip.dropna(subset=["날짜"]).copy()

    feat_cols: list[str] = []
    keep = ["날짜"]
    for code in item_codes:
        new_col = f"wip_{code}"
        if code in df_wip.columns:
            df_wip[new_col] = pd.to_numeric(df_wip[code], errors="coerce").fillna(0.0)
        else:
            df_wip[new_col] = 0.0
        keep.append(new_col)
        feat_cols.append(new_col)

    merged = df_base.merge(df_wip[keep], on="날짜", how="left")
    for c in feat_cols:
        merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0.0)
    return merged, feat_cols


# safe features + WIP을 결합한 학습 매트릭스를 만듭니다.
def build_X(df: pd.DataFrame, wip_cols: list[str]) -> pd.DataFrame:
    base = get_safe_features(df)
    if not wip_cols:
        return base
    wip_frame = df[wip_cols].copy()
    wip_frame.columns = sanitize_feature_columns(list(wip_frame.columns))
    for col in wip_frame.columns:
        wip_frame[col] = pd.to_numeric(wip_frame[col], errors="coerce").fillna(0.0)
    return pd.concat([base, wip_frame], axis=1)


# 한 plant-target에 대해 baseline / +WIP 두 조건을 학습하고 MAPE를 반환합니다.
def run_one(
    plant: str,
    target: str,
    target_col: str,
    holiday_set: set,
    wip_codes: list[str],
) -> dict:
    df_base = load_plant_base_frame(plant, holiday_set)
    if df_base.empty or target_col not in df_base.columns:
        return {"plant": plant, "target": target, "status": "no_data"}

    df_with_wip, wip_cols = attach_wip(df_base, plant, wip_codes)

    # 특이 이벤트 제외
    d_full = detect_special_events(make_features(df_with_wip, target_col, holiday_set))
    d = d_full[~d_full["is_special_event"]].copy()

    mtr = (d["date"] >= EFFECTIVE_TRAIN_START) & (d["date"] <= TRAIN_END)
    mva = (d["date"] >= VALID_START) & (d["date"] <= VALID_END)
    mte = (d["date"] >= TEST_START) & (d["date"] <= TEST_END)
    if mtr.sum() < 100 or mva.sum() < 30 or mte.sum() < 10:
        return {
            "plant": plant, "target": target, "status": "insufficient_rows",
            "n_train": int(mtr.sum()), "n_val": int(mva.sum()), "n_test": int(mte.sum()),
        }

    y = d[target_col].to_numpy(dtype=float)
    ytr, yva, yte = y[mtr], y[mva], y[mte]

    result: dict = {
        "plant": plant, "target": target, "status": "ok",
        "n_wip_features": len(wip_cols), "wip_item_codes": ",".join(wip_codes),
        "n_train": int(mtr.sum()), "n_val": int(mva.sum()), "n_test": int(mte.sum()),
    }

    for label, cols in [("baseline", []), ("plus_wip", wip_cols)]:
        X_all = build_X(d, cols)
        Xtr, Xva, Xte = X_all[mtr], X_all[mva], X_all[mte]
        model = LGBMRegressor(
            n_estimators=N_ESTIMATORS,
            learning_rate=0.05,
            num_leaves=31,
            random_state=42,
            verbose=-1,
            n_jobs=-1,
        )
        model.fit(Xtr, np.log1p(ytr))
        pred_va = np.expm1(model.predict(Xva))
        pred_te = np.expm1(model.predict(Xte))
        result[f"{label}_val_mape"] = mape(yva, pred_va)
        result[f"{label}_test_mape"] = mape(yte, pred_te)

    result["val_delta_mape"] = result["plus_wip_val_mape"] - result["baseline_val_mape"]
    result["test_delta_mape"] = result["plus_wip_test_mape"] - result["baseline_test_mape"]
    result["adopt"] = bool(result["val_delta_mape"] <= -MAPE_IMPROVEMENT_MIN)
    return result


# 메인 실행 흐름입니다.
def main() -> None:
    if not PATH_ENERGY_SOURCE.exists():
        print(f"[ERROR] 에너지 원본 없음: {PATH_ENERGY_SOURCE}")
        return
    holiday_set = load_holidays_excel()
    shortlist = load_residual_shortlist()

    rows: list[dict] = []
    for plant in PLANTS:
        for target, target_col in TARGETS.items():
            residual_codes = shortlist.get((plant, target), [])
            container_codes = EMPTY_CONTAINER_CODES.get(plant, [])
            # 잔차 통과 + 공병 강제 포함, 중복 제거
            wip_codes = list(dict.fromkeys([*residual_codes, *container_codes]))
            print(
                f"\n[{plant}-{target}] WIP 후보 {len(wip_codes)}개: {wip_codes}"
                f" (잔차 {len(residual_codes)} + 공병 {len(container_codes)})"
            )
            try:
                res = run_one(plant, target, target_col, holiday_set, wip_codes)
            except Exception as exc:
                print(f"  ! 실패: {exc}")
                rows.append({"plant": plant, "target": target, "status": f"error: {exc}"})
                continue
            if res.get("status") != "ok":
                print(f"  status={res.get('status')}")
                rows.append(res)
                continue
            print(
                f"  baseline: val={res['baseline_val_mape']:.2f}%  test={res['baseline_test_mape']:.2f}%"
                f"\n  +WIP    : val={res['plus_wip_val_mape']:.2f}%  test={res['plus_wip_test_mape']:.2f}%"
                f"\n  Δval={res['val_delta_mape']:+.2f}%p  Δtest={res['test_delta_mape']:+.2f}%p"
                f"  → {'ADOPT' if res['adopt'] else 'reject'}"
            )
            rows.append(res)

    df = pd.DataFrame(rows)
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n[OK] ablation 결과 저장: {OUTPUT_CSV}")

    ok = df[df["status"] == "ok"].copy() if "status" in df.columns else pd.DataFrame()
    if not ok.empty:
        adopt = ok[ok["adopt"]]
        print(f"\n== 채택 추천 ({len(adopt)}/{len(ok)}) ==")
        for _, r in adopt.iterrows():
            print(
                f"  {r['plant']}-{r['target']}: Δval {r['val_delta_mape']:+.2f}%p, "
                f"Δtest {r['test_delta_mape']:+.2f}%p, items={r['wip_item_codes']}"
            )


if __name__ == "__main__":
    main()
