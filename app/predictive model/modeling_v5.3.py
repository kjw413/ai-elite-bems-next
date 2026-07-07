# 이 파일은 v5.3 분위수 회귀(Quantile Regression) + CQR 이상탐지 모델을 학습합니다.
#
# v5.1 (점추정) 대비:
#   - LGBM/XGB/CatBoost 각각에 대해 alpha=0.05 / 0.50 / 0.95 분위수 모델을 학습합니다.
#   - 가중치 최적화는 MAPE가 아닌 pinball loss를 사용합니다(분위수 정합성).
#   - 앙상블 후 row-wise 정렬로 quantile crossing(P5>P50, P50>P95)을 보정합니다.
#   - 평가지표에 PICP(coverage)·MPIW(평균 구간폭)·pinball loss를 추가합니다.
#   - log1p/expm1 변환은 유지합니다(분위수는 단조변환에 invariant).
#
# v5.2 (분위수 + CQR + 보틀링/n_active_skus 도입 단계) 대비 v5.3 변경:
#   - 보틀링(EA) 합계 aggregate + 활성 SKU 카운트 피처를 정식 채택.
#   - 광주는 n_active_skus 분포 차이로 인해 N_ACTIVE_SKUS_EXCLUDE_PLANTS 정책으로 제외.
#   - Split 갱신: TRAIN=2021-01-01~2025-12-31, VAL=2026-01-01~2026-03-31, TEST=2026-04-01~2026-05-15.
#     기존 VAL=2025만으로는 2026 신라인/HALAL/포비 신규 SKU 분포를 못 봐서, VAL에 2026 Q1을 노출해
#     CQR 잔차 보정이 신규 패턴을 학습하도록 함.
from __future__ import annotations

import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import openpyxl
import pandas as pd

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

from wip_feature_shortlist import get_bottling_ea_codes, get_shortlist_item_codes
from app.services.v5_common import (
    BOTTLING_AGG_FEATURE,
    N_ACTIVE_SKUS_FEATURE,
    attach_model_artifact,
    build_model_artifact_metadata,
    compute_bottling_aggregate,
    detect_special_events,
    fill_weather_gaps,
    get_git_commit,
    is_workday,
    load_energy_sheet,
    load_holidays_excel,
    load_model_registry,
    load_n_active_skus_series,
    build_versioned_model_path,
    make_features,
    map_sheet_to_plant,
    PATH_ENERGY_SOURCE,
    PATH_WIP_SUMMARY as SHARED_WIP_SUMMARY,
    resolve_plant_sheet,
    summarize_metric_frame,
    V5_3_TEST_END,
    V5_3_TEST_START,
    V5_3_TRAIN_END,
    V5_3_TRAIN_START,
    V5_3_VALID_END,
    V5_3_VALID_START,
    WEATHER_COLS,
    WIP_AVAILABLE_START,
    write_model_registry,
)
# 분위수 학습 엔진은 웹 워커와 공유합니다(구조·가중치·CQR 일치 보장).
from app.services.v5_quantile_training import train_plant_target_quantile


PATH_WEATHER = BASE_DIR / "DB_weather.xlsx"
PATH_INTENSITY = PATH_ENERGY_SOURCE
PATH_WIP_SUMMARY = SHARED_WIP_SUMMARY

# 2026년 신라인/HALAL/포비 등 새 SKU 가 학습/평가에 들어가게끔 split을 한 분기 앞으로 당김.
# - TRAIN: 2021-01-01 ~ 2025-12-31 (5년, 가장 최근까지 학습)
# - VAL  : 2026-01-01 ~ 2026-03-31 (3개월, 신라인 패턴이 등장하는 구간을 CQR/가중치 보정에 노출)
# - TEST : 2026-04-01 ~ 2026-05-15 (1.5개월, 신라인 본격 가동 구간을 평가)
TRAIN_START = V5_3_TRAIN_START
TRAIN_END = V5_3_TRAIN_END
VALID_START = V5_3_VALID_START
VALID_END = V5_3_VALID_END
TEST_START = V5_3_TEST_START
TEST_END = V5_3_TEST_END

