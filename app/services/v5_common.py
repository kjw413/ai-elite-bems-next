# 이 파일은 v5 예측 모델에서 공통으로 쓰는 경로와 보조 함수를 모아둡니다.
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import openpyxl
import pandas as pd

from app.config.paths import PROJECT_ROOT, sampled_db_path
from app.domain.factories import (
    AGGREGATE_FACTORY_MEMBERS as AGGREGATE_FACTORY_MEMBERS,
    BATCH_ALL_FACTORIES_LABEL as BATCH_ALL_FACTORIES_LABEL,
    FACTORY_AGGREGATE_DISPLAY_ORDER as FACTORY_AGGREGATE_DISPLAY_ORDER,
    FACTORY_DISPLAY_ORDER as FACTORY_DISPLAY_ORDER,
    FACTORY_PHYSICAL_DISPLAY_ORDER as FACTORY_PHYSICAL_DISPLAY_ORDER,
    FACTORY_TO_WEATHER_STATION,
    PREDICTION_FACTORY_OPTIONS as PREDICTION_FACTORY_OPTIONS,
    SHEET_TO_FACTORY_MAP,
)

logger = logging.getLogger(__name__)

# 사용 가능한 폴더 경로를 고릅니다.
def _pick_existing_dir(candidates: list[Path], default: Path) -> Path:
    for p in candidates:
        try:
            if p.exists() and p.is_dir():
                return p
        except Exception:
            continue
    return default


# 사용 가능한 파일 경로를 고릅니다.
def _pick_existing_file(candidates: list[Path], default: Path) -> Path:
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return p
        except Exception:
            continue
    return default


PREDICTIVE_MODEL_DIR = _pick_existing_dir(
    candidates=[
        PROJECT_ROOT / "app" / "predictive model",
        PROJECT_ROOT / "app" / "prediction model",
    ],
    default=PROJECT_ROOT / "app" / "predictive model",
)

TRAINED_MODEL_DIR = _pick_existing_dir(
    candidates=[
        PREDICTIVE_MODEL_DIR / "energy usage",
        PREDICTIVE_MODEL_DIR / "trained_model",
    ],
    default=PREDICTIVE_MODEL_DIR / "energy usage",
)

REGISTRY_PATH = TRAINED_MODEL_DIR / "v5_model_registry.json"
STATUS_PATH = TRAINED_MODEL_DIR / "v5_training_status.json"
MODEL_ARTIFACT_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MODEL_ARTIFACT_RETENTION_KEEP = _env_int("V5_MODEL_ARTIFACT_RETENTION_KEEP", 2)

MODEL_REGISTRY_PRESETS: dict[str, dict[str, Any]] = {
    "v5": {
        "label": "기본 v5 모델 (점추정)",
        "path": TRAINED_MODEL_DIR / "v5.pkl",
        "training_profile": "baseline",
    },
    "v5.1": {
        "label": "재공품 반영 v5 모델 (점추정)",
        "path": TRAINED_MODEL_DIR / "v5.1.pkl",
        "training_profile": "wip_shortlist",
    },
    "v5.2": {
        "label": "분위수 회귀 + CQR (정상범주 밴드)",
        "path": TRAINED_MODEL_DIR / "v5.2.pkl",
        "training_profile": "quantile_wip",
    },
    "v5.3": {
        "label": "v5.2 + 보틀링/활성SKU 피처 + 2026-Q1 VAL split",
        "path": TRAINED_MODEL_DIR / "v5.3.pkl",
        "training_profile": "quantile_wip",
    },
}
DEFAULT_MODEL_KEY = "v5.3"

DEFAULT_MODEL_PATH = next(
    (
        p
        for p in [
            TRAINED_MODEL_DIR / "v5.pkl",
            PREDICTIVE_MODEL_DIR / "v5.pkl",
            PREDICTIVE_MODEL_DIR / "trained_models_v5.pkl",
        ]
        if p.exists()
    ),
    TRAINED_MODEL_DIR / "v5.pkl",
)


PATH_WEATHER = PREDICTIVE_MODEL_DIR / "DB_weather.xlsx"
PATH_HOLIDAY = PREDICTIVE_MODEL_DIR / "DB_holiday.xlsx"
PATH_ENERGY_SOURCE = sampled_db_path("RawDB_에너지.xlsx", "ENERGY_SOURCE_XLSX")
PATH_WIP_SUMMARY = sampled_db_path("DB_재공품.xlsx", "WIP_SUMMARY_XLSX")
PATH_WIP_ITEM_MASTER = sampled_db_path("RawDB_재공품.xlsx", "WIP_ITEM_MASTER_XLSX")

ENERGY_UPLOAD_TO_MODEL_COLUMNS = {
    "date": "날짜",
    "freezing_power_kwh": "냉동전력량[kWh]",
    "air_compressor_kwh": "공압기[kWh]",
    "total_power_kwh": "전력량[kWh]",
    "fuel_nm3": "연료량[N㎥]",
    "water_ton": "용수량[ton]",
    "wastewater_ton": "폐수량[ton]",
    "mix_prod_kg": "믹스생산량[kg]",
    "power_per_ton_kwh": "전력원단위[kWh/mix-ton]",
    "fuel_per_ton_nm3": "연료원단위[N㎥/mix-ton]",
    "water_per_ton_ton": "용수원단위[ton/mix-ton]",
}

STATION_MAP: dict[str, str] = dict(FACTORY_TO_WEATHER_STATION)

# 마이그레이션 이전 양식의 Excel 시트명(F-코드) → 현재 한글 공장명 매핑.
# RawDB_에너지.xlsx, DB_재공품.xlsx 등 외부 파일이 한글 시트로 통일되기 전까지
# 한글 ↔ F-코드 간 양방향 폴백을 위해 사용합니다.
LEGACY_SHEET_TO_PLANT: dict[str, str] = {
    k: v for k, v in SHEET_TO_FACTORY_MAP.items() if str(k).startswith("F")
}

# 기상청 ASOS 지점 코드 매핑 (시트명 → 지점코드)
STATION_CODE_MAP: dict[str, int] = {
    "서울": 108,
    "김해": 253,
    "이천": 203,
    "부여": 236,
    "대구": 143,  # 경산공장 (2026-07 신규)
}


