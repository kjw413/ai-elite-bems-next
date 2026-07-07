# 이 파일은 v5.3 분위수 회귀(Quantile Regression) + CQR 학습 엔진을 모아둔 공용 모듈입니다.
#
# 웹 재학습 워커(v5_retrain_worker.py)와 오프라인 스크립트(modeling_v5.3.py)가
# 동일한 학습 코드를 사용하도록 분리했습니다. 두 경로의 분위수 모델 구조·가중치·CQR
# 보정이 항상 일치해야 하므로, 분위수 학습 로직은 반드시 이 모듈만 수정합니다.
#
# 분할(split)은 호출 측이 boolean mask(mtr/mva/mte)로 넘깁니다.
#   - modeling_v5.3.py: 고정 날짜 기준(2021~2025 / 2026Q1 / 2026Q2 일부)
#   - v5_retrain_worker.py: 최신 energy_daily 기준 롤링 영업일(테스트 30 / 검증 60 / 학습 나머지)
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from catboost import CatBoostRegressor
from lightgbm import LGBMRegressor
from xgboost import XGBRegressor

from app.services.v5_common import build_feature_frame, resolve_cqr_margins, select_features

# 분위수 구성: 하한 / 중앙값 / 상한
QUANTILES: tuple[float, ...] = (0.05, 0.50, 0.95)
LOWER_Q, MEDIAN_Q, UPPER_Q = QUANTILES

# 분위수 모델 구조: M1·M3·M4 × (LGBM·XGB·CatBoost). 분위수당 9개, 3분위수 합계 27개.
#
# M2(HDD/CDD/생산) 제거(2026-06-25): 활성 모델 15개 대상의 학습 가중치 감사 결과
# M2의 P50 가중치 평균 share 0.036(80%에서 ≤0.05로 사실상 0). ablation 백테스트에서
# M2 제거 시 P50 MAPE Δ≤0.01%p(김해 용수는 −0.10%p로 오히려 개선, PICP 0.76→0.79).
# 즉 M2는 죽은 모델군이라 제거 → 대상당 모델 36→27(학습/추론 25% 경량화).
# (M3=기온 모델은 전력에서 실제 사용, M4=용수 밴드 기여 → 유지. 근거: diagnostics/_weight_audit.txt,
#  diagnostics/_ablation_mtypes.txt) 기존 4-type 모델은 spec.m_types 를 직접 읽어 하위호환.
DEFAULT_M_TYPES: tuple[str, ...] = ("M1", "M3", "M4")
ALGOS_PER_MODEL_TYPE = 3
MODELS_PER_QUANTILE = len(DEFAULT_M_TYPES) * ALGOS_PER_MODEL_TYPE  # 9
TOTAL_MODELS_PER_TARGET = MODELS_PER_QUANTILE * len(QUANTILES)     # 27

DEFAULT_N_ESTIMATORS = 3000

# 진행률 콜백: (model_type, quantile) 단위로 호출(대상당 12회).
ProgressCb = Callable[[str, float], None]


def operating_cpu_number() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


# =========================
# 평가 지표
# =========================
# 실제값과 예측값의 오차 비율을 계산합니다(P50 평가용).
def mape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = 1.0) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100.0)


# Pinball loss: 분위수 회귀의 본래 손실함수. q-quantile 예측의 일관성을 측정합니다.
def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, q: float) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    diff = y_true - y_pred
    return float(np.mean(np.maximum(q * diff, (q - 1.0) * diff)))