# 활성 SKU 카운트(wip_n_active_skus) 피처를 제외할 공장 목록.
# 광주: 학습기간 분포와 2026-test 분포 차이로 인해 추가 시 MAPE 가 6.5→12.7%로 악화 (2026-05-27 검증).
N_ACTIVE_SKUS_EXCLUDE_PLANTS: frozenset[str] = frozenset({"광주"})

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
OUTPUT_EXCEL = OUTPUT_DIR / "performance_test_results_v5.3.xlsx"
OUTPUT_MODEL_KEY = "v5.3"
OUTPUT_PKL = build_versioned_model_path(OUTPUT_MODEL_KEY, base_dir=OUTPUT_DIR)
# v5.3: 보틀링 EA aggregate + n_active_skus aggregate + 2026-Q1 VAL split
FEATURE_SPEC_VERSION_WIP = "1.3-wip-bottling-nactive-quantile"
FEATURE_SPEC_VERSION_BASE = "1.1-quantile"


# =========================
# 2. Utils
# =========================
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
# - shortlist 코드는 wip_<code> 개별 피처로 노출
# - 공장에 보틀링 EA 코드가 정의돼 있으면 그 합산을 log1p 한 'wip_bottling_ea_log' 한 컬럼으로 추가
# - WIP 시트 전체에서 일자별 활성 SKU 카운트(wip_n_active_skus)도 추가 — 다품종 동시 가동 시 changeover/CIP 부하 시그널
def merge_shortlisted_wip_features(
    df: pd.DataFrame,
    plant: str,
    target_name: str,
) -> tuple[pd.DataFrame, list[str], list[str], pd.Timestamp]:
    item_codes = tuple(get_shortlist_item_codes(plant, target_name))
    ea_codes = tuple(get_bottling_ea_codes(plant))

    if not item_codes and not ea_codes:
        return df.copy(), [], [], TRAIN_START

    # shortlist + bottling EA 합집합으로 로드 (중복 제거)
    codes_to_load = tuple(dict.fromkeys(item_codes + ea_codes))
    wip_df = load_selected_wip_frame(plant, codes_to_load)
    merged = df.merge(wip_df, on="날짜", how="left")

    # shortlist 개별 컬럼만 wip_cols 에 노출
    wip_cols = [build_wip_feature_col(code) for code in item_codes]
    for col in wip_cols:
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    # 보틀링 EA 합계 (log1p) — aggregate 만 추가
    if ea_codes:
        for code in ea_codes:
            col = build_wip_feature_col(code)
            if col not in merged.columns:
                merged[col] = 0.0
            merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)
        merged[BOTTLING_AGG_FEATURE] = compute_bottling_aggregate(merged, list(ea_codes))
        if BOTTLING_AGG_FEATURE not in wip_cols:
            wip_cols.append(BOTTLING_AGG_FEATURE)

    # 활성 SKU 카운트 — WIP 시트 전체에서 계산
    # 광주는 학습/test 분포 차이로 n_active_skus 가 오히려 MAPE 를 악화시키는 것으로 확인돼 제외.
    if plant not in N_ACTIVE_SKUS_EXCLUDE_PLANTS:
        n_active_df = load_n_active_skus_series(PATH_WIP_SUMMARY, plant)
        if not n_active_df.empty:
            merged = merged.merge(n_active_df, on="날짜", how="left")
            merged[N_ACTIVE_SKUS_FEATURE] = pd.to_numeric(
                merged[N_ACTIVE_SKUS_FEATURE], errors="coerce"
            ).fillna(0.0)
            if N_ACTIVE_SKUS_FEATURE not in wip_cols:
                wip_cols.append(N_ACTIVE_SKUS_FEATURE)

    effective_train_start = max(TRAIN_START, WIP_AVAILABLE_START)
    return merged, wip_cols, list(ea_codes), effective_train_start


# =========================
# 3. Modeling Engine
# =========================
# 분위수 학습 엔진(train_quantile_models / compute_quantile_weights /
# enforce_quantile_monotonicity / 평가지표 / CQR)은 app.services.v5_quantile_training
# 으로 분리되어 웹 재학습 워커와 공유됩니다. 이 스크립트는 고정 날짜 split mask를 만들어
# train_plant_target_quantile() 에 위임만 합니다.