# 공장 표시 순서 — UI에서 모든 공장 목록을 노출할 때 따라야 하는 표준 순서.
# 사용 패턴:
#   - 5개 집계 뷰: 전사 → 남양주 → 김해 → 광주 → 논산
#   - 5개 개별 뷰: 남양주1 → 남양주2 → 김해 → 광주 → 논산
#   - 7개 전체 뷰(드롭다운): 전사 → 남양주1 → 남양주2 → 남양주 → 김해 → 광주 → 논산
# 전체 — 예측 실행 탭 전용 라벨. 5개 실공장(남양주1·남양주2·김해·광주·논산)을
# 한 번의 클릭으로 순차 예측한 뒤, 공장별로 분리해 결과를 보여 줍니다.
# (집계 합산 결과 1행만 보여 주는 '전사' 와는 다른 동작입니다.)
# AI 예측 페이지의 공장 셀렉트박스 옵션 — '전체' 이 가장 먼저, 그다음 전사·개별·집계.

WEATHER_COLS = ["평균기온", "일강수량", "상대습도", "일사량", "일조시간"]

TARGET_SPECS: dict[str, dict[str, str]] = {
    "전력": {"db_col": "total_power_kwh", "model_col": "전력량[kWh]", "unit": "kWh"},
    "연료": {"db_col": "fuel_nm3", "model_col": "연료량[N㎥]", "unit": "Nm³"},
    "용수": {"db_col": "water_ton", "model_col": "용수량[ton]", "unit": "ton"},
}

V5_1_TRAIN_START = pd.Timestamp("2023-01-01")
V5_1_TRAIN_END = pd.Timestamp("2024-12-31")
V5_1_VALID_START = pd.Timestamp("2025-01-01")
V5_1_VALID_END = pd.Timestamp("2025-12-31")
V5_1_TEST_START = pd.Timestamp("2026-01-01")
V5_1_TEST_END = pd.Timestamp("2026-04-30")

V5_3_TRAIN_START = pd.Timestamp("2021-01-01")
V5_3_TRAIN_END = pd.Timestamp("2025-12-31")
V5_3_VALID_START = pd.Timestamp("2026-01-01")
V5_3_VALID_END = pd.Timestamp("2026-03-31")
V5_3_TEST_START = pd.Timestamp("2026-04-01")
V5_3_TEST_END = pd.Timestamp("2026-05-15")

WIP_AVAILABLE_START = pd.Timestamp("2023-01-01")


# 피처 컬럼 이름을 안전한 형식으로 바꿉니다.
def sanitize_feature_columns(cols: list[str]) -> list[str]:
    return [re.sub(r"[^a-zA-Z0-9가-힣]", "_", str(c)) for c in cols]


# 에너지 컬럼 값을 일정한 형식으로 맞춥니다.
def normalize_energy_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        src: dst
        for src, dst in ENERGY_UPLOAD_TO_MODEL_COLUMNS.items()
        if src in df.columns
    }
    return df.rename(columns=rename_map).copy()


# 에너지 시트가 wide format(첫 컬럼=항목명, 헤더=날짜)이면 long format으로 transpose 합니다.
def _maybe_transpose_energy(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or df.shape[1] < 2:
        return df

    first_col_values = [str(v).strip() for v in df.iloc[:, 0].dropna().tolist()]
    metric_keywords = (
        "전력량", "냉동전력량", "공압기", "공기압축기", "공업기",
        "연료량", "용수량", "폐수량", "생산량", "원단위",
    )
    is_wide = any(any(k in v for k in metric_keywords) for v in first_col_values)
    if not is_wide:
        return df

    raw = df.copy()
    cats = raw.iloc[:, 0].astype(str).tolist()
    date_headers = list(raw.columns[1:])
    values = raw.iloc[:, 1:].to_numpy()
    long_df = pd.DataFrame(values.T, columns=cats)
    long_df = long_df.loc[:, ~long_df.columns.duplicated()].copy()
    long_df.insert(0, "날짜", pd.to_datetime(date_headers, errors="coerce"))
    long_df = long_df.dropna(subset=["날짜"]).reset_index(drop=True)
    for c in long_df.columns:
        if c == "날짜":
            continue
        long_df[c] = pd.to_numeric(long_df[c], errors="coerce")
    return long_df


# 에너지 시트 데이터를 불러옵니다(wide/long format 모두 처리).
def load_energy_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=sheet_name)
    df = _maybe_transpose_energy(df)
    return normalize_energy_columns(df)


# 휴일 엑셀 데이터를 불러옵니다.
#
# 보강 정책: 근로자의 날(매년 5/1)은 한국 법정공휴일은 아니지만
# 대부분 식품공장이 휴무 또는 축소가동하므로 모델 관점에서 휴일로 처리합니다.
# DB_holiday.xlsx에 누락되어 있던 케이스(예: 2026-05-01 근로자의 날)에서
# 예측이 평상시처럼 잡혀 큰 오차가 발생한 사례가 있었습니다.
def load_holidays_excel() -> set:
    s: set = set()
    if PATH_HOLIDAY.exists():
        hol = pd.read_excel(PATH_HOLIDAY)
        if "날짜" in hol.columns:
            hol["날짜"] = pd.to_datetime(hol["날짜"], errors="coerce")
            hol = hol.dropna(subset=["날짜"])
            s = set(hol["날짜"].dt.date.tolist())

    # 근로자의 날(5/1) 자동 보강 — 엑셀 범위 + 여유 1년 전후
    from datetime import date as _date
    years = {d.year for d in s} if s else set()
    if years:
        min_y, max_y = min(years) - 1, max(years) + 2
    else:
        cur = _date.today().year
        min_y, max_y = cur - 3, cur + 1
    for y in range(min_y, max_y + 1):
        s.add(_date(y, 5, 1))
    return s


# 주어진 날짜가 영업일인지 확인합니다.
def is_workday(ts: pd.Timestamp, holiday_set: set) -> bool:
    return (ts.dayofweek < 5) and (ts.date() not in holiday_set)


# 주어진 날짜의 비영업 여부(주말 또는 공휴일).
def _is_nonwork(ts: pd.Timestamp, holiday_set: set | None) -> bool:
    hs = holiday_set or set()
    return (ts.dayofweek >= 5) or (ts.date() in hs)


