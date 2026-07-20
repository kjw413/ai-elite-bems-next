# 이 파일은 v5 모델 예측과 예측 이력 처리를 담당합니다.
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import streamlit as st

from app.database.db_connection import get_connection, is_admin
from app.domain.factories import FACTORY_PHYSICAL_DISPLAY_ORDER
from app.services.production_actual_service import (
    get_actual_production_kg,
    overlay_actual_production,
)
from app.services.v5_common import (
    AGGREGATE_FACTORY_MEMBERS,
    BOTTLING_AGG_FEATURE,
    FACTORY_AGGREGATE_DISPLAY_ORDER,
    N_ACTIVE_SKUS_FEATURE,
    PATH_WIP_SUMMARY,
    STATUS_PATH,
    STATION_MAP,
    TARGET_SPECS,
    WEATHER_COLS,
    calendar_flags,
    classify_band,
    compute_bottling_aggregate,
    fill_weather_gaps,
    get_active_model_artifact,
    is_workday,
    load_holidays_excel,
    load_model_registry,
    load_n_active_skus_series,
    load_weather_station_excel,
    make_features,
    detect_special_events,
    get_safe_features,
    read_json,
    resolve_cqr_margins,
    resolve_model_path,
    resolve_plant_sheet,
    sanitize_feature_columns,
    spec_is_quantile,
    validate_model_artifact,
)
from app.services.v5_band_calibration import apply_band_calibration

# 날씨 동기화 서비스 (API 미설정 등으로 실패해도 예측 진행)
try:
    from app.services.weather_sync_service import sync_all_stations as _sync_all_weather
    _WEATHER_SYNC_AVAILABLE = True
except ImportError:
    _WEATHER_SYNC_AVAILABLE = False

_weather_synced_this_session: bool = False  # 세션당 1회만 자동 동기화


SUPPORTED_FEATURE_SPEC_VERSIONS = {
    # v5.1 (점추정)
    "1.1",
    "1.2-wip-shortlist",
    # v5.2 (분위수 회귀 + CQR)
    "1.1-quantile",
    "1.2-wip-shortlist-quantile",
    # v5.3 (v5.2 + 보틀링 EA aggregate + 활성 SKU 카운트 + 2026-Q1 VAL split)
    "1.3-wip-bottling-nactive-quantile",
}
WIP_AVAILABLE_START = pd.Timestamp("2023-01-01")
WIP_SOURCE_PATH = PATH_WIP_SUMMARY

# === 미래 구간 재귀 lag (recursive forecasting) ===
# 다중일-선행 배치 예측에서, 실측이 아직 없는 미래 날짜는 직전 실측에 lag 이 동결되어
# 온난화/냉각 램프를 못 따라가 체계적 편향(남양주2 2026 여름 전력 +14% 과소예측)을 만든다.
# 미래 날짜의 예측 P50 을 working history 에 되먹여 다음날 lag1/r7mean/intensity_lag1 이
# 추세를 따라가게 한다(실측이 있는 날은 현행대로 실측 lag 사용).
# 백테스트: frozen 6월 +3.8% → recursive −0.8%, MAPE 5.62→4.75(fresh 4.67 수준 복귀).
# 환경변수 V5_RECURSIVE_FUTURE_LAG=0 으로 비활성화(기존 동결 동작) 가능.
RECURSIVE_FUTURE_LAG = os.getenv("V5_RECURSIVE_FUTURE_LAG", "1") != "0"

# === 신설 라인 사후 보정(drift correction) — 가동 초기 램프 구간 한정 ===
# 신설 라인이 가동을 시작하면 그 부하가 최근 실측·학습 데이터에 아직 반영되지 않아
# 모델이 잠시 과소예측한다. analyze_223579_drift.py 의 OLS 로 단위부하 α 추정 후 가산.
#
# ★중요(이중계산 방지): v5 모델은 자기회귀(lag1/r7mean = 최근 실측 에너지)다. 신설 라인이
# ~1주 이상 정상가동하면 그 부하가 lag 에 자동 흡수되어 raw 예측이 이미 반영한다. 그 위에
# α×물량을 또 더하면 '이중계산' → 체계적 과대예측이 된다. (김해 연료/전력 2026 여름
# 과대예측의 실제 원인이 이 이중계산이었음: 반월 백테스트상 raw 는 6월 +0.2% 로 정확한데
# raw+drift 는 −16%. 가동 초기 03_하~04_상에서만 drift 가 raw 보다 우수.)
# → effective_start ~ effective_end(가동 램프) 구간에서만 가산하고 이후 자동 만료.
#   이는 예측일 기준 게이트라 재학습/모델교체와 무관하게 영구적으로 재발을 막는다.
# 형식: {(factory, target): (item_code, alpha_per_unit, effective_start, effective_end)}
DRIFT_CORRECTIONS: dict[tuple[str, str], tuple[str, float, pd.Timestamp, pd.Timestamp]] = {
    ("김해", "전력"): ("223579", 203.0, pd.Timestamp("2026-03-25"), pd.Timestamp("2026-04-15")),
    ("김해", "연료"): ("223579", 38.51, pd.Timestamp("2026-03-25"), pd.Timestamp("2026-04-15")),
    # 용수는 효과 없음(Δ=0)으로 제외
}

# === 휴일 인접일 사후 보정 ===
# 캘린더 피처를 포함해 재학습하기 전까지 적용하는 임시 가드.
# 관측 근거(남양주2 2026-04~05 prediction_log):
#   · 휴일 당일 저생산 (예: 5/1 근로자의 날, 생산 48k vs 평일 200k+)
#       → 모델이 보일러/CIP 베이스 로드를 평상시처럼 잡아 연료 +216%, 용수 +93% 과예측
#   · 장기연휴 직후 첫 가동일 (예: 5/4 월)
#       → CIP·재가동으로 실제 용수가 평소 대비 2~3배 → 과소예측 (음의 오차)
#       → 동일 라인 연료는 product mix 부족 → 과예측
# 형식: {target: {flag: multiplier}} — flag 우선순위 is_holiday > is_post_long_weekend.
# 전력은 안정적이라 보정 대상 외.
HOLIDAY_ADJ_CORRECTIONS: dict[str, dict[str, float]] = {
    "연료": {
        "is_holiday":           0.55,  # 휴일 당일은 베이스 로드만 → 약 반토막
        "is_post_long_weekend": 0.85,  # 연휴 직후 첫 가동일은 일관되게 과예측
    },
    "용수": {
        "is_holiday":           0.60,
        "is_post_long_weekend": 1.30,  # CIP·재가동 복귀로 실제 사용량 ↑
    },
    # "전력": 보정 없음 (예측 안정)
}


# 휴일 인접일 사후 보정 적용 (드리프트 보정과 별도, 캘린더 피처 재학습 전 임시 가드).
def _apply_holiday_adjacent_correction(
    base_pred: float,
    target_name: str,
    pred_date: date,
    holiday_set: set,
) -> tuple[float, dict[str, Any] | None]:
    """
    Returns: (corrected_pred, info_dict_or_None)

    우선순위: is_holiday > is_post_long_weekend. 둘 다 해당이 안 되면 무보정.
    """
    spec = HOLIDAY_ADJ_CORRECTIONS.get(target_name)
    if not spec:
        return base_pred, None

    flags = calendar_flags(pred_date, holiday_set)

    factor: float | None = None
    rule: str | None = None
    if flags.get("is_holiday") and "is_holiday" in spec:
        factor = float(spec["is_holiday"])
        rule = "is_holiday"
    elif flags.get("is_post_long_weekend") and "is_post_long_weekend" in spec:
        factor = float(spec["is_post_long_weekend"])
        rule = "is_post_long_weekend"

    if factor is None or abs(factor - 1.0) < 1e-9:
        return base_pred, None

    corrected = float(base_pred) * factor
    return corrected, {
        "rule": rule,
        "factor": factor,
        "adjustment": corrected - float(base_pred),
        "consecutive_nonwork_before": int(flags.get("consecutive_nonwork_before", 0)),
    }


# 활성 모델의 사후 보정 정책(post_processing)을 읽습니다.
#
# 전체 재학습(full_quantile) 모델은 캘린더 피처를 최신 데이터로 직접 학습하므로
# 휴일 인접일 임시 가드(HOLIDAY_ADJ_CORRECTIONS)를 비활성화한다. 반면 223579 드리프트
# 보정은 아직 학습 피처가 아니므로 유지한다. 이 메타데이터는 artifact(우선) 또는 레지스트리
# 최상위에 기록된다. 메타데이터가 없는 과거 모델은 두 보정 모두 적용(하위 호환).
def _active_post_processing() -> dict[str, Any]:
    try:
        registry = load_model_registry(auto_create=True)
    except Exception:
        return {}
    artifact = get_active_model_artifact(registry)
    pp = artifact.get("post_processing") if isinstance(artifact, dict) else None
    if not pp:
        pp = registry.get("post_processing")
    return pp if isinstance(pp, dict) else {}


# 휴일 인접일 사후 보정 활성 여부(기본 True — 과거 모델 하위 호환).
def _holiday_correction_enabled() -> bool:
    return bool(_active_post_processing().get("holiday_adjacent_correction", True))


# 드리프트(223579) 사후 보정 활성 여부(기본 True — 아직 학습 피처 아님).
def _drift_correction_enabled() -> bool:
    return bool(_active_post_processing().get("drift_correction", True))


# 신설 라인 단위 부하를 가산하는 사후 보정을 적용합니다.
def _apply_drift_correction(
    base_pred: float,
    factory: str,
    target_name: str,
    pred_date: date,
    wip_inputs: dict[str, float] | None,
) -> tuple[float, dict[str, Any] | None]:
    """
    Returns: (corrected_pred, info_dict_or_None)
    info_dict 는 보정 적용 시 디버그/UI용 메타데이터.
    신설 라인 코드는 학습 shortlist에 없을 수 있으므로 wip_inputs에 없으면
    WIP 시트에서 직접 조회합니다.
    """
    spec = DRIFT_CORRECTIONS.get((factory, target_name))
    if spec is None:
        return base_pred, None

    item_code, alpha, effective_start, effective_end = spec
    pred_ts = pd.Timestamp(pred_date)
    # 가동 초기 램프 구간에서만 가산. 이후엔 자기회귀 lag 가 신설 부하를 흡수하므로
    # 보정을 만료시켜 이중계산(과대예측)을 방지한다(상단 DRIFT_CORRECTIONS 주석 참고).
    if pred_ts < effective_start or pred_ts > effective_end:
        return base_pred, None

    feature_col = _build_wip_feature_col(item_code)
    qty = 0.0

    # 1) 이미 조회된 wip_inputs에서 우선 조회
    if wip_inputs:
        qty = float(wip_inputs.get(feature_col, wip_inputs.get(item_code, 0.0)) or 0.0)

    # 2) 못 찾으면 WIP 시트에서 직접 조회 (신설 라인은 shortlist에 없을 수 있음)
    if qty <= 0:
        try:
            df_wip = _load_wip_history(factory, pred_date, pred_date, [item_code])
            if not df_wip.empty:
                row = df_wip.iloc[0]
                qty = float(row.get(feature_col, 0.0) or 0.0)
        except Exception:
            qty = 0.0

    if qty <= 0:
        return base_pred, None

    adjustment = alpha * qty
    return base_pred + adjustment, {
        "item_code": item_code,
        "qty": qty,
        "alpha_per_unit": alpha,
        "adjustment": adjustment,
    }


