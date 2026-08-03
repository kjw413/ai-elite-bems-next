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
    "verified": "검증됨", "review": "재확인", "unverified": "미확인", "pending": "판정 보류",
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
            "reason": "전·후 구간 또는 전년 동기의 원단위를 계산할 수 없어(해당 기간 생산실적 없음) 검증을 보류합니다.",
            "intensities": intensities,
        }
    if not actual_qty_after or actual_qty_after <= 0:
        return {
            "status": "pending",
            "statusLabel": STATUS_LABELS["pending"],
            "reason": "시행 이후 등록된 절감 실적이 없어 검증을 보류합니다.",
            "intensities": intensities,
        }

    r_before = intensities["before"] / intensities["beforePrev"] - 1
    r_after = intensities["after"] / intensities["afterPrev"] - 1
    delta = r_after - r_before
    avoided = -delta * intensities["afterPrev"] * after["productionTon"]
    explain_pct = (avoided / actual_qty_after * 100) if actual_qty_after > 0 else None

    if delta > 0:
        status = "unverified"
        reason = "시행 후 원단위가 전년 동기 대비 오히려 더 나빠져, 절감 효과가 원단위에서 확인되지 않습니다."
    elif explain_pct is None or explain_pct < WEAK_EXPLAIN_PCT:
        status = "unverified"
        reason = (
            f"원단위 개선분이 등록 실적의 {explain_pct:.0f}%만 설명해 우연한 변동과 구분되지 않습니다."
            if explain_pct is not None else "설명률을 계산할 수 없습니다."
        )
    elif explain_pct < STRONG_EXPLAIN_PCT or after_months < MIN_AFTER_MONTHS:
        status = "review"
        parts = []
        if explain_pct < STRONG_EXPLAIN_PCT:
            parts.append(f"설명률 {explain_pct:.0f}%로 일부만 설명됩니다")
        if after_months < MIN_AFTER_MONTHS:
            parts.append(f"관측 기간이 {after_months}개월로 짧습니다")
        reason = " · ".join(parts) + "."
    else:
        status = "verified"
        reason = f"원단위 개선이 등록 실적의 {explain_pct:.0f}%를 설명해 절감이 뒷받침됩니다."

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


def reconcile_factory_energy_type(
    current_year: WindowTotals,
    previous_year: WindowTotals,
    registered_qty: float,
) -> dict[str, Any]:
    """공장·에너지원 단위 총량 대사 — 개별 테마 귀속과 무관하게, 실제 연간 사용량
    감소분과 그 공장에 등록된 절감 실적 합을 맞춰본다.

    테마별 검증은 같은 구간에 테마가 겹치면 개별 귀속이 불가능하지만, 이 대사는
    공장 전체를 한 덩어리로 보므로 겹쳐도 새지 않는다 — 절감 실적 과대보고를
    잡는 최종 안전장치(설계서 §6-⑥).
    """
    usage_change = current_year["usage"] - previous_year["usage"]
    if usage_change >= 0:
        return {
            "verdict": "역행" if registered_qty > 0 else "해당없음",
            "usageChange": usage_change,
            "avoidedUsage": None,
            "explainPct": None,
        }
    avoided_usage = -usage_change
    explain_pct = (registered_qty / avoided_usage * 100) if avoided_usage > 0 else None
    verdict = "과대" if (explain_pct is not None and explain_pct > 130.0) else "정합"
    return {
        "verdict": verdict,
        "usageChange": usage_change,
        "avoidedUsage": avoided_usage,
        "explainPct": explain_pct,
    }