# PICP: 실측값이 [lower, upper] 구간 안에 들어온 비율(목표 ~ upper-lower).
def picp(y_true: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    inside = (y_true >= np.asarray(lower, dtype=float)) & (y_true <= np.asarray(upper, dtype=float))
    return float(np.mean(inside))


# MPIW: 평균 예측 구간 폭(절대단위). 좁을수록 모델이 날카로움.
def mpiw(lower: np.ndarray, upper: np.ndarray) -> float:
    return float(np.mean(np.asarray(upper, dtype=float) - np.asarray(lower, dtype=float)))


# =========================
# 모델 학습 엔진
# =========================
# 단일 분위수(alpha)에 대해 LGBM/XGB/CatBoost 3개 모델을 학습합니다.
# y는 log1p 공간에서 학습합니다(분위수는 단조변환 invariant이므로 안전).
def train_quantile_models(
    X_tr: pd.DataFrame,
    y_tr: np.ndarray,
    alpha: float,
    n_estimators: int = DEFAULT_N_ESTIMATORS,
    n_jobs: int | None = None,
) -> list[Any]:
    n_jobs = operating_cpu_number() if n_jobs is None else n_jobs
    y_log = np.log1p(y_tr)

    lgbm = LGBMRegressor(
        objective="quantile",
        alpha=alpha,
        n_estimators=n_estimators,
        learning_rate=0.05,
        num_leaves=31,
        random_state=42,
        n_jobs=n_jobs,
    )
    lgbm.fit(X_tr, y_log)

    # XGBoost 2.0+ 는 reg:quantileerror + quantile_alpha 를 지원합니다.
    xgb = XGBRegressor(
        objective="reg:quantileerror",
        quantile_alpha=alpha,
        n_estimators=n_estimators,
        learning_rate=0.05,
        max_depth=6,
        random_state=42,
        n_jobs=n_jobs,
        tree_method="hist",
    )
    xgb.fit(X_tr, y_log)

    cat = CatBoostRegressor(
        loss_function=f"Quantile:alpha={alpha}",
        iterations=n_estimators,
        learning_rate=0.05,
        depth=6,
        verbose=0,
        random_seed=42,
        thread_count=n_jobs,
    )
    cat.fit(X_tr, y_log)

    return [lgbm, xgb, cat]


# 한 분위수 q에 대한 모델 가중치를 pinball loss 기준으로 최적화합니다.
def compute_quantile_weights(
    val_preds: list[np.ndarray],
    y_val: np.ndarray,
    q: float,
) -> np.ndarray:
    n_models = len(val_preds)
    if n_models == 0:
        return np.array([])

    y_val = np.asarray(y_val, dtype=float)

    # 현재 가중치 조합의 pinball loss를 계산합니다.
    def objective(w: np.ndarray) -> float:
        pred = np.zeros_like(y_val, dtype=float)
        for idx in range(n_models):
            pred += w[idx] * val_preds[idx]
        return pinball_loss(y_val, pred, q)

    constraints = ({"type": "eq", "fun": lambda w: np.sum(w) - 1.0},)
    bounds = [(0.0, 1.0)] * n_models
    init = np.ones(n_models, dtype=float) / float(n_models)
    res = minimize(objective, init, method="SLSQP", bounds=bounds, constraints=constraints)
    return res.x if res.success else init


# row-wise 정렬로 P5 <= P50 <= P95 단조성을 강제합니다(quantile crossing 보정).
def enforce_quantile_monotonicity(
    pred_lower: np.ndarray,
    pred_median: np.ndarray,
    pred_upper: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    stacked = np.vstack(
        [
            np.asarray(pred_lower, dtype=float),
            np.asarray(pred_median, dtype=float),
            np.asarray(pred_upper, dtype=float),
        ]
    )
    sorted_stack = np.sort(stacked, axis=0)
    return sorted_stack[0], sorted_stack[1], sorted_stack[2]


# =========================
# (plant, target) 학습 결과
# =========================
@dataclass
class QuantileTrainResult:
    # 저장용 spec(models_by_q/weights_by_q/quantiles/m_types/features/target_col/cqr_q_hat).
    spec: dict[str, Any]
    # test 구간 평가 지표.
    metrics: dict[str, float]
    # 재로드 후 샘플 예측 검증용 — test 마지막 행의 전체 피처 프레임(1행).
    probe_X: pd.DataFrame = field(default=None)  # type: ignore[assignment]
    # 위 행의 실측값(가능하면).
    probe_y: float | None = None


# (plant, target) 단위로 분위수 모델을 학습하고 평가 지표를 반환합니다.
#
# 분할(mtr/mva/mte)은 호출 측에서 결정해 boolean mask로 전달합니다.
# progress_cb 가 주어지면 (model_type, quantile) 학습 1건 완료마다 호출됩니다.
def train_plant_target_quantile(
    d: pd.DataFrame,
    ycol: str,
    wip_cols: list[str],
    mtr: "pd.Series | np.ndarray",
    mva: "pd.Series | np.ndarray",
    mte: "pd.Series | np.ndarray",
    *,
    n_estimators: int = DEFAULT_N_ESTIMATORS,
    n_jobs: int | None = None,
    m_types: tuple[str, ...] | list[str] = DEFAULT_M_TYPES,
    progress_cb: ProgressCb | None = None,
) -> QuantileTrainResult | None:
    m_types = list(m_types)
    y = d[ycol].to_numpy(dtype=float)
    ytr = y[mtr]
    yva = y[mva]
    yte = y[mte]

    X_all = build_feature_frame(d, wip_cols)

    # 분위수별로 분리해 학습 → val/test 예측 적재.
    val_preds_by_q: dict[float, list[np.ndarray]] = {q: [] for q in QUANTILES}
    te_preds_by_q: dict[float, list[np.ndarray]] = {q: [] for q in QUANTILES}
    models_by_q: dict[float, list[Any]] = {q: [] for q in QUANTILES}
    features_used: dict[str, list[str]] = {}

    for model_type in m_types:
        X_sub = select_features(X_all, model_type)
        features_used[model_type] = list(X_sub.columns)

        Xtr = X_sub[mtr]
        Xva = X_sub[mva]
        Xte = X_sub[mte]

        for q in QUANTILES:
            models = train_quantile_models(Xtr, ytr, alpha=q, n_estimators=n_estimators, n_jobs=n_jobs)
            for model in models:
                models_by_q[q].append(model)
                val_preds_by_q[q].append(np.expm1(model.predict(Xva)))
                te_preds_by_q[q].append(np.expm1(model.predict(Xte)))
            if progress_cb is not None:
                progress_cb(model_type, q)

    # 분위수별 가중치 최적화 (pinball loss 기준).
    weights_by_q: dict[float, np.ndarray] = {}
    ensemble_val_by_q: dict[float, np.ndarray] = {}
    ensemble_te_by_q: dict[float, np.ndarray] = {}
    for q in QUANTILES:
        w = compute_quantile_weights(val_preds_by_q[q], yva, q)
        if w.size == 0:
            return None
        weights_by_q[q] = w
        ensemble_val_by_q[q] = sum(weight * pred for weight, pred in zip(w, val_preds_by_q[q]))
        ensemble_te_by_q[q] = sum(weight * pred for weight, pred in zip(w, te_preds_by_q[q]))

    # quantile crossing 보정.
    p05_val, p50_val, p95_val = enforce_quantile_monotonicity(
        ensemble_val_by_q[LOWER_Q],
        ensemble_val_by_q[MEDIAN_Q],
        ensemble_val_by_q[UPPER_Q],
    )
    p05_te, p50_te, p95_te = enforce_quantile_monotonicity(
        ensemble_te_by_q[LOWER_Q],
        ensemble_te_by_q[MEDIAN_Q],
        ensemble_te_by_q[UPPER_Q],
    )

    # Conformalized Quantile Regression (CQR) 후처리 보정
    # Validation 구간에서의 오차(Non-conformity score) 계산
    error_lower = p05_val - yva  # 양수 = 실측이 P05 아래로 벗어남
    error_upper = yva - p95_val  # 양수 = 실측이 P95 위로 벗어남
    scores = np.maximum(error_lower, error_upper)
    n_val = len(yva)

    # (구 대칭 보정) Coverage 90% 수준을 위한 단일 q_hat — 하위 호환용으로 계속 저장.
    q_level = min(0.90 * (1 + 1 / n_val), 1.0) if n_val > 0 else 0.90
    q_hat = float(np.quantile(scores, q_level)) if scores.size else 0.0
    q_hat = max(q_hat, 0.0)  # 밴드 축소 방지

    # (개선안 §10.4 상단 전용 보정) 하한/상한을 각각 별도 conformity score 로 보정한다.
    # 중심 90% 밴드(P05~P95)의 양쪽 tail 목표 미스커버리지는 각 5% → 한쪽당 95% 레벨.
    # 상단/하단 오차 분포가 다를 때 단일 대칭 보정보다 상단 coverage 를 정확히 맞춘다.
    lower_level = min((1.0 - LOWER_Q) * (1 + 1 / n_val), 1.0) if n_val > 0 else (1.0 - LOWER_Q)
    upper_level = min(UPPER_Q * (1 + 1 / n_val), 1.0) if n_val > 0 else UPPER_Q
    q_hat_lower = float(np.quantile(error_lower, lower_level)) if error_lower.size else 0.0
    q_hat_upper = float(np.quantile(error_upper, upper_level)) if error_upper.size else 0.0
    q_hat_lower = max(q_hat_lower, 0.0)
    q_hat_upper = max(q_hat_upper, 0.0)

    # Validation 및 Test 예측값 Calibration (비대칭 보정 적용)
    p05_val_cal = p05_val - q_hat_lower
    p95_val_cal = p95_val + q_hat_upper
    p05_te_cal = p05_te - q_hat_lower
    p95_te_cal = p95_te + q_hat_upper

    # test 구간 P95 초과율(과사용 후보 비율) — coverage gate/보정 진단용.
    yte_arr = np.asarray(yte, dtype=float)
    p95_exceed_rate_te = (
        float(np.mean(yte_arr > np.asarray(p95_te_cal, dtype=float))) if yte_arr.size else 0.0
    )

    # 평가 지표 (test 구간 기준, 보정된 밴드로 계산).
    metrics: dict[str, float] = {
        "MAPE_p50": mape(yte, p50_te),
        "Pinball_p05": pinball_loss(yte, p05_te_cal, LOWER_Q),
        "Pinball_p50": pinball_loss(yte, p50_te, MEDIAN_Q),
        "Pinball_p95": pinball_loss(yte, p95_te_cal, UPPER_Q),
        "PICP_90": picp(yte, p05_te_cal, p95_te_cal),
        "MPIW_90": mpiw(p05_te_cal, p95_te_cal),
        "Val_PICP_90": picp(yva, p05_val_cal, p95_val_cal),
        "P95_exceed_rate": p95_exceed_rate_te,
    }

    spec = {
        "models_by_q": models_by_q,
        "weights_by_q": weights_by_q,
        "quantiles": list(QUANTILES),
        "m_types": m_types,
        "features": features_used,
        "target_col": ycol,
        # CQR 보정값. cqr_q_hat=대칭(하위호환), *_lower/*_upper=비대칭(§10.4).
        "cqr_q_hat": q_hat,
        "cqr_q_hat_lower": q_hat_lower,
        "cqr_q_hat_upper": q_hat_upper,
    }

    # 재로드 후 검증용 probe — test 마지막 행의 전체 피처(1행) + 실측값.
    probe_X = None
    probe_y = None
    X_te = X_all[mte]
    if len(X_te) > 0:
        probe_X = X_te.iloc[[-1]].copy()
        if len(yte) > 0:
            probe_y = float(yte[-1])

    return QuantileTrainResult(spec=spec, metrics=metrics, probe_X=probe_X, probe_y=probe_y)


# =========================
# 재로드 후 후보 검증
# =========================
# 저장된 spec 1건의 분위수 모델 구조가 올바른지 검사합니다.
# (15개 대상 검증의 단위 검사 — 36개 모델/분위수당 12개 가중치/유한 q_hat)
def validate_quantile_spec_structure(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec is not a dict"]

    quantiles = list(spec.get("quantiles") or [])
    if sorted(float(q) for q in quantiles) != sorted(QUANTILES):
        errors.append(f"quantiles mismatch: {quantiles}")

    models_by_q = spec.get("models_by_q") or {}
    weights_by_q = spec.get("weights_by_q") or {}
    total_models = 0
    for q in QUANTILES:
        models = models_by_q.get(q)
        if models is None:
            errors.append(f"missing models for quantile {q}")
            continue
        if len(models) != MODELS_PER_QUANTILE:
            errors.append(f"quantile {q} has {len(models)} models (expected {MODELS_PER_QUANTILE})")
        total_models += len(models)

        weights = weights_by_q.get(q)
        if weights is None:
            errors.append(f"missing weights for quantile {q}")
        else:
            warr = np.asarray(weights, dtype=float)
            if warr.shape[0] != len(models or []):
                errors.append(
                    f"quantile {q} weight count {warr.shape[0]} != model count {len(models or [])}"
                )
            if not np.all(np.isfinite(warr)):
                errors.append(f"quantile {q} has non-finite weights")

    if total_models != TOTAL_MODELS_PER_TARGET:
        errors.append(f"total models {total_models} != {TOTAL_MODELS_PER_TARGET}")

    q_hat = spec.get("cqr_q_hat")
    try:
        if not np.isfinite(float(q_hat)):
            errors.append("cqr_q_hat is not finite")
    except (TypeError, ValueError):
        errors.append(f"cqr_q_hat invalid: {q_hat!r}")

    # 비대칭 보정폭(§10.4)은 선택 키 — 존재하면 유한·음수불가만 확인.
    for key in ("cqr_q_hat_lower", "cqr_q_hat_upper"):
        if key not in spec:
            continue
        val = spec.get(key)
        try:
            fv = float(val)
        except (TypeError, ValueError):
            errors.append(f"{key} invalid: {val!r}")
            continue
        if not np.isfinite(fv):
            errors.append(f"{key} is not finite")
        elif fv < 0:
            errors.append(f"{key} is negative: {fv}")

    if not (spec.get("features") and spec.get("m_types") and spec.get("target_col")):
        errors.append("spec missing features/m_types/target_col")

    return errors


# 재로드된 spec + probe 피처로 샘플 분위수 예측을 수행해 단조성·유한성을 검사합니다.
def predict_quantile_probe(spec: dict[str, Any], probe_X: pd.DataFrame) -> tuple[float, float, float]:
    quantiles = sorted(float(q) for q in (spec.get("quantiles") or QUANTILES))
    q_lo, q_mid, q_hi = quantiles[0], quantiles[len(quantiles) // 2], quantiles[-1]
    m_types = list(spec.get("m_types") or DEFAULT_M_TYPES)
    features_map: dict[str, list[str]] = spec.get("features") or {}
    models_by_q = spec.get("models_by_q") or {}
    weights_by_q = spec.get("weights_by_q") or {}

    def _ensemble(q: float) -> float:
        models = models_by_q.get(q)
        models = list(models) if models is not None else []
        raw_w = weights_by_q.get(q)
        weights = np.asarray(raw_w if raw_w is not None else [], dtype=float)
        if not models or weights.shape[0] != len(models):
            raise ValueError(f"probe predict: bad models/weights for quantile {q}")
        acc = 0.0
        for idx, mdl in enumerate(models):
            mt = m_types[idx // ALGOS_PER_MODEL_TYPE] if m_types else "M1"
            feat = features_map.get(mt) or list(probe_X.columns)
            X_row = probe_X.reindex(columns=feat, fill_value=0.0)
            acc += float(weights[idx]) * float(np.expm1(mdl.predict(X_row))[0])
        return acc

    p05 = _ensemble(q_lo)
    p50 = _ensemble(q_mid)
    p95 = _ensemble(q_hi)

    # CQR 보정 — 비대칭(cqr_q_hat_lower/upper) 우선, 없으면 대칭 fallback.
    q_hat_lower, q_hat_upper = resolve_cqr_margins(spec)
    p05 -= q_hat_lower
    p95 += q_hat_upper

    p05, p50, p95 = sorted([p05, p50, p95])
    return p05, p50, p95