# 단일 날짜에 대한 캘린더 플래그를 계산합니다.
#
# 반환 키:
#   is_holiday                  : 당일이 주말 또는 공휴일이면 1
#   is_pre_holiday              : 익일이 주말/공휴일이면 1 (연휴 직전일)
#   is_post_holiday             : 전일이 주말/공휴일이면 1 (연휴 직후일)
#   is_post_long_weekend        : 당일은 영업일이고, 직전에 3일 이상 연속 비영업
#                                 (평일 공휴일이 끼인 장기연휴 직후 첫 가동일).
#                                 일반 주말(토+일=2일)은 학습 데이터에 충분히 빈번해서
#                                 모델이 직접 처리 가능 → 별도 보정 트리거에서 제외.
#   consecutive_nonwork_before  : 당일 직전 연속 비영업 일수 (0~7로 cap)
def calendar_flags(target_date, holiday_set: set | None) -> dict:
    d = pd.Timestamp(target_date).normalize()
    is_today_nonwork = _is_nonwork(d, holiday_set)

    tmrw = d + pd.Timedelta(days=1)
    yest = d - pd.Timedelta(days=1)
    is_tmrw_nonwork = _is_nonwork(tmrw, holiday_set)
    is_yest_nonwork = _is_nonwork(yest, holiday_set)

    consecutive = 0
    cur = yest
    for _ in range(14):  # 최대 14일 lookback
        if _is_nonwork(cur, holiday_set):
            consecutive += 1
            cur = cur - pd.Timedelta(days=1)
        else:
            break

    return {
        "is_holiday":                 int(is_today_nonwork),
        "is_pre_holiday":             int(is_tmrw_nonwork),
        "is_post_holiday":            int(is_yest_nonwork),
        "is_post_long_weekend":       int((not is_today_nonwork) and consecutive >= 3),
        "consecutive_nonwork_before": int(min(consecutive, 7)),
    }


# Excel 시트명을 공장명으로 매핑합니다(구버전 F-코드 호환).
def map_sheet_to_plant(sheet_name: str) -> str | None:
    if sheet_name in STATION_MAP:
        return sheet_name
    return LEGACY_SHEET_TO_PLANT.get(sheet_name)


# 공장명에 해당하는 실제 Excel 시트명을 찾습니다.
# 한글 공장명 시트가 있으면 그대로, 없으면 LEGACY_SHEET_TO_PLANT 역참조로
# 대응되는 F-코드 시트(F10A/F10B/F20/F30/F40)를 사용합니다.
def resolve_plant_sheet(xlsx_path: Path, plant: str) -> str | None:
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    except Exception:
        return None
    try:
        sheets = set(wb.sheetnames)
    finally:
        try:
            wb.close()
        except Exception:
            pass
    if plant in sheets:
        return plant
    for legacy, mapped in LEGACY_SHEET_TO_PLANT.items():
        if mapped == plant and legacy in sheets:
            return legacy
    return None


# 날씨 관측소 엑셀 데이터를 불러옵니다.
def load_weather_station_excel(station_name: str) -> pd.DataFrame:
    """
    Returns DataFrame with columns:
      ["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"]
    """
    if not PATH_WEATHER.exists():
        return pd.DataFrame(columns=["날짜"] + WEATHER_COLS)

    try:
        df_xl = pd.read_excel(PATH_WEATHER, sheet_name=station_name)
    except Exception as exc:
        logger.warning(
            "[v5_common] 날씨 엑셀 읽기 실패: path=%s station=%s error=%s",
            PATH_WEATHER,
            station_name,
            exc,
        )
        return pd.DataFrame(columns=["날짜"] + WEATHER_COLS)

    if df_xl.empty:
        return pd.DataFrame(columns=["날짜"] + WEATHER_COLS)

    if "일시" not in df_xl.columns:
        return pd.DataFrame(columns=["날짜"] + WEATHER_COLS)

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

    # 결측은 0이 아니라 NaN으로 보존합니다(0과 "측정 안 됨"은 의미가 다릅니다).
    # 갭 채움은 호출 측에서 fill_weather_gaps()로 처리합니다.
    for c in WEATHER_COLS:
        if c not in df_w.columns:
            df_w[c] = np.nan
        else:
            df_w[c] = pd.to_numeric(df_w[c], errors="coerce")

    df_w = df_w[["날짜"] + WEATHER_COLS]
    return df_w


# 컬럼별 결측 처리 정책 (KMA 도메인 규약 기반)
#   "zero"        : 결측은 0으로 채움 (강수량은 빈 셀이 무강수를 의미)
#   "interpolate" : 시계열 ffill→bfill로 채우고, 그래도 남으면 0으로 안전장치
#   "nan"         : 결측을 NaN 그대로 보존 (트리 기반 모델이 NaN을 직접 처리;
#                    일사량/일조시간은 서로 보완 관계라 한쪽 결측 시 다른 쪽이 신호로 동작)
WEATHER_FILL_POLICY: dict[str, str] = {
    "일강수량": "zero",
    "평균기온": "interpolate",
    "상대습도": "interpolate",
    "일사량":   "nan",
    "일조시간": "nan",
}


# 결측 날씨 값을 컬럼별 정책에 따라 채웁니다.
def fill_weather_gaps(df: pd.DataFrame, weather_cols: list[str] | None = None) -> pd.DataFrame:
    """병합된 날씨 컬럼의 결측을 KMA 도메인 규약에 따라 처리합니다.

    정책은 :data:`WEATHER_FILL_POLICY` 매핑을 참조합니다.

    - 일강수량: 결측은 무강수(0).
    - 평균기온/상대습도: 시계열 ffill→bfill, 그래도 남으면 0(안전장치).
    - 일사량/일조시간: NaN 그대로 보존(트리 모델이 NaN 분기 처리,
      두 컬럼이 상호 보완 신호로 동작하도록 의도적으로 채우지 않음).

    호출 전제: df에 ``날짜``가 있고 시간순으로 정렬되어 있을 것.
    """
    cols = weather_cols if weather_cols is not None else WEATHER_COLS
    out = df.copy()
    if "날짜" in out.columns:
        out = out.sort_values("날짜").reset_index(drop=True)

    for col in cols:
        if col not in out.columns:
            out[col] = np.nan
        series = pd.to_numeric(out[col], errors="coerce")
        policy = WEATHER_FILL_POLICY.get(col, "interpolate")
        if policy == "zero":
            out[col] = series.fillna(0.0)
        elif policy == "nan":
            out[col] = series  # 결측 보존 (트리 모델이 처리)
        else:  # "interpolate"
            out[col] = series.ffill().bfill().fillna(0.0)
    return out


