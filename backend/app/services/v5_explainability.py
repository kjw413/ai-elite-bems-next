"""
V5 Model Explainability
=======================
앙상블(LGBM/XGB/CatBoost × M1~M4) 모델의 변수 중요도를
사람이 읽을 수 있는 한국어 라벨과 함께 추출합니다.

용도:
  - 대시보드 AI 이상감지 진단 카드: "이 예측에 가장 큰 영향을 준 변수 Top 5"
  - AI 예측 결과 화면: "이 예측이 어떤 변수에 의해 만들어졌는지" 설명

참고: 본 함수는 모델 자체의 split-importance(LGBM/XGB) 또는 feature importance
(CatBoost) 를 ensemble weight 로 가중평균한 추정치입니다. 단일 예측에 대한
SHAP 같은 인스턴스 단위 기여도가 아니라, 모델이 학습 과정에서 변수를
얼마나 자주·강하게 사용했는지의 거시적 지표입니다.
"""
from __future__ import annotations

from typing import Any

import numpy as np

# 기술적 피처 이름 → 실무자가 이해할 수 있는 한국어 라벨.
# 정확한 이름이 없으면 부분 일치(fallback) 로 추정.
_FEATURE_LABELS: dict[str, str] = {
    "log_mix_ton":         "당일 생산량 (로그 변환)",
    "log_mix_ton_lag1":    "전일 생산량 (로그 변환)",
    "log_mix_ton_r7mean":  "최근 7일 평균 생산량 (로그)",
    "lag1":                "전일 사용량",
    "r7mean":              "최근 7일 평균 사용량",
    "intensity_lag1":      "전일 원단위 (사용량/생산량)",
    "dist_to_h":           "다음 휴일까지 남은 일수",
    "dist_from_h":         "직전 휴일로부터 경과 일수",
    "dow":                 "요일 (0=월, 6=일)",
    "month":               "월(月)",
    "HDD":                 "난방도일 (외기 ↓일수록 ↑)",
    "CDD":                 "냉방도일 (외기 ↑일수록 ↑)",
    "THI":                 "체감 불쾌지수 (온·습도)",
    "평균기온":             "평균 외기온도",
    "평균기온_lag1":         "전일 평균 외기온도",
    "평균기온_r7mean":       "최근 7일 평균 외기온도",
    "일강수량":             "일 강수량",
    "상대습도":             "상대습도",
    "일사량":               "일사량",
    "일조시간":             "일조 시간",
}


def humanize_feature_name(feat: str) -> str:
    """기술적 피처명 → 한국어 라벨. wip_<품목코드> / unknown 도 안전하게 처리."""
    if feat in _FEATURE_LABELS:
        return _FEATURE_LABELS[feat]
    # 재공품 피처
    if feat.startswith("wip_"):
        item_code = feat[4:]
        return f"재공품 {item_code}"
    # 매칭 실패 시 원래 이름 그대로 반환
    return feat


def get_v5_feature_importance(
    factory: str,
    target: str,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """
    factory + target 의 활성 v5 앙상블 모델에서 가중평균 feature importance 를 산출.

    반환:
      [
        {"feature": "log_mix_ton", "label": "당일 생산량 (로그 변환)",
         "importance": 0.18, "rank": 1},
        ...
      ]
      0~1 로 정규화된 importance, 내림차순. 데이터 부재 시 빈 리스트.
    """
    try:
        # 순환 import 방지를 위해 함수 내부 import
        from app.services.usage_prediction_v5_service import get_active_model
    except Exception:
        return []

    try:
        model_dict = get_active_model()
    except Exception:
        return []

    if not model_dict or factory not in model_dict:
        return []
    if target not in model_dict[factory]:
        return []

    spec = model_dict[factory][target]
    models = list(spec.get("models") or [])
    raw_w = spec.get("weights")
    if not models:
        # 분위수 모델(v5.2/v5.3) 폴백 — 중앙값(P50) 분위수의 모델·가중치를 사용.
        # modeling_v5.3.py 는 models_by_q/weights_by_q 를 float 키(0.05/0.50/0.95)로
        # 저장하지만, 직렬화 경로에 따라 문자열 키일 수 있어 양쪽 모두 조회한다.
        models_by_q = spec.get("models_by_q") or {}
        weights_by_q = spec.get("weights_by_q") or {}

        def _lookup_q(d: dict, q: float) -> Any:
            if q in d:
                return d[q]
            for key, val in d.items():
                try:
                    if abs(float(key) - q) < 1e-9:
                        return val
                except (TypeError, ValueError):
                    continue
            return None

        models = list(_lookup_q(models_by_q, 0.5) or [])
        raw_w = _lookup_q(weights_by_q, 0.5)
    if not models:
        return []

    weights = (
        np.array(raw_w, dtype=float)
        if raw_w is not None and len(raw_w) == len(models)
        else np.ones(len(models)) / len(models)
    )
    m_types = list(spec.get("m_types") or ["M1", "M2", "M3", "M4"])
    features_map: dict[str, list[str]] = spec.get("features") or {}

    # 누적: feature → 가중 importance 합계
    accum: dict[str, float] = {}
    for i, mdl in enumerate(models):
        mt = m_types[i // 3] if i // 3 < len(m_types) else m_types[-1]
        feat_list = features_map.get(mt) or []
        if not feat_list:
            continue

        # 모델별 importance 추출 (LGBM/XGB/CatBoost 모두 .feature_importances_ 노출)
        try:
            imp = np.asarray(getattr(mdl, "feature_importances_", []), dtype=float)
        except Exception:
            continue
        if imp.size == 0 or imp.size != len(feat_list):
            continue

        # 모델 내부에서 0~1 로 정규화 후 모델 가중치 곱
        total = float(imp.sum())
        if total <= 0:
            continue
        norm = imp / total
        w = float(weights[i])
        for f, v in zip(feat_list, norm):
            accum[f] = accum.get(f, 0.0) + v * w

    if not accum:
        return []

    # 전체 정규화
    total_all = sum(accum.values()) or 1.0
    sorted_items = sorted(
        ((f, v / total_all) for f, v in accum.items()),
        key=lambda x: x[1], reverse=True,
    )
    top = sorted_items[: max(1, int(top_n))]
    return [
        {
            "feature": f,
            "label": humanize_feature_name(f),
            "importance": float(v),
            "rank": rank + 1,
        }
        for rank, (f, v) in enumerate(top)
    ]


def explain_top_features_korean(items: list[dict[str, Any]]) -> str:
    """Top features 리스트를 한 줄 자연어 문장으로 요약 (실무자용)."""
    if not items:
        return ""
    pieces = []
    for it in items[:3]:
        pct = it["importance"] * 100.0
        pieces.append(f"{it['label']} ({pct:.0f}%)")
    head = "이 예측에 가장 큰 영향을 준 변수는 "
    return head + ", ".join(pieces) + " 순입니다."