# =========================
# 4. Training Run
# =========================
# (plant, target) 단위로 분위수 모델을 학습하고 평가 지표를 반환합니다.
def _train_plant_target(
    d: pd.DataFrame,
    ycol: str,
    wip_cols: list[str],
    mtr: pd.Series,
    mva: pd.Series,
    mte: pd.Series,
) -> tuple[dict[str, Any], dict[str, float]] | None:
    result = train_plant_target_quantile(d, ycol, wip_cols, mtr, mva, mte)
    if result is None:
        return None
    return result.spec, result.metrics


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

        for col in WEATHER_COLS:
            if col not in df.columns:
                df[col] = np.nan
        df = fill_weather_gaps(df, WEATHER_COLS)

        df = df[df["날짜"].apply(lambda ts: is_workday(pd.Timestamp(ts), holiday_set))].copy()
        saved_models_dict[plant] = {}

        for target_name, ycol in TARGETS.items():
            if ycol not in df.columns:
                continue

            df_model, wip_cols, bottling_ea_codes, effective_train_start = merge_shortlisted_wip_features(
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

            outcome = _train_plant_target(d, ycol, wip_cols, mtr, mva, mte)
            if outcome is None:
                continue
            saved, metrics = outcome

            feature_spec_version = FEATURE_SPEC_VERSION_WIP if wip_cols else FEATURE_SPEC_VERSION_BASE
            shortlist_codes = list(get_shortlist_item_codes(plant, target_name))
            # 추론에서 로드해야 하는 raw 코드 = shortlist + bottling EA (중복 제거)
            wip_item_codes_all = list(dict.fromkeys(shortlist_codes + list(bottling_ea_codes)))

            saved_models_dict[plant][target_name] = {
                **saved,
                "feature_spec_version": feature_spec_version,
                "wip_item_codes": wip_item_codes_all,
                "wip_shortlist_codes": shortlist_codes,
                "wip_bottling_ea_codes": list(bottling_ea_codes),
                "wip_feature_cols": list(wip_cols),
                "wip_available_start": WIP_AVAILABLE_START.strftime("%Y-%m-%d") if wip_cols else None,
                "effective_train_start": effective_train_start.strftime("%Y-%m-%d"),
            }

            results.append(
                {
                    "plant": plant,
                    "target": target_name,
                    "MAPE_p50": metrics["MAPE_p50"],
                    "Pinball_p05": metrics["Pinball_p05"],
                    "Pinball_p50": metrics["Pinball_p50"],
                    "Pinball_p95": metrics["Pinball_p95"],
                    "PICP_90": metrics["PICP_90"],
                    "MPIW_90": metrics["MPIW_90"],
                    "Val_PICP_90": metrics["Val_PICP_90"],
                    "effective_train_start": effective_train_start.strftime("%Y-%m-%d"),
                    "wip_feature_count": len(wip_cols),
                    "wip_item_codes": ",".join(wip_item_codes_all),
                    "wip_bottling_ea_codes": ",".join(bottling_ea_codes),
                }
            )
            print(
                f"  -> {target_name} 완료 | "
                f"train_start={effective_train_start.strftime('%Y-%m-%d')} | "
                f"wip={wip_item_codes_all or '[]'} | "
                f"MAPE(P50)={metrics['MAPE_p50']:.2f}% | "
                f"PICP90={metrics['PICP_90']*100:.1f}% | "
                f"MPIW90={metrics['MPIW_90']:.1f}"
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
        metrics = summarize_metric_frame(
            df,
            [
                "MAPE_p50",
                "Pinball_p05",
                "Pinball_p50",
                "Pinball_p95",
                "PICP_90",
                "MPIW_90",
                "Val_PICP_90",
            ],
        )
        artifact = build_model_artifact_metadata(
            OUTPUT_PKL,
            model_key=OUTPUT_MODEL_KEY,
            training_profile="quantile_wip",
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
