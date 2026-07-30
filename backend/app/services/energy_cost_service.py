"""
Energy Cost Service
===================
에너지 비용·단가 집계 규칙의 단일 출처.

배경
----
2026-07 MIS '원단위 실적입력(일단위)' 개편으로 전력비·전력단가·연료비·연료단가가
``energy_daily`` 에 **일별로** 적재된다. 사람이 입력하는 경로는 없다.

이 모듈이 존재하는 이유는 **집계 방식이 컬럼마다 다르기 때문**이다:

    비용  → SUM
    단가  → SUM(비용) / SUM(사용량)  가중평균.  SUM 하면 무의미한 값이 된다.
    COD   → 평균 (농도)

세 종류를 한 목록에 넣고 일괄 SUM 하는 실수를 막으려고, 단가 계산과 원인분해를
여기 한 곳에 가둔다. DB 접근은 하지 않는다 — 호출자(server.py)가 집계한 합계를
넘기고, 이 모듈은 순수 계산만 한다(테스트가 DB 없이 돌도록).

용수·폐수는 대상이 아니다
-------------------------
용수·폐수 처리비는 전력·연료 대비 비중이 작아 시스템 관리 대상이 아니며 사외
파일로 월별 금액만 수기 관리한다(2026-07-30 확정). 따라서 이 모듈이 다루는
에너지원은 전력·연료뿐이고, 화면의 '합계'는 **총 에너지비가 아니라 전력·연료 합**이다.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

# 비용·단가가 적재되기 시작한 날. 그 이전 행은 원본 엑셀에 없어 0 이 들어 있으므로
# 실제 0원과 구분해 반드시 None 으로 내보내야 한다 — 0으로 그리면 전년비가 통째로 틀어진다.
COST_DATA_START = date(2024, 1, 1)

# 비용 지표 → energy_daily 컬럼. 'total' 은 전력+연료 합으로 파생되며 단가가 없다
# (kWh 와 Nm³ 는 더할 수 없다 — 합계 단가는 물리적으로 무의미).
COST_METRICS: dict[str, dict[str, str]] = {
    "power": {
        "label": "전력",
        "usage_col": "total_power_kwh",
        "cost_col": "power_cost_krw",
        "price_col": "power_price_krw_kwh",
        "usage_unit": "kWh",
        "price_unit": "원/kWh",
    },
    "fuel": {
        "label": "연료",
        "usage_col": "fuel_nm3",
        "cost_col": "fuel_cost_krw",
        "price_col": "fuel_price_krw_nm3",
        "usage_unit": "Nm³",
        "price_unit": "원/Nm³",
    },
}

# 화면 탭 순서 — 'total' 은 파생 지표라 COST_METRICS 에 없다.
COST_METRIC_KEYS: tuple[str, ...] = ("power", "fuel", "total")

TOTAL_METRIC = "total"


def metric_spec(metric: str) -> dict[str, str] | None:
    """실측 컬럼을 가진 지표의 스펙. 'total' 은 파생이므로 None."""
    return COST_METRICS.get(metric)


def is_supported_metric(metric: str) -> bool:
    return metric in COST_METRIC_KEYS


def weighted_price(cost: float | None, usage: float | None) -> float | None:
    """기간 단가 = Σ비용 ÷ Σ사용량.

    일별 단가의 산술평균이 아니다 — 사용량이 적은 날의 단가가 같은 무게로 들어가면
    실제 지출과 어긋난다. 원단위 페이지의 가중 누계(Σ사용량÷Σ생산톤)와 같은 원칙.

    ⚠ 분자·분모는 반드시 **짝지어진(비용·사용량이 모두 있는) 날**의 합이어야 한다.
    비용이 적재되지 않은 날의 사용량이 분모에 섞이면 단가가 그만큼 낮아진다 —
    실측 사례: 경산은 사용량이 2021년부터 있는데 비용은 2026-04부터라, 전체 합으로
    계산하면 전사 단가가 166원/kWh 로 나와 모든 개별 공장(180~184원)보다 낮아졌다.
    가중평균이 구성원 범위를 벗어나면 분모가 오염된 것이다.
    """
    if cost is None or usage is None or usage <= 0:
        return None
    return cost / usage


# 원인분해를 신뢰할 수 있는 최소 비용 커버리지. 이 아래면 "비용이 붙지 않은 사용량"이
# 많아 C = Q × P 관계가 성립하지 않으므로 분해를 내보내지 않는다.
BRIDGE_MIN_COVERAGE = 0.99


def cost_coverage(priced_usage: float | None, total_usage: float | None) -> float | None:
    """비용이 매겨진 사용량의 비율 (0~1). 부분 결측을 화면이 알 수 있게 한다."""
    if not total_usage or total_usage <= 0:
        return None
    return min(1.0, (priced_usage or 0.0) / total_usage)


def is_bridge_reliable(*coverages: float | None) -> bool:
    """모든 비교 구간의 비용 커버리지가 충분한지 — 하나라도 부족하면 분해를 포기한다."""
    return all(c is not None and c >= BRIDGE_MIN_COVERAGE for c in coverages)


def rate_change(current: float | None, previous: float | None) -> float | None:
    """증감률(%). 분모가 없거나 0이면 None — 0.0 으로 내보내면 '변화 없음'으로 오독된다."""
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous - 1) * 100


def cost_per_ton(cost: float | None, production_ton: float | None) -> float | None:
    """톤당 에너지비용(원/생산ton). 합계 탭에서 단가 자리를 대신하는 지표."""
    if cost is None or not production_ton or production_ton <= 0:
        return None
    return cost / production_ton


def has_cost_data(period_start: date) -> bool:
    """해당 기간에 비용 데이터가 존재할 수 있는지 — COST_DATA_START 이전이면 없다."""
    return period_start >= COST_DATA_START


def cost_bridge(
    current: Mapping[str, float],
    previous: Mapping[str, float],
) -> dict[str, Any] | None:
    """비용 증감 3단 원인분해.

    비용은 ``C = Q × P`` 이고 ``Q = T × I`` 이므로(T 생산톤, I 원단위, P 단가),
    전년 동기(0)와 금년(1)의 차이를 세 효과로 정확히 가를 수 있다::

        생산량 효과 = (T₁ − T₀) × I₀ × P₀     더 만들어서 늘어난 몫
        효율   효과 = (I₁ − I₀) × T₁ × P₀     원단위가 변해서 늘어난 몫
        단가   효과 = (P₁ − P₀) × Q₁          단가가 올라서 늘어난 몫

    세 효과의 합은 ``C₁ − C₀`` 와 **정확히** 같다(잔차 0):

        (T₁−T₀)·I₀·P₀ + (I₁−I₀)·T₁·P₀ = (Q₁−Q₀)·P₀
        (Q₁−Q₀)·P₀ + (P₁−P₀)·Q₁       = Q₁P₁ − Q₀P₀

    Parameters
    ----------
    current, previous
        ``{"cost": 원, "usage": kWh|Nm³, "production_ton": ton}``

    Returns
    -------
    dict | None
        분모가 0이면 None — 비율을 만들 수 없는 구간을 0으로 위장하지 않는다.
    """
    cost_curr, usage_curr, ton_curr = (
        current.get("cost", 0.0), current.get("usage", 0.0), current.get("production_ton", 0.0),
    )
    cost_prev, usage_prev, ton_prev = (
        previous.get("cost", 0.0), previous.get("usage", 0.0), previous.get("production_ton", 0.0),
    )
    if min(usage_curr, usage_prev, ton_curr, ton_prev) <= 0 or cost_prev <= 0:
        return None

    price_curr = cost_curr / usage_curr
    price_prev = cost_prev / usage_prev
    intensity_curr = usage_curr / ton_curr
    intensity_prev = usage_prev / ton_prev

    production_effect = (ton_curr - ton_prev) * intensity_prev * price_prev
    efficiency_effect = (intensity_curr - intensity_prev) * ton_curr * price_prev
    price_effect = (price_curr - price_prev) * usage_curr

    return {
        "previous": cost_prev,
        "current": cost_curr,
        "productionEffect": production_effect,
        "efficiencyEffect": efficiency_effect,
        "priceEffect": price_effect,
        # 근거 테이블 — 세 효과가 어느 원자료에서 나왔는지 화면에서 따라갈 수 있게.
        "tonPrev": ton_prev, "tonCurr": ton_curr,
        "tonChange": rate_change(ton_curr, ton_prev),
        "intensityPrev": intensity_prev, "intensityCurr": intensity_curr,
        "intensityChange": rate_change(intensity_curr, intensity_prev),
        "pricePrev": price_prev, "priceCurr": price_curr,
        "priceChange": rate_change(price_curr, price_prev),
        "usagePrev": usage_prev, "usageCurr": usage_curr,
        "usageChange": rate_change(usage_curr, usage_prev),
        "costChange": rate_change(cost_curr, cost_prev),
    }


def bridge_residual(bridge: Mapping[str, float]) -> float:
    """세 효과의 합과 실제 비용 증감의 차이. 항등식이 성립하면 0 (부동소수 오차 이내)."""
    total_effect = (
        bridge["productionEffect"] + bridge["efficiencyEffect"] + bridge["priceEffect"]
    )
    return total_effect - (bridge["current"] - bridge["previous"])


def average_concentration(values: Iterable[float | None]) -> float | None:
    """COD 같은 농도 지표의 기간 대푯값 — 합이 아니라 평균이다.

    측정하지 않는 공장은 0 이 적재되므로(경산 원수 COD) 0 을 제외한다.
    엄밀히는 유량가중 평균이어야 하나 일별 유량이 없어 단순평균으로 둔다.
    """
    measured = [float(v) for v in values if v is not None and float(v) > 0]
    if not measured:
        return None
    return sum(measured) / len(measured)
