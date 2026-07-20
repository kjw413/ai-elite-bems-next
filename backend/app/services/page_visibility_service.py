"""
Page Visibility Service
========================
관리자가 조회 사용자(viewer)에게 보여줄 화면을 제어하기 위한 설정.
예측 모델처럼 아직 안정화되지 않은 화면을 숨기거나, 데모·테스트 목적으로
노출 범위를 조정할 때 사용한다(2026-07). 관리자 화면에는 이 설정과 무관하게
항상 모든 페이지가 보인다 — 프런트(BemsApp)가 role=admin이면 필터링을 생략한다.
"""
from __future__ import annotations

import logging

from app.database.db_connection import managed_cursor
from app.services.audit_service import get_current_user

logger = logging.getLogger(__name__)

# 프런트 lib/bems-pages.ts의 PageId와 1:1 대응 — 새 화면을 추가하면 양쪽에 함께 반영한다.
PAGE_KEYS: list[str] = ["dashboard", "energy", "intensity", "production", "prediction", "report", "admin"]

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS page_visibility (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    page_key           VARCHAR(40)  NOT NULL,
    visible_to_viewer  TINYINT(1)   NOT NULL DEFAULT 1,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    changed_by         TEXT,
    UNIQUE KEY uq_page_visibility (page_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_TABLE_READY = False


def _ensure_table() -> None:
    """schema.sql 적용 안 된 기존 DB 환경에서도 동작하도록 1회 보장."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        with managed_cursor(admin=True) as (conn, cursor):
            cursor.execute(_TABLE_DDL)
            conn.commit()
        _TABLE_READY = True
    except Exception as exc:
        logger.warning("page_visibility _ensure_table skipped: %s", exc)
        _TABLE_READY = True


def get_visibility() -> dict[str, bool]:
    """전체 페이지 키의 노출 여부. 행이 없는 키는 기본값(True)."""
    _ensure_table()
    result = {key: True for key in PAGE_KEYS}
    try:
        with managed_cursor(dictionary=True) as (_conn, cursor):
            cursor.execute("SELECT page_key, visible_to_viewer FROM page_visibility")
            for row in cursor.fetchall():
                if row["page_key"] in result:
                    result[row["page_key"]] = bool(row["visible_to_viewer"])
    except Exception as exc:
        logger.warning("page_visibility get_visibility failed, defaulting to all visible: %s", exc)
    return result


def set_visibility(updates: dict[str, bool]) -> dict[str, bool]:
    """전달된 키만 갱신(부분 업데이트) 후 전체 상태를 반환."""
    _ensure_table()
    user = get_current_user()
    valid = {key: bool(value) for key, value in updates.items() if key in PAGE_KEYS}
    if valid:
        with managed_cursor(admin=True) as (conn, cursor):
            for key, visible in valid.items():
                cursor.execute(
                    """
                    INSERT INTO page_visibility (page_key, visible_to_viewer, changed_by)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      visible_to_viewer = VALUES(visible_to_viewer),
                      changed_by = VALUES(changed_by)
                    """,
                    (key, int(visible), user),
                )
            conn.commit()
    return get_visibility()
