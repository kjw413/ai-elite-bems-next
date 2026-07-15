# 이 파일은 재공품 피처를 포함한 v5 실험 모델을 학습합니다.
from __future__ import annotations

import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import openpyxl
import pandas as pd
from catboost import CatBoostRegressor
from lightgbm import LGBMRegressor
from scipy.optimize import minimize
from xgboost import XGBRegressor

warnings.filterwarnings("ignore")


# =========================
# 1. Paths / Config
# =========================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from wip_feature_shortlist import get_shortlist_item_codes
from app.services.v5_common import (
    attach_model_artifact,
    build_model_artifact_metadata,
    build_feature_frame,
    build_versioned_model_path,
    detect_special_events,
    fill_weather_gaps,
    get_git_commit,
    is_workday,
    load_energy_sheet,
    load_holidays_excel,
    load_model_registry,
    make_features,
    map_sheet_to_plant,
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY as SHARED_WIP_SUMMARY,
    resolve_plant_sheet,
    select_features,
    summarize_metric_frame,
    V5_1_TEST_END,
    V5_1_TEST_START,
    V5_1_TRAIN_END,
    V5_1_TRAIN_START,
    V5_1_VALID_END,
    V5_1_VALID_START,
    WEATHER_COLS,
    WIP_AVAILABLE_START,
    write_model_registry,
)


PATH_WEATHER = BASE_DIR / "DB_weather.xlsx"
PATH_INTENSITY = PATH_ENERGY_SOURCE
PATH_WIP_SUMMARY = SHARED_WIP_SUMMARY

TRAIN_START = V5_1_TRAIN_START  # WIP 데이터 시작점과 통일 (2026-05-15 변경: 2021→2023)
TRAIN_END = V5_1_TRAIN_END
VALID_START = V5_1_VALID_START
VALID_END = V5_1_VALID_END
TEST_START = V5_1_TEST_START
TEST_END = V5_1_TEST_END

STATION_MAP = {
    "남양주1": "서울",
    "남양주2": "서울",
    "김해": "김해",
    "광주": "이천",
    "논산": "부여",
}


TARGETS = {
    "전력": "전력량[kWh]",
    "연료": "연료량[N㎥]",
    "용수": "용수량[ton]",
}

OUTPUT_DIR = BASE_DIR / "energy usage"
OUTPUT_EXCEL = OUTPUT_DIR / "performance_test_results_v5.1.xlsx"
OUTPUT_MODEL_KEY = "v5.1"
OUTPUT_PKL = build_versioned_model_path(OUTPUT_MODEL_KEY, base_dir=OUTPUT_DIR)
FEATURE_SPEC_VERSION_WIP = "1.2-wip-shortlist"

OPERATING_CPU_NUMBER = max(1, (os.cpu_count() or 2) - 1)


# =========================
# 2. Utils
# =========================
# 실제값과 예측값의 오차 비율을 계산합니다.
def mape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = 1.0) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100.0)


# 날씨 데이터 값을 가져옵니다.
def get_weather_data(station_name: str) -> pd.DataFrame:
    if not PATH_WEATHER.exists():
        return pd.DataFrame(columns=["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"])

    try:
        df_xl = pd.read_excel(PATH_WEATHER, sheet_name=station_name)
    except Exception:
        return pd.DataFrame(columns=["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"])

    if df_xl.empty or "일시" not in df_xl.columns:
        return pd.DataFrame(columns=["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"])

    df_xl = df_xl.copy()
    df_xl["날짜"] = pd.to_datetime(df_xl["일시"], errors="coerce")
    df_xl = df_xl.dropna(subset=["날짜"])

    col_map = {
        "평균기온(°C)": "평균기온",
        "일강수량(mm)": "일강수량",
        "평균 상대습도(%)": "상대습도",
        "합계 일사량(MJ/m2)": "일사량",
        "합계 일조시간(hr)": "일조시간",
    }
    df_w = df_xl.rename(columns=col_map)
    # 결측은 0이 아니라 NaN으로 보존합니다(0과 "측정 안 됨"의 의미가 다름).
    # 갭 채움은 호출 측에서 fill_weather_gaps()로 처리합니다.
    for col in ["평균기온", "일강수량", "상대습도", "일사량", "일조시간"]:
        if col not in df_w.columns:
            df_w[col] = np.nan
        else:
            df_w[col] = pd.to_numeric(df_w[col], errors="coerce")

    return df_w[["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"]]


