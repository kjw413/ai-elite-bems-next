# 이 파일은 재공품 피처 우선순위 목록을 정의합니다.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WipPrioritySpec:
    priority: int
    plant: str
    target: str
    item_codes: tuple[str, ...]
    note: str


# 2026-05-15 최종 (김해 단독 ablation + 도메인 정정 후):
# - 학습 시작 2023-01-01로 통일, test_end 2026-04-30
# - 김해: 단독 학습 스크립트(modeling_v5.1_김해.py)로 plant-target별 최적 조합 발견
#   * 전력: 공병 3종 (test -0.68%p)
#   * 연료: 바나나맛우유믹스 + 농축유 + 바나나맛우유믹스(수출용) (val -0.64%p, test -2.96%p)
#   * 용수: 같은 조합 (val -0.21%p, test +0.03%p - reject되었으나 도메인 일관성 유지)
# - 신설 라인 223579는 별도 사후 보정(drift correction)으로 처리 — usage_prediction_v5_service.py
PRIORITY_WIP_SHORTLIST: tuple[WipPrioritySpec, ...] = (
    # === 광주 ===
    WipPrioritySpec(
        priority=1,
        plant="광주",
        target="연료",
        item_codes=("260014", "260042", "260016"),
        note="탈지분유+유크림믹스+생크림(냉동). 공병은 광주 연료엔 노이즈.",
    ),
    WipPrioritySpec(
        priority=2,
        plant="광주",
        target="용수",
        item_codes=("260014", "260042", "260016"),
        note="탈지분유+유크림믹스+생크림(냉동). 공병은 광주 용수엔 노이즈.",
    ),
    WipPrioritySpec(
        priority=3,
        plant="광주",
        target="전력",
        item_codes=("220067", "220068"),
        note="공병(바나나병) — mix 외 전력 부하. test -2.48%p 개선",
    ),
    # === 남양주2 ===
    WipPrioritySpec(
        priority=4,
        plant="남양주2",
        target="전력",
        item_codes=("210376", "210405", "213137", "210417"),
        note="남양주2 주력 4종 (도메인 지식)",
    ),
    WipPrioritySpec(
        priority=5,
        plant="남양주2",
        target="연료",
        item_codes=("260015", "210376", "210405", "213137", "210417"),
        note="농축유(살균/농축) + 남양주2 주력 4종",
    ),
    WipPrioritySpec(
        priority=6,
        plant="남양주2",
        target="용수",
        item_codes=("260015", "210376", "210405", "213137", "210417"),
        note="농축유(세척/희석) + 남양주2 주력 4종",
    ),
    # === 김해 (단독 ablation 최종) ===
    WipPrioritySpec(
        priority=7,
        plant="김해",
        target="전력",
        item_codes=("220032", "220067", "220068"),
        note="공병 3종 (욥닥터/바나나 상·하) — test -0.68%p",
    ),
    WipPrioritySpec(
        priority=8,
        plant="김해",
        target="연료",
        item_codes=("220006", "260015", "220051"),
        note="바나나맛우유믹스 + 농축유 + 바나나맛우유믹스(수출용) — test -2.96%p",
    ),
    WipPrioritySpec(
        priority=9,
        plant="김해",
        target="용수",
        item_codes=("220006", "260015", "220051"),
        note="동일 조합 (val -0.21%p, 용수는 신호 약하나 도메인 일관성 유지)",
    ),
    # === 남양주1 (실 생산품만 채택) ===
    # 시트의 다른 재공품 코드들(예: 210376, 210405)은 사실 남양주2 생산이라 제외.
    WipPrioritySpec(
        priority=10,
        plant="남양주1",
        target="전력",
        item_codes=("220006", "220184"),
        note="바나나맛우유믹스 + 시유믹스 — 남양주1 실 생산품",
    ),
    WipPrioritySpec(
        priority=11,
        plant="남양주1",
        target="연료",
        item_codes=("220006", "220184"),
        note="바나나맛우유믹스 + 시유믹스 — 살균 부하",
    ),
    WipPrioritySpec(
        priority=12,
        plant="남양주1",
        target="용수",
        item_codes=("220006", "220184"),
        note="바나나맛우유믹스 + 시유믹스 — 세척 부하",
    ),
    # === 논산 (용수만) ===
    WipPrioritySpec(
        priority=13,
        plant="논산",
        target="용수",
        item_codes=("220059", "220006"),
        note="딸기맛우유믹스 + 바나나맛우유믹스. test -0.99%p",
    ),
)


# 우선순위 specs을 차례로 반환합니다.
def iter_priority_specs() -> tuple[WipPrioritySpec, ...]:
    return PRIORITY_WIP_SHORTLIST


# 우선 목록 품목 코드 값을 가져옵니다.
def get_shortlist_item_codes(plant: str, target: str) -> tuple[str, ...]:
    for spec in PRIORITY_WIP_SHORTLIST:
        if spec.plant == plant and spec.target == target:
            return spec.item_codes
    return ()


# 보틀링 라인(EA 단위 병제품) 품목 코드 — 공장별로 합계 피처(wip_bottling_ea_log)에 사용.
# 믹스 생산량(mix_ton)이 낮은 날에도 보틀링 라인이 풀가동하면 전력/연료/용수가 평소 수준으로
# 소비되는 패턴(2026-05-22 김해 over-band 사례)을 모델이 학습할 수 있도록 도입.
# - 김해: 욥닥터/바나나(상·하)/메론(중국)/메로나 5종
# - 광주: 바나나(상·하)/딸기(상)/라이트병/중국·대만 수출용 + 닥터캡슐/무가당/고구마 등 10종
# 콘과자(논산 212008/9/10)는 EA지만 보틀링과 공정이 달라 제외.
EA_BOTTLING_CODES_BY_PLANT: dict[str, tuple[str, ...]] = {
    "김해": ("220032", "220067", "220068", "220170", "260353"),
    "광주": (
        "220067", "220068", "220069", "220073", "220076", "220077",
        "260349", "260358", "260361", "260362",
    ),
}


# 보틀링 EA 합계 피처에 사용할 품목 코드 목록을 반환합니다.
def get_bottling_ea_codes(plant: str) -> tuple[str, ...]:
    return EA_BOTTLING_CODES_BY_PLANT.get(plant, ())
