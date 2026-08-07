"""
Savings Verification Service
=============================
절감 테마의 "원단위 전후 비교" 검증 — 순수 계산만 담당한다(DB 접근 없음).

배경
----
계측기가 없어 절감을 직접 측정할 수 없으므로, 테마 시행월 전후로 해당
공장·에너지원의 원단위(Σ사용량 ÷ Σ생산톤)가 얼마나 달라졌는지로 간접
확인한다. 원단위는 시행과 무관하게 계절·생산 믹스로도 움직이므로, "시행
후 원단위 수준"이 아니라 "시행 전 구간과 후 구간이 각각 전년 동기 대비
얼마나 달라졌는가의 차이"를 본다 — 계절 요인은 전년 동기 비교로 상쇄되고,
남는 차이(Δ)만 시행 효과로 돌린다.

    Δ = r_after − r_before
      r_before = I_before ÷ I_before(전년 동기) − 1
      r_after  = I_after  ÷ I_after(전년 동기)  − 1

Δ ≤ 0(개선)이면, "전 구간의 추세(r_before)가 그대로 이어졌을 때의 가상
원단위"와 "실제 후 구간 원단위"의 차이를 후 구간 생산량에 곱해 회피
사용량으로 환산한다:

    avoided = −Δ × I_after(전년 동기) × T_after

이 회피량이 관리자가 등록한 실적 절감량을 얼마나 설명하는지(explainPct)로
최종 등급을 매긴다. 이 모듈은 DB 조회를 하지 않는다 — server.py 가
energy_daily/production 을 월 경계로 집계해 (usage, productionTon) 4쌍
(전/후/전년전/전년후)을 만들어 넘기면, 여기서는 Δ·회피량·설명률·판정만
계산한다. energy_cost_service.py 와 같은 역할 분리(순수 계산 vs DB 조회)를
따른다.
"""
from __future__ import annotations

from typing import Any, TypedDict

# 시행 후 관측 구간의 기본 길이(개월) — 전/후 구간 모두 이 길이를 우선 시도하고,
# 데이터 경계에 걸리면 있는 만큼만 쓴다(server.py 가 클리핑).
WINDOW_MONTHS = 6

# 판정 임계값 — 화면에 그대로 노출되므로 근거를 주석으로 남긴다.
MIN_AFTER_MONTHS = 3        # 이보다 짧으면 '재확인'으로 낮춘다 — 계절 1순환도 못 채움
STRONG_EXPLAIN_PCT = 70.0   # 등록 실적의 이 비율 이상을 원단위 개선으로 설명하면 '검증됨'
WEAK_EXPLAIN_PCT = 30.0     # 이 비율 미만이면 '미확인' — 우연한 변동과 구분되지 않음

STATUS_LABELS: dict[str, str] = {
    "verified": "효과 확인", "review": "추가 확인 필요", "unverified": "효과 미확인", "pending": "데이터 부족",
}


class WindowTotals(TypedDict):
    usage: float
    productionTon: float


def window_intensity(window: WindowTotals) -> float | None:
    """Σ사용량 ÷ Σ생산톤. 생산량이 0이면 원단위를 만들 수 없다."""
    production = window["productionTon"]
    return window["usage"] / production if production > 0 else None