# 품목 코드 값을 일정한 형식으로 맞춥니다.
def normalize_item_code(value: object) -> str:
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


# 재공품 피처 컬럼 데이터를 만듭니다.
def build_wip_feature_col(item_code: str) -> str:
    return f"wip_{normalize_item_code(item_code)}"


# selected 재공품 데이터프레임 데이터를 불러옵니다.
def load_selected_wip_frame(plant: str, item_codes: tuple[str, ...]) -> pd.DataFrame:
    if not item_codes:
        return pd.DataFrame(columns=["날짜"])

    if not PATH_WIP_SUMMARY.exists():
        raise FileNotFoundError(f"WIP summary file not found: {PATH_WIP_SUMMARY}")

    # Excel 시트명이 구버전(F-코드)일 수 있으므로 호환 처리
    sheet_name = resolve_plant_sheet(PATH_WIP_SUMMARY, plant) or plant
    df_wip = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet_name)
    if "날짜" not in df_wip.columns:
        raise ValueError(f"'날짜' column not found in WIP sheet: {plant}")

    rename_map = {}
    for col in df_wip.columns:
        if col == "날짜":
            continue
        rename_map[col] = normalize_item_code(col)
    df_wip = df_wip.rename(columns=rename_map)

    df_wip["날짜"] = pd.to_datetime(df_wip["날짜"], errors="coerce")
    df_wip = df_wip.dropna(subset=["날짜"]).copy()

    for item_code in item_codes:
        if item_code not in df_wip.columns:
            df_wip[item_code] = 0.0
        df_wip[item_code] = pd.to_numeric(df_wip[item_code], errors="coerce").fillna(0.0)

    keep_cols = ["날짜", *item_codes]
    out = df_wip[keep_cols].copy()
    out = out.rename(columns={code: build_wip_feature_col(code) for code in item_codes})
    return out


# shortlisted 재공품 피처 데이터를 합칩니다.
def merge_shortlisted_wip_features(
    df: pd.DataFrame,
    plant: str,
    target_name: str,
) -> tuple[pd.DataFrame, list[str], pd.Timestamp]:
    item_codes = tuple(get_shortlist_item_codes(plant, target_name))
    if not item_codes:
        return df.copy(), [], TRAIN_START

    wip_df = load_selected_wip_frame(plant, item_codes)
    merged = df.merge(wip_df, on="날짜", how="left")

    wip_cols = [build_wip_feature_col(code) for code in item_codes]
    for col in wip_cols:
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    effective_train_start = max(TRAIN_START, WIP_AVAILABLE_START)
    return merged, wip_cols, effective_train_start


# =========================
# 3. Modeling Engine
# =========================
# 모델을 학습합니다.
def train_models(X_tr: pd.DataFrame, y_tr: np.ndarray, n_estimators: int = 3000) -> list[Any]:
    lgbm = LGBMRegressor(
        n_estimators=n_estimators,
        learning_rate=0.05,
        num_leaves=31,
        random_state=42,
        n_jobs=OPERATING_CPU_NUMBER,
    )
    lgbm.fit(X_tr, np.log1p(y_tr))

    xgb = XGBRegressor(
        n_estimators=n_estimators,
        learning_rate=0.05,
        max_depth=6,
        random_state=42,
        n_jobs=OPERATING_CPU_NUMBER,
    )
    xgb.fit(X_tr, np.log1p(y_tr))

    cat = CatBoostRegressor(
        iterations=n_estimators,
        learning_rate=0.05,
        depth=6,
        verbose=0,
        random_seed=42,
        thread_count=OPERATING_CPU_NUMBER,
    )
    cat.fit(X_tr, np.log1p(y_tr))

    return [lgbm, xgb, cat]


