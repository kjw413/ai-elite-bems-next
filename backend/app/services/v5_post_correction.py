# 이 파일은 v5 예측의 사후 보정(비근무일 배수, 근무일 롤링 편향 보정)을 담당합니다.
#
# DB·모델 파일 없이 동작하는 순수 함수만 둔다. usage_prediction_v5_service 가 예측값과
# 이력을 넘겨 호출하고, backend/tools/fit_v5_post_correction.py 가 같은 함수로 배수를
# 다시 추정·백테스트한다.
#
# 근거: prediction_performance_review.xlsx(2025-09-17~2026-10-16, v5.3 재적용) 분석.
#   · 학습은 근무일만 쓰고 특별일(저생산)을 뺀다(v5_retrain_worker._filter_workdays).
#     비근무일 예측은 근무일 수준을 외삽한 값이라 배수 보정이 필요하다.
#   · 기존 연휴 직후 배수(연료 0.85, 용수 1.30)는 45일 전부에서 오차를 키웠다
#     (MAPE 연료 15.8→8.7%, 용수 26.6→9.0% — 배수 제거 시). 보정 전 실측/예측비가
#     0.99~1.05라 보정할 필요가 없었으므로 규칙을 삭제했다.
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.services.v5_common import calendar_flags

# =============================================================================
# 비근무일 배수 보정
# =============================================================================
# 비근무일이라도 생산이 있으면(특근) 사용량 수준이 크게 다르다. 특별일 판정과 같은
# 1 ton 기준으로 생산 유무를 나눈다.
NONWORK_PRODUCTION_THRESHOLD_KG = 1000.0

# {공장: {대상: {"production": 생산有 배수, "no_production": 생산無 배수}}}
# 값 = 비근무일 실측 / 보정 전 예측의 중앙값(2025-09-17~2026-10-16, 공장·대상·생산유무별
# 32~98일). backend/tools/fit_v5_post_correction.py 로 재추정한다.
# 시간 분할 검증(2026-03까지로 추정 → 2026-04~ 평가) WAPE:
#   전력 38.3→12.1%, 용수 56.9→30.9%, 연료 48.5→38.2%.
NONWORK_MULTIPLIERS: dict[str, dict[str, dict[str, float]]] = {
    "광주": {
        "전력": {"production": 0.57, "no_production": 0.58},
        "연료": {"production": 0.65, "no_production": 0.56},
        "용수": {"production": 0.55, "no_production": 0.48},
    },
    "김해": {
        "전력": {"production": 0.81, "no_production": 0.53},
        "연료": {"production": 0.57, "no_production": 0.05},
        "용수": {"production": 0.35, "no_production": 0.04},
    },
    "남양주1": {
        "전력": {"production": 0.67, "no_production": 0.55},
        "연료": {"production": 0.52, "no_production": 0.43},
        "용수": {"production": 0.64, "no_production": 0.34},
    },
    "남양주2": {
        "전력": {"production": 0.94, "no_production": 0.66},
        "연료": {"production": 0.47, "no_production": 0.39},
        "용수": {"production": 0.58, "no_production": 0.29},
    },
    "논산": {
        "전력": {"production": 0.79, "no_production": 0.84},
        "연료": {"production": 0.60, "no_production": 0.96},
        "용수": {"production": 0.51, "no_production": 0.65},
    },
}

# 배수 표에 없는 공장(경산 등 신규)용 대상별 기본값 = 5개 공장 배수의 중앙값.
NONWORK_DEFAULT_MULTIPLIERS: dict[str, dict[str, float]] = {
    "전력": {"production": 0.79, "no_production": 0.58},
    "연료": {"production": 0.57, "no_production": 0.43},
    "용수": {"production": 0.55, "no_production": 0.34},
}


