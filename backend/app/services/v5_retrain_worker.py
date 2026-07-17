# 이 파일은 v5 모델 재학습 작업을 실제로 실행합니다.
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from scipy.optimize import minimize

# 프로젝트 루트를 import 경로에 추가합니다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from catboost import CatBoostRegressor
from lightgbm import LGBMRegressor
from xgboost import XGBRegressor

from app.database.db_connection import get_connection
from app.services.production_actual_service import overlay_actual_production
from app.services.v5_common import (
    BOTTLING_AGG_FEATURE,
    DEFAULT_MODEL_PATH,
    MODEL_ARTIFACT_RETENTION_KEEP,
    N_ACTIVE_SKUS_FEATURE,
    PATH_WIP_SUMMARY,
    PREDICTIVE_MODEL_DIR,
    RETRAIN_POST_PROCESSING_DEFAULTS,
    STATUS_PATH,
    STATION_MAP,
    TARGET_SPECS,
    V5_3_TRAIN_START,
    WEATHER_COLS,
    WIP_AVAILABLE_START,
    attach_model_artifact,
    build_model_artifact_metadata,
    build_feature_frame,
    build_versioned_model_path,
    cleanup_model_artifacts,
    compute_bottling_aggregate,
    detect_special_events,
    fill_weather_gaps,
    get_git_commit,
    is_workday,
    load_holidays_excel,
    load_model_registry,
    load_n_active_skus_series,
    load_weather_station_excel,
    make_features,
    perf_report_path_for,
    read_json,
    resolve_model_path,
    resolve_plant_sheet,
    select_features,
    summarize_metric_frame,
    to_project_relative_path_str,
    write_model_registry,
)
from app.services.v5_quantile_training import (
    MODELS_PER_QUANTILE,
    predict_quantile_probe,
    train_plant_target_quantile,
    validate_quantile_spec_structure,
)
from app.services.v5_retrain_lock import (
    LOCK_PATH,
    release_training_lock,
    start_training_heartbeat,
)

if str(PREDICTIVE_MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(PREDICTIVE_MODEL_DIR))

try:
    from wip_feature_shortlist import get_bottling_ea_codes, get_shortlist_item_codes
except Exception:
    # 우선순위 파일을 읽지 못하면 빈 목록으로 처리합니다.
    def get_shortlist_item_codes(plant: str, target: str) -> tuple[str, ...]:
        return ()

    def get_bottling_ea_codes(plant: str) -> tuple[str, ...]:
        return ()


TRAIN_START = V5_3_TRAIN_START
BASE_FEATURE_SPEC_VERSION = "1.1"
WIP_FEATURE_SPEC_VERSION = "1.2-wip-shortlist"

# 분위수(v5.3) 전체 재학습 spec 버전 — 예측 측 SUPPORTED_FEATURE_SPEC_VERSIONS 와 동일하게 유지.
QUANTILE_WIP_FEATURE_SPEC_VERSION = "1.3-wip-bottling-nactive-quantile"
QUANTILE_BASE_FEATURE_SPEC_VERSION = "1.1-quantile"

# 활성 SKU 카운트(wip_n_active_skus) 피처를 제외할 공장 목록.
# 광주: 학습기간 분포와 2026-test 분포 차이로 인해 추가 시 MAPE 가 악화 (2026-05-27 검증, modeling_v5.3.py 와 동일 정책).
N_ACTIVE_SKUS_EXCLUDE_PLANTS: frozenset[str] = frozenset({"광주"})
_WIP_SHEET_CACHE: dict[str, pd.DataFrame] = {}


# JSON 데이터를 안전하게 저장합니다.
def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 현재 시각 문자열을 만듭니다.
def _now_iso() -> str:
    return datetime.now().isoformat()


# 환경변수에서 정수 설정값을 읽습니다(파싱 실패 시 기본값).
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# 진행률 퍼센트를 계산합니다.
def _calc_progress_pct(completed_steps: int, total_steps: int) -> float:
    if total_steps <= 0:
        return 0.0
    return round(min(max(completed_steps, 0), total_steps) / float(total_steps) * 100.0, 1)


# 실행 중 상태를 파일에 기록합니다.
def _write_running_status(
    *,
    started_at: str,
    trigger_mode: str,
    total_steps: int,
    completed_steps: int,
    message: str,
    phase: str,
    active_model_key: str | None = None,
    current_factory: str | None = None,
    current_target: str | None = None,
    data_end_date: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    status: dict[str, Any] = {
        "status": "running",
        "started_at": started_at,
        "ended_at": None,
        "mode": "running",
        "trigger_mode": trigger_mode,
        "phase": phase,
        "message": message,
        "error": None,
        "new_model_path": None,
        "active_model_key": active_model_key,
        "worker_pid": os.getpid(),
        "heartbeat_at": _now_iso(),
        "lock_path": str(LOCK_PATH),
        "progress_current": int(max(completed_steps, 0)),
        "progress_total": int(max(total_steps, 0)),
        "progress_pct": _calc_progress_pct(completed_steps, total_steps),
        "current_step": message,
        "current_factory": current_factory,
        "current_target": current_target,
        "data_end_date": data_end_date,
    }
    if extra:
        status.update(extra)
    _write_json_atomic(STATUS_PATH, status)


# 가중치 업데이트 대상 목록을 만듭니다.
def _get_weight_update_targets(model_dict: dict[str, Any], factories: list[str]) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for factory in factories:
        factory_specs = model_dict.get(factory)
        if not isinstance(factory_specs, dict):
            continue
        for target_name in list(factory_specs.keys()):
            if target_name in TARGET_SPECS:
                targets.append((factory, target_name))
    return targets


# 전체 재학습 대상 목록을 만듭니다.
def _get_full_retrain_targets(factories: list[str]) -> list[tuple[str, str]]:
    return [(factory, target_name) for factory in factories for target_name in TARGET_SPECS.keys()]


# 실제값과 예측값의 오차 비율을 계산합니다.
def mape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = 1.0) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100.0)


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

    constraints = {"type": "eq", "fun": lambda w: np.sum(w) - 1.0}
    bounds = [(0.0, 1.0)] * n_models
    init = np.ones(n_models, dtype=float) / float(n_models)
    res = minimize(objective, init, method="SLSQP", bounds=bounds, constraints=constraints)
    return res.x if res.success else init


