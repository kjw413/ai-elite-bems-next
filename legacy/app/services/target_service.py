"""
Savings Target Service
======================
KPI 요약 카드의 "목표 대비 X%" 산출을 위한 절감 목표 관리.

- 공장 × 지표 × 연도 단위로 목표를 저장.
- 목표 의미:
    * 원단위 3종(power_per_ton, fuel_per_ton, water_per_ton):
      "전년 대비 X% 절감" → 목표값 = 전년동기 × (1 - X/100), 낮을수록 좋음.
      (폐수 원단위는 폐기 — 폐수/용수 비는 목표 없이 현황만 표시하므로 제외)
    * 생산량(mix_prod):
      "전년 대비 X% 증가" → 목표값 = 전년동기 × (1 + X/100), 높을수록 좋음.
- 미입력(목표 없음) 시 KPI 카드 진척률 막대 비표시.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.database.db_connection import managed_cursor
from app.services.audit_service import get_current_user

logger = logging.getLogger(__name__)

# 카드/모달에서 사용하는 표준 metric 키 — schema.sql 와 동기.
# (폐수 원단위는 폐기 — 폐수/용수 비는 목표 없이 현황만 표시)
TARGET_METRICS: list[str] = [
    "power_per_ton",
    "fuel_per_ton",
    "water_per_ton",
    "mix_prod",
]

# 모달 입력에서 노출할 공장 목록(전사 = ALL).
TARGET_FACTORIES: list[str] = ["ALL", "남양주", "김해", "광주", "논산", "경산"]

# UI 라벨 (한국어).
METRIC_LABELS: dict[str, str] = {
    "power_per_ton":      "전력 원단위 (kWh/mix-ton)",
    "fuel_per_ton":       "연료 원단위 (Nm³/mix-ton)",
    "water_per_ton":      "용수 원단위 (ton/mix-ton)",
    "mix_prod":           "생산량(DB 실적) (ton)",
}

FACTORY_LABELS: dict[str, str] = {
    "ALL":   "전사",
    "남양주": "남양주",
    "김해":   "김해",
    "광주":   "광주",
    "논산":   "논산",
    "경산":   "경산",
}

# 생산량(증가 목표) vs 원단위(절감 목표) 구분
INCREASE_METRICS = frozenset({"mix_prod"})


def get_targets(year: int, factories: Iterable[str] | None = None) -> dict[tuple[str, str], float]:
    """주어진 연도의 목표값을 (factory, metric) -> target_pct 형태로 반환."""
    try:
        with managed_cursor() as (_conn, cursor):
            facs = list(factories or [])
            if facs:
                placeholders = ",".join(["%s"] * len(facs))
                cursor.execute(
                    f"SELECT factory, metric, target_pct FROM savings_target "
                    f"WHERE year=%s AND factory IN ({placeholders})",
                    (year, *facs),
                )
            else:
                cursor.execute(
                    "SELECT factory, metric, target_pct FROM savings_target WHERE year=%s",
                    (year,),
                )
            return {(row[0], row[1]): float(row[2]) for row in cursor.fetchall()}
    except Exception as exc:
        logger.exception("Failed to fetch savings targets for year=%s factories=%s: %s", year, factories, exc)
        return {}


def upsert_targets(year: int, items: list[dict], note: str | None = None) -> int:
    """
    목표 일괄 저장.

    items: [{factory, metric, target_pct}, ...]
    target_pct 가 None 또는 빈 값이면 해당 (factory, metric) 행을 삭제합니다.
    반환: 적용된 행 수.
    """
    if not items:
        return 0
    user = get_current_user()
    affected = 0
    with managed_cursor(admin=True) as (conn, cursor):
        try:
            for it in items:
                factory = str(it.get("factory", "")).strip()
                metric = str(it.get("metric", "")).strip()
                raw = it.get("target_pct")
                if not factory or not metric or metric not in TARGET_METRICS:
                    continue
                if raw is None or raw == "" or (isinstance(raw, float) and raw != raw):  # NaN
                    cursor.execute(
                        "DELETE FROM savings_target WHERE factory=%s AND year=%s AND metric=%s",
                        (factory, year, metric),
                    )
                else:
                    try:
                        pct = float(raw)
                    except (TypeError, ValueError):
                        continue
                    cursor.execute(
                        """
                        INSERT INTO savings_target (factory, year, metric, target_pct, note, changed_by)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          target_pct = VALUES(target_pct),
                          note       = VALUES(note),
                          changed_by = VALUES(changed_by)
                        """,
                        (factory, year, metric, pct, note, user),
                    )
                affected += 1
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise


def compute_progress_pct(
    current_value: float | None,
    prev_value: float | None,
    target_pct: float | None,
    is_increase_metric: bool,
) -> float | None:
    """
    KPI 카드의 "목표 대비 X%" 값 계산.

    is_increase_metric:
      • True  → 생산량처럼 높을수록 좋은 지표. 목표값 = prev × (1 + target_pct/100)
      • False → 원단위처럼 낮을수록 좋은 지표. 목표값 = prev × (1 - target_pct/100)

    반환:
      • None  → 입력값이 비어 진척률 계산 불가
      • float → 100을 기준으로 비교 (해석 방향은 is_increase_metric 에 따라 다름)
    """
    if current_value is None or prev_value is None or target_pct is None:
        return None
    if prev_value == 0:
        return None
    if is_increase_metric:
        target_value = prev_value * (1 + target_pct / 100.0)
    else:
        target_value = prev_value * (1 - target_pct / 100.0)
    if target_value == 0:
        return None
    return (current_value / target_value) * 100.0