def resolve_nonwork_multiplier(
    factory: str,
    target_name: str,
    mix_prod_kg: float | None,
    multipliers: dict[str, dict[str, dict[str, float]]] | None = None,
) -> tuple[float | None, str]:
    """비근무일 배수와 생산 유무 키("production"/"no_production")를 반환한다.

    multipliers 를 주면 NONWORK_MULTIPLIERS 대신 사용한다(백테스트용).
    """
    try:
        prod = float(mix_prod_kg or 0.0)
    except (TypeError, ValueError):
        prod = 0.0
    if not np.isfinite(prod):
        prod = 0.0
    key = "production" if prod >= NONWORK_PRODUCTION_THRESHOLD_KG else "no_production"
    table = NONWORK_MULTIPLIERS if multipliers is None else multipliers
    spec = (table.get(factory) or {}).get(target_name)
    if spec is None:
        spec = NONWORK_DEFAULT_MULTIPLIERS.get(target_name)
    if not spec or key not in spec:
        return None, key
    return float(spec[key]), key


def apply_nonwork_correction(
    base_pred: float,
    factory: str,
    target_name: str,
    pred_date: date,
    holiday_set: set,
    mix_prod_kg: float | None,
    multipliers: dict[str, dict[str, dict[str, float]]] | None = None,
) -> tuple[float, dict[str, Any] | None]:
    """비근무일(주말·공휴일)이면 공장·대상·생산유무별 배수를 곱한다.

    Returns: (corrected_pred, info_dict_or_None)
    """
    flags = calendar_flags(pred_date, holiday_set)
    if not flags.get("is_holiday"):
        return base_pred, None

    factor, prod_key = resolve_nonwork_multiplier(factory, target_name, mix_prod_kg, multipliers)
    if factor is None or abs(factor - 1.0) < 1e-9:
        return base_pred, None

    corrected = float(base_pred) * factor
    return corrected, {
        "rule": "nonwork",
        "production_key": prod_key,
        "factor": factor,
        "adjustment": corrected - float(base_pred),
        "consecutive_nonwork_before": int(flags.get("consecutive_nonwork_before", 0)),
    }


def fit_nonwork_multipliers(rows: pd.DataFrame) -> dict[str, dict[str, dict[str, float]]]:
    """비근무일 행으로 공장·대상·생산유무별 배수(실측/보정 전 예측의 중앙값)를 추정한다.

    rows 컬럼: factory, target, raw_pred, actual, mix_prod_kg (비근무일 행만 넘긴다).
    실측·예측이 0 이하인 행은 비율이 정의되지 않아 제외한다.
    """
    need = {"factory", "target", "raw_pred", "actual", "mix_prod_kg"}
    missing = need - set(rows.columns)
    if missing:
        raise ValueError(f"fit_nonwork_multipliers: 컬럼 누락 {sorted(missing)}")

    d = rows.copy()
    d["raw_pred"] = pd.to_numeric(d["raw_pred"], errors="coerce")
    d["actual"] = pd.to_numeric(d["actual"], errors="coerce")
    d = d[(d["raw_pred"] > 0) & (d["actual"] > 0)]
    prod = pd.to_numeric(d["mix_prod_kg"], errors="coerce").fillna(0.0)
    d["prod_key"] = np.where(prod >= NONWORK_PRODUCTION_THRESHOLD_KG, "production", "no_production")

    out: dict[str, dict[str, dict[str, float]]] = {}
    for (factory, target, key), g in d.groupby(["factory", "target", "prod_key"]):
        ratio = float((g["actual"] / g["raw_pred"]).median())
        out.setdefault(str(factory), {}).setdefault(str(target), {})[str(key)] = round(ratio, 2)
    return out