# 피처 데이터를 만듭니다.
def make_features(df: pd.DataFrame, ycol: str, holiday_set: set | None = None) -> pd.DataFrame:
    """Feature engineering — synchronized with modeling_v5.1.py training script."""
    d = df.copy()
    d["date"] = pd.to_datetime(d["날짜"], errors="coerce")
    d = d.dropna(subset=["date"]).sort_values("date")

    # 1. 생산량 로그 변환
    if "믹스생산량[kg]" in d.columns:
        d["mix_ton"] = pd.to_numeric(d["믹스생산량[kg]"], errors="coerce").fillna(0.0) / 1000.0
    else:
        d["mix_ton"] = 0.0
    d["log_mix_ton"] = np.log1p(d["mix_ton"])

    # 2. 지연 변수 및 원단위(Intensity) 생성 (당일 실측치 누수 방지)
    if ycol in d.columns:
        d["lag1"] = d[ycol].shift(1)
        d["r7mean"] = d[ycol].rolling(7, min_periods=1).mean().shift(1)
        
        lag1_mix_ton = d["mix_ton"].shift(1)
        d["intensity_lag1"] = d["lag1"] / (lag1_mix_ton + 1e-6)
    else:
        d["lag1"] = 0.0
        d["r7mean"] = 0.0
        d["intensity_lag1"] = 0.0

    # 3. 공휴일 거리 변수
    if holiday_set:
        h_list = sorted(list(holiday_set))

        # 휴일과의 날짜 차이를 계산합니다.
        def _get_dist(target_date):
            future_h = [h for h in h_list if h > target_date.date()]
            past_h = [h for h in h_list if h < target_date.date()]
            to_h = (future_h[0] - target_date.date()).days if future_h else 30
            from_h = (target_date.date() - past_h[-1]).days if past_h else 30
            return min(to_h, 30), min(from_h, 30)

        d["dist_to_h"], d["dist_from_h"] = zip(*d["date"].apply(_get_dist))
    else:
        d["dist_to_h"] = 30
        d["dist_from_h"] = 30

    # 3-b. 캘린더 플래그 (주말/공휴일 구조)
    #     휴일 당일, 직전·직후, 장기연휴 직후 첫 가동일 등 train 분포에서
    #     상대적으로 드물어 연료/용수 예측 오차가 크게 튀는 구간을 학습에 노출.
    flag_records = d["date"].apply(lambda ts: calendar_flags(ts, holiday_set))
    flag_df = pd.DataFrame(list(flag_records.values), index=d.index)
    for col in ["is_holiday", "is_pre_holiday", "is_post_holiday",
                "is_post_long_weekend", "consecutive_nonwork_before"]:
        d[col] = flag_df[col].astype(int) if col in flag_df.columns else 0

    # 기본 시계열 및 기상 변수
    d["dow"] = d["date"].dt.dayofweek
    d["month"] = d["date"].dt.month

    for col in ["log_mix_ton", "평균기온"]:
        if col in d.columns:
            d[f"{col}_lag1"] = d[col].shift(1)
            d[f"{col}_r7mean"] = d[col].rolling(7, min_periods=1).mean()

    # 기상 지수
    if "평균기온" in d.columns:
        d["HDD"] = np.maximum(18 - d["평균기온"], 0)
        d["CDD"] = np.maximum(d["평균기온"] - 22, 0)
        # 다중 분기점 냉방도일 — 단일 22℃ 데드밴드는 냉동 기저부하가 크게 오르는
        # 18~24℃ 램프 구간을 CDD=0으로 가려 냉방 신호를 죽인다(남양주2 2026 여름
        # 전력 과소예측 진단: 잔차가 1월 −2.5%→6월 +14%로 냉동부하와 동행, corr +0.65).
        # 낮은 임계값 분기점을 추가해 연속적 냉방 부하를 트리가 조각선형으로 학습하도록 노출.
        d["CDD16"] = np.maximum(d["평균기온"] - 16, 0)
        d["CDD10"] = np.maximum(d["평균기온"] - 10, 0)
        # 계절·축냉(열관성) 앵커 — 빠른 온난화 램프에서 target lag1/r7mean의 평균회귀로
        # 생기는 과소예측을 완화. 14/30일 이동평균 기온과 14일 평활 냉방도일.
        # (기온은 입력 피처이므로 당일 포함 rolling 도 누수가 아니다.)
        d["평균기온_r14mean"] = d["평균기온"].rolling(14, min_periods=1).mean()
        d["평균기온_r30mean"] = d["평균기온"].rolling(30, min_periods=1).mean()
        d["CDD16_r14mean"] = d["CDD16"].rolling(14, min_periods=1).mean()
    if "평균기온" in d.columns and "상대습도" in d.columns:
        d["THI"] = 0.72 * (d["평균기온"] + d["상대습도"]) + 40.6

    # 전역 dropna 제거하고 필수 컬럼 결측치만 확인 (예측행 탈락 방지)
    req_cols = ["lag1", "log_mix_ton", "dist_to_h"]
    check_cols = [c for c in req_cols if c in d.columns]
    d = d.dropna(subset=check_cols).reset_index(drop=True)
    return d


# =============================================================================
# 보틀링(EA 단위 병제품) 합계 피처
# =============================================================================
# 믹스 생산량(mix_ton)이 낮아도 보틀링 라인이 풀가동하면 전력/연료가 평소처럼
# 소비되는 케이스(2026-05-22 김해 over-band)를 모델에 명시적으로 노출.
# 개별 EA 코드 합 / 1000 후 log1p 변환 — 보틀(EA) 수치는 수십만 단위라
# 로그스케일이 다른 BT 단위 피처와 비교 가능해진다.
BOTTLING_AGG_FEATURE = "wip_bottling_ea_log"

