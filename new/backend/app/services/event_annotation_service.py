"""
Event Annotation Service
========================
실무자가 차트 스파이크/AI 이상 알림에 대해 남기는 원인·조치 메모.

- 차트 marker / 검색 페이지 양쪽에서 사용.
- target 의미:
    * power / fuel / water / wastewater / production
    * overall — 특정 항목과 무관한 전반적 메모
- tag: 센서고장 / 설비정비 / 생산변경 / 외부요인 / 기타
- severity: info / warn / critical
"""
from __future__ import annotations

import logging

import pandas as pd

from app.database.db_connection import managed_connection, managed_cursor
from app.services.audit_service import get_current_user

logger = logging.getLogger(__name__)

EVENT_TARGETS: list[str] = ["overall", "power", "fuel", "water", "wastewater", "production"]
EVENT_TAGS: list[str] = ["센서고장", "설비정비", "생산변경", "외부요인", "기타"]
EVENT_SEVERITIES: list[str] = ["info", "warn", "critical"]

TARGET_LABELS: dict[str, str] = {
    "overall":    "전반",
    "power":      "전력",
    "fuel":       "연료",
    "water":      "용수",
    "wastewater": "폐수",
    "production": "생산",
}

# v5 모델 / prediction_log 의 target 코드 → event_annotation 의 target 코드 매핑
PRED_TARGET_TO_EVENT: dict[str, str] = {
    "power":      "power",
    "fuel":       "fuel",
    "water":      "water",
    "wastewater": "wastewater",
}


def add_event(
    factory: str,
    event_date: str,
    note: str,
    target: str = "overall",
    tag: str = "기타",
    severity: str = "info",
) -> int:
    """이벤트 메모 추가. 반환: 생성된 row id."""
    if not note or not note.strip():
        raise ValueError("메모 내용은 비어 있을 수 없습니다.")
    if target not in EVENT_TARGETS:
        target = "overall"
    if tag not in EVENT_TAGS:
        tag = "기타"
    if severity not in EVENT_SEVERITIES:
        severity = "info"

    user = get_current_user()
    with managed_cursor(admin=True) as (conn, cursor):
        cursor.execute(
            """
            INSERT INTO event_annotation
              (factory, event_date, target, tag, severity, note, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (factory, event_date, target, tag, severity, note.strip(), user),
        )
        new_id = cursor.lastrowid
        conn.commit()
        return int(new_id)


def update_event(event_id: int, note: str, tag: str | None = None, severity: str | None = None) -> bool:
    """기존 메모 수정. 반환: 수정 성공 여부."""
    if not note or not note.strip():
        raise ValueError("메모 내용은 비어 있을 수 없습니다.")
    user = get_current_user()
    with managed_cursor(admin=True) as (conn, cursor):
        sets = ["note=%s", "created_by=%s"]
        params: list = [note.strip(), user]
        if tag is not None and tag in EVENT_TAGS:
            sets.append("tag=%s"); params.append(tag)
        if severity is not None and severity in EVENT_SEVERITIES:
            sets.append("severity=%s"); params.append(severity)
        params.append(event_id)
        cursor.execute(
            f"UPDATE event_annotation SET {', '.join(sets)} WHERE id=%s",
            tuple(params),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_event(event_id: int) -> bool:
    """메모 삭제."""
    with managed_cursor(admin=True) as (conn, cursor):
        cursor.execute("DELETE FROM event_annotation WHERE id=%s", (event_id,))
        conn.commit()
        return cursor.rowcount > 0


def list_events(
    factory: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    target: str | None = None,
    limit: int = 500,
) -> pd.DataFrame:
    """조건에 맞는 이벤트 메모 조회 (최신 순)."""
    conditions = []
    params: list = []
    if factory and factory != "전체":
        conditions.append("factory=%s"); params.append(factory)
    if date_from:
        conditions.append("event_date >= %s"); params.append(date_from)
    if date_to:
        conditions.append("event_date <= %s"); params.append(date_to)
    if target and target != "전체":
        conditions.append("target=%s"); params.append(target)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT id, factory, event_date, target, tag, severity, note,
               created_at, updated_at, created_by
        FROM event_annotation
        {where}
        ORDER BY event_date DESC, id DESC
        LIMIT %s
    """
    params.append(int(limit))
    try:
        with managed_connection() as conn:
            return pd.read_sql_query(sql, conn, params=tuple(params))
    except Exception as exc:
        logger.exception(
            "Failed to list events factory=%s date_from=%s date_to=%s target=%s limit=%s: %s",
            factory,
            date_from,
            date_to,
            target,
            limit,
            exc,
        )
        return pd.DataFrame(columns=[
            "id","factory","event_date","target","tag","severity","note",
            "created_at","updated_at","created_by",
        ])


def list_events_for_chart(
    factory: str,
    date_from: str,
    date_to: str,
    targets: list[str] | None = None,
) -> pd.DataFrame:
    """차트 marker 용 — factory + 기간 + 관심 target 만 가져오기.

    targets 에는 'overall' 도 포함될 수 있으며, 'overall' 메모는 모든 차트에 공통 노출.
    """
    conditions = ["factory=%s", "event_date BETWEEN %s AND %s"]
    params: list = [factory, date_from, date_to]
    if targets:
        targets_with_overall = list(set(targets) | {"overall"})
        placeholders = ",".join(["%s"] * len(targets_with_overall))
        conditions.append(f"target IN ({placeholders})")
        params.extend(targets_with_overall)

    sql = f"""
        SELECT id, factory, event_date, target, tag, severity, note, created_by
        FROM event_annotation
        WHERE {' AND '.join(conditions)}
        ORDER BY event_date ASC, id ASC
    """
    try:
        with managed_connection() as conn:
            return pd.read_sql_query(sql, conn, params=tuple(params))
    except Exception as exc:
        logger.exception(
            "Failed to list chart events factory=%s date_from=%s date_to=%s targets=%s: %s",
            factory,
            date_from,
            date_to,
            targets,
            exc,
        )
        return pd.DataFrame()