# 최적 weights를 계산합니다.
def compute_optimal_weights(val_preds: list[np.ndarray], y_val: np.ndarray) -> np.ndarray:
    n_models = len(val_preds)
    if n_models == 0:
        return np.array([])

    y_val = np.asarray(y_val, dtype=float)

    # 현재 조건의 오차를 계산합니다.
    def objective(w: np.ndarray) -> float:
        pred = np.zeros_like(y_val, dtype=float)
        for idx in range(n_models):
            pred += w[idx] * val_preds[idx]
        return mape(y_val, pred)

    constraints = ({"type": "eq", "fun": lambda w: np.sum(w) - 1.0},)
    bounds = [(0.0, 1.0)] * n_models
    init = np.ones(n_models, dtype=float) / float(n_models)
    res = minimize(objective, init, method="SLSQP", bounds=bounds, constraints=constraints)
    return res.x if res.success else init


# =========================
# 4. Training Run
# =========================
# 생산 작업을 실행합니다.
def run_production() -> tuple[pd.DataFrame, dict[str, Any]]:
    holiday_set = load_holidays_excel()

    if not PATH_INTENSITY.exists():
        print(f"Error: intensity file not found -> {PATH_INTENSITY}")
        return pd.DataFrame(), {}

    if not PATH_WIP_SUMMARY.exists():
        print(f"Error: WIP summary file not found -> {PATH_WIP_SUMMARY}")
        return pd.DataFrame(), {}

    wb = openpyxl.load_workbook(PATH_INTENSITY, read_only=True, data_only=True)
    # Excel 시트명을 공장명으로 매핑(구버전 F-코드 시트도 포함)
    sheet_to_plant: list[tuple[str, str]] = []
    seen_plants: set[str] = set()
    for sheet in wb.sheetnames:
        plant = map_sheet_to_plant(sheet)
        if plant and plant not in seen_plants:
            sheet_to_plant.append((sheet, plant))
            seen_plants.add(plant)

    results: list[dict[str, Any]] = []
    saved_models_dict: dict[str, Any] = {}

    for sheet_name, plant in sheet_to_plant:
        print(f"Processing {plant} (sheet: {sheet_name})...")

        df_raw = load_energy_sheet(PATH_INTENSITY, sheet_name)
        if "날짜" not in df_raw.columns:
            continue
        df_raw["날짜"] = pd.to_datetime(df_raw["날짜"], errors="coerce")
        df_raw = df_raw.dropna(subset=["날짜"]).copy()

        station_name = STATION_MAP[plant]
        df_weather = get_weather_data(station_name)
        if not df_weather.empty:
            df = df_raw.merge(df_weather, on="날짜", how="left")
        else:
            df = df_raw.copy()

        # 결측을 0으로 즉시 치환하지 않고 시계열 ffill/bfill로 보간합니다.
        # 강수량은 결측을 무강수(0)로 처리합니다.
        for col in WEATHER_COLS:
            if col not in df.columns:
                df[col] = np.nan
        df = fill_weather_gaps(df, WEATHER_COLS)

        df = df[df["날짜"].apply(lambda ts: is_workday(pd.Timestamp(ts), holiday_set))].copy()
        saved_models_dict[plant] = {}

        for target_name, ycol in TARGETS.items():
            if ycol not in df.columns:
                continue

            df_model, wip_cols, effective_train_start = merge_shortlisted_wip_features(
                df=df,
                plant=plant,
                target_name=target_name,
            )

            d_full = detect_special_events(make_features(df_model, ycol, holiday_set))
            d = d_full[~d_full["is_special_event"]].copy()

            mtr = (d["date"] >= effective_train_start) & (d["date"] <= TRAIN_END)
            mva = (d["date"] >= VALID_START) & (d["date"] <= VALID_END)
            mte = (d["date"] >= TEST_START) & (d["date"] <= TEST_END)

            if mtr.sum() < 100 or mva.sum() < 30 or mte.sum() < 30:
                continue

            y = d[ycol].to_numpy(dtype=float)
            ytr = y[mtr]
            yva = y[mva]
            yte = y[mte]

            X_all = build_feature_frame(d, wip_cols)

            val_preds: list[np.ndarray] = []
            te_preds: list[np.ndarray] = []
            m_types = ["M1", "M2", "M3", "M4"]

            plant_target_models: list[Any] = []
            features_used: dict[str, list[str]] = {}

            for model_type in m_types:
                X_sub = select_features(X_all, model_type)
                features_used[model_type] = list(X_sub.columns)

                Xtr = X_sub[mtr]
                Xva = X_sub[mva]
                Xte = X_sub[mte]

                models = train_models(Xtr, ytr)
                for model in models:
                    plant_target_models.append(model)
                    val_preds.append(np.expm1(model.predict(Xva)))
                    te_preds.append(np.expm1(model.predict(Xte)))

            opt_weights = compute_optimal_weights(val_preds, yva)
            if opt_weights.size == 0:
                continue

            ensemble_pred = sum(weight * pred for weight, pred in zip(opt_weights, te_preds))
            final_mape = mape(yte, ensemble_pred)

            feature_spec_version = FEATURE_SPEC_VERSION_WIP if wip_cols else "1.1"
            wip_item_codes = list(get_shortlist_item_codes(plant, target_name))

            saved_models_dict[plant][target_name] = {
                "models": plant_target_models,
                "weights": opt_weights,
                "m_types": m_types,
                "features": features_used,
                "target_col": ycol,
                "feature_spec_version": feature_spec_version,
                "wip_item_codes": wip_item_codes,
                "wip_feature_cols": list(wip_cols),
                "wip_available_start": WIP_AVAILABLE_START.strftime("%Y-%m-%d") if wip_cols else None,
                "effective_train_start": effective_train_start.strftime("%Y-%m-%d"),
            }

            results.append(
                {
                    "plant": plant,
                    "target": target_name,
                    "Advanced_MAPE": final_mape,
                    "effective_train_start": effective_train_start.strftime("%Y-%m-%d"),
                    "wip_feature_count": len(wip_cols),
                    "wip_item_codes": ",".join(wip_item_codes),
                }
            )
            print(
                f"  -> {target_name} 완료 | "
                f"train_start={effective_train_start.strftime('%Y-%m-%d')} | "
                f"wip={wip_item_codes or '[]'} | "
                f"MAPE={final_mape:.2f}%"
            )

    return pd.DataFrame(results), saved_models_dict