# =============================================================================
# 근무일 롤링 편향 보정
# =============================================================================
# 광주처럼 수준 편향이 지속되는 시리즈(2026 근무일 평균 +13.7%)를 최근 실측으로 맞춘다.
# 직전 window 개 일반 근무일(특별일 제외)의 실측합 / 보정 전 예측합을 곱한다.
#
# 적용 조건(게이트) 비교 — 2026-Q1로 선택, 2026-04~ 평가, 15개 시리즈 평균 MAPE:
#   미보정 7.51/6.97%, 항상 적용 5.95/5.96%, |편향|≥3% 6.05/6.07%,
#   t≥2 6.11/6.03%, 단방향 70% 6.06/6.04%.
# 항상 적용이 두 구간 모두 가장 좋아 min_abs_bias 기본값은 0이다. 편향이 거의 없는
# 김해·남양주2 전력은 0.1~0.2%p 나빠지므로 factor 상·하한으로 급변만 막는다.
@dataclass(frozen=True)
class BiasCorrectionConfig:
    window: int = 20
    min_points: int = 10
    lookback_days: int = 120
    min_abs_bias: float = 0.0
    factor_min: float = 0.80
    factor_max: float = 1.25
    special_min_kg: float = 1000.0
    special_median_ratio: float = 0.1
    special_median_days: int = 30


DEFAULT_BIAS_CONFIG = BiasCorrectionConfig()


def _normal_history_mask(hist: pd.DataFrame, config: BiasCorrectionConfig) -> pd.Series:
    """detect_special_events 와 같은 기준(1 ton 미만, 30일 중앙값의 10% 미만)으로 특별일을 뺀다."""
    prod = pd.to_numeric(hist["mix_prod_kg"], errors="coerce").fillna(0.0)
    median = prod.rolling(config.special_median_days, min_periods=7).median()
    special = (prod < config.special_min_kg) | (prod < median.fillna(0.0) * config.special_median_ratio)
    return ~special


def compute_bias_factor(
    history: pd.DataFrame | None,
    pred_date: date,
    config: BiasCorrectionConfig = DEFAULT_BIAS_CONFIG,
) -> tuple[float, dict[str, Any] | None]:
    """예측일 이전 근무일 이력으로 편향 보정 배수를 계산한다.

    history 컬럼: pred_date, pred_ref(편향 보정 전 예측), actual, mix_prod_kg.
    history 는 근무일 행만 담아야 한다(비근무일은 별도 배수 보정 대상).
    Returns: (factor, info_or_None) — 표본이 부족하거나 보정이 필요 없으면 (1.0, None).
    """
    if history is None or history.empty:
        return 1.0, None

    h = history.copy()
    h["pred_date"] = pd.to_datetime(h["pred_date"], errors="coerce")
    h["pred_ref"] = pd.to_numeric(h["pred_ref"], errors="coerce")
    h["actual"] = pd.to_numeric(h["actual"], errors="coerce")
    if "mix_prod_kg" not in h.columns:
        h["mix_prod_kg"] = np.nan
    pred_ts = pd.Timestamp(pred_date).normalize()
    lo = pred_ts - pd.Timedelta(days=config.lookback_days)
    h = h[(h["pred_date"] < pred_ts) & (h["pred_date"] >= lo)]
    h = h.dropna(subset=["pred_date"]).sort_values("pred_date")
    h = h.drop_duplicates(subset=["pred_date"], keep="last").reset_index(drop=True)
    if h.empty:
        return 1.0, None

    h = h[_normal_history_mask(h, config).values]
    h = h[(h["pred_ref"] > 0) & (h["actual"] > 0)].tail(config.window)
    if len(h) < config.min_points:
        return 1.0, None

    ratio = float(h["actual"].sum() / h["pred_ref"].sum())
    if abs(ratio - 1.0) < config.min_abs_bias:
        return 1.0, None

    factor = float(min(max(ratio, config.factor_min), config.factor_max))
    if abs(factor - 1.0) < 1e-9:
        return 1.0, None
    return factor, {
        "rule": "rolling_bias",
        "factor": factor,
        "raw_ratio": ratio,
        "n_points": int(len(h)),
        "window_start": h["pred_date"].iloc[0].strftime("%Y-%m-%d"),
        "window_end": h["pred_date"].iloc[-1].strftime("%Y-%m-%d"),
    }