def verify_theme(
    before: WindowTotals,
    after: WindowTotals,
    before_prev: WindowTotals,
    after_prev: WindowTotals,
    actual_qty_after: float | None,
    after_months: int,
) -> dict[str, Any]:
    """네 구간의 (usage, productionTon) 합계로 판정을 산출한다.

    Parameters
    ----------
    before, after
        시행 전/후 구간의 (Σ사용량, Σ생산톤).
    before_prev, after_prev
        각각의 전년 동기 구간.
    actual_qty_after
        후 구간에 등록된 실적 절감량 합(None = 미입력).
    after_months
        후 구간 중 실측(생산량>0)이 있는 개월 수 — 관측 기간 판단에 쓴다.
    """
    intensities = {
        "before": window_intensity(before),
        "after": window_intensity(after),
        "beforePrev": window_intensity(before_prev),
        "afterPrev": window_intensity(after_prev),
    }
    if any(value is None for value in intensities.values()):
        return {
            "status": "pending",
            "statusLabel": STATUS_LABELS["pending"],
            "reason": "시행 전·후 또는 전년 같은 기간의 생산실적이 없어 실제 에너지 원단위를 비교할 수 없습니다.",
            "intensities": intensities,
        }
    if not actual_qty_after or actual_qty_after <= 0:
        return {
            "status": "pending",
            "statusLabel": STATUS_LABELS["pending"],
            "reason": "시행 이후 등록된 절감 실적이 없어 실제 사용량과 비교할 수 없습니다.",
            "intensities": intensities,
        }

    r_before = intensities["before"] / intensities["beforePrev"] - 1
    r_after = intensities["after"] / intensities["afterPrev"] - 1
    delta = r_after - r_before
    avoided = -delta * intensities["afterPrev"] * after["productionTon"]
    explain_pct = (avoided / actual_qty_after * 100) if actual_qty_after > 0 else None

    if delta > 0:
        status = "unverified"
        reason = "시행 후 에너지 원단위가 전년 같은 기간보다 나빠져 실제 사용량에서 절감 효과가 확인되지 않습니다."
    elif explain_pct is None or explain_pct < WEAK_EXPLAIN_PCT:
        status = "unverified"
        reason = (
            f"등록 실적 대비 추정 원단위 개선량이 {explain_pct:.0f}%에 그쳐 절감 효과로 보기 어렵습니다."
            if explain_pct is not None else "등록 실적 대비 추정 개선량 비율을 계산할 수 없습니다."
        )
    elif explain_pct < STRONG_EXPLAIN_PCT or after_months < MIN_AFTER_MONTHS:
        status = "review"
        parts = []
        if explain_pct < STRONG_EXPLAIN_PCT:
            parts.append(f"등록 실적 대비 추정 원단위 개선량이 {explain_pct:.0f}%입니다")
        if after_months < MIN_AFTER_MONTHS:
            parts.append(f"관측 기간이 {after_months}개월로 짧습니다")
        reason = " · ".join(parts) + "."
    else:
        status = "verified"
        reason = f"등록 실적 대비 추정 원단위 개선량이 {explain_pct:.0f}%입니다."

    return {
        "status": status,
        "statusLabel": STATUS_LABELS[status],
        "reason": reason,
        "intensities": intensities,
        "rBeforePct": r_before * 100,
        "rAfterPct": r_after * 100,
        "deltaPct": delta * 100,
        "avoidedQty": avoided,
        "explainPct": explain_pct,
        "afterMonths": after_months,
    }


def production_adjusted_expected_usage(periods: list[dict[str, float]]) -> float | None:
    """같은 월·물리공장끼리 맞춘 BAU 사용량 합계를 만든다.

    연간 합계 원단위를 한 번만 적용하면 계절별 원단위나 공장별 제품 믹스가 다른
    경우 생산 비중 변화 자체를 절감으로 오인한다. 따라서 서버가 완전성이 확인된
    월 × 물리공장 셀을 넘기고, 이 함수는 각 셀의 전년 원단위를 올해 같은 셀의
    생산량에 적용해 합산한다.
    """
    if not periods:
        return None
    expected = 0.0
    for period in periods:
        previous_usage = period["previousUsage"]
        previous_production = period["previousProductionTon"]
        current_production = period["currentProductionTon"]
        if previous_usage <= 0 or previous_production <= 0 or current_production <= 0:
            return None
        expected += previous_usage / previous_production * current_production
    return expected