# =============================================================================
# 활성 SKU 카운트 피처
# =============================================================================
# 당일 처리량 > 0 인 WIP 품목 코드 수. 다품종 동시 가동 시의 changeover/CIP
# 오버헤드를 모델에 노출하기 위한 피처. mix_ton/보틀링 합과 독립적인 시그널이며,
# 학습기간엔 잘 안 나오던 신라인이 한 날에 몰리는 패턴(2026-05-12, 5/18)에 유용.
# 컬럼은 raw 카운트(0~100 정도). 로그 변환 없이 그대로 사용.
N_ACTIVE_SKUS_FEATURE = "wip_n_active_skus"


def _wip_col_name_for(code: str) -> str:
    return f"wip_{str(code).strip()}"


# 보틀링 EA 합계(log1p) 피처를 계산합니다.
# merged: 'wip_<code>' 컬럼들이 이미 join 된 데이터프레임
# ea_codes: 합산할 raw item code 목록 (예: ('220032','220067',...))
def compute_bottling_aggregate(
    merged: pd.DataFrame,
    ea_codes: list[str] | tuple[str, ...] | None,
) -> pd.Series:
    if not ea_codes:
        return pd.Series(0.0, index=merged.index, name=BOTTLING_AGG_FEATURE)
    cols = [_wip_col_name_for(c) for c in ea_codes]
    present = [c for c in cols if c in merged.columns]
    if not present:
        return pd.Series(0.0, index=merged.index, name=BOTTLING_AGG_FEATURE)
    total = (
        merged[present]
        .apply(pd.to_numeric, errors="coerce")
        .fillna(0.0)
        .sum(axis=1)
    )
    return np.log1p(total / 1000.0).rename(BOTTLING_AGG_FEATURE)