# 집계 공장 여부를 확인합니다.
def _is_aggregate_factory(factory: str | None) -> bool:
    return str(factory or "") in AGGREGATE_FACTORY_MEMBERS


# 조회/예측 대상 공장 목록을 확정합니다.
def _resolve_factory_members(factory: str) -> list[str]:
    if factory in AGGREGATE_FACTORY_MEMBERS:
        return list(AGGREGATE_FACTORY_MEMBERS[factory])
    if factory in STATION_MAP:
        return [factory]
    raise ValueError(f"Unknown factory: {factory}")


# 집계 팩토리의 상세 예측 편집 지원 여부를 확인합니다.
def is_manual_editor_supported(factory: str) -> bool:
    return not _is_aggregate_factory(factory)


# 휴일를 캐시에 저장해 재사용합니다.
@st.cache_data(ttl=3600)
def _cached_holidays() -> set:
    return load_holidays_excel()


# 날씨 관측소를 캐시에 저장해 재사용합니다.
@st.cache_data(ttl=3600)
def _cached_weather_station(station_name: str) -> pd.DataFrame:
    return load_weather_station_excel(station_name)


# 재공품 시트를 캐시에 저장해 재사용합니다.
@st.cache_data(ttl=3600)
def _cached_wip_sheet(source_path: str, factory: str) -> pd.DataFrame:
    path = Path(source_path)
    if not path.exists():
        return pd.DataFrame(columns=["날짜"])

    # 한글 공장명 시트가 없으면 LEGACY F-코드 시트로 폴백.
    sheet_name = resolve_plant_sheet(path, factory)
    if sheet_name is None:
        return pd.DataFrame(columns=["날짜"])

    try:
        df = pd.read_excel(path, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame(columns=["날짜"])

    if df.empty or "날짜" not in df.columns:
        return pd.DataFrame(columns=["날짜"])

    rename_map: dict[object, str] = {}
    for col in df.columns:
        if col == "날짜":
            continue
        rename_map[col] = _normalize_item_code(col)

    out = df.rename(columns=rename_map).copy()
    out["날짜"] = pd.to_datetime(out["날짜"], errors="coerce")
    out = out.dropna(subset=["날짜"]).sort_values("날짜").reset_index(drop=True)
    return out


# 품목 코드 값을 일정한 형식으로 맞춥니다.
def _normalize_item_code(value: object) -> str:
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


# 재공품 피처 컬럼 데이터를 만듭니다.
def _build_wip_feature_col(item_code: str) -> str:
    return f"wip_{_normalize_item_code(item_code)}"


# derive 품목 코드 from 피처 컬럼 관련 처리를 담당합니다.
def _derive_item_code_from_feature_col(feature_col: str) -> str:
    text = str(feature_col).strip()
    # 보틀링 EA 합계/활성 SKU 카운트 같은 derived aggregate 는 raw item_code 가 아니므로 빈 문자열 반환
    if text in (BOTTLING_AGG_FEATURE, N_ACTIVE_SKUS_FEATURE):
        return ""
    if text.startswith("wip_"):
        return _normalize_item_code(text[4:])
    return _normalize_item_code(text)


# 스펙에서 보틀링 EA 합계 피처 산정용 raw 코드 목록을 반환합니다.
def _get_spec_bottling_ea_codes(spec: dict[str, Any]) -> list[str]:
    codes = spec.get("wip_bottling_ea_codes") or []
    return [_normalize_item_code(c) for c in codes if _normalize_item_code(c)]


# 모델 dict 의 (factory, targets) 중 하나라도 wip_n_active_skus 를 쓰는지 확인.
def _model_needs_n_active_skus(
    model_dict: dict[str, Any] | None,
    factory: str,
    targets: list[str] | None,
) -> bool:
    if not model_dict or factory not in model_dict:
        return False
    target_list = targets or list(model_dict[factory].keys())
    for tgt in target_list:
        spec = model_dict[factory].get(tgt)
        if not isinstance(spec, dict):
            continue
        if N_ACTIVE_SKUS_FEATURE in (spec.get("wip_feature_cols") or []):
            return True
    return False


# 지원 대상 피처 스펙 버전 여부를 확인합니다.
def _is_supported_feature_spec_version(version: object) -> bool:
    return str(version or "") in SUPPORTED_FEATURE_SPEC_VERSIONS


# 스펙 재공품 피처 컬럼 값을 가져옵니다.
def _get_spec_wip_feature_cols(spec: dict[str, Any]) -> list[str]:
    direct = [str(col) for col in (spec.get("wip_feature_cols") or []) if str(col).strip()]
    if direct:
        return direct

    features_map: dict[str, list[str]] = spec.get("features") or {}
    derived: list[str] = []
    for feat_list in features_map.values():
        for col in feat_list or []:
            col_str = str(col)
            if col_str.startswith("wip_") and col_str not in derived:
                derived.append(col_str)
    return derived


# 스펙 재공품 품목 코드 값을 가져옵니다.
def _get_spec_wip_item_codes(spec: dict[str, Any]) -> list[str]:
    direct = [_normalize_item_code(code) for code in (spec.get("wip_item_codes") or [])]
    direct = [code for code in direct if code]
    if direct:
        return direct
    derived = [_derive_item_code_from_feature_col(col) for col in _get_spec_wip_feature_cols(spec)]
    return [c for c in derived if c]


# 스펙 uses 재공품 관련 처리를 담당합니다.
def _spec_uses_wip(spec: dict[str, Any]) -> bool:
    return bool(_get_spec_wip_feature_cols(spec) or _get_spec_wip_item_codes(spec))


# collect required 재공품 품목 코드 관련 처리를 담당합니다.
def _collect_required_wip_item_codes(
    model_dict: dict[str, Any],
    factory: str,
    targets: list[str] | None,
) -> list[str]:
    if factory not in model_dict:
        return []

    target_list = targets or list(model_dict[factory].keys())
    codes: list[str] = []
    for target in target_list:
        spec = model_dict[factory].get(target)
        if not isinstance(spec, dict):
            continue
        for code in _get_spec_wip_item_codes(spec):
            if code and code not in codes:
                codes.append(code)
    return codes


# collect required 재공품 피처 컬럼 관련 처리를 담당합니다.
def _collect_required_wip_feature_cols(
    model_dict: dict[str, Any],
    factory: str,
    targets: list[str] | None,
) -> list[str]:
    return [_build_wip_feature_col(code) for code in _collect_required_wip_item_codes(model_dict, factory, targets)]


# 재공품 이력 데이터를 불러옵니다.
def _load_wip_history(
    factory: str,
    date_from: date | pd.Timestamp,
    date_to: date | pd.Timestamp,
    item_codes: list[str],
) -> pd.DataFrame:
    if not item_codes:
        return pd.DataFrame(columns=["날짜"])

    if not WIP_SOURCE_PATH.exists():
        raise FileNotFoundError(f"WIP source file not found: {WIP_SOURCE_PATH}")

    source_df = _cached_wip_sheet(str(WIP_SOURCE_PATH), factory)
    feature_cols = [_build_wip_feature_col(code) for code in item_codes]
    if source_df.empty:
        return pd.DataFrame(columns=["날짜", *feature_cols])

    start_ts = pd.Timestamp(date_from)
    end_ts = pd.Timestamp(date_to)
    sliced = source_df[(source_df["날짜"] >= start_ts) & (source_df["날짜"] <= end_ts)].copy()

    for code in item_codes:
        if code not in sliced.columns:
            sliced[code] = 0.0
        sliced[code] = pd.to_numeric(sliced[code], errors="coerce").fillna(0.0)

    out = sliced[["날짜", *item_codes]].copy()
    return out.rename(columns={code: _build_wip_feature_col(code) for code in item_codes})


# 재공품 입력값 for 날짜를 확정합니다.
def _resolve_wip_inputs_for_date(
    factory: str,
    pred_date: date,
    item_codes: list[str],
    row_override: pd.Series | None = None,
) -> dict[str, float]:
    if not item_codes:
        return {}

    resolved = {col: 0.0 for col in [_build_wip_feature_col(code) for code in item_codes]}

    if pd.Timestamp(pred_date) >= WIP_AVAILABLE_START:
        prefill_df = _load_wip_history(factory, pred_date, pred_date, item_codes)
        if not prefill_df.empty:
            prefill_row = prefill_df.iloc[0]
            for code in item_codes:
                feature_col = _build_wip_feature_col(code)
                try:
                    resolved[feature_col] = float(prefill_row.get(feature_col, 0.0) or 0.0)
                except Exception:
                    resolved[feature_col] = 0.0

    if row_override is not None:
        for code in item_codes:
            feature_col = _build_wip_feature_col(code)
            raw_value = None
            for key in [feature_col, code]:
                if key in row_override.index:
                    raw_value = row_override.get(key)
                    break
            if raw_value is None or pd.isna(raw_value):
                continue
            try:
                resolved[feature_col] = float(raw_value)
            except Exception:
                resolved[feature_col] = 0.0

    return resolved


# 스펙 재공품 피처을 추가합니다.
def _append_spec_wip_features(X_all: pd.DataFrame, d_full: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    wip_feature_cols = _get_spec_wip_feature_cols(spec)
    if not wip_feature_cols:
        return X_all

    # 보틀링 EA 합계 derived 컬럼은 raw EA 컬럼 합산으로 채운다 (학습 시 동일 로직).
    if BOTTLING_AGG_FEATURE in wip_feature_cols:
        ea_codes = _get_spec_bottling_ea_codes(spec)
        d_full[BOTTLING_AGG_FEATURE] = compute_bottling_aggregate(d_full, ea_codes)

    # n_active_skus 는 d_full 에 미리 채워져 있어야 함 (_prepare_inference_frame 에서 merge).
    # 누락 시 0 으로 안전 처리.
    if N_ACTIVE_SKUS_FEATURE in wip_feature_cols and N_ACTIVE_SKUS_FEATURE not in d_full.columns:
        d_full[N_ACTIVE_SKUS_FEATURE] = 0.0

    wip_frame = pd.DataFrame(index=d_full.index)
    for feature_col in wip_feature_cols:
        if feature_col not in d_full.columns:
            d_full[feature_col] = 0.0
        wip_frame[feature_col] = pd.to_numeric(d_full[feature_col], errors="coerce").fillna(0.0)

    wip_frame.columns = sanitize_feature_columns(list(wip_frame.columns))
    return pd.concat([X_all, wip_frame], axis=1)


# required 재공품 컬럼 for 대상 값을 가져옵니다.
def _get_required_wip_columns_for_targets(
    factory: str,
    targets: list[str] | None = None,
) -> list[str]:
    try:
        model_dict = get_active_model()
    except Exception:
        return []
    return _collect_required_wip_feature_cols(model_dict, factory, targets)


# 모델 등록 정보 값을 가져옵니다.
def get_model_registry() -> dict[str, Any]:
    return load_model_registry(auto_create=True)


# 모델 캐시 데이터를 불러옵니다.
@st.cache_resource(max_entries=1)
def _load_model_cached(
    abs_model_path: str,
    mtime_ns: int,
    size_bytes: int,
    expected_sha256: str,
    expected_size_bytes: int | None,
) -> dict:
    artifact = {
        "sha256": expected_sha256,
        "size_bytes": expected_size_bytes,
    }
    validate_model_artifact(Path(abs_model_path), artifact)
    return joblib.load(abs_model_path)


# 현재 모델 경로 값을 가져옵니다.
def get_active_model_path() -> Path:
    registry = get_model_registry()
    return resolve_model_path(registry.get("active_model_path"))


# 현재 모델 값을 가져옵니다.
def get_active_model() -> dict:
    registry = get_model_registry()
    model_path = resolve_model_path(registry.get("active_model_path"))
    if not model_path.exists():
        raise FileNotFoundError(f"Active model file not found: {model_path}")
    stat = model_path.stat()
    artifact = get_active_model_artifact(registry)
    expected_sha = str(artifact.get("sha256") or "")
    expected_size_raw = artifact.get("size_bytes")
    try:
        expected_size = int(expected_size_raw) if expected_size_raw not in (None, "") else None
    except (TypeError, ValueError):
        expected_size = None
    return _load_model_cached(
        str(model_path),
        stat.st_mtime_ns,
        stat.st_size,
        expected_sha,
        expected_size,
    )


# 학습 상태 값을 가져옵니다.
#
# 상태 파일이 "running" 으로 남아 있어도 워커가 실제로 죽었으면(전원 차단/크래시)
# 'interrupted' 로 파생해 반환한다. 디스크 파일은 건드리지 않으며(읽기 경로),
# 다음 재학습 요청 시 새 상태로 덮어쓰여 자동 복구된다. 이렇게 해야 UI 가
# "진행 중" 으로 영구히 멈춰 재학습 버튼이 잠기는 데드락을 방지할 수 있다.
def get_training_status() -> dict[str, Any]:
    status = read_json(
        STATUS_PATH,
        {
            "status": "unknown",
            "started_at": None,
            "ended_at": None,
            "mode": None,
            "trigger_mode": None,
            "phase": None,
            "message": None,
            "error": None,
            "new_model_path": None,
            "active_model_key": None,
            "progress_current": 0,
            "progress_total": 0,
            "progress_pct": 0.0,
            "current_step": None,
            "current_factory": None,
            "current_target": None,
        },
    )

    if isinstance(status, dict) and status.get("status") == "running":
        try:
            from app.services.v5_retrain_lock import training_run_is_stale
            dead = training_run_is_stale()
        except Exception:
            dead = False
        if dead:
            status = {
                **status,
                "status": "interrupted",
                "stale": True,
                "raw_status": "running",
                "message": "이전 학습이 중단되었습니다 (전원 차단/프로세스 종료). 다시 요청하세요.",
            }

    return status


# 날씨 기본 입력값 값을 가져옵니다.
def get_weather_prefill(factory: str, target_date: date) -> dict[str, float | None]:
    station = STATION_MAP.get(factory)
    if not station:
        return {c: None for c in WEATHER_COLS}

    df_w = _cached_weather_station(station)
    if df_w.empty:
        return {c: None for c in WEATHER_COLS}

    ts = pd.Timestamp(target_date)
    row = df_w[df_w["날짜"] == ts]
    if row.empty:
        return {c: None for c in WEATHER_COLS}

    r0 = row.iloc[0]
    out: dict[str, float | None] = {}
    for c in WEATHER_COLS:
        val = r0.get(c)
        out[c] = float(val) if val is not None and not pd.isna(val) else None
    return out


# 믹스 생산량 기본 입력값 값을 가져옵니다.
def get_mix_prod_prefill(factory: str, target_date: date) -> float | None:
    """DB_생산실적의 당일 실제 생산량을 예측 입력 기본값으로 반환."""
    return get_actual_production_kg(factory, target_date)


# 에너지 이력 데이터를 조회합니다.
# [BEMS Next 수정 №1] overlay_actual_production은 date·factory 컬럼을 필수로
# 요구하는데 기존 쿼리는 factory 없이 호출해 ValueError로 죽었다. factory를
# SELECT에 포함해 overlay 후 제거한다 (반환 스키마는 기존과 동일).
def _fetch_energy_history(factory: str, date_from: date, date_to: date) -> pd.DataFrame:
    cols = ["date", "factory", "mix_prod_kg"] + [spec["db_col"] for spec in TARGET_SPECS.values()]
    col_sql = ", ".join(cols)
    query = f"""
        SELECT {col_sql}
        FROM energy_daily
        WHERE factory = %s AND date >= %s AND date <= %s
        ORDER BY date
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            query,
            conn,
            params=(factory, date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d")),
        )
        return overlay_actual_production(df).drop(columns=["factory"])
    finally:
        conn.close()


# to korean 스키마 관련 처리를 담당합니다.
def _to_korean_schema(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "date": "날짜",
        "mix_prod_kg": "믹스생산량[kg]",
    }
    for tgt_name, spec in TARGET_SPECS.items():
        rename_map[spec["db_col"]] = spec["model_col"]
    out = df.rename(columns=rename_map).copy()
    if "날짜" in out.columns:
        out["날짜"] = pd.to_datetime(out["날짜"], errors="coerce")
    for c in ["믹스생산량[kg]"] + [spec["model_col"] for spec in TARGET_SPECS.values()]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
        else:
            out[c] = 0.0
    return out


# 피처 원본 데이터프레임 데이터를 만듭니다.
def _build_feature_source_frame(
    factory: str,
    pred_date: date,
    mix_prod_kg: float,
    weather: dict[str, float],
    model_dict: dict[str, Any] | None = None,
    targets: list[str] | None = None,
    wip_override: dict[str, Any] | None = None,
    lookback_days: int = 450,
) -> tuple[pd.DataFrame, dict[str, float]]:
    holiday_set = _cached_holidays()

    hist_from = pred_date - timedelta(days=lookback_days)
    hist_to = pred_date - timedelta(days=1)
    df_raw = _fetch_energy_history(factory, hist_from, hist_to)
    df = _to_korean_schema(df_raw)

    # Workday history only (as training), but keep prediction date even if non-workday
    if not df.empty and "날짜" in df.columns:
        df = df[df["날짜"].apply(lambda x: is_workday(pd.Timestamp(x), holiday_set))]

    # Merge weather (예측행은 아직 합치지 않았으므로 보간은 pred_row 합친 뒤에 수행).
    station = STATION_MAP.get(factory)
    df_w = _cached_weather_station(station) if station else pd.DataFrame(columns=["날짜"] + WEATHER_COLS)
    if df_w.empty:
        for c in WEATHER_COLS:
            df[c] = np.nan
    else:
        df = df.merge(df_w, on="날짜", how="left")
        for c in WEATHER_COLS:
            if c not in df.columns:
                df[c] = np.nan

    if model_dict is None:
        try:
            model_dict = get_active_model()
        except Exception:
            model_dict = {}

    required_wip_item_codes = _collect_required_wip_item_codes(model_dict, factory, targets)
    override_series = pd.Series(wip_override) if wip_override else None
    wip_inputs = _resolve_wip_inputs_for_date(
        factory=factory,
        pred_date=pred_date,
        item_codes=required_wip_item_codes,
        row_override=override_series,
    )

    if required_wip_item_codes:
        if pd.Timestamp(hist_to) >= WIP_AVAILABLE_START:
            df_wip = _load_wip_history(factory, hist_from, hist_to, required_wip_item_codes)
            df = df.merge(df_wip, on="날짜", how="left")
        for feature_col in [_build_wip_feature_col(code) for code in required_wip_item_codes]:
            if feature_col not in df.columns:
                df[feature_col] = 0.0
            df[feature_col] = pd.to_numeric(df[feature_col], errors="coerce").fillna(0.0)

    # 활성 SKU 카운트 (학습 시 모든 wip 코드 기반) — 모델이 사용하는 경우만 머지.
    n_active_skus_for_pred_date = 0.0
    if _model_needs_n_active_skus(model_dict, factory, targets):
        n_df = load_n_active_skus_series(PATH_WIP_SUMMARY, factory)
        if not n_df.empty:
            df = df.merge(n_df, on="날짜", how="left")
            df[N_ACTIVE_SKUS_FEATURE] = pd.to_numeric(
                df[N_ACTIVE_SKUS_FEATURE], errors="coerce"
            ).fillna(0.0)
            # 예측행이 WIP 시트에 있는 날짜라면 그 값 사용 (이미 머지됨), 아니면 직전 영업일 값 보간
            pd_ts = pd.Timestamp(pred_date)
            match = n_df.loc[n_df["날짜"] == pd_ts, N_ACTIVE_SKUS_FEATURE]
            if not match.empty:
                n_active_skus_for_pred_date = float(match.iloc[0])
            else:
                before = n_df.loc[n_df["날짜"] < pd_ts, N_ACTIVE_SKUS_FEATURE]
                if not before.empty:
                    n_active_skus_for_pred_date = float(before.iloc[-1])
        else:
            df[N_ACTIVE_SKUS_FEATURE] = 0.0

    # Append prediction row (NaN인 날씨 값을 보존하여 fill_weather_gaps가 보간하도록 함)
    pred_row: dict[str, Any] = {"날짜": pd.Timestamp(pred_date), "믹스생산량[kg]": float(mix_prod_kg)}
    for c in WEATHER_COLS:
        v = weather.get(c)
        try:
            pred_row[c] = float(v) if v is not None and not pd.isna(v) else np.nan
        except (TypeError, ValueError):
            pred_row[c] = np.nan
    for spec in TARGET_SPECS.values():
        pred_row[spec["model_col"]] = 0.0
    for feature_col, value in wip_inputs.items():
        pred_row[feature_col] = float(value)
    if _model_needs_n_active_skus(model_dict, factory, targets):
        pred_row[N_ACTIVE_SKUS_FEATURE] = n_active_skus_for_pred_date

    df = pd.concat([df, pd.DataFrame([pred_row])], ignore_index=True)
    df = df.sort_values("날짜").reset_index(drop=True)
    # 예측행 합친 뒤에 시계열 보간을 적용 → 예측행의 결측 날씨도 가까운 영업일 값으로 채워짐.
    df = fill_weather_gaps(df, WEATHER_COLS)
    return df, wip_inputs


# 단일 분위수에 대한 12모델 앙상블 예측을 가중합 (v5.2 helper).
# X_row_all: 분위수와 무관한 공통 피처 행. modeling_v5.3.py와 동일하게
# m_types 순서 = ["M1","M2","M3","M4"], 각 m별로 LGBM/XGB/CB 3개씩.
def _predict_quantile_ensemble(
    models_for_q: list[Any],
    weights_for_q: np.ndarray,
    m_types: list[str],
    features_map: dict[str, list[str]],
    X_row_all: pd.DataFrame,
) -> tuple[float, list[dict[str, Any]]]:
    algo_names = ["LGBM", "XGB", "CB"]
    per_model: list[dict[str, Any]] = []
    weighted_preds: list[float] = []

    if weights_for_q.shape[0] != len(models_for_q):
        weights_for_q = np.ones(len(models_for_q), dtype=float) / float(len(models_for_q))

    for i, (mdl, w) in enumerate(zip(models_for_q, weights_for_q)):
        mt = m_types[i // 3] if m_types else "M1"
        feat_list = features_map.get(mt) or list(X_row_all.columns)

        missing = [f for f in feat_list if f not in X_row_all.columns]
        if missing:
            raise RuntimeError(f"모델 예측에 필요한 필수 피처가 누락되었습니다: {missing}")

        X_row = X_row_all[feat_list]

        try:
            pred = float(np.expm1(mdl.predict(X_row))[0])
        except Exception as e:
            raise RuntimeError(f"Model prediction failed (idx={i}, mt={mt}): {e}") from e

        weighted_preds.append(float(w) * pred)
        per_model.append({
            "idx": i,
            "mt": mt,
            "algo": algo_names[i % 3],
            "weight": float(w),
            "pred": pred,
        })

    return float(np.sum(weighted_preds)), per_model


# 실측값 DB 조회 (없으면 None).
def _fetch_actual_value(factory: str, target_name: str, pred_date: date) -> float | None:
    try:
        db_col = TARGET_SPECS[target_name]["db_col"]
        query = f"SELECT {db_col} FROM energy_daily WHERE factory=%s AND date=%s LIMIT 1"
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(query, (factory, pred_date.strftime("%Y-%m-%d")))
            r = cur.fetchone()
            if r and r[0] is not None:
                return float(r[0])
        finally:
            try:
                cur.close()
            except Exception:
                pass
            conn.close()
    except Exception:
        return None
    return None


# infer 대상 관련 처리를 담당합니다.
# v5.1(점추정) / v5.2(분위수 + CQR) 모두를 지원합니다.
#   v5.1 결과: pred(=P50과 동일), pred_p05=None, pred_p95=None
#   v5.2 결과: pred(=P50), pred_p05, pred_p95, band_status, band_position
def _infer_target(
    model_dict: dict,
    factory: str,
    target_name: str,
    pred_date: date,
    df_source: pd.DataFrame,
) -> dict[str, Any]:
    if factory not in model_dict:
        raise ValueError(f"Factory '{factory}' not found in model.")
    if target_name not in model_dict[factory]:
        raise ValueError(f"Target '{target_name}' not found for factory '{factory}'.")

    spec = model_dict[factory][target_name]
    feature_spec_version = spec.get("feature_spec_version")
    if not _is_supported_feature_spec_version(feature_spec_version):
        raise RuntimeError(
            f"지원되지 않는 모델 스펙입니다. 재생성/재학습이 필요합니다. "
            f"({factory} - {target_name}, spec={feature_spec_version})"
        )

    if _spec_uses_wip(spec) and pd.Timestamp(pred_date) < WIP_AVAILABLE_START:
        raise ValueError("재공품 피처 모델은 2023-01-01 이후 날짜만 예측할 수 있습니다.")

    ycol = spec.get("target_col") or TARGET_SPECS[target_name]["model_col"]

    holiday_set = _cached_holidays()
    d_full = detect_special_events(make_features(df_source, ycol, holiday_set))

    ts = pd.Timestamp(pred_date)
    row_mask = d_full["date"] == ts
    if not row_mask.any():
        raise ValueError(
            "피처 생성에 실패했습니다. 과거 영업일 데이터가 최소 7일 이상 필요합니다."
        )

    # X construction (match training)
    X_all = get_safe_features(d_full)
    X_all = _append_spec_wip_features(X_all, d_full, spec)

    X_row_all = X_all[row_mask]
    if X_row_all.empty:
        raise ValueError("예측 대상 행을 찾지 못했습니다.")

    m_types = list(spec.get("m_types") or ["M1", "M2", "M3", "M4"])
    features_map: dict[str, list[str]] = spec.get("features") or {}

    is_special_event = bool(d_full.loc[row_mask, "is_special_event"].iloc[0])
    actual_val = _fetch_actual_value(factory, target_name, pred_date)
    pred_ts = pd.Timestamp(pred_date)
    workday = is_workday(pred_ts, holiday_set)

    # =========================================================================
    # v5.2 (분위수 회귀 + CQR) 분기
    # =========================================================================
    if spec_is_quantile(spec):
        quantiles = list(spec.get("quantiles") or [0.05, 0.50, 0.95])
        if len(quantiles) < 3:
            raise RuntimeError(f"v5.2 모델 스펙에 quantiles가 3개 미만입니다: {quantiles}")
        q_lo, q_mid, q_hi = sorted(quantiles)[0], sorted(quantiles)[len(quantiles)//2], sorted(quantiles)[-1]

        models_by_q: dict[float, list[Any]] = spec.get("models_by_q") or {}
        weights_by_q: dict[float, Any] = spec.get("weights_by_q") or {}

        def _w(q: float) -> np.ndarray:
            raw = weights_by_q.get(q)
            return np.array(raw, dtype=float) if raw is not None else np.array([], dtype=float)

        p05, pm_05 = _predict_quantile_ensemble(
            models_by_q.get(q_lo) or [], _w(q_lo), m_types, features_map, X_row_all)
        p50, pm_50 = _predict_quantile_ensemble(
            models_by_q.get(q_mid) or [], _w(q_mid), m_types, features_map, X_row_all)
        p95, pm_95 = _predict_quantile_ensemble(
            models_by_q.get(q_hi) or [], _w(q_hi), m_types, features_map, X_row_all)

        # CQR 보정 — 비대칭(cqr_q_hat_lower/upper) 우선, 없으면 대칭 cqr_q_hat fallback (§10.4).
        q_hat_lower, q_hat_upper = resolve_cqr_margins(spec)
        p05 -= q_hat_lower
        p95 += q_hat_upper

        # 단조성 강제 (앙상블 후에도 P05<=P50<=P95)
        p05, p50, p95 = sorted([p05, p50, p95])

        # 공장-대상별 calibration layer (§10.5) — 최근 운영 이력 기반 상·하한 사후 보정.
        p05, p95, calib_info = apply_band_calibration(factory, target_name, p05, p50, p95)
        if calib_info is not None:
            p05, p50, p95 = sorted([p05, p50, p95])

        band_status, band_position = classify_band(actual_val, p05, p50, p95)

        return {
            "target": target_name,
            "unit": TARGET_SPECS[target_name]["unit"],
            "pred": p50,                # 호환: 기존 'pred' 키 = P50 채움
            "pred_p05": p05,
            "pred_p50": p50,
            "pred_p95": p95,
            "cqr_q_hat": q_hat_upper,   # 호환: 단일 값 소비처엔 상단 보정폭 노출
            "cqr_q_hat_lower": q_hat_lower,
            "cqr_q_hat_upper": q_hat_upper,
            "band_calibration": calib_info,
            "actual": actual_val,
            "band_status": band_status,
            "band_position": band_position,
            "is_workday": workday,
            "is_special_event": is_special_event,
            "per_model": pm_50,         # 호환: 'per_model' = P50 모델별 기여
            "per_model_by_q": {q_lo: pm_05, q_mid: pm_50, q_hi: pm_95},
            "model_kind": "quantile",
        }

    # =========================================================================
    # v5.1 (점추정) 분기 — 기존 동작 그대로
    # =========================================================================
    models = list(spec.get("models") or [])

    _raw_w = spec.get("weights")
    weights = np.array(_raw_w, dtype=float) if _raw_w is not None else np.array([], dtype=float)

    if not models:
        raise ValueError(f"No models found for factory={factory}, target={target_name}.")

    if weights.shape[0] != len(models):
        weights = np.ones(len(models), dtype=float) / float(len(models))

    algo_names = ["LGBM", "XGB", "CB"]
    per_model: list[dict[str, Any]] = []
    weighted_preds: list[float] = []

    for i, (mdl, w) in enumerate(zip(models, weights)):
        mt = m_types[i // 3] if m_types else "M1"
        feat_list = features_map.get(mt) or list(X_row_all.columns)

        missing = [f for f in feat_list if f not in X_row_all.columns]
        if missing:
            raise RuntimeError(f"모델 예측에 필요한 필수 피처가 누락되었습니다: {missing}")

        X_row = X_row_all[feat_list]

        try:
            pred = float(np.expm1(mdl.predict(X_row))[0])
        except Exception as e:
            raise RuntimeError(f"Model prediction failed (idx={i}, mt={mt}): {e}") from e

        weighted_preds.append(float(w) * pred)
        per_model.append(
            {
                "idx": i,
                "mt": mt,
                "algo": algo_names[i % 3],
                "weight": float(w),
                "pred": pred,
                "missing_feature_count": len(missing),
            }
        )

    ensemble_pred = float(np.sum(weighted_preds))

    return {
        "target": target_name,
        "unit": TARGET_SPECS[target_name]["unit"],
        "pred": ensemble_pred,
        "pred_p05": None,
        "pred_p50": ensemble_pred,
        "pred_p95": None,
        "actual": actual_val,
        "band_status": None,
        "band_position": None,
        "is_workday": workday,
        "is_special_event": is_special_event,
        "per_model": per_model,
        "model_kind": "point",
    }


# v5를 예측합니다.
def predict_v5(
    factory: str,
    pred_date: date,
    mix_prod_kg: float,
    weather_override: dict[str, Any] | None = None,
    wip_override: dict[str, Any] | None = None,
    targets: list[str] | None = None,
) -> dict[str, Any]:
    """
    v5 ensemble inference.

    Parameters
    ----------
    factory : str
      남양주1/남양주2/김해/광주/논산
    pred_date : date
      prediction date
    mix_prod_kg : float
      production (kg) for pred_date
    weather_override : dict
      keys in WEATHER_COLS
    targets : list[str] | None
      subset of ["전력","연료","용수"]
    """
    if targets is None:
        targets = list(TARGET_SPECS.keys())
    targets = [t for t in targets if t in TARGET_SPECS]

    # weather resolve (결측은 NaN으로 보존 → df_source 합친 뒤 fill_weather_gaps가 보간)
    base_weather = get_weather_prefill(factory, pred_date)
    weather: dict[str, float] = {}
    for c in WEATHER_COLS:
        val = None
        if weather_override and c in weather_override:
            val = weather_override.get(c)
        if val is None:
            val = base_weather.get(c)
        try:
            weather[c] = float(val) if val is not None and not pd.isna(val) else float("nan")
        except Exception:
            weather[c] = float("nan")

    model_dict = get_active_model()
    df_source, resolved_wip_inputs = _build_feature_source_frame(
        factory=factory,
        pred_date=pred_date,
        mix_prod_kg=mix_prod_kg,
        weather=weather,
        model_dict=model_dict,
        targets=targets,
        wip_override=wip_override,
    )

    out: dict[str, Any] = {
        "factory": factory,
        "date": pred_date.strftime("%Y-%m-%d"),
        "mix_prod_kg": float(mix_prod_kg),
        "weather": weather,
        "wip_inputs": resolved_wip_inputs,
        "model_path": str(get_active_model_path()),
        "results": {},
    }

    # 휴일 보정에 쓸 holiday_set은 루프 외부에서 한 번만 로드
    holiday_set_for_adj = _cached_holidays()
    # 활성 모델의 사후 보정 정책(재학습 모델은 휴일 보정 비활성화).
    drift_on = _drift_correction_enabled()
    holiday_on = _holiday_correction_enabled()

    for tgt in targets:
        result = _infer_target(model_dict, factory, tgt, pred_date, df_source)
        # 신설 라인 사후 보정 (학습 데이터에 없는 신설 부하)
        base_pred = float(result["pred"])
        drift_info = None
        if drift_on:
            corrected_pred, drift_info = _apply_drift_correction(
                base_pred=base_pred,
                factory=factory,
                target_name=tgt,
                pred_date=pred_date,
                wip_inputs=resolved_wip_inputs,
            )
        if drift_info is not None:
            result["pred_before_drift"] = base_pred
            result["pred"] = corrected_pred
            result["drift_correction"] = drift_info
            _shift_band_inplace(result, corrected_pred - base_pred)

        # 휴일/장기연휴 인접일 사후 보정 (캘린더 피처 재학습 전 임시 가드)
        pre_hol_pred = float(result["pred"])
        hol_info = None
        if holiday_on:
            hol_adj_pred, hol_info = _apply_holiday_adjacent_correction(
                base_pred=pre_hol_pred,
                target_name=tgt,
                pred_date=pred_date,
                holiday_set=holiday_set_for_adj,
            )
        if hol_info is not None:
            result["pred_before_holiday_adj"] = pre_hol_pred
            result["pred"] = hol_adj_pred
            result["holiday_correction"] = hol_info
            # 휴일 보정은 곱셈(factor) — 밴드도 동일 비율 스케일링
            _scale_band_inplace(result, float(hol_info.get("factor", 1.0)))

        # 보정 후 band_status 재계산 (v5.2 결과만)
        if result.get("model_kind") == "quantile":
            actual_after = result.get("actual")
            new_status, new_pos = classify_band(
                actual_after,
                result.get("pred_p05"),
                result.get("pred_p50"),
                result.get("pred_p95"),
            )
            result["band_status"] = new_status
            result["band_position"] = new_pos

        out["results"][tgt] = result

    return out


# 밴드 평행이동 (드리프트 가산 보정 후)
def _shift_band_inplace(result: dict[str, Any], delta: float) -> None:
    if result.get("model_kind") != "quantile":
        return
    for k in ("pred_p05", "pred_p50", "pred_p95"):
        v = result.get(k)
        if v is not None:
            result[k] = float(v) + float(delta)


# 밴드 비율 스케일링 (휴일 곱셈 보정 후)
def _scale_band_inplace(result: dict[str, Any], factor: float) -> None:
    if result.get("model_kind") != "quantile":
        return
    if abs(factor - 1.0) < 1e-12:
        return
    for k in ("pred_p05", "pred_p50", "pred_p95"):
        v = result.get(k)
        if v is not None:
            result[k] = float(v) * float(factor)


# ──────────────────────────────────────────────────────────────
# Batch prediction & history
# ──────────────────────────────────────────────────────────────

# 날씨 for 날짜를 확정합니다.
def _resolve_weather_for_date(factory: str, d: date) -> dict[str, float]:
    """Resolve weather values for a single date from cached Excel.

    결측이면 0이 아니라 NaN을 반환합니다(다운스트림 fill_weather_gaps가 보간).
    """
    base = get_weather_prefill(factory, d)
    out: dict[str, float] = {}
    for c in WEATHER_COLS:
        v = base.get(c)
        out[c] = float(v) if v is not None and not pd.isna(v) else float("nan")
    return out


# 예측 로그 데이터를 저장합니다.
def _save_prediction_log(rows: list[dict]) -> int:
    """UPSERT prediction results into prediction_log table.

    v5.1(점추정): pred_p05/pred_p95/band_status/band_position은 NULL.
    v5.2(분위수): 모두 채움. mape는 호환을 위해 계속 계산.

    Note: prediction_log requires INSERT/UPDATE privileges.
    """
    if not rows:
        return 0
    if not is_admin():
        raise PermissionError("prediction_log 저장은 관리자 권한이 필요합니다.")
    query = """
        INSERT INTO prediction_log
            (factory, pred_date, target,
             pred_value, pred_p05, pred_p95,
             actual_value, mape,
             band_status, band_position,
             mix_prod_kg, model_path)
        VALUES (%s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s)
        ON DUPLICATE KEY UPDATE
            pred_value    = VALUES(pred_value),
            pred_p05      = VALUES(pred_p05),
            pred_p95      = VALUES(pred_p95),
            actual_value  = VALUES(actual_value),
            mape          = VALUES(mape),
            band_status   = VALUES(band_status),
            band_position = VALUES(band_position),
            mix_prod_kg   = VALUES(mix_prod_kg),
            model_path    = VALUES(model_path),
            updated_at    = CURRENT_TIMESTAMP
    """
    params_list = []
    for r in rows:
        actual = r.get("actual_value")
        pred = r["pred_value"]
        p05 = r.get("pred_p05")
        p95 = r.get("pred_p95")

        # mape는 호환을 위해 계속 계산 (참고 지표)
        mape_val = None
        if actual is not None and actual != 0:
            mape_val = abs(pred - actual) / max(abs(actual), 1.0) * 100.0

        # band_status/band_position: 행에 이미 있으면 사용, 없으면 재계산
        band_status = r.get("band_status")
        band_position = r.get("band_position")
        if band_status is None and p05 is not None and p95 is not None:
            band_status, band_position = classify_band(actual, p05, pred, p95)

        params_list.append((
            r["factory"],
            r["pred_date"],
            r["target"],
            pred,
            p05,
            p95,
            actual,
            mape_val,
            band_status,
            band_position,
            r.get("mix_prod_kg", 0.0),
            r.get("model_path"),
        ))
    conn = get_connection()
    cur = None
    try:
        cur = conn.cursor()
        cur.executemany(query, params_list)
        conn.commit()
        return cur.rowcount
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def _fetch_prediction_log_rows(
    factory: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    targets: list[str] | None = None,
) -> pd.DataFrame:
    """prediction_log 원본 조회."""
    conditions: list[str] = []
    params: list[Any] = []

    if factory:
        conditions.append("p.factory = %s")
        params.append(factory)
    if date_from:
        conditions.append("p.pred_date >= %s")
        params.append(date_from.strftime("%Y-%m-%d"))
    if date_to:
        conditions.append("p.pred_date <= %s")
        params.append(date_to.strftime("%Y-%m-%d"))
    if targets:
        target_list = [t for t in targets if t in TARGET_SPECS]
        if target_list:
            placeholders = ", ".join(["%s"] * len(target_list))
            conditions.append(f"p.target IN ({placeholders})")
            params.extend(target_list)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"""
        SELECT
            p.factory,
            p.pred_date,
            p.target,
            p.pred_value,
            p.pred_p05,
            p.pred_p95,
            p.actual_value,
            p.mape,
            p.band_status,
            p.band_position,
            p.mix_prod_kg,
            p.created_at,
            p.updated_at
        FROM prediction_log p
        {where}
        ORDER BY p.pred_date DESC, p.factory, p.target
    """
    conn = get_connection()
    try:
        return pd.read_sql_query(query, conn, params=params or None)
    finally:
        conn.close()


def _build_history_rows_from_prediction(prediction: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """예측 결과를 저장/표시용 행으로 바꿉니다.

    v5.2 결과(pred_p05/pred_p95/band_status)도 그대로 행에 보존.
    """
    log_rows: list[dict[str, Any]] = []
    display_rows: list[dict[str, Any]] = []
    now_ts = pd.Timestamp(datetime.now())

    for target_name, result in (prediction.get("results") or {}).items():
        if not isinstance(result, dict) or "error" in result:
            continue

        pred_val = float(result.get("pred", 0.0))
        actual_val = result.get("actual")
        actual_float = float(actual_val) if actual_val is not None else None
        mape_val = None
        if actual_float is not None:
            mape_val = abs(pred_val - actual_float) / max(abs(actual_float), 1.0) * 100.0

        p05 = result.get("pred_p05")
        p95 = result.get("pred_p95")
        band_status = result.get("band_status")
        band_position = result.get("band_position")

        base_row = {
            "factory": prediction["factory"],
            "pred_date": prediction["date"],
            "target": target_name,
            "pred_value": pred_val,
            "pred_p05": (float(p05) if p05 is not None else None),
            "pred_p95": (float(p95) if p95 is not None else None),
            "actual_value": actual_float,
            "mape": mape_val,
            "band_status": band_status,
            "band_position": (float(band_position) if band_position is not None else None),
            "mix_prod_kg": float(prediction.get("mix_prod_kg", 0.0) or 0.0),
        }
        log_rows.append(
            {
                **base_row,
                "model_path": prediction.get("model_path"),
            }
        )
        display_rows.append(
            {
                **base_row,
                "created_at": now_ts,
                "updated_at": now_ts,
            }
        )

    return log_rows, display_rows


def _ensure_prediction_history_rows(
    factory: str,
    date_from: date,
    date_to: date,
    targets: list[str] | None = None,
    save_missing: bool = False,
) -> pd.DataFrame:
    """실공장 기준 prediction_log 조회 + 없는 값만 추가 예측."""
    if factory not in STATION_MAP:
        raise ValueError(f"Unknown base factory: {factory}")

    target_list = [t for t in (targets or list(TARGET_SPECS.keys())) if t in TARGET_SPECS]
    existing_df = _fetch_prediction_log_rows(
        factory=factory,
        date_from=date_from,
        date_to=date_to,
        targets=target_list,
    )

    holiday_set = _cached_holidays()
    workdays = [
        ts.date()
        for ts in pd.date_range(date_from, date_to, freq="D")
        if is_workday(pd.Timestamp(ts), holiday_set)
    ]
    if not workdays:
        return existing_df

    existing_keys: set[tuple[date, str]] = set()
    if not existing_df.empty:
        existing_dates = pd.to_datetime(existing_df["pred_date"], errors="coerce")
        for idx, pred_ts in existing_dates.items():
            if pd.isna(pred_ts):
                continue
            existing_keys.add((pred_ts.date(), str(existing_df.at[idx, "target"])))

    prediction_rows: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    for pred_d in workdays:
        missing_targets = [t for t in target_list if (pred_d, t) not in existing_keys]
        if not missing_targets:
            continue

        mix_val = get_mix_prod_prefill(factory, pred_d)
        mix_prod_kg = float(mix_val) if mix_val is not None else 0.0
        prediction = predict_v5(
            factory=factory,
            pred_date=pred_d,
            mix_prod_kg=mix_prod_kg,
            targets=missing_targets,
        )
        log_rows, display_rows = _build_history_rows_from_prediction(prediction)
        prediction_rows.extend(log_rows)
        history_rows.extend(display_rows)

    if prediction_rows and save_missing and is_admin():
        _save_prediction_log(prediction_rows)

    if not history_rows:
        return existing_df

    new_df = pd.DataFrame(history_rows)
    if existing_df.empty:
        return new_df
    return pd.concat([existing_df, new_df], ignore_index=True)


def _history_key_set(df: pd.DataFrame) -> set[tuple[str, date, str]]:
    keys: set[tuple[str, date, str]] = set()
    if df.empty or not {"factory", "pred_date", "target"}.issubset(df.columns):
        return keys
    pred_dates = pd.to_datetime(df["pred_date"], errors="coerce")
    for idx, pred_ts in pred_dates.items():
        if pd.isna(pred_ts):
            continue
        keys.add((str(df.at[idx, "factory"]), pred_ts.date(), str(df.at[idx, "target"])))
    return keys


def _resolve_history_base_factories(factory: str | None) -> list[str]:
    if factory is None:
        # 전사/전체 범위에서는 활성 모델이 학습하지 않은 신규 공장(예: 경산)을
        # 조용히 제외한다 — 없으면 predict_v5가 "Factory not found in model"으로
        # 배치 전체를 실패시킨다. 특정 공장을 직접 지정한 호출은 server.py의
        # PREDICTION_FACTORIES 검사가 먼저 걸러내므로 여기까지 오지 않는다.
        physical = list(FACTORY_PHYSICAL_DISPLAY_ORDER)
        try:
            trained = set(get_active_model().keys())
        except Exception:
            return physical
        return [f for f in physical if f in trained]
    if _is_aggregate_factory(factory):
        return list(_resolve_factory_members(factory))
    if factory in STATION_MAP:
        return [factory]
    raise ValueError(f"Unknown factory: {factory}")


def generate_missing_prediction_history(
    factory: str | None,
    date_from: date,
    date_to: date,
    targets: list[str] | None = None,
    save_to_db: bool = True,
) -> dict[str, Any]:
    """명시적으로 누락된 prediction_log 행을 예측 생성합니다.

    조회 함수(get_prediction_history)는 읽기 전용이어야 하므로, 관리자가 화면에서
    버튼을 눌렀을 때만 이 함수를 호출해 누락분을 생성/저장합니다.
    """
    if save_to_db and not is_admin():
        raise PermissionError("누락 예측 생성/저장은 관리자 권한이 필요합니다.")
    if date_from is None or date_to is None:
        raise ValueError("date_from/date_to가 필요합니다.")
    if date_from > date_to:
        raise ValueError("date_from은 date_to보다 늦을 수 없습니다.")

    target_list = [t for t in (targets or list(TARGET_SPECS.keys())) if t in TARGET_SPECS]
    total_generated_rows = 0
    per_factory: dict[str, int] = {}

    for base_factory in _resolve_history_base_factories(factory):
        before_df = _fetch_prediction_log_rows(
            factory=base_factory,
            date_from=date_from,
            date_to=date_to,
            targets=target_list,
        )
        before_keys = _history_key_set(before_df)
        after_df = _ensure_prediction_history_rows(
            factory=base_factory,
            date_from=date_from,
            date_to=date_to,
            targets=target_list,
            save_missing=save_to_db,
        )
        generated = len(_history_key_set(after_df) - before_keys)
        per_factory[base_factory] = generated
        total_generated_rows += generated

    return {
        "generated_rows": total_generated_rows,
        "saved_rows": total_generated_rows if save_to_db else 0,
        "per_factory": per_factory,
    }


def _aggregate_prediction_history_rows(
    aggregate_factory: str,
    member_frames: list[pd.DataFrame],
) -> pd.DataFrame:
    """실공장 예측 이력을 집계 공장 이력으로 합칩니다.

    v5.2 밴드 컬럼(pred_p05/pred_p95) 합산 규칙:
      - 독립성을 가정한 단순 가법 합산 (보수적 추정).
        실제 분위수의 합은 일반적으로 < 합의 분위수이지만, 운영상 보수적
        밴드(약간 더 넓음)는 false positive 감소 효과가 있어 무방함.
      - 모든 구성 공장이 v5.2여야 합산. 일부라도 v5.1이면 밴드는 NULL.
    """
    valid_frames = [df.copy() for df in member_frames if df is not None and not df.empty]
    if not valid_frames:
        return pd.DataFrame(
            columns=[
                "factory", "pred_date", "target",
                "pred_value", "pred_p05", "pred_p95",
                "actual_value", "mape",
                "band_status", "band_position",
                "mix_prod_kg", "created_at", "updated_at",
            ]
        )

    combined = pd.concat(valid_frames, ignore_index=True)
    combined["pred_date"] = pd.to_datetime(combined["pred_date"], errors="coerce")
    combined["pred_value"] = pd.to_numeric(combined["pred_value"], errors="coerce")
    combined["actual_value"] = pd.to_numeric(combined["actual_value"], errors="coerce")
    combined["mix_prod_kg"] = pd.to_numeric(combined["mix_prod_kg"], errors="coerce").fillna(0.0)
    combined["created_at"] = pd.to_datetime(combined["created_at"], errors="coerce")
    combined["updated_at"] = pd.to_datetime(combined["updated_at"], errors="coerce")
    # v5.2 밴드 컬럼은 없을 수도 있어 안전 처리
    for c in ("pred_p05", "pred_p95"):
        if c not in combined.columns:
            combined[c] = np.nan
        combined[c] = pd.to_numeric(combined[c], errors="coerce")
    combined = combined.dropna(subset=["pred_date", "target"])

    expected_member_count = len(_resolve_factory_members(aggregate_factory))
    rows: list[dict[str, Any]] = []
    grouped = combined.groupby(["pred_date", "target"], sort=False)

    for (pred_date, target_name), group in grouped:
        pred_count = int(group["pred_value"].notna().sum())
        pred_total = float(group["pred_value"].sum()) if pred_count > 0 else np.nan

        # 밴드 합산: 모든 구성 공장이 P05/P95를 가지고 있을 때만 합산
        if group["pred_p05"].notna().sum() == expected_member_count:
            p05_total = float(group["pred_p05"].sum())
        else:
            p05_total = None
        if group["pred_p95"].notna().sum() == expected_member_count:
            p95_total = float(group["pred_p95"].sum())
        else:
            p95_total = None

        actual_complete = int(group["actual_value"].notna().sum()) >= expected_member_count
        actual_total = float(group["actual_value"].sum()) if actual_complete else np.nan
        mape_val = None
        if actual_complete:
            mape_val = abs(pred_total - actual_total) / max(abs(actual_total), 1.0) * 100.0

        # 집계 밴드 상태 재산출
        band_status_agg, band_pos_agg = (None, None)
        if actual_complete and p05_total is not None and p95_total is not None:
            band_status_agg, band_pos_agg = classify_band(
                actual_total, p05_total, pred_total, p95_total
            )

        rows.append(
            {
                "factory": aggregate_factory,
                "pred_date": pred_date,
                "target": target_name,
                "pred_value": pred_total,
                "pred_p05": p05_total,
                "pred_p95": p95_total,
                "actual_value": actual_total,
                "mape": mape_val,
                "band_status": band_status_agg,
                "band_position": band_pos_agg,
                "mix_prod_kg": float(group["mix_prod_kg"].sum()),
                "created_at": group["created_at"].max(),
                "updated_at": group["updated_at"].max(),
            }
        )

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _build_aggregate_batch_results(
    aggregate_factory: str,
    df_hist: pd.DataFrame,
    date_from: date,
    date_to: date,
    targets: list[str],
) -> list[dict[str, Any]]:
    """집계 이력을 batch 결과 형식으로 바꿉니다."""
    if df_hist.empty:
        return []

    holiday_set = _cached_holidays()
    workdays = [
        ts.date()
        for ts in pd.date_range(date_from, date_to, freq="D")
        if is_workday(pd.Timestamp(ts), holiday_set)
    ]
    if not workdays:
        return []

    df_work = df_hist.copy()
    df_work["pred_date"] = pd.to_datetime(df_work["pred_date"], errors="coerce")
    df_work = df_work.dropna(subset=["pred_date"]).sort_values(["pred_date", "target"]).reset_index(drop=True)

    results: list[dict[str, Any]] = []
    for pred_d in workdays:
        day_mask = df_work["pred_date"] == pd.Timestamp(pred_d)
        day_df = df_work[day_mask].copy()
        mix_prod_kg = 0.0
        if not day_df.empty and day_df["mix_prod_kg"].notna().any():
            mix_prod_kg = float(pd.to_numeric(day_df["mix_prod_kg"], errors="coerce").fillna(0.0).max())

        day_result: dict[str, Any] = {
            "factory": aggregate_factory,
            "date": pred_d.strftime("%Y-%m-%d"),
            "mix_prod_kg": mix_prod_kg,
            "wip_inputs": {},
            "results": {},
        }

        for target_name in targets:
            row = day_df[day_df["target"] == target_name]
            if row.empty:
                day_result["results"][target_name] = {
                    "error": f"{aggregate_factory} 집계에 필요한 예측값이 없습니다."
                }
                continue

            r0 = row.iloc[0]
            pred_val = pd.to_numeric(pd.Series([r0.get("pred_value")]), errors="coerce").iloc[0]
            if pd.isna(pred_val):
                day_result["results"][target_name] = {
                    "error": f"{aggregate_factory} 집계 예측값 계산에 실패했습니다."
                }
                continue

            actual_val = pd.to_numeric(pd.Series([r0.get("actual_value")]), errors="coerce").iloc[0]
            actual = None if pd.isna(actual_val) else float(actual_val)

            def _nullable_float(value):
                if value is None:
                    return None
                try:
                    if pd.isna(value):
                        return None
                except (TypeError, ValueError):
                    pass
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            p05 = _nullable_float(r0.get("pred_p05") if "pred_p05" in r0.index else None)
            p95 = _nullable_float(r0.get("pred_p95") if "pred_p95" in r0.index else None)
            bs_raw = r0.get("band_status") if "band_status" in r0.index else None
            band_status = bs_raw if isinstance(bs_raw, str) else None
            band_position = _nullable_float(r0.get("band_position") if "band_position" in r0.index else None)

            day_result["results"][target_name] = {
                "target": target_name,
                "unit": TARGET_SPECS[target_name]["unit"],
                "pred": float(pred_val),
                "pred_p05": p05,
                "pred_p50": float(pred_val),
                "pred_p95": p95,
                "actual": actual,
                "band_status": band_status,
                "band_position": band_position,
                "is_workday": True,
                "is_special_event": False,
                "per_model": [],
                "model_kind": "quantile" if (p05 is not None and p95 is not None) else "point",
            }

        results.append(day_result)

    return results


def _predict_v5_batch_aggregate(
    factory: str,
    date_from: date,
    date_to: date,
    targets: list[str],
    save_to_db: bool,
) -> list[dict[str, Any]]:
    """집계 공장 batch 예측."""
    member_frames = [
        _ensure_prediction_history_rows(
            factory=member_factory,
            date_from=date_from,
            date_to=date_to,
            targets=targets,
            save_missing=save_to_db,
        )
        for member_factory in _resolve_factory_members(factory)
    ]
    agg_hist = _aggregate_prediction_history_rows(factory, member_frames)
    if agg_hist.empty:
        return []
    return _build_aggregate_batch_results(factory, agg_hist, date_from, date_to, targets)


# v5 일괄를 예측합니다.
def predict_v5_batch(
    factory: str,
    date_from: date,
    date_to: date,
    targets: list[str] | None = None,
    override_df: pd.DataFrame | None = None,
    save_to_db: bool = True,
) -> list[dict[str, Any]]:
    """
    Batch prediction for a date range.

    Builds the feature source frame once with the full lookback,
    then iterates over each workday in [date_from, date_to].
    Results are saved to prediction_log automatically.
    """
    if targets is None:
        targets = list(TARGET_SPECS.keys())
    targets = [t for t in targets if t in TARGET_SPECS]

    # 예측 실행 전 날씨 데이터 자동 동기화 (세션당 1회, 실패 시 스킵)
    global _weather_synced_this_session
    if _WEATHER_SYNC_AVAILABLE and not _weather_synced_this_session:
        try:
            _sync_all_weather()
            _weather_synced_this_session = True
            # 날씨 데이터 오래된 캐시 무효화
            _cached_weather_station.clear()
        except Exception as _e:
            import logging
            logging.getLogger(__name__).warning(f"[predict_v5] 날씨 자동 동기화 실패 (예측 계속): {_e}")

    if _is_aggregate_factory(factory):
        return _predict_v5_batch_aggregate(
            factory=factory,
            date_from=date_from,
            date_to=date_to,
            targets=targets,
            save_to_db=save_to_db,
        )

    holiday_set = _cached_holidays()
    model_dict = get_active_model()
    model_path_str = str(get_active_model_path())
    # 활성 모델의 사후 보정 정책(재학습 모델은 휴일 보정 비활성화).
    drift_on = _drift_correction_enabled()
    holiday_on = _holiday_correction_enabled()
    required_wip_item_codes = _collect_required_wip_item_codes(model_dict, factory, targets)
    required_wip_feature_cols = [_build_wip_feature_col(code) for code in required_wip_item_codes]

    # If override_df is provided, we use the dates from there (which might include non-workdays if user wants)
    # Otherwise, generate list of workdays in range
    if override_df is not None and not override_df.empty:
        all_dates = pd.to_datetime(override_df["날짜"]).dt.date.tolist()
        workdays = all_dates
    else:
        all_dates = pd.date_range(date_from, date_to, freq="D")
        workdays = [d.date() for d in all_dates if is_workday(d, holiday_set)]

    if not workdays:
        return []

    if required_wip_item_codes and min(workdays) < WIP_AVAILABLE_START.date():
        raise ValueError("재공품 피처 모델은 2023-01-01 이후 날짜만 배치 예측할 수 있습니다.")

    # Fetch history once (lookback from earliest date)
    lookback_days = 450
    hist_from = workdays[0] - timedelta(days=lookback_days)
    hist_to = workdays[-1]
    df_raw = _fetch_energy_history(factory, hist_from, hist_to)
    df_hist = _to_korean_schema(df_raw)

    # Filter to workdays only
    if not df_hist.empty and "날짜" in df_hist.columns:
        df_hist = df_hist[df_hist["날짜"].apply(lambda x: is_workday(pd.Timestamp(x), holiday_set))]

    # Merge weather once (보간은 예측 루프 안에서 pred_row 합친 뒤 수행).
    station = STATION_MAP.get(factory)
    df_w = _cached_weather_station(station) if station else pd.DataFrame(columns=["날짜"] + WEATHER_COLS)
    if df_w.empty:
        for c in WEATHER_COLS:
            df_hist[c] = np.nan
    else:
        df_hist = df_hist.merge(df_w, on="날짜", how="left")
        for c in WEATHER_COLS:
            if c not in df_hist.columns:
                df_hist[c] = np.nan

    if required_wip_item_codes:
        if pd.Timestamp(hist_to) >= WIP_AVAILABLE_START:
            df_wip_hist = _load_wip_history(factory, hist_from, hist_to, required_wip_item_codes)
            df_hist = df_hist.merge(df_wip_hist, on="날짜", how="left")
        for feature_col in required_wip_feature_cols:
            if feature_col not in df_hist.columns:
                df_hist[feature_col] = 0.0
            df_hist[feature_col] = pd.to_numeric(df_hist[feature_col], errors="coerce").fillna(0.0)

    results: list[dict[str, Any]] = []
    log_rows: list[dict] = []

    # 재귀 lag 용 working history — 실측(df_hist)으로 시작, 미래 날짜는 예측치를 되먹인다.
    # 재귀가 올바르려면 날짜 오름차순으로 순회해야 한다.
    df_work = df_hist
    actual_dates = set(df_hist["날짜"].tolist()) if not df_hist.empty else set()
    target_model_cols = {t: TARGET_SPECS[t]["model_col"] for t in targets}
    workdays = sorted(workdays)

    for pred_d in workdays:
        # Override lookups
        row_override = None
        if override_df is not None and not override_df.empty:
            match = override_df[pd.to_datetime(override_df["날짜"]).dt.date == pred_d]
            if not match.empty:
                row_override = match.iloc[0]

        if row_override is not None:
            mix_prod_kg = float(row_override.get("믹스생산량[kg]", 0.0))
            # 사용자 명시 입력은 그대로 사용. 빈 셀은 NaN으로 보존하여 보간 대상이 되게 함.
            weather: dict[str, float] = {}
            for c in WEATHER_COLS:
                v = row_override.get(c)
                try:
                    weather[c] = float(v) if v is not None and not pd.isna(v) else float("nan")
                except (TypeError, ValueError):
                    weather[c] = float("nan")
        else:
            # Get mix_prod_kg from DB if available
            mix_val = get_mix_prod_prefill(factory, pred_d)
            mix_prod_kg = float(mix_val) if mix_val is not None else 0.0
            # Get weather for this date (결측은 NaN으로 반환됨)
            weather = _resolve_weather_for_date(factory, pred_d)

        wip_inputs = _resolve_wip_inputs_for_date(
            factory=factory,
            pred_date=pred_d,
            item_codes=required_wip_item_codes,
            row_override=row_override,
        )

        # Build prediction row (날씨는 NaN을 보존하여 fill_weather_gaps가 보간하도록 함)
        pred_row: dict[str, Any] = {"날짜": pd.Timestamp(pred_d), "믹스생산량[kg]": mix_prod_kg}
        for c in WEATHER_COLS:
            v = weather.get(c)
            try:
                pred_row[c] = float(v) if v is not None and not pd.isna(v) else np.nan
            except (TypeError, ValueError):
                pred_row[c] = np.nan
        for spec in TARGET_SPECS.values():
            pred_row[spec["model_col"]] = 0.0
        for feature_col, value in wip_inputs.items():
            pred_row[feature_col] = float(value)

        # Combine history (up to pred_date - 1) with prediction row.
        # df_work 는 실측 + (재귀 시) 앞선 미래 날짜의 예측치를 포함 → lag/r7mean 이 추세를 추종.
        df_before = df_work[df_work["날짜"] < pd.Timestamp(pred_d)].copy()
        df_source = pd.concat([df_before, pd.DataFrame([pred_row])], ignore_index=True)
        df_source = df_source.sort_values("날짜").reset_index(drop=True)
        # 시계열 보간(예측행 포함). 강수량은 결측을 0(무강수)으로, 그 외는 ffill/bfill.
        df_source = fill_weather_gaps(df_source, WEATHER_COLS)

        day_result: dict[str, Any] = {
            "factory": factory,
            "date": pred_d.strftime("%Y-%m-%d"),
            "mix_prod_kg": mix_prod_kg,
            "wip_inputs": wip_inputs,
            "results": {},
        }

        for tgt in targets:
            try:
                r = _infer_target(model_dict, factory, tgt, pred_d, df_source)
                # 신설 라인 사후 보정
                base_pred = float(r["pred"])
                drift_info = None
                if drift_on:
                    corrected_pred, drift_info = _apply_drift_correction(
                        base_pred=base_pred,
                        factory=factory,
                        target_name=tgt,
                        pred_date=pred_d,
                        wip_inputs=wip_inputs,
                    )
                if drift_info is not None:
                    r["pred_before_drift"] = base_pred
                    r["pred"] = corrected_pred
                    r["drift_correction"] = drift_info
                    _shift_band_inplace(r, corrected_pred - base_pred)

                # 휴일/장기연휴 인접일 사후 보정
                pre_hol_pred = float(r["pred"])
                hol_info = None
                if holiday_on:
                    hol_adj_pred, hol_info = _apply_holiday_adjacent_correction(
                        base_pred=pre_hol_pred,
                        target_name=tgt,
                        pred_date=pred_d,
                        holiday_set=holiday_set,
                    )
                if hol_info is not None:
                    r["pred_before_holiday_adj"] = pre_hol_pred
                    r["pred"] = hol_adj_pred
                    r["holiday_correction"] = hol_info
                    _scale_band_inplace(r, float(hol_info.get("factor", 1.0)))

                # 보정 후 band_status 재계산
                if r.get("model_kind") == "quantile":
                    new_status, new_pos = classify_band(
                        r.get("actual"),
                        r.get("pred_p05"),
                        r.get("pred_p50"),
                        r.get("pred_p95"),
                    )
                    r["band_status"] = new_status
                    r["band_position"] = new_pos

                day_result["results"][tgt] = r
                log_rows.append({
                    "factory": factory,
                    "pred_date": pred_d.strftime("%Y-%m-%d"),
                    "target": tgt,
                    "pred_value": r["pred"],
                    "pred_p05": r.get("pred_p05"),
                    "pred_p95": r.get("pred_p95"),
                    "band_status": r.get("band_status"),
                    "band_position": r.get("band_position"),
                    "actual_value": r.get("actual"),
                    "mix_prod_kg": mix_prod_kg,
                    "model_path": model_path_str,
                })
            except Exception as e:
                day_result["results"][tgt] = {"error": str(e)}

        # 재귀 lag: 실측이 없는 미래 날짜는 이번 예측 P50 을 working history 에 추가해
        # 다음날 lag1/r7mean/intensity_lag1 이 추세를 따라가게 한다(실측 있는 날은 건드리지 않음).
        pred_ts = pd.Timestamp(pred_d)
        if RECURSIVE_FUTURE_LAG and pred_ts not in actual_dates:
            synth = dict(pred_row)
            has_pred = False
            for tgt in targets:
                res = day_result["results"].get(tgt)
                if isinstance(res, dict) and res.get("pred") is not None and "error" not in res:
                    synth[target_model_cols[tgt]] = float(res["pred"])
                    has_pred = True
            if has_pred:
                df_work = pd.concat([df_work, pd.DataFrame([synth])], ignore_index=True)
                df_work = df_work.sort_values("날짜").reset_index(drop=True)
                actual_dates.add(pred_ts)  # 같은 배치 내 재추가 방지

        results.append(day_result)

    # Save all results to DB (admin only)
    if log_rows and save_to_db:
        _save_prediction_log(log_rows)

    return results


# 기본 입력값 데이터 일괄 값을 가져옵니다.
def get_prefill_data_batch(
    factory: str,
    date_from: date,
    date_to: date,
    targets: list[str] | None = None,
) -> pd.DataFrame:
    """Fetch auto-fill values (mix_prod_kg, weather) for a date range to show in data editor."""
    holiday_set = _cached_holidays()
    all_dates = pd.date_range(date_from, date_to, freq="D")
    workdays = [d.date() for d in all_dates if is_workday(d, holiday_set)]
    required_wip_feature_cols = _get_required_wip_columns_for_targets(factory, targets)
    required_wip_item_codes = [_derive_item_code_from_feature_col(col) for col in required_wip_feature_cols]

    rows = []
    wip_prefill_df = pd.DataFrame(columns=["날짜", *required_wip_feature_cols])
    if required_wip_item_codes and workdays and pd.Timestamp(workdays[-1]) >= WIP_AVAILABLE_START:
        wip_prefill_df = _load_wip_history(factory, workdays[0], workdays[-1], required_wip_item_codes)

    for d in workdays:
        mix_val = get_mix_prod_prefill(factory, d)
        mix_prod_kg = float(mix_val) if mix_val is not None else 0.0
        weather = _resolve_weather_for_date(factory, d)

        row = {"날짜": pd.Timestamp(d), "믹스생산량[kg]": mix_prod_kg}
        for c in WEATHER_COLS:
            row[c] = weather.get(c, 0.0)
        if required_wip_feature_cols:
            row_wip = _resolve_wip_inputs_for_date(factory, d, required_wip_item_codes)
            if not wip_prefill_df.empty:
                match = wip_prefill_df[wip_prefill_df["날짜"] == pd.Timestamp(d)]
                if not match.empty:
                    for feature_col in required_wip_feature_cols:
                        try:
                            row_wip[feature_col] = float(match.iloc[0].get(feature_col, row_wip.get(feature_col, 0.0)) or 0.0)
                        except Exception:
                            row_wip[feature_col] = float(row_wip.get(feature_col, 0.0))
            for feature_col in required_wip_feature_cols:
                row[feature_col] = float(row_wip.get(feature_col, 0.0))
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["날짜", "믹스생산량[kg]"] + WEATHER_COLS + required_wip_feature_cols)

    return pd.DataFrame(rows)


# 예측 이력 값을 가져옵니다.
def get_prediction_history(
    factory: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> pd.DataFrame:
    """Query prediction history from prediction_log table.

    This function is read-only. Missing prediction rows must be generated through
    generate_missing_prediction_history(), which is an explicit admin action.

    factory 동작 규칙:
      - 특정 실공장(남양주1/남양주2/김해/광주/논산) → 그 공장 행만 반환
      - 집계 공장(남양주/전사) → 구성 공장 이력을 합산해 단일 집계 행 반환
      - None("전체") → 5개 집계 단위(전사/남양주/김해/광주/논산)로 묶어 반환.
        남양주1·남양주2 개별 행은 노출하지 않음 — UI 표준 순서 상의 5개 항목만 보임.
    """
    if factory and _is_aggregate_factory(factory):
        member_frames = [
            _fetch_prediction_log_rows(
                factory=member_factory,
                date_from=date_from,
                date_to=date_to,
                targets=None,
            )
            for member_factory in _resolve_factory_members(factory)
        ]

        agg_df = _aggregate_prediction_history_rows(factory, member_frames)
        if agg_df.empty:
            return agg_df
        return _sort_history_by_factory_order(agg_df)

    if factory is None:
        return _build_overview_history(date_from=date_from, date_to=date_to)

    df = _fetch_prediction_log_rows(
        factory=factory,
        date_from=date_from,
        date_to=date_to,
        targets=None,
    )
    if df.empty:
        return df
    return _sort_history_by_factory_order(df)


# 표준 정렬: pred_date(최신 우선) → factory(표준 순서) → target.
_FACTORY_RANK = {f: i for i, f in enumerate(FACTORY_AGGREGATE_DISPLAY_ORDER)}


def _sort_history_by_factory_order(df: pd.DataFrame) -> pd.DataFrame:
    """예측 이력 DataFrame을 (날짜 ↓, 공장 표준순서 ↑, 항목 ↑)로 정렬.

    주의: 호출자에 따라 pred_date 컬럼이 `datetime.date` (raw fetch) 또는
    `pd.Timestamp` (집계 결과) 로 들어올 수 있어, 둘이 섞이면 sort_values 가
    TypeError 를 냅니다. 여기서 일괄 Timestamp 로 정규화한 뒤 정렬합니다.
    """
    if df.empty or "factory" not in df.columns:
        return df.reset_index(drop=True) if not df.empty else df
    out = df.copy()
    if "pred_date" in out.columns:
        out["pred_date"] = pd.to_datetime(out["pred_date"], errors="coerce")
    # 표준 순서에 없는 공장은 맨 뒤에 그대로 따라붙도록 큰 값을 부여.
    out["_rank"] = out["factory"].map(lambda f: _FACTORY_RANK.get(f, 999))
    out = out.sort_values(
        ["pred_date", "_rank", "target"],
        ascending=[False, True, True],
    ).drop(columns=["_rank"]).reset_index(drop=True)
    return out


def _build_overview_history(
    date_from: date | None,
    date_to: date | None,
) -> pd.DataFrame:
    """'전체' 필터 결과 — 5개 집계 단위(전사/남양주/김해/광주/논산)로 묶어 반환.

    DB에는 실공장(남양주1/남양주2/김해/광주/논산) 단위로만 저장되어 있으므로,
    여기서 다음과 같이 합산합니다:
      - 전사 = 5개 실공장 합
      - 남양주 = 남양주1 + 남양주2
      - 김해 / 광주 / 논산 = 그대로 통과 (단일 공장이지만 형식 통일을 위해 동일 포맷)
    """
    raw = _fetch_prediction_log_rows(
        factory=None,
        date_from=date_from,
        date_to=date_to,
        targets=None,
    )
    if raw.empty:
        return raw

    # 집계 카테고리 정의 — 표준 순서대로 빌드.
    aggregate_specs: list[tuple[str, list[str]]] = []
    for label in FACTORY_AGGREGATE_DISPLAY_ORDER:
        if label == "전사":
            members = list(_resolve_factory_members("전사"))
        elif label == "남양주":
            members = list(_resolve_factory_members("남양주"))
        else:
            members = [label]
        aggregate_specs.append((label, members))

    frames: list[pd.DataFrame] = []
    for label, members in aggregate_specs:
        sub = raw[raw["factory"].isin(members)]
        if sub.empty:
            continue
        # 단일 멤버이고 해당 라벨이 그대로 일치하면 그대로 사용 (집계 비용 절감).
        if len(members) == 1 and members[0] == label:
            frames.append(sub.copy())
            continue
        agg_one = _aggregate_prediction_history_rows(label, [sub])
        if not agg_one.empty:
            frames.append(agg_one)

    if not frames:
        return raw.iloc[0:0]

    combined = pd.concat(frames, ignore_index=True)
    return _sort_history_by_factory_order(combined)


# actuals의 빈값을 채웁니다.
def backfill_actuals() -> int:
    """
    Fill actual_value, mape, band_status, band_position in prediction_log
    from energy_daily for rows where actual_value IS NULL.

    v5.2 행(pred_p05/pred_p95 존재): band_status도 재계산해 기록.
    v5.1 행: mape만 채움(밴드 컬럼은 NULL 유지).
    """
    if not is_admin():
        raise PermissionError("실측값 역채움은 관리자 권한이 필요합니다.")
    target_col_map = {tgt: spec["db_col"] for tgt, spec in TARGET_SPECS.items()}
    updated = 0

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, factory, pred_date, target, pred_value, pred_p05, pred_p95 "
            "FROM prediction_log WHERE actual_value IS NULL"
        )
        rows = cur.fetchall()
        cur.close()

        for row in rows:
            db_col = target_col_map.get(row["target"])
            if not db_col:
                continue
            cur2 = conn.cursor()
            cur2.execute(
                f"SELECT {db_col} FROM energy_daily WHERE factory=%s AND date=%s LIMIT 1",
                (row["factory"], row["pred_date"]),
            )
            actual_row = cur2.fetchone()
            cur2.close()

            if actual_row and actual_row[0] is not None:
                actual_val = float(actual_row[0])
                pred_val = float(row["pred_value"])
                mape_val = abs(pred_val - actual_val) / max(abs(actual_val), 1.0) * 100.0

                p05 = row.get("pred_p05")
                p95 = row.get("pred_p95")
                band_status, band_position = classify_band(
                    actual_val,
                    float(p05) if p05 is not None else None,
                    pred_val,
                    float(p95) if p95 is not None else None,
                )

                cur3 = conn.cursor()
                cur3.execute(
                    "UPDATE prediction_log "
                    "SET actual_value=%s, mape=%s, band_status=%s, band_position=%s "
                    "WHERE id=%s",
                    (actual_val, mape_val, band_status, band_position, row["id"]),
                )
                cur3.close()
                updated += 1

        conn.commit()
    finally:
        conn.close()

    return updated
