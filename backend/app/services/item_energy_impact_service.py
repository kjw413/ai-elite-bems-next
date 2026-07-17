"""
Item × Energy Impact Estimator
==============================
`tools/analyze_item_energy_impact.py` 가 사전 계산한 회귀 계수
(`analysis_results/item_energy_impact_lookup.json`)를 로드하여
'kg 변화 → 에너지 변화' 추정을 제공합니다.

사용 예
-------
    from app.services.item_energy_impact_service import (
        estimate_impact_by_category,
        estimate_impact_by_item,
        get_intensity_table,
    )

    # 광주 발효유(FM) 100,000 kg 추가 시 추가 에너지 소비량 추정
    res = estimate_impact_by_category(
        factory="광주", category1="냉장", category2="FM",
        delta_kg=100_000,
    )
    # → {'전력_kwh': 22773.0, '연료_nm3': 3826.0, '용수_ton': 339.0,
    #    'ci_lo': {...}, 'ci_hi': {...}, 'sources': [...]}

    # 신규 품목인데 카테고리만 안다면 그대로 같은 함수 호출.
    # 기존 품목이고 Lasso top 에 들어와 있다면 by_item 가 더 정확.
    res = estimate_impact_by_item(factory="남양주", item_code="123054", delta_kg=10_000)

룩업 파일이 없으면 `LookupNotAvailable` 예외를 raise.
사용자/AI 측에서 graceful 처리하도록.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOOKUP_PATH = PROJECT_ROOT / "analysis_results" / "item_energy_impact_lookup.json"

_TARGETS_TO_ENERGY_KEY: dict[str, str] = {
    "전력": "전력_kwh",
    "연료": "연료_nm3",
    "용수": "용수_ton",
}

# ── 캐시 / 로더 ──────────────────────────────────────────────
_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float | None = None
_LOCK = threading.Lock()


class LookupNotAvailable(RuntimeError):
    """analysis_results 폴더에 룩업 파일이 없거나 손상된 경우."""


def _load_lookup() -> dict[str, Any]:
    """파일 mtime 기반 캐시. 파일이 갱신되면 자동 reload."""
    global _CACHE, _CACHE_MTIME
    if not LOOKUP_PATH.exists():
        raise LookupNotAvailable(
            f"item-energy 룩업 파일이 없습니다: {LOOKUP_PATH}\n"
            "→ `py -3 tools/analyze_item_energy_impact.py` 실행 후 다시 시도하세요."
        )
    mtime = LOOKUP_PATH.stat().st_mtime
    if _CACHE is not None and _CACHE_MTIME == mtime:
        return _CACHE

    with _LOCK:
        if _CACHE is not None and _CACHE_MTIME == mtime:
            return _CACHE
        try:
            data = json.loads(LOOKUP_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LookupNotAvailable(f"룩업 파일 파싱 실패: {exc}")
        _CACHE = data
        _CACHE_MTIME = mtime
        return data


def reload_lookup() -> dict[str, Any]:
    """캐시 강제 무효화 (관리자/CLI 용)."""
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        _CACHE = None
        _CACHE_MTIME = None
    return _load_lookup()


def lookup_status() -> dict[str, Any]:
    """UI 배지용 상태 — 룩업 존재여부, 생성 시각, 공장/타겟 목록."""
    if not LOOKUP_PATH.exists():
        return {"available": False, "path": str(LOOKUP_PATH)}
    try:
        data = _load_lookup()
        return {
            "available": True,
            "path": str(LOOKUP_PATH),
            "generated_at": data.get("generated_at"),
            "factories": data.get("factories", []),
            "targets": data.get("targets", []),
            "schema_version": data.get("schema_version", 1),
        }
    except LookupNotAvailable as exc:
        return {"available": False, "path": str(LOOKUP_PATH), "error": str(exc)}


# ── 추정 결과 데이터 클래스 ──────────────────────────────────
@dataclass
class ImpactEstimate:
    factory: str
    basis: str  # "category" | "item"
    delta_kg: float
    estimates: dict[str, float] = field(default_factory=dict)        # 전력_kwh / 연료_nm3 / 용수_ton
    ci_lo: dict[str, float] = field(default_factory=dict)
    ci_hi: dict[str, float] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factory": self.factory,
            "basis": self.basis,
            "delta_kg": self.delta_kg,
            "estimates": self.estimates,
            "ci_lo": self.ci_lo,
            "ci_hi": self.ci_hi,
            "sources": self.sources,
            "notes": self.notes,
        }


# ── 카테고리 기반 추정 ───────────────────────────────────────
def estimate_impact_by_category(
    factory: str,
    category1: str,
    category2: str | None,
    delta_kg: float,
) -> ImpactEstimate:
    """카테고리(category1, category2) 단위 회귀계수로 에너지 변화 추정.

    Returns ImpactEstimate. 룩업에 매칭되는 항목이 없으면 estimates 가 비어있고 notes 에 사유.
    """
    data = _load_lookup()
    by_cat = data.get("by_category", {}).get(factory.upper())
    est = ImpactEstimate(factory=factory.upper(), basis="category", delta_kg=float(delta_kg))

    if not by_cat:
        est.notes.append(f"{factory} 의 카테고리 회귀 결과가 없습니다.")
        return est

    cat2_norm = (category2 or "").upper()
    cat1_norm = category1 or ""

    matched = False
    for target, rows in by_cat.items():
        for r in rows:
            if r.get("category1") == cat1_norm and (
                (r.get("category2") or "") == cat2_norm
                or (r.get("category2") in (None, "(미분류)", "") and not cat2_norm)
            ):
                k = _TARGETS_TO_ENERGY_KEY.get(target, target)
                est.estimates[k] = float(r["intensity_per_kg"]) * float(delta_kg)
                est.ci_lo[k] = float(r["ci_lo"]) * float(delta_kg)
                est.ci_hi[k] = float(r["ci_hi"]) * float(delta_kg)
                est.sources.append({
                    "target": target,
                    "category1": r["category1"],
                    "category2": r.get("category2"),
                    "intensity_per_kg": r["intensity_per_kg"],
                    "share_of_kg": r.get("share_of_kg"),
                    "r2_model": r.get("r2_model"),
                })
                matched = True

    if not matched:
        est.notes.append(
            f"{factory}/{cat1_norm}/{cat2_norm} 조합에 대한 회귀 계수가 없습니다. "
            "동일 공장의 유사 카테고리를 참고하거나 분석을 재실행하세요."
        )

    return est


# ── 단일 품목 기반 추정 ──────────────────────────────────────
def estimate_impact_by_item(
    factory: str,
    item_code: str,
    delta_kg: float,
) -> ImpactEstimate:
    """Lasso top 품목에 들어 있으면 해당 계수, 아니면 카테고리 fallback.

    Returns ImpactEstimate. fallback 시 sources 에 (basis='item->category') 노트 추가.
    """
    data = _load_lookup()
    by_top = data.get("by_item_top", {}).get(factory.upper(), {})
    est = ImpactEstimate(factory=factory.upper(), basis="item", delta_kg=float(delta_kg))
    matched_targets: set[str] = set()

    for target, rows in by_top.items():
        for r in rows:
            if str(r.get("item_code")) == str(item_code):
                k = _TARGETS_TO_ENERGY_KEY.get(target, target)
                est.estimates[k] = float(r["intensity_per_kg"]) * float(delta_kg)
                est.sources.append({
                    "target": target,
                    "item_code": item_code,
                    "item_name": r.get("item_name"),
                    "rank": r.get("rank"),
                    "intensity_per_kg": r["intensity_per_kg"],
                    "alpha": r.get("alpha"),
                })
                matched_targets.add(target)

    # fallback: 누락 타겟은 카테고리에서 채움
    missing = set(_TARGETS_TO_ENERGY_KEY.keys()) - matched_targets
    if missing:
        # 품목의 카테고리 추정 — by_top 에서 같은 item_code 의 다른 row 메타 활용
        cat1, cat2 = _infer_category_for_item(data, factory.upper(), str(item_code))
        if cat1 is not None:
            cat_est = estimate_impact_by_category(factory, cat1, cat2, delta_kg)
            for k, v in cat_est.estimates.items():
                # target name 변환 (전력_kwh ↔ 전력)
                if k not in est.estimates:
                    est.estimates[k] = v
                    est.ci_lo[k] = cat_est.ci_lo.get(k, v)
                    est.ci_hi[k] = cat_est.ci_hi.get(k, v)
            est.notes.append(
                f"품목 {item_code} 의 일부 타겟({sorted(missing)})은 Lasso top 에 없어 "
                f"카테고리({cat1}/{cat2}) 평균으로 보간했습니다."
            )
            est.sources.extend(cat_est.sources)
        else:
            est.notes.append(
                f"품목 {item_code} 의 카테고리를 룩업에서 찾을 수 없습니다. "
                "production_daily 메타와 분석을 갱신하세요."
            )
    return est


def _infer_category_for_item(
    data: dict[str, Any], factory: str, item_code: str,
) -> tuple[str | None, str | None]:
    by_top = data.get("by_item_top", {}).get(factory, {})
    for target, rows in by_top.items():
        for r in rows:
            if str(r.get("item_code")) == item_code:
                return r.get("category1"), r.get("category2")
    return None, None


# ── 디스플레이용 테이블 빌더 ─────────────────────────────────
def get_intensity_table(factory: str, target: str | None = None) -> list[dict[str, Any]]:
    """UI/AI 표시용 — 카테고리별 단위 강도 표 (kWh/kg, Nm³/kg, ton/kg)."""
    data = _load_lookup()
    by_cat = data.get("by_category", {}).get(factory.upper())
    if not by_cat:
        return []
    rows: list[dict[str, Any]] = []
    for t, sub in by_cat.items():
        if target and t != target:
            continue
        for r in sub:
            rows.append({
                "factory": factory.upper(),
                "target": t,
                "unit": r.get("unit"),
                "category1": r.get("category1"),
                "category2": r.get("category2"),
                "intensity_per_kg": r.get("intensity_per_kg"),
                "ci_lo": r.get("ci_lo"),
                "ci_hi": r.get("ci_hi"),
                "share_of_kg": r.get("share_of_kg"),
                "r2_model": r.get("r2_model"),
            })
    return rows


# ── 자연어 요약 (AI 보고서/UI 캡션) ──────────────────────────
def estimate_summary_text(est: ImpactEstimate) -> str:
    """ImpactEstimate 를 한국어 한 단락 요약으로."""
    if not est.estimates:
        return f"⚠️ {est.factory} 추정 불가: " + " / ".join(est.notes or ["사유 미상"])
    bits = [
        f"{est.factory} 에서 {est.delta_kg:+,.0f} kg 변화 시 "
        f"({'카테고리' if est.basis == 'category' else '품목'} 회귀 기준):"
    ]
    for k, v in est.estimates.items():
        unit = k.split("_")[-1].upper()
        sign = "+" if v >= 0 else ""
        bits.append(f"  • {k}: {sign}{v:,.1f} {unit}")
    if est.notes:
        bits.append("(주의: " + "; ".join(est.notes) + ")")
    return "\n".join(bits)


__all__ = [
    "LookupNotAvailable",
    "ImpactEstimate",
    "estimate_impact_by_category",
    "estimate_impact_by_item",
    "get_intensity_table",
    "estimate_summary_text",
    "lookup_status",
    "reload_lookup",
    "LOOKUP_PATH",
]