# 공장 WIP 시트 전체에서 일자별 활성 SKU 카운트를 계산해 ['날짜', wip_n_active_skus] DataFrame 으로 반환합니다.
# wip_summary_path: PATH_WIP_SUMMARY 또는 그 호환 경로
# plant: 공장명 (한글 또는 F-코드)
def load_n_active_skus_series(wip_summary_path, plant: str) -> pd.DataFrame:
    from pathlib import Path as _Path  # 지연 import (순환 회피)
    p = _Path(wip_summary_path)
    if not p.exists():
        return pd.DataFrame(columns=["날짜", N_ACTIVE_SKUS_FEATURE])
    sheet_name = resolve_plant_sheet(p, plant) or plant
    try:
        df = pd.read_excel(p, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame(columns=["날짜", N_ACTIVE_SKUS_FEATURE])
    if "날짜" not in df.columns:
        return pd.DataFrame(columns=["날짜", N_ACTIVE_SKUS_FEATURE])
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"]).copy()
    code_cols = [c for c in df.columns if re.fullmatch(r"\d{4,}", str(c))]
    if not code_cols:
        return pd.DataFrame({"날짜": df["날짜"], N_ACTIVE_SKUS_FEATURE: 0.0})
    numeric = df[code_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    counts = (numeric > 0).sum(axis=1).astype(float)
    return pd.DataFrame({"날짜": df["날짜"], N_ACTIVE_SKUS_FEATURE: counts}).reset_index(drop=True)


# 안전한 피처 값을 가져옵니다.
def get_safe_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Predict/Train 시 안전하게 사용할 수 있는 허용된 피처(Whitelist)만 반환.
    Data Leakage를 방지하기 위해 실측치(y, 원단위 등)는 필터링됨.
    """
    allowed_patterns = [
        r"^log_mix_ton$",
        r"^log_mix_ton_lag1$",
        r"^log_mix_ton_r7mean$",
        r"^lag1$",
        r"^r7mean$",
        r"^intensity_lag1$",
        r"^dist_to_h$",
        r"^dist_from_h$",
        r"^is_holiday$",
        r"^is_pre_holiday$",
        r"^is_post_holiday$",
        r"^is_post_long_weekend$",
        r"^consecutive_nonwork_before$",
        r"^dow$",
        r"^month$",
        r"^HDD$",
        r"^CDD$",
        r"^CDD16$",
        r"^CDD10$",
        r"^CDD16_r14mean$",
        r"^THI$",
        r"^평균기온$",
        r"^평균기온_lag1$",
        r"^평균기온_r7mean$",
        r"^평균기온_r14mean$",
        r"^평균기온_r30mean$",
        r"^상대습도$",
    ]
    
    safe_cols = []
    for col in df.columns:
        col_str = str(col)
        if any(re.match(p, col_str) for p in allowed_patterns):
            safe_cols.append(col)
            
    res = df[safe_cols].copy()
    res.columns = [re.sub(r"[^a-zA-Z0-9가-힣]", "_", str(c)) for c in res.columns]
    return res


def build_feature_frame(df: pd.DataFrame, wip_feature_cols: list[str] | None = None) -> pd.DataFrame:
    """Build the canonical safe v5 feature frame, optionally appending WIP features."""
    base = get_safe_features(df)
    if not wip_feature_cols:
        return base

    wip_frame = pd.DataFrame(index=df.index)
    for col in wip_feature_cols:
        raw = df[col] if col in df.columns else pd.Series(0.0, index=df.index)
        wip_frame[col] = pd.to_numeric(raw, errors="coerce").fillna(0.0)

    wip_frame.columns = sanitize_feature_columns(list(wip_frame.columns))
    return pd.concat([base, wip_frame], axis=1)


def select_features(X: pd.DataFrame, model_type: str) -> pd.DataFrame:
    """Select model-specific feature subsets for all v5 training paths."""
    cols = list(X.columns)
    wip_cols = [c for c in cols if str(c).startswith("wip_")]

    if model_type == "M1":
        use = [
            c for c in cols
            if "mix" in c or "lag" in c or "dow" in c or "month" in c or "r7" in c
            or "holiday" in c or "nonwork" in c or "weekend" in c
            or "dist_to_h" in c or "dist_from_h" in c
        ]
    elif model_type == "M2":
        use = [c for c in cols if "HDD" in c or "CDD" in c or "mix" in c]
    elif model_type == "M3":
        use = [
            c for c in cols
            if "평균기온" in c or "상대습도" in c or "Solar" in c or "mix" in c
        ]
    elif model_type == "M4":
        use = [c for c in cols if "THI" in c or "CDD" in c or "HDD" in c or "mix" in c]
    else:
        use = cols

    use = list(dict.fromkeys(use + wip_cols))
    return X[use].copy()


# 특이 이벤트를 감지합니다.
def detect_special_events(d: pd.DataFrame) -> pd.DataFrame:
    dd = d.copy()
    if "mix_ton" not in dd.columns:
        dd["is_special_event"] = False
        return dd

    rolling_median = dd["mix_ton"].rolling(30, min_periods=7).median()
    dd["is_special_event"] = (dd["mix_ton"] < rolling_median * 0.1) | (dd["mix_ton"] < 1.0)
    dd["is_special_event"] = dd["is_special_event"].fillna(False)
    return dd


# =============================================================================
# v5.2 분위수 밴드 헬퍼
# =============================================================================
# 정상범주 밴드 라벨/상태 정의 — DB의 prediction_log.band_status 와 UI 라벨이 모두 이 상수 참조.
BAND_INSIDE = "inside"
BAND_OVER = "over"    # 실측이 P95 초과 — 과사용/이상 발생 의심
BAND_UNDER = "under"  # 실측이 P05 미만 — 저사용/생산중단/측정 누락 의심

# UI에서 노출할 한국어 라벨 (이상감지 배너, 진단 카드 등).
BAND_STATUS_LABELS_KO: dict[str, str] = {
    BAND_INSIDE: "정상범주",
    BAND_OVER:   "과사용 의심 ↑",
    BAND_UNDER:  "저사용 의심 ↓",
}


def classify_band(
    actual: float | None,
    pred_p05: float | None,
    pred_p50: float | None,
    pred_p95: float | None,
) -> tuple[str | None, float | None]:
    """실측값이 [P05, P95] 정상범주 안인지 분류.

    Returns
    -------
    (band_status, band_position)
        band_status: 'inside' | 'over' | 'under' | None(실측없음 또는 밴드없음)
        band_position: (actual - P50) / ((P95 - P05) / 2)
            ±1 ≈ 밴드 가장자리, ±2 = 밴드 폭만큼 벗어남(매우 비정상).
    """
    if actual is None or pred_p05 is None or pred_p50 is None or pred_p95 is None:
        return None, None
    try:
        a = float(actual); lo = float(pred_p05); mid = float(pred_p50); hi = float(pred_p95)
    except (TypeError, ValueError):
        return None, None

    # 단조성 안전장치 — 보정 등으로 lo>hi가 될 일은 거의 없지만 방어적으로 정렬.
    if lo > hi:
        lo, hi = hi, lo
    half = max((hi - lo) / 2.0, 1.0)  # 0 나눗셈 방지: 최소 1.0
    pos = (a - mid) / half

    if a < lo:
        return BAND_UNDER, pos
    if a > hi:
        return BAND_OVER, pos
    return BAND_INSIDE, pos


def spec_is_quantile(spec: dict | None) -> bool:
    """모델 spec이 v5.2(분위수) 구조인지 판별.

    v5.2/v5.3의 spec은 modeling_v5.3.py(이전 modeling_v5.2.py)에서 'models_by_q'/'weights_by_q'/'quantiles' 키로 저장됨.
    """
    if not spec:
        return False
    return "models_by_q" in spec and "weights_by_q" in spec


def resolve_cqr_margins(spec: dict | None) -> tuple[float, float]:
    """spec에서 CQR 하한/상한 보정폭 ``(q_hat_lower, q_hat_upper)`` 을 결정.

    개선안 §10.4(상단 전용 보정) 대응. 학습이 상·하 비대칭 보정폭을
    ``cqr_q_hat_lower`` / ``cqr_q_hat_upper`` 로 저장하면 그 값을 쓰고,
    없으면(구 모델) 대칭 ``cqr_q_hat`` 을 양쪽에 동일 적용한다.

    적용 규약(추론·probe 공통):
        p05_final = p05 - q_hat_lower
        p95_final = p95 + q_hat_upper
    반환값은 항상 0 이상(밴드 축소 방지).
    """
    if not spec:
        return 0.0, 0.0

    def _num(value: Any) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(f):
            return None
        return f

    sym = _num(spec.get("cqr_q_hat"))
    lower = _num(spec.get("cqr_q_hat_lower"))
    upper = _num(spec.get("cqr_q_hat_upper"))

    if lower is None:
        lower = sym
    if upper is None:
        upper = sym

    lower = max(lower or 0.0, 0.0)
    upper = max(upper or 0.0, 0.0)
    return lower, upper


# JSON 데이터를 읽습니다.
def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# JSON 데이터를 안전하게 저장합니다.
def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# JSON에 안전하게 넣을 수 있는 값으로 바꿉니다.
def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    return value


# 경로를 프로젝트 기준 문자열로 바꿉니다.
def to_project_relative_path_str(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except Exception:
        return str(path)


# 모델 파일 경로를 확정합니다.
def resolve_model_path(path_str: str | None, default: Path | None = None) -> Path:
    fallback = (default or DEFAULT_MODEL_PATH).resolve()
    if not path_str:
        return fallback
    path = Path(path_str)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


# 모델 키에 대응하는 기본 경로를 가져옵니다.
def get_model_preset_path(model_key: str | None) -> Path | None:
    preset = MODEL_REGISTRY_PRESETS.get(str(model_key or ""))
    if not preset:
        return None
    return Path(preset["path"])


# 경로가 모델 프리셋의 고정 파일명과 정확히 일치하는지 확인합니다.
def infer_model_key_from_preset_path(path: str | Path | None) -> str | None:
    if not path:
        return None

    raw = str(path)
    try:
        resolved = resolve_model_path(raw)
    except Exception:
        resolved = Path(raw)

    for key, preset in MODEL_REGISTRY_PRESETS.items():
        if resolved == Path(preset["path"]).resolve():
            return key
    return None


# 경로에서 대표 모델 키를 추정합니다.
def infer_model_key_from_path(path: str | Path | None) -> str | None:
    if not path:
        return None

    raw = str(path)
    try:
        resolved = resolve_model_path(raw)
    except Exception:
        resolved = Path(raw)

    name = resolved.name.lower()
    if "wip_shortlist" in name:
        return "v5.1"
    if name == "v5.pkl" or name.startswith("trained_models_v5_"):
        return "v5"

    preset_key = infer_model_key_from_preset_path(path)
    if preset_key:
        return preset_key

    stem = resolved.stem.lower()
    for key in sorted(MODEL_REGISTRY_PRESETS.keys(), key=len, reverse=True):
        key_l = key.lower()
        if stem.startswith(f"{key_l}_") or stem.startswith(f"{key_l}-"):
            return key
    return None


# 모델 키를 안전한 파일명 일부로 바꿉니다.
def _safe_model_key(model_key: str | None) -> str:
    key = str(model_key or DEFAULT_MODEL_KEY).strip() or DEFAULT_MODEL_KEY
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


# 새 학습 결과를 저장할 버전드 모델 파일 경로를 만듭니다.
def build_versioned_model_path(
    model_key: str | None,
    *,
    base_dir: Path | None = None,
    timestamp: datetime | None = None,
    suffix: str = ".pkl",
) -> Path:
    target_dir = base_dir or TRAINED_MODEL_DIR
    stamp = (timestamp or datetime.now()).strftime(MODEL_ARTIFACT_TIMESTAMP_FORMAT)
    base_name = f"{_safe_model_key(model_key)}_{stamp}"
    candidate = target_dir / f"{base_name}{suffix}"
    seq = 1
    while candidate.exists():
        seq += 1
        candidate = target_dir / f"{base_name}_{seq}{suffix}"
    return candidate


# 모델 버전 파일에 딸린 성능 보고서(sidecar) 경로를 만듭니다.
def perf_report_path_for(model_path: Path) -> Path:
    p = Path(model_path)
    return p.with_name(f"{p.stem}_perf.json")


# 오래된 버전드 모델 artifact 파일을 정리합니다(활성 + 최근 이전 모델만 보존).
#
# 정책: 같은 model_key 의 ``{key}_{timestamp}.pkl`` 파일을 mtime 내림차순으로 정렬해
# 최신 ``keep`` 개만 남기고 나머지를 삭제합니다. 활성 경로(active_path)는 어떤 경우에도
# 보존합니다. 고정 프리셋 파일(예: v5.3.pkl)은 ``{key}_*`` 패턴에 걸리지 않아 안전합니다.
# 각 모델의 성능 보고서(sidecar `_perf.json`)도 함께 삭제합니다.
#
# 반환: 실제로 삭제된 파일 경로 목록(문자열).
def cleanup_model_artifacts(
    model_key: str | None,
    *,
    keep: int = MODEL_ARTIFACT_RETENTION_KEEP,
    base_dir: Path | None = None,
    active_path: Path | str | None = None,
) -> list[str]:
    target_dir = base_dir or TRAINED_MODEL_DIR
    safe_key = _safe_model_key(model_key)
    keep = max(int(keep), 1)

    try:
        candidates = sorted(
            target_dir.glob(f"{safe_key}_*.pkl"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
    except Exception:
        return []

    active_resolved: Path | None = None
    if active_path:
        try:
            active_resolved = Path(active_path).resolve()
        except Exception:
            active_resolved = None

    deleted: list[str] = []
    kept = 0
    for path in candidates:
        try:
            is_active = active_resolved is not None and path.resolve() == active_resolved
        except Exception:
            is_active = False

        if is_active or kept < keep:
            kept += 1
            continue

        # 보존 한도를 초과한 비활성 모델 → 삭제(+ 성능 보고서 sidecar).
        try:
            path.unlink(missing_ok=True)  # type: ignore[call-arg]
            deleted.append(str(path))
        except Exception:
            continue
        sidecar = perf_report_path_for(path)
        try:
            if sidecar.exists():
                sidecar.unlink(missing_ok=True)  # type: ignore[call-arg]
                deleted.append(str(sidecar))
        except Exception:
            pass

    return deleted


# 파일 SHA-256을 계산합니다.
def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 현재 git commit을 가져옵니다. git이 없거나 실패하면 None을 반환합니다.
def get_git_commit(short: bool = True) -> str | None:
    try:
        args = ["git", "rev-parse", "--short" if short else "HEAD"]
        result = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        value = result.stdout.strip()
        return value or None
    except Exception:
        return None


# 성능 결과 테이블을 레지스트리에 넣기 좋은 요약값으로 바꿉니다.
def summarize_metric_frame(df: pd.DataFrame, metric_cols: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"rows": int(len(df))}
    for col in metric_cols:
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            continue
        summary[col] = {
            "mean": float(series.mean()),
            "median": float(series.median()),
            "min": float(series.min()),
            "max": float(series.max()),
        }
    return summary


# 재학습 모델의 사후 보정 정책 기본값.
#   - holiday_adjacent_correction: 휴일 인접일 곱셈 보정(HOLIDAY_ADJ_CORRECTIONS).
#       전체 재학습 모델은 캘린더 피처(is_holiday/is_post_long_weekend 등)를 최신 데이터로
#       직접 학습하므로 임시 가드인 휴일 사후 보정을 비활성화한다.
#   - drift_correction: 223579 같은 신설 라인 단위부하 가산 보정(DRIFT_CORRECTIONS).
#       아직 학습 피처가 아니므로 재학습 후에도 유지한다.
# 예측 측은 활성 artifact 의 post_processing 을 읽어 보정 적용 여부를 결정한다.
# 이 메타데이터가 없는 과거 모델은 두 보정 모두 적용(하위 호환)한다.
RETRAIN_POST_PROCESSING_DEFAULTS: dict[str, bool] = {
    "holiday_adjacent_correction": False,
    "drift_correction": True,
}


# 모델 파일에 대한 불변 메타데이터를 만듭니다.
def build_model_artifact_metadata(
    path: Path,
    *,
    model_key: str | None = None,
    training_profile: str | None = None,
    metrics: dict[str, Any] | None = None,
    split: dict[str, Any] | None = None,
    data_end_date: str | None = None,
    train_end_date: str | None = None,
    git_commit: str | None = None,
    post_processing: dict[str, Any] | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    artifact: dict[str, Any] = {
        "schema_version": schema_version,
        "path": to_project_relative_path_str(resolved),
        "filename": resolved.name,
        "model_key": str(model_key or infer_model_key_from_path(resolved) or ""),
        "training_profile": str(training_profile or ""),
        "sha256": sha256_file(resolved),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "data_end_date": data_end_date,
        "train_end_date": train_end_date,
        "metrics": metrics or {},
        "split": split or {},
        "post_processing": post_processing or {},
    }
    # post_processing 은 빈 dict 라도 의미가 있으므로 None/"" 만 제거하고 dict 는 보존.
    cleaned = {
        k: v
        for k, v in artifact.items()
        if v not in (None, "") or k == "post_processing"
    }
    return _json_safe(cleaned)


# 레지스트리에 모델 artifact를 추가하고, 필요하면 활성 포인터를 교체합니다.
def attach_model_artifact(
    registry: dict[str, Any],
    artifact: dict[str, Any],
    *,
    active: bool = True,
) -> dict[str, Any]:
    reg = dict(registry or {})
    artifact_record = _json_safe(dict(artifact or {}))
    artifact_path = str(artifact_record.get("path") or "").strip()
    if not artifact_path:
        return reg

    artifacts = dict(reg.get("artifacts") or {})
    artifacts[artifact_path] = artifact_record
    reg["artifacts"] = artifacts
    reg["artifact_retention"] = {
        "keep_per_model_key": MODEL_ARTIFACT_RETENTION_KEEP,
        "policy": "active_plus_recent",
        "cleanup": "manual",
    }

    if active:
        reg["active_artifact"] = artifact_record
        reg["active_model_path"] = artifact_path
        if artifact_record.get("model_key"):
            reg["active_model_key"] = str(artifact_record["model_key"])
        if artifact_record.get("training_profile"):
            reg["training_profile"] = str(artifact_record["training_profile"])
    return reg


# 레지스트리에서 현재 활성 artifact 메타데이터를 찾습니다.
def get_active_model_artifact(registry: dict[str, Any] | None) -> dict[str, Any]:
    reg = registry or {}
    active_path = str(reg.get("active_model_path") or "").strip()
    active = reg.get("active_artifact")
    if isinstance(active, dict):
        artifact_path = str(active.get("path") or "").strip()
        if not active_path or artifact_path == active_path:
            return active
        try:
            active_resolved = resolve_model_path(active_path)
            artifact_resolved = resolve_model_path(artifact_path)
            if artifact_resolved == active_resolved:
                return active
        except Exception:
            pass

    artifacts = reg.get("artifacts")
    if not active_path or not isinstance(artifacts, dict):
        return {}

    direct = artifacts.get(active_path)
    if isinstance(direct, dict):
        return direct

    try:
        active_resolved = resolve_model_path(active_path)
    except Exception:
        active_resolved = Path(active_path)

    for record in artifacts.values():
        if not isinstance(record, dict):
            continue
        try:
            record_path = resolve_model_path(str(record.get("path") or ""))
        except Exception:
            record_path = Path(str(record.get("path") or ""))
        if record_path == active_resolved:
            return record
    return {}


# 레지스트리에 기록된 size/SHA와 실제 모델 파일이 일치하는지 확인합니다.
def validate_model_artifact(path: Path, artifact: dict[str, Any] | None) -> None:
    if not artifact:
        return

    p = Path(path)
    stat = p.stat()

    expected_size = artifact.get("size_bytes")
    if expected_size not in (None, ""):
        try:
            expected_size_int = int(expected_size)
        except (TypeError, ValueError):
            expected_size_int = None
        if expected_size_int is not None and expected_size_int != stat.st_size:
            raise ValueError(
                f"Model artifact size mismatch: {p} "
                f"(expected {expected_size_int}, actual {stat.st_size})"
            )

    expected_sha = str(artifact.get("sha256") or "").strip().lower()
    if expected_sha:
        actual_sha = sha256_file(p).lower()
        if actual_sha != expected_sha:
            raise ValueError(
                f"Model artifact SHA-256 mismatch: {p} "
                f"(expected {expected_sha}, actual {actual_sha})"
            )


# 모델 레지스트리 값을 일관된 형식으로 정리합니다.
def normalize_model_registry(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(registry or {})
    requested_key = str(raw.get("active_model_key") or "").strip()
    known_key = requested_key if requested_key in MODEL_REGISTRY_PRESETS else ""
    inferred_key = infer_model_key_from_path(raw.get("active_model_path"))
    active_model_key = known_key or inferred_key or DEFAULT_MODEL_KEY

    preset_path = get_model_preset_path(active_model_key)
    raw_active_model_path = str(raw.get("active_model_path") or "").strip()
    raw_preset_key = infer_model_key_from_preset_path(raw_active_model_path) if raw_active_model_path else None

    if known_key and preset_path is not None and (not raw_active_model_path or raw_preset_key is not None):
        active_model_path = preset_path
    elif raw_active_model_path:
        active_model_path = resolve_model_path(raw_active_model_path)
    else:
        active_model_path = preset_path or DEFAULT_MODEL_PATH

    training_profile = raw.get("training_profile")
    if active_model_key in MODEL_REGISTRY_PRESETS:
        training_profile = MODEL_REGISTRY_PRESETS[active_model_key]["training_profile"]
    elif not training_profile:
        training_profile = "baseline"

    raw["active_model_key"] = active_model_key
    raw["active_model_path"] = to_project_relative_path_str(active_model_path)
    raw["training_profile"] = str(training_profile)
    active_artifact = get_active_model_artifact(raw)
    if active_artifact:
        raw["active_artifact"] = active_artifact
    raw["missing_model_keys"] = [
        key
        for key, preset in MODEL_REGISTRY_PRESETS.items()
        if not Path(preset["path"]).exists()
    ]
    raw["available_models"] = {
        key: {
            "label": str(preset["label"]),
            "path": to_project_relative_path_str(Path(preset["path"])),
            "training_profile": str(preset["training_profile"]),
        }
        for key, preset in MODEL_REGISTRY_PRESETS.items()
    }
    return raw


# 모델 레지스트리 값을 불러오고 필요하면 생성합니다.
def load_model_registry(auto_create: bool = True) -> dict[str, Any]:
    file_exists = REGISTRY_PATH.exists()
    raw = read_json(REGISTRY_PATH, {})
    registry = normalize_model_registry(raw)
    if auto_create and ((not file_exists) or registry != raw):
        _write_json_atomic(REGISTRY_PATH, registry)
    return registry


# 모델 레지스트리 값을 저장합니다.
def write_model_registry(registry: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_model_registry(registry)
    _write_json_atomic(REGISTRY_PATH, normalized)
    return normalized