def reconcile_factory_energy_type(
    current_year: WindowTotals,
    previous_year: WindowTotals,
    registered_qty: float,
    *,
    comparable: bool = True,
    expected_usage: float | None = None,
    registration_complete: bool = True,
) -> dict[str, Any]:
    """생산량을 보정한 공장·에너지원 단위 절감 효과 추정.

    expected_usage에는 같은 월·물리공장별로 계산한
    Σ(전년 사용량/생산량 × 올해 생산량)을 받는다. 호출자가 생략한 경우에만
    단일 공장·단일 구간 테스트와의 호환을 위해 합계 원단위 방식으로 계산한다.

        estimated_avoided = expected_usage - current_usage
        expected_after_registered = max(expected_usage - registered_qty, 0)
        residual = current_usage - expected_after_registered

    explainPct는 추정 절감량 ÷ 등록 절감량이다. 100%를 넘을 수 있으며 이는
    등록량보다 실제 사용량 감소 추정치가 더 크다는 뜻이다.
    """
    usage_change = current_year["usage"] - previous_year["usage"]
    current_production = current_year["productionTon"]
    previous_production = previous_year["productionTon"]
    base_result = {
        "usageChange": usage_change,
        "productionChangePct": (
            (current_production / previous_production - 1) * 100
            if comparable and previous_production > 0 else None
        ),
        "usageChangePct": (
            (current_year["usage"] / previous_year["usage"] - 1) * 100
            if comparable and previous_year["usage"] > 0 else None
        ),
    }
    if expected_usage is None and comparable and current_production > 0 and previous_production > 0:
        expected_usage = previous_year["usage"] / previous_production * current_production
    if (
        not comparable
        or current_production <= 0
        or previous_production <= 0
        or expected_usage is None
        or expected_usage <= 0
    ):
        return {
            **base_result,
            "verdict": "비교불가",
            "previousIntensity": None,
            "currentIntensity": None,
            "intensityChangePct": None,
            "expectedUsage": None,
            "expectedAfterRegistered": None,
            "normalizedUsageChange": None,
            "avoidedUsage": None,
            "residualQty": None,
            "explainPct": None,
            "registeredExceedsBaseline": None,
        }

    previous_intensity = previous_year["usage"] / previous_production
    current_intensity = current_year["usage"] / current_production
    normalized_change = current_year["usage"] - expected_usage
    estimated_avoided = -normalized_change
    registered_exceeds_baseline = registered_qty > expected_usage
    expected_after_registered = max(expected_usage - registered_qty, 0.0)
    residual_qty = current_year["usage"] - expected_after_registered
    realization_pct = (
        max(estimated_avoided, 0.0) / registered_qty * 100
        if registered_qty > 0 else None
    )

    if not registration_complete:
        verdict = "등록미완료"
    elif registered_qty <= 0:
        verdict = "해당없음"
    elif registered_exceeds_baseline:
        verdict = "과대"
    elif estimated_avoided < 0:
        verdict = "역행"
    elif estimated_avoided == 0:
        verdict = "과대"
    elif realization_pct is not None and realization_pct < STRONG_EXPLAIN_PCT:
        verdict = "과대"
    else:
        verdict = "정합"

    return {
        **base_result,
        "verdict": verdict,
        "previousIntensity": previous_intensity,
        "currentIntensity": current_intensity,
        # 합계 원단위끼리의 단순 비교 대신 월·공장 보정 예상치 대비 실제 변화율.
        "intensityChangePct": (current_year["usage"] / expected_usage - 1) * 100,
        "expectedUsage": expected_usage,
        "expectedAfterRegistered": (
            expected_after_registered if registration_complete else None
        ),
        "normalizedUsageChange": normalized_change,
        "avoidedUsage": estimated_avoided if estimated_avoided >= 0 else None,
        "residualQty": residual_qty if registration_complete else None,
        "explainPct": realization_pct if registration_complete else None,
        "registeredExceedsBaseline": (
            registered_exceeds_baseline if registration_complete else None
        ),
    }