# 모델을 학습합니다.
def train_models(X_tr: pd.DataFrame, y_tr: np.ndarray, n_estimators: int) -> list[Any]:
    cpu_n = max(1, (os.cpu_count() or 2) - 1)

    lgbm = LGBMRegressor(
        n_estimators=n_estimators,
        learning_rate=0.05,
        num_leaves=31,
        random_state=42,
        n_jobs=cpu_n,
    )
    lgbm.fit(X_tr, np.log1p(y_tr))

    xgb = XGBRegressor(
        n_estimators=n_estimators,
        learning_rate=0.05,
        max_depth=6,
        random_state=42,
        n_jobs=cpu_n,
    )
    xgb.fit(X_tr, np.log1p(y_tr))

    cat = CatBoostRegressor(
        iterations=n_estimators,
        learning_rate=0.05,
        depth=6,
        verbose=0,
        random_seed=42,
        thread_count=cpu_n,
    )
    cat.fit(X_tr, np.log1p(y_tr))

    return [lgbm, xgb, cat]


# 에너지 전체 데이터를 조회합니다.
def _fetch_energy_all(factory: str) -> pd.DataFrame:
    cols = ["date", "mix_prod_kg"] + [spec["db_col"] for spec in TARGET_SPECS.values()]
    col_sql = ", ".join(cols)
    query = f"""
        SELECT {col_sql}
        FROM energy_daily
        WHERE factory = %s
        ORDER BY date
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(query, conn, params=(factory,))
    finally:
        conn.close()
    return overlay_actual_production(df)


# to korean 스키마 관련 처리를 담당합니다.
def _to_korean_schema(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "date": "날짜",
        "mix_prod_kg": "믹스생산량[kg]",
    }
    for spec in TARGET_SPECS.values():
        rename_map[spec["db_col"]] = spec["model_col"]
    out = df.rename(columns=rename_map).copy()
    out["날짜"] = pd.to_datetime(out["날짜"], errors="coerce")
    out = out.dropna(subset=["날짜"])

    for col in ["믹스생산량[kg]"] + [spec["model_col"] for spec in TARGET_SPECS.values()]:
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

    return out


# 날씨 데이터를 합칩니다.
def _merge_weather(factory: str, df: pd.DataFrame) -> pd.DataFrame:
    station = STATION_MAP.get(factory)
    df_w = load_weather_station_excel(station) if station else pd.DataFrame(columns=["날짜"] + WEATHER_COLS)
    if df_w.empty:
        out = df.copy()
        for col in WEATHER_COLS:
            out[col] = np.nan
        return fill_weather_gaps(out, WEATHER_COLS)

    out = df.merge(df_w, on="날짜", how="left")
    # 결측을 0으로 즉시 치환하지 않고 fill_weather_gaps로 시계열 보간을 적용합니다.
    return fill_weather_gaps(out, WEATHER_COLS)


# 영업일만 남깁니다.
def _filter_workdays(df: pd.DataFrame, holiday_set: set) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df["날짜"].apply(lambda x: is_workday(pd.Timestamp(x), holiday_set))].copy()


# 품목 코드 값을 일정한 형식으로 맞춥니다.
def _normalize_item_code(value: object) -> str:
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


# 재공품 피처 컬럼 데이터를 만듭니다.
def _build_wip_feature_col(item_code: str) -> str:
    return f"wip_{_normalize_item_code(item_code)}"


# 피처 컬럼에서 품목 코드를 복원합니다.
def _derive_item_code_from_feature_col(feature_col: str) -> str:
    text = str(feature_col).strip()
    if text in (BOTTLING_AGG_FEATURE, N_ACTIVE_SKUS_FEATURE):
        return ""
    if text.startswith("wip_"):
        return _normalize_item_code(text[4:])
    return _normalize_item_code(text)


# 스펙 재공품 품목 코드 값을 가져옵니다.
def _get_spec_wip_item_codes(spec: dict[str, Any] | None) -> list[str]:
    spec_dict = spec if isinstance(spec, dict) else {}

    direct = [_normalize_item_code(code) for code in (spec_dict.get("wip_item_codes") or [])]
    direct = [code for code in direct if code]
    if direct:
        return direct

    derived = [_derive_item_code_from_feature_col(col) for col in (spec_dict.get("wip_feature_cols") or [])]
    return [code for code in derived if code]


# 대상별 재공품 품목 코드 값을 확정합니다.
def _get_target_wip_item_codes(
    factory: str,
    target_name: str,
    spec: dict[str, Any] | None,
    training_profile: str,
) -> list[str]:
    codes = _get_spec_wip_item_codes(spec)
    if codes:
        return codes

    if training_profile == "wip_shortlist":
        derived = [_normalize_item_code(code) for code in get_shortlist_item_codes(factory, target_name)]
        return [code for code in derived if code]

    return []


# 재공품 시트 데이터를 불러옵니다.
def _load_wip_sheet(factory: str) -> pd.DataFrame:
    if factory in _WIP_SHEET_CACHE:
        return _WIP_SHEET_CACHE[factory].copy()

    if not PATH_WIP_SUMMARY.exists():
        raise FileNotFoundError(f"WIP summary file not found: {PATH_WIP_SUMMARY}")

    # 한글 공장명 시트가 없으면 LEGACY F-코드 시트로 폴백.
    sheet_name = resolve_plant_sheet(PATH_WIP_SUMMARY, factory)
    if sheet_name is None:
        raise FileNotFoundError(f"WIP sheet not found for factory '{factory}': {PATH_WIP_SUMMARY}")

    try:
        df_wip = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet_name)
    except Exception as exc:
        raise FileNotFoundError(f"WIP sheet not found for factory '{factory}': {PATH_WIP_SUMMARY}") from exc

    if "날짜" not in df_wip.columns:
        raise ValueError(f"'날짜' column not found in WIP sheet: {factory}")

    rename_map: dict[object, str] = {}
    for col in df_wip.columns:
        if col == "날짜":
            continue
        rename_map[col] = _normalize_item_code(col)

    out = df_wip.rename(columns=rename_map).copy()
    out["날짜"] = pd.to_datetime(out["날짜"], errors="coerce")
    out = out.dropna(subset=["날짜"]).sort_values("날짜").reset_index(drop=True)
    _WIP_SHEET_CACHE[factory] = out
    return out.copy()


# 대상별 재공품 피처 데이터를 합칩니다.
# shortlist 개별 컬럼 + 보틀링 EA 합계(log1p) aggregate 를 함께 노출합니다.
def _merge_target_wip_features(
    factory: str,
    df: pd.DataFrame,
    shortlist_codes: list[str],
    bottling_ea_codes: list[str],
) -> tuple[pd.DataFrame, list[str], pd.Timestamp]:
    if not shortlist_codes and not bottling_ea_codes:
        return df.copy(), [], TRAIN_START

    df_wip = _load_wip_sheet(factory)
    # 로드 대상 = shortlist + bottling EA (중복 제거)
    codes_to_load = list(dict.fromkeys(list(shortlist_codes) + list(bottling_ea_codes)))

    for item_code in codes_to_load:
        if item_code not in df_wip.columns:
            df_wip[item_code] = 0.0
        df_wip[item_code] = pd.to_numeric(df_wip[item_code], errors="coerce").fillna(0.0)

    wip_subset = df_wip[["날짜", *codes_to_load]].copy()
    wip_subset = wip_subset.rename(columns={code: _build_wip_feature_col(code) for code in codes_to_load})

    merged = df.merge(wip_subset, on="날짜", how="left")
    all_wip_cols = [_build_wip_feature_col(code) for code in codes_to_load]
    for col in all_wip_cols:
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    # shortlist 개별 컬럼만 feature_cols 에 노출
    feature_cols = [_build_wip_feature_col(code) for code in shortlist_codes]

    # 보틀링 EA 합계 (log1p) aggregate
    if bottling_ea_codes:
        merged[BOTTLING_AGG_FEATURE] = compute_bottling_aggregate(merged, bottling_ea_codes)
        if BOTTLING_AGG_FEATURE not in feature_cols:
            feature_cols.append(BOTTLING_AGG_FEATURE)

    # 활성 SKU 카운트 — WIP 시트 전체에서 계산. 광주는 제외 (modeling_v5.3.py 정책 참조).
    if factory not in N_ACTIVE_SKUS_EXCLUDE_PLANTS:
        n_active_df = load_n_active_skus_series(PATH_WIP_SUMMARY, factory)
        if not n_active_df.empty:
            merged = merged.merge(n_active_df, on="날짜", how="left")
            merged[N_ACTIVE_SKUS_FEATURE] = pd.to_numeric(
                merged[N_ACTIVE_SKUS_FEATURE], errors="coerce"
            ).fillna(0.0)
            if N_ACTIVE_SKUS_FEATURE not in feature_cols:
                feature_cols.append(N_ACTIVE_SKUS_FEATURE)

    return merged, feature_cols, max(TRAIN_START, WIP_AVAILABLE_START)


# 대상별 학습용 데이터프레임을 준비합니다.
def _build_target_training_frame(
    factory: str,
    target_name: str,
    spec: dict[str, Any] | None,
    training_profile: str,
    df_source: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str], list[str], list[str], pd.Timestamp]:
    shortlist_codes = _get_target_wip_item_codes(factory, target_name, spec, training_profile)
    bottling_codes = [_normalize_item_code(c) for c in get_bottling_ea_codes(factory)]
    bottling_codes = [c for c in bottling_codes if c]

    if not shortlist_codes and not bottling_codes:
        return df_source.copy(), [], [], [], TRAIN_START

    df_model, wip_feature_cols, effective_train_start = _merge_target_wip_features(
        factory, df_source, shortlist_codes, bottling_codes,
    )
    # spec 에 저장할 raw 코드 = shortlist + bottling (중복 제거)
    wip_item_codes_all = list(dict.fromkeys(list(shortlist_codes) + list(bottling_codes)))
    return df_model, wip_feature_cols, wip_item_codes_all, bottling_codes, effective_train_start


# 스펙에 현재 피처 구성을 기록합니다.
def _apply_spec_metadata(
    spec: dict[str, Any],
    wip_feature_cols: list[str],
    wip_item_codes: list[str],
    bottling_ea_codes: list[str],
    effective_train_start: pd.Timestamp,
) -> dict[str, Any]:
    spec["feature_spec_version"] = WIP_FEATURE_SPEC_VERSION if wip_feature_cols else BASE_FEATURE_SPEC_VERSION
    spec["effective_train_start"] = effective_train_start.strftime("%Y-%m-%d")

    if wip_feature_cols:
        spec["wip_item_codes"] = list(wip_item_codes)
        spec["wip_feature_cols"] = list(wip_feature_cols)
        spec["wip_bottling_ea_codes"] = list(bottling_ea_codes)
        spec["wip_available_start"] = WIP_AVAILABLE_START.strftime("%Y-%m-%d")
    else:
        spec.pop("wip_item_codes", None)
        spec.pop("wip_bottling_ea_codes", None)
        spec.pop("wip_feature_cols", None)
        spec.pop("wip_available_start", None)

    return spec


# 공장별 가중치만 다시 계산합니다.
def _weight_update_for_factory(
    model_dict: dict[str, Any],
    factory: str,
    window_bdays: int,
    holiday_set: set,
    df_source: pd.DataFrame,
    training_profile: str,
    progress_callback: Any | None = None,
) -> bool:
    if factory not in model_dict:
        return False

    updated_any = False
    df_src = _filter_workdays(df_source, holiday_set)
    df_src = _merge_weather(factory, df_src)

    for target_name in list(model_dict[factory].keys()):
        if target_name not in TARGET_SPECS:
            continue

        if progress_callback is not None:
            progress_callback(
                phase="weights",
                message=f"가중치 업데이트 중: {factory} / {target_name}",
                current_factory=factory,
                current_target=target_name,
                advance=False,
            )

        step_message = f"가중치 업데이트 완료: {factory} / {target_name}"
        try:
            spec = model_dict[factory].get(target_name)
            if not isinstance(spec, dict):
                step_message = f"가중치 업데이트 건너뜀: {factory} / {target_name}"
                continue

            ycol = spec.get("target_col") or TARGET_SPECS[target_name]["model_col"]
            df_model, wip_feature_cols, wip_item_codes, bottling_ea_codes, effective_train_start = _build_target_training_frame(
                factory=factory,
                target_name=target_name,
                spec=spec,
                training_profile=training_profile,
                df_source=df_src,
            )

            d_full = detect_special_events(make_features(df_model, ycol, holiday_set))
            if "is_special_event" in d_full.columns:
                d_clean = d_full[~d_full["is_special_event"]].copy()
            else:
                d_clean = d_full.copy()

            d_clean = d_clean[d_clean["date"] >= effective_train_start].copy()
            if len(d_clean) < max(window_bdays, 20):
                step_message = f"가중치 업데이트 건너뜀: {factory} / {target_name}"
                continue

            d_clean = d_clean.sort_values("date").reset_index(drop=True)
            d_val = d_clean.tail(window_bdays).copy()
            y_val = d_val[ycol].to_numpy(dtype=float)

            X_all = build_feature_frame(d_clean, wip_feature_cols)
            X_val_all = X_all.tail(window_bdays).copy()

            models = list(spec.get("models") or [])
            if not models:
                step_message = f"가중치 업데이트 건너뜀: {factory} / {target_name}"
                continue

            m_types = list(spec.get("m_types") or ["M1", "M2", "M3", "M4"])
            features_map: dict[str, list[str]] = spec.get("features") or {}

            val_preds: list[np.ndarray] = []
            for idx, mdl in enumerate(models):
                model_type = m_types[idx // 3] if m_types else "M1"
                feat_list = features_map.get(model_type) or list(X_val_all.columns)
                X_val = X_val_all.reindex(columns=feat_list, fill_value=0.0)
                try:
                    val_preds.append(np.expm1(mdl.predict(X_val)))
                except Exception:
                    val_preds.append(np.zeros_like(y_val, dtype=float))

            new_w = compute_optimal_weights(val_preds, y_val)
            if new_w.size == 0:
                step_message = f"가중치 업데이트 건너뜀: {factory} / {target_name}"
                continue

            spec["weights"] = new_w
            spec = _apply_spec_metadata(spec, wip_feature_cols, wip_item_codes, bottling_ea_codes, effective_train_start)
            model_dict[factory][target_name] = spec
            updated_any = True
        finally:
            if progress_callback is not None:
                progress_callback(
                    phase="weights",
                    message=step_message,
                    current_factory=factory,
                    current_target=target_name,
                    advance=True,
                )

    return updated_any


# 공장별 전체 모델을 다시 학습합니다.
def _full_retrain_for_factory(
    model_dict: dict[str, Any],
    factory: str,
    window_bdays: int,
    holiday_set: set,
    df_source: pd.DataFrame,
    n_estimators: int,
    training_profile: str,
    progress_callback: Any | None = None,
) -> bool:
    df_src = _filter_workdays(df_source, holiday_set)
    df_src = _merge_weather(factory, df_src)

    previous_specs = model_dict.get(factory, {}) if isinstance(model_dict.get(factory, {}), dict) else {}
    model_dict.setdefault(factory, {})

    updated_any = False
    m_types = ["M1", "M2", "M3", "M4"]

    for target_name in TARGET_SPECS.keys():
        if progress_callback is not None:
            progress_callback(
                phase="full",
                message=f"전체 재학습 중: {factory} / {target_name}",
                current_factory=factory,
                current_target=target_name,
                advance=False,
            )

        step_message = f"전체 재학습 완료: {factory} / {target_name}"
        try:
            previous_spec = previous_specs.get(target_name, {}) if isinstance(previous_specs, dict) else {}
            ycol = TARGET_SPECS[target_name]["model_col"]

            df_model, wip_feature_cols, wip_item_codes, bottling_ea_codes, effective_train_start = _build_target_training_frame(
                factory=factory,
                target_name=target_name,
                spec=previous_spec,
                training_profile=training_profile,
                df_source=df_src,
            )

            d_full = detect_special_events(make_features(df_model, ycol, holiday_set))
            if "is_special_event" in d_full.columns:
                d_clean = d_full[~d_full["is_special_event"]].copy()
            else:
                d_clean = d_full.copy()

            d_clean = d_clean[d_clean["date"] >= effective_train_start].copy()
            d_clean = d_clean.sort_values("date").reset_index(drop=True)

            if len(d_clean) < (window_bdays + 120):
                step_message = f"전체 재학습 건너뜀: {factory} / {target_name}"
                continue

            y = d_clean[ycol].to_numpy(dtype=float)
            ytr = y[:-window_bdays]
            yva = y[-window_bdays:]
            if len(ytr) == 0 or len(yva) == 0:
                step_message = f"전체 재학습 건너뜀: {factory} / {target_name}"
                continue

            X_all = build_feature_frame(d_clean, wip_feature_cols)

            val_preds: list[np.ndarray] = []
            plant_target_models: list[Any] = []
            features_used: dict[str, list[str]] = {}

            for model_type in m_types:
                X_sub = select_features(X_all, model_type)
                features_used[model_type] = list(X_sub.columns)

                Xtr = X_sub.iloc[:-window_bdays].copy()
                Xva = X_sub.iloc[-window_bdays:].copy()
                models = train_models(Xtr, ytr, n_estimators=n_estimators)

                for mdl in models:
                    plant_target_models.append(mdl)
                    val_preds.append(np.expm1(mdl.predict(Xva)))

            opt_w = compute_optimal_weights(val_preds, yva)
            if opt_w.size == 0:
                step_message = f"전체 재학습 건너뜀: {factory} / {target_name}"
                continue

            spec = {
                "models": plant_target_models,
                "weights": opt_w,
                "m_types": m_types,
                "features": features_used,
                "target_col": ycol,
            }
            spec = _apply_spec_metadata(spec, wip_feature_cols, wip_item_codes, bottling_ea_codes, effective_train_start)
            model_dict[factory][target_name] = spec
            updated_any = True
        finally:
            if progress_callback is not None:
                progress_callback(
                    phase="full",
                    message=step_message,
                    current_factory=factory,
                    current_target=target_name,
                    advance=True,
                )

    return updated_any


# 현재 활성 모델 파일 경로를 고릅니다.
def _get_active_model_output_path(registry: dict[str, Any]) -> Path:
    model_key = str(registry.get("active_model_key") or "").strip()
    return build_versioned_model_path(model_key or "v5")


# 모델 artifact 데이터를 저장합니다.
def _write_model_artifact(
    model_dict: dict[str, Any],
    registry: dict[str, Any],
    *,
    metrics: dict[str, Any],
    split: dict[str, Any],
    data_end_date: str | None,
    train_end_date: str | None,
) -> tuple[Path, dict[str, Any]]:
    out_path = _get_active_model_output_path(registry)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    joblib.dump(model_dict, tmp_path, compress=3)
    os.replace(tmp_path, out_path)
    artifact = build_model_artifact_metadata(
        out_path,
        model_key=str(registry.get("active_model_key") or "v5"),
        training_profile=str(registry.get("training_profile") or "baseline"),
        metrics=metrics,
        split=split,
        data_end_date=data_end_date,
        train_end_date=train_end_date,
        git_commit=get_git_commit(),
    )
    return out_path, artifact


# 등록 정보 값을 갱신합니다.
def _update_registry(
    registry: dict[str, Any],
    active_model_path: Path,
    artifact_metadata: dict[str, Any] | None,
    weights_updated_at: str,
    full_trained_at: str | None,
    data_end_date: str | None,
    train_end_date: str | None = None,
) -> dict[str, Any]:
    reg = dict(registry)
    if artifact_metadata:
        reg = attach_model_artifact(reg, artifact_metadata, active=True)
    else:
        reg["active_model_path"] = to_project_relative_path_str(active_model_path)
    reg["weights_updated_at"] = weights_updated_at
    if full_trained_at is not None:
        reg["full_trained_at"] = full_trained_at
    if data_end_date is not None:
        reg["data_end_date_global"] = data_end_date
    if train_end_date is not None:
        reg["train_end_date_global"] = train_end_date
    return write_model_registry(reg)


# iso 문자열을 datetime으로 바꿉니다.
def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


# =============================================================================
# 전체 분위수(v5.3) 재학습 — energy_daily 기반, 롤링 영업일 split, 후보 검증 후 교체
# =============================================================================
# 파일을 조용히 삭제합니다(존재하지 않아도 무시).
def _safe_delete(path: Path | str) -> None:
    try:
        Path(path).unlink(missing_ok=True)  # type: ignore[call-arg]
    except Exception:
        pass


# 정렬된 영업일 데이터에 롤링 split mask 를 만듭니다.
#   - test  : 최근 test_bdays 행
#   - valid : 그 직전 valid_bdays 행 (검증/CQR)
#   - train : 나머지 이전 데이터
# 데이터가 부족하면 None 을 반환합니다.
def _rolling_split_masks(
    d: pd.DataFrame,
    test_bdays: int,
    valid_bdays: int,
    min_train_rows: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]] | None:
    n = len(d)
    if n < test_bdays + valid_bdays + min_train_rows:
        return None

    idx = np.arange(n)
    test_lo = n - test_bdays
    val_lo = test_lo - valid_bdays

    mtr = idx < val_lo
    mva = (idx >= val_lo) & (idx < test_lo)
    mte = idx >= test_lo

    dates = pd.to_datetime(d["date"]).reset_index(drop=True)
    split = {
        "split_mode": "rolling_bdays",
        "test_bdays": int(test_bdays),
        "valid_bdays": int(valid_bdays),
        "train_start": dates.iloc[0].strftime("%Y-%m-%d"),
        "train_end": dates.iloc[val_lo - 1].strftime("%Y-%m-%d"),
        "valid_start": dates.iloc[val_lo].strftime("%Y-%m-%d"),
        "valid_end": dates.iloc[test_lo - 1].strftime("%Y-%m-%d"),
        "test_start": dates.iloc[test_lo].strftime("%Y-%m-%d"),
        "test_end": dates.iloc[-1].strftime("%Y-%m-%d"),
    }
    return mtr, mva, mte, split


# 분위수 spec 에 피처/분할/재학습 메타데이터를 기록합니다(modeling_v5.3.py 와 동일 스키마).
def _apply_quantile_spec_metadata(
    spec: dict[str, Any],
    shortlist_codes: list[str],
    bottling_codes: list[str],
    wip_feature_cols: list[str],
    effective_train_start: pd.Timestamp,
    split: dict[str, Any],
) -> dict[str, Any]:
    wip_item_codes_all = list(dict.fromkeys(list(shortlist_codes) + list(bottling_codes)))
    spec["feature_spec_version"] = (
        QUANTILE_WIP_FEATURE_SPEC_VERSION if wip_feature_cols else QUANTILE_BASE_FEATURE_SPEC_VERSION
    )
    spec["effective_train_start"] = effective_train_start.strftime("%Y-%m-%d")
    spec["split_mode"] = "rolling_bdays"
    spec["split"] = split
    spec["retrain_source"] = "web_worker_full_quantile"

    if wip_feature_cols:
        spec["wip_item_codes"] = wip_item_codes_all
        spec["wip_shortlist_codes"] = list(shortlist_codes)
        spec["wip_bottling_ea_codes"] = list(bottling_codes)
        spec["wip_feature_cols"] = list(wip_feature_cols)
        spec["wip_available_start"] = WIP_AVAILABLE_START.strftime("%Y-%m-%d")
    else:
        for key in (
            "wip_item_codes",
            "wip_shortlist_codes",
            "wip_bottling_ea_codes",
            "wip_feature_cols",
            "wip_available_start",
        ):
            spec.pop(key, None)
    return spec


# 재로드한 후보 모델의 구조·무결성·샘플 예측을 검증합니다(실패 시 예외).
def _validate_quantile_candidate(
    model_dict: dict[str, Any],
    probes: dict[tuple[str, str], dict[str, Any]],
    factories: list[str],
    targets: list[str],
) -> None:
    errors: list[str] = []
    for factory in factories:
        specs = model_dict.get(factory)
        if not isinstance(specs, dict):
            errors.append(f"공장 spec 누락: {factory}")
            continue
        for target_name in targets:
            spec = specs.get(target_name)
            if not isinstance(spec, dict):
                errors.append(f"대상 spec 누락: {factory}/{target_name}")
                continue

            errors.extend(
                f"{factory}/{target_name}: {e}" for e in validate_quantile_spec_structure(spec)
            )

            probe = probes.get((factory, target_name))
            probe_X = probe.get("probe_X") if probe else None
            if probe_X is None or len(probe_X) == 0:
                errors.append(f"{factory}/{target_name}: 샘플 예측용 테스트 행 없음")
                continue
            try:
                p05, p50, p95 = predict_quantile_probe(spec, probe_X)
            except Exception as exc:
                errors.append(f"{factory}/{target_name}: 샘플 예측 실패 ({exc})")
                continue
            if not all(np.isfinite([p05, p50, p95])):
                errors.append(f"{factory}/{target_name}: 샘플 예측이 유한값이 아님")
            elif not (p05 <= p50 <= p95):
                errors.append(
                    f"{factory}/{target_name}: 단조성 위반 (P05={p05:.2f}, P50={p50:.2f}, P95={p95:.2f})"
                )

    if errors:
        raise RuntimeError("후보 모델 검증 실패: " + "; ".join(errors[:20]))


# 성능 보고서(sidecar JSON)를 저장합니다.
def _write_perf_report(
    model_path: Path,
    results_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    split: dict[str, Any],
    run_id: str | None,
) -> None:
    report = {
        "generated_at": _now_iso(),
        "run_id": run_id,
        "model_file": model_path.name,
        "split": split,
        "summary": summary,
        "rows": results_rows,
    }
    try:
        _write_json_atomic(perf_report_path_for(model_path), report)
    except Exception as exc:
        print(f"[v5] perf report 저장 실패(무시): {exc}")


# v5.3 전체 분위수 재학습을 실행하고 검증 통과 시 활성 모델을 교체합니다.
def _run_full_quantile_retrain(
    *,
    registry: dict[str, Any],
    started_at: str,
    trigger_mode: str,
    run_id: str | None,
    progress_state: dict[str, Any],
    progress_callback: Any,
) -> int:
    test_bdays = _env_int("V5_QUANTILE_TEST_BDAYS", 30)
    valid_bdays = _env_int("V5_QUANTILE_VALID_BDAYS", 60)
    n_estimators = _env_int("V5_FULL_RETRAIN_N_ESTIMATORS", 3000)
    min_train_rows = _env_int("V5_QUANTILE_MIN_TRAIN_ROWS", 100)
    retention_keep = _env_int("V5_MODEL_ARTIFACT_RETENTION_KEEP", MODEL_ARTIFACT_RETENTION_KEEP)

    model_key = str(registry.get("active_model_key") or "v5.3")
    training_profile = "quantile_wip"
    factories = list(STATION_MAP.keys())
    targets = list(TARGET_SPECS.keys())

    holiday_set = load_holidays_excel()

    progress_state["total"] = len(factories) * len(targets) * MODELS_PER_QUANTILE
    base_extra = {
        "run_id": run_id,
        "mode": "full_quantile",
        "split_mode": "rolling_bdays",
        "test_bdays": test_bdays,
        "valid_bdays": valid_bdays,
    }
    progress_state["status_extra"] = dict(base_extra)

    progress_callback(phase="prepare", message="전체 분위수 재학습 대상 계산", advance=False)

    candidate: dict[str, Any] = {}
    probes: dict[tuple[str, str], dict[str, Any]] = {}
    results_rows: list[dict[str, Any]] = []
    data_end_dates: list[pd.Timestamp] = []
    train_end_dates: list[pd.Timestamp] = []
    last_split: dict[str, Any] | None = None

    for factory in factories:
        print(f"[v5] full quantile retrain: {factory}")
        df_all = _to_korean_schema(_fetch_energy_all(factory))
        if df_all.empty:
            raise RuntimeError(f"energy_daily 데이터가 비어 있습니다: {factory}")
        data_end_dates.append(df_all["날짜"].max())
        progress_state["data_end_date"] = pd.Timestamp(max(data_end_dates)).strftime("%Y-%m-%d")

        df_src = _merge_weather(factory, _filter_workdays(df_all, holiday_set))
        candidate.setdefault(factory, {})

        for target_name in targets:
            ycol = TARGET_SPECS[target_name]["model_col"]

            shortlist_codes = [
                c for c in (_normalize_item_code(x) for x in get_shortlist_item_codes(factory, target_name)) if c
            ]
            bottling_codes = [
                c for c in (_normalize_item_code(x) for x in get_bottling_ea_codes(factory)) if c
            ]

            if shortlist_codes or bottling_codes:
                df_model, wip_feature_cols, effective_train_start = _merge_target_wip_features(
                    factory, df_src, shortlist_codes, bottling_codes,
                )
            else:
                df_model, wip_feature_cols, effective_train_start = df_src.copy(), [], TRAIN_START

            d_full = detect_special_events(make_features(df_model, ycol, holiday_set))
            if "is_special_event" in d_full.columns:
                d_clean = d_full[~d_full["is_special_event"]].copy()
            else:
                d_clean = d_full.copy()
            d_clean = d_clean[d_clean["date"] >= effective_train_start].sort_values("date").reset_index(drop=True)

            split_result = _rolling_split_masks(d_clean, test_bdays, valid_bdays, min_train_rows)
            if split_result is None:
                need = test_bdays + valid_bdays + min_train_rows
                raise RuntimeError(
                    f"학습 데이터가 부족합니다: {factory}/{target_name} "
                    f"(필요≥{need}영업일, 확보={len(d_clean)})"
                )
            mtr, mva, mte, split = split_result
            last_split = split
            train_end_dates.append(pd.Timestamp(split["train_end"]))

            def _engine_cb(model_type: str, q: float, *, _f=factory, _t=target_name, _split=split) -> None:
                progress_state["status_extra"] = {
                    **base_extra,
                    "current_model_type": model_type,
                    "current_quantile": float(q),
                    "split": _split,
                }
                progress_callback(
                    phase="full_quantile",
                    message=f"분위수 학습: {_f}/{_t} [{model_type} q{q:g}]",
                    current_factory=_f,
                    current_target=_t,
                    advance=True,
                )

            result = train_plant_target_quantile(
                d_clean,
                ycol,
                wip_feature_cols,
                mtr,
                mva,
                mte,
                n_estimators=n_estimators,
                progress_cb=_engine_cb,
            )
            if result is None:
                raise RuntimeError(f"분위수 학습 실패(가중치 최적화 불가): {factory}/{target_name}")

            spec = _apply_quantile_spec_metadata(
                result.spec, shortlist_codes, bottling_codes, wip_feature_cols, effective_train_start, split,
            )
            candidate[factory][target_name] = spec
            probes[(factory, target_name)] = {"probe_X": result.probe_X, "probe_y": result.probe_y}
            results_rows.append(
                {
                    "plant": factory,
                    "target": target_name,
                    **{k: float(v) for k, v in result.metrics.items()},
                    "wip_feature_count": int(len(wip_feature_cols)),
                    "effective_train_start": effective_train_start.strftime("%Y-%m-%d"),
                }
            )

    # 후보 완전성 — 15개 (공장×대상) spec 모두 존재해야 함.
    missing = [(f, t) for f in factories for t in targets if t not in candidate.get(f, {})]
    if missing:
        raise RuntimeError(f"재학습 후보에 누락된 대상이 있습니다: {missing}")

    # 후보를 버전 경로에 임시 저장 → 다시 로드 → 검증.
    candidate_path = build_versioned_model_path(model_key)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = candidate_path.with_suffix(candidate_path.suffix + ".tmp")
    joblib.dump(candidate, tmp_path, compress=3)
    os.replace(tmp_path, candidate_path)

    progress_state["status_extra"] = {**base_extra, "candidate_path": str(candidate_path)}
    progress_callback(phase="validate", message="후보 모델 재로드 후 검증", advance=False)

    try:
        reloaded = joblib.load(candidate_path)
        _validate_quantile_candidate(reloaded, probes, factories, targets)
    except Exception:
        # 검증 실패 → 후보 삭제. 기존 활성 모델·레지스트리·학습일은 그대로 유지.
        _safe_delete(candidate_path)
        _safe_delete(perf_report_path_for(candidate_path))
        raise

    # 검증 성공 → 성능 보고서 + artifact + 레지스트리 원자적 교체.
    progress_callback(phase="finalize", message="검증 통과 — 모델 활성화 중", advance=False)

    perf_df = pd.DataFrame(results_rows)
    metric_cols = [
        "MAPE_p50", "Pinball_p05", "Pinball_p50", "Pinball_p95", "PICP_90", "MPIW_90", "Val_PICP_90",
    ]
    metrics_summary = summarize_metric_frame(perf_df, metric_cols)

    data_end = pd.Timestamp(max(data_end_dates)).strftime("%Y-%m-%d") if data_end_dates else None
    train_end = pd.Timestamp(max(train_end_dates)).strftime("%Y-%m-%d") if train_end_dates else None

    split_global = dict(last_split or {})
    split_global.update({"train_end": train_end, "test_bdays": test_bdays, "valid_bdays": valid_bdays})

    metrics_meta = {
        "mode": "full_quantile",
        "trained_factories": factories,
        "target_count": len(factories) * len(targets),
        "n_estimators": n_estimators,
        "per_target": metrics_summary,
    }
    artifact = build_model_artifact_metadata(
        candidate_path,
        model_key=model_key,
        training_profile=training_profile,
        metrics=metrics_meta,
        split=split_global,
        data_end_date=data_end,
        train_end_date=train_end,
        git_commit=get_git_commit(),
        post_processing=dict(RETRAIN_POST_PROCESSING_DEFAULTS),
        schema_version=2,
    )

    _write_perf_report(candidate_path, results_rows, metrics_summary, split_global, run_id)

    now = _now_iso()
    reg = attach_model_artifact(dict(registry), artifact, active=True)
    reg["weights_updated_at"] = now
    reg["full_trained_at"] = now
    reg["data_end_date_global"] = data_end
    reg["train_end_date_global"] = train_end
    reg["post_processing"] = dict(RETRAIN_POST_PROCESSING_DEFAULTS)
    normalized = write_model_registry(reg)

    # 보존 정책 — 활성 + 최근 이전 모델만 유지.
    deleted = cleanup_model_artifacts(model_key, keep=retention_keep, active_path=candidate_path)
    if deleted:
        print(f"[v5] cleaned old artifacts: {deleted}")

    _write_json_atomic(
        STATUS_PATH,
        {
            "status": "success",
            "started_at": started_at,
            "ended_at": now,
            "mode": "full_quantile",
            "run_id": run_id,
            "trigger_mode": trigger_mode,
            "phase": "complete",
            "message": "전체 분위수 재학습 완료 — 새 모델 활성화됨",
            "error": None,
            "new_model_path": str(candidate_path),
            "new_model_sha256": artifact.get("sha256"),
            "new_model_size_bytes": artifact.get("size_bytes"),
            "active_model_key": normalized.get("active_model_key"),
            "worker_pid": os.getpid(),
            "heartbeat_at": now,
            "lock_path": str(LOCK_PATH),
            "progress_current": int(progress_state["total"] or 0),
            "progress_total": int(progress_state["total"] or 0),
            "progress_pct": 100.0,
            "current_step": "모델 활성화 완료",
            "current_factory": None,
            "current_target": None,
            "data_end_date": data_end,
            "split": split_global,
            "post_processing": dict(RETRAIN_POST_PROCESSING_DEFAULTS),
        },
    )
    print(f"[v5] full quantile retrain success → {candidate_path}")
    return 0


# 이 파일의 전체 실행 흐름을 시작합니다.
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factories", type=str, default="")
    args = parser.parse_args()

    changed_factories = [f.strip() for f in (args.factories or "").split(",") if f.strip()]

    window_bdays = int(os.getenv("V5_WEIGHT_UPDATE_WINDOW_BDAYS", "60"))
    full_interval_hours = int(os.getenv("V5_FULL_RETRAIN_MIN_INTERVAL_HOURS", "168"))
    n_estimators = int(os.getenv("V5_FULL_RETRAIN_N_ESTIMATORS", "3000"))

    status_started = read_json(STATUS_PATH, {})
    started_at = status_started.get("started_at") or _now_iso()
    trigger_mode = str(status_started.get("trigger_mode") or "manual")
    run_id = status_started.get("run_id")
    heartbeat_stop = start_training_heartbeat(
        worker_pid=os.getpid(),
        trigger_mode=trigger_mode,
        started_at=started_at,
        run_id=run_id,
    )
    progress_state: dict[str, Any] = {
        "total": 0,
        "completed": 0,
        "phase": "prepare",
        "message": "재학습 준비 중",
        "current_factory": None,
        "current_target": None,
        "active_model_key": None,
        "data_end_date": None,
        "run_id": run_id,
        # full_quantile 경로에서 노출하는 추가 상태(현재 모델 유형/분위수/경로/분할).
        "status_extra": {"run_id": run_id},
    }

    def progress_callback(
        *,
        phase: str,
        message: str,
        current_factory: str | None = None,
        current_target: str | None = None,
        advance: bool = False,
    ) -> None:
        if advance:
            progress_state["completed"] = min(
                int(progress_state["completed"]) + 1,
                int(progress_state["total"] or 0),
            )

        progress_state["phase"] = phase
        progress_state["message"] = message
        progress_state["current_factory"] = current_factory
        progress_state["current_target"] = current_target

        _write_running_status(
            started_at=started_at,
            trigger_mode=trigger_mode,
            total_steps=int(progress_state["total"] or 0),
            completed_steps=int(progress_state["completed"] or 0),
            message=message,
            phase=phase,
            active_model_key=progress_state.get("active_model_key"),
            current_factory=current_factory,
            current_target=current_target,
            data_end_date=progress_state.get("data_end_date"),
            extra=progress_state.get("status_extra"),
        )

    try:
        registry = load_model_registry(auto_create=True)
        progress_state["active_model_key"] = registry.get("active_model_key")
        # 직전 학습 기준 데이터 마지막 날짜를 우선 노출(현 학습 중에 갱신됨).
        progress_state["data_end_date"] = registry.get("data_end_date_global")
        training_profile = str(registry.get("training_profile") or "baseline")
        active_path = resolve_model_path(registry.get("active_model_path"), default=DEFAULT_MODEL_PATH)

        # v5.2/v5.3(분위수 회귀+CQR) 모델은 modeling_v5.3.py 와 동일한 공용 엔진으로
        # energy_daily 기반 전체 재학습을 수행한다(롤링 영업일 split, 후보 검증 후 원자적 교체).
        if training_profile == "quantile_wip":
            return _run_full_quantile_retrain(
                registry=registry,
                started_at=started_at,
                trigger_mode=trigger_mode,
                run_id=run_id,
                progress_state=progress_state,
                progress_callback=progress_callback,
            )

        if active_path.exists():
            print(f"[v5] loading active model: {active_path}")
            model_dict: dict[str, Any] = joblib.load(active_path)
        else:
            print(f"[v5] active model not found. full retrain will start from scratch: {active_path}")
            model_dict = {}

        active_model_missing = not active_path.exists() or not model_dict
        factories = changed_factories or list(model_dict.keys()) or list(STATION_MAP.keys())
        if active_model_missing:
            factories = list(STATION_MAP.keys())
        factories = [f for f in factories if f in STATION_MAP]

        holiday_set = load_holidays_excel()
        updated_any = False
        data_end_dates: list[pd.Timestamp] = []
        train_end_dates: list[pd.Timestamp] = []

        last_full = _parse_iso(registry.get("full_trained_at"))
        due_full = (
            active_model_missing
            or last_full is None
            or (datetime.now() - last_full > timedelta(hours=full_interval_hours))
        )

        weight_targets = _get_weight_update_targets(model_dict, factories) if model_dict else []
        full_targets = _get_full_retrain_targets(factories) if due_full else []
        progress_state["total"] = len(weight_targets) + len(full_targets)

        progress_callback(
            phase="prepare",
            message="재학습 대상 계산 완료",
            current_factory=None,
            current_target=None,
            advance=False,
        )

        if model_dict:
            for factory in factories:
                print(f"[v5] weight update: {factory}")
                df_all = _to_korean_schema(_fetch_energy_all(factory))
                if df_all.empty:
                    continue
                data_end_dates.append(df_all["날짜"].max())
                progress_state["data_end_date"] = pd.Timestamp(max(data_end_dates)).strftime("%Y-%m-%d")
                # 영업일/휴일 필터 후 60bday 직전 일자 = 학습 끝일 후보(검증 윈도우 직전).
                _df_wd = _filter_workdays(df_all, holiday_set).sort_values("날짜").reset_index(drop=True)
                if len(_df_wd) > window_bdays:
                    train_end_dates.append(pd.Timestamp(_df_wd["날짜"].iloc[-window_bdays - 1]))
                updated_any |= _weight_update_for_factory(
                    model_dict=model_dict,
                    factory=factory,
                    window_bdays=window_bdays,
                    holiday_set=holiday_set,
                    df_source=df_all,
                    training_profile=training_profile,
                    progress_callback=progress_callback,
                )

        did_full = False
        if due_full:
            for factory in factories:
                print(f"[v5] full retrain: {factory} ({training_profile})")
                df_all = _to_korean_schema(_fetch_energy_all(factory))
                if df_all.empty:
                    continue
                data_end_dates.append(df_all["날짜"].max())
                progress_state["data_end_date"] = pd.Timestamp(max(data_end_dates)).strftime("%Y-%m-%d")
                _df_wd = _filter_workdays(df_all, holiday_set).sort_values("날짜").reset_index(drop=True)
                if len(_df_wd) > window_bdays:
                    train_end_dates.append(pd.Timestamp(_df_wd["날짜"].iloc[-window_bdays - 1]))
                did_full |= _full_retrain_for_factory(
                    model_dict=model_dict,
                    factory=factory,
                    window_bdays=window_bdays,
                    holiday_set=holiday_set,
                    df_source=df_all,
                    n_estimators=n_estimators,
                    training_profile=training_profile,
                    progress_callback=progress_callback,
                )

        if updated_any or did_full:
            progress_callback(
                phase="finalize",
                message="모델 파일 저장 중",
                current_factory=None,
                current_target=None,
                advance=False,
            )
            data_end = None
            if data_end_dates:
                data_end = pd.Timestamp(max(data_end_dates)).strftime("%Y-%m-%d")
            train_end = None
            if train_end_dates:
                train_end = pd.Timestamp(max(train_end_dates)).strftime("%Y-%m-%d")
            update_mode = "full" if did_full else "weights"
            split = {
                "train_start": TRAIN_START.strftime("%Y-%m-%d"),
                "train_end": train_end,
                "weight_update_window_bdays": window_bdays,
                "full_retrain_min_interval_hours": full_interval_hours,
            }
            metrics = {
                "mode": update_mode,
                "changed_factories": changed_factories,
                "trained_factories": factories,
                "weight_target_count": len(weight_targets),
                "full_target_count": len(full_targets),
                "n_estimators": n_estimators if did_full else None,
            }
            out_path, artifact_metadata = _write_model_artifact(
                model_dict,
                registry,
                metrics=metrics,
                split=split,
                data_end_date=data_end,
                train_end_date=train_end,
            )
            weights_ts = _now_iso()
            full_ts = _now_iso() if did_full else None

            normalized_registry = _update_registry(
                registry=registry,
                active_model_path=out_path,
                artifact_metadata=artifact_metadata,
                weights_updated_at=weights_ts,
                full_trained_at=full_ts,
                data_end_date=data_end,
                train_end_date=train_end,
            )
            _write_json_atomic(
                STATUS_PATH,
                {
                    "status": "success",
                    "started_at": started_at,
                    "ended_at": _now_iso(),
                    "mode": update_mode,
                    "trigger_mode": trigger_mode,
                    "phase": "complete",
                    "message": "모델 업데이트 완료",
                    "error": None,
                    "new_model_path": str(out_path),
                    "new_model_sha256": artifact_metadata.get("sha256"),
                    "new_model_size_bytes": artifact_metadata.get("size_bytes"),
                    "active_model_key": normalized_registry.get("active_model_key"),
                    "worker_pid": os.getpid(),
                    "heartbeat_at": _now_iso(),
                    "lock_path": str(LOCK_PATH),
                    "progress_current": int(progress_state["total"] or 0),
                    "progress_total": int(progress_state["total"] or 0),
                    "progress_pct": 100.0,
                    "current_step": "모델 업데이트 완료",
                    "current_factory": None,
                    "current_target": None,
                    "data_end_date": data_end or progress_state.get("data_end_date"),
                },
            )
        else:
            _write_json_atomic(
                STATUS_PATH,
                {
                    "status": "success",
                    "started_at": started_at,
                    "ended_at": _now_iso(),
                    "mode": "noop",
                    "trigger_mode": trigger_mode,
                    "phase": "complete",
                    "message": "업데이트할 데이터가 부족하거나 변경사항이 없습니다.",
                    "error": None,
                    "new_model_path": None,
                    "active_model_key": registry.get("active_model_key"),
                    "worker_pid": os.getpid(),
                    "heartbeat_at": _now_iso(),
                    "lock_path": str(LOCK_PATH),
                    "progress_current": int(progress_state["total"] or 0),
                    "progress_total": int(progress_state["total"] or 0),
                    "progress_pct": 100.0,
                    "current_step": "업데이트 대상 없음",
                    "current_factory": None,
                    "current_target": None,
                    "data_end_date": progress_state.get("data_end_date") or registry.get("data_end_date_global"),
                },
            )

        return 0
    except Exception as e:
        _write_json_atomic(
            STATUS_PATH,
            {
                "status": "fail",
                "started_at": started_at,
                "ended_at": _now_iso(),
                "mode": "fail",
                "trigger_mode": trigger_mode,
                "phase": progress_state.get("phase"),
                "message": "학습 실패",
                "error": str(e),
                "new_model_path": None,
                "active_model_key": progress_state.get("active_model_key"),
                "worker_pid": os.getpid(),
                "heartbeat_at": _now_iso(),
                "lock_path": str(LOCK_PATH),
                "progress_current": int(progress_state["completed"] or 0),
                "progress_total": int(progress_state["total"] or 0),
                "progress_pct": _calc_progress_pct(
                    int(progress_state["completed"] or 0),
                    int(progress_state["total"] or 0),
                ),
                "current_step": progress_state.get("message"),
                "current_factory": progress_state.get("current_factory"),
                "current_target": progress_state.get("current_target"),
                "data_end_date": progress_state.get("data_end_date"),
                "run_id": progress_state.get("run_id"),
            },
        )
        print(f"[v5] ERROR: {e}")
        return 1
    finally:
        heartbeat_stop.set()
        release_training_lock(owner_pid=os.getpid(), owner_run_id=progress_state.get("run_id"))


if __name__ == "__main__":
    raise SystemExit(main())