# 이 파일의 전체 실행 흐름을 시작합니다.
def main() -> None:
    df, models_dict = run_production()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if df.empty:
        print("\n[알림] 예측 결과가 비어 있어 파일을 저장하지 않았습니다.")
        return

    try:
        df.to_excel(OUTPUT_EXCEL, index=False)
        print(f"\n[성공] 성능 테스트 결과 저장: {OUTPUT_EXCEL}")

        joblib.dump(models_dict, OUTPUT_PKL, compress=3)
        print(f"[성공] 재학습 모델 저장: {OUTPUT_PKL}")

        split = {
            "train_start": TRAIN_START.strftime("%Y-%m-%d"),
            "train_end": TRAIN_END.strftime("%Y-%m-%d"),
            "valid_start": VALID_START.strftime("%Y-%m-%d"),
            "valid_end": VALID_END.strftime("%Y-%m-%d"),
            "test_start": TEST_START.strftime("%Y-%m-%d"),
            "test_end": TEST_END.strftime("%Y-%m-%d"),
        }
        metrics = summarize_metric_frame(df, ["Advanced_MAPE"])
        artifact = build_model_artifact_metadata(
            OUTPUT_PKL,
            model_key=OUTPUT_MODEL_KEY,
            training_profile="wip_shortlist",
            metrics=metrics,
            split=split,
            data_end_date=TEST_END.strftime("%Y-%m-%d"),
            train_end_date=TRAIN_END.strftime("%Y-%m-%d"),
            git_commit=get_git_commit(),
        )
        registry = load_model_registry(auto_create=True)
        registry = attach_model_artifact(registry, artifact, active=True)
        now = datetime.now().isoformat()
        registry["weights_updated_at"] = now
        registry["full_trained_at"] = now
        registry["data_end_date_global"] = TEST_END.strftime("%Y-%m-%d")
        registry["train_end_date_global"] = TRAIN_END.strftime("%Y-%m-%d")
        write_model_registry(registry)
        print("[성공] 모델 레지스트리 갱신 완료")
    except Exception as exc:
        print(f"\n[오류] 저장 중 문제가 발생했습니다: {exc}")


if __name__ == "__main__":
    main()
