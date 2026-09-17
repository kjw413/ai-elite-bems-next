"""
Access Stats Service
====================
일 접속자 수 집계. 사내망 이용 현황을 운영 지표로 남기기 위한 모듈이다.

집계 단위
---------
- ``access_visit``  : (일자, 클라이언트 IP) 단위 상세. 사내망에서는 IP가 사실상
  PC 1대 = 사용자 1명이라, 기존 권한 판정(``client_is_admin``)과 같은 식별자를 쓴다.
- ``access_daily``  : 일자 단위 롤업. 화면·CSV가 읽는 단일 출처.

롤업을 따로 두는 이유는 두 가지다. 첫째, 조회할 때마다 ``COUNT(DISTINCT)`` 를 돌리지
않아도 되고, 둘째, 상세 행이 없는 과거 구간(로깅 도입 이전)도 같은 표에서 함께
표현할 수 있다. 상세가 있는 날의 롤업은 매 기록 시 상세로부터 다시 계산하므로
두 표가 어긋나지 않는다.

``access_daily.source`` 가 그 날의 숫자가 어디서 왔는지를 남긴다.
  - ``live``     : 이 서비스가 실제 요청을 받아 적재한 값
  - ``backfill`` : 로깅 도입 이전 구간을 ``tools/backfill_access_daily.py`` 로 채운 값
집계·표시 계층은 두 값을 구분해서 다룰 수 있어야 하므로 API 응답까지 그대로 올린다.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from app.database.db_connection import managed_cursor
from app.services.audit_service import get_current_user

logger = logging.getLogger(__name__)

# 롤업 행의 출처 값. 저장 시 이 목록 밖의 값은 거부한다.
SOURCE_LIVE = "live"
SOURCE_BACKFILL = "backfill"
VALID_SOURCES = (SOURCE_LIVE, SOURCE_BACKFILL)

_VISIT_DDL = """
CREATE TABLE IF NOT EXISTS access_visit (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    visit_date    DATE         NOT NULL,
    client_ip     VARCHAR(45)  NOT NULL,
    hit_count     INT          NOT NULL DEFAULT 1,
    first_seen_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_access_visit (visit_date, client_ip),
    INDEX idx_access_visit_date (visit_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS access_daily (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    visit_date   DATE        NOT NULL,
    unique_users INT         NOT NULL DEFAULT 0,
    visit_count  INT         NOT NULL DEFAULT 0,
    source       VARCHAR(20) NOT NULL DEFAULT 'live',
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    changed_by   TEXT,
    UNIQUE KEY uq_access_daily (visit_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_TABLES_READY = False


def _ensure_tables() -> None:
    """schema.sql 적용 안 된 기존 DB 환경에서도 동작하도록 1회 보장."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        with managed_cursor(admin=True) as (conn, cursor):
            cursor.execute(_VISIT_DDL)
            cursor.execute(_DAILY_DDL)
            conn.commit()
    except Exception as exc:
        logger.warning("access_stats _ensure_tables skipped: %s", exc)
    _TABLES_READY = True


def _rollup_from_visits(cursor, visit_date: date) -> None:
    """해당 일자의 상세를 다시 세어 롤업 행에 반영한다.

    증분 가산이 아니라 매번 재계산이라 상세와 롤업이 어긋날 여지가 없다.
    상세가 하루치(사내 규모에서 수십 행)뿐이라 비용도 무시할 만하다.
    """
    cursor.execute(
        """
        INSERT INTO access_daily (visit_date, unique_users, visit_count, source, changed_by)
        SELECT visit_date, COUNT(*), COALESCE(SUM(hit_count), 0), %s, %s
          FROM access_visit
         WHERE visit_date = %s
         GROUP BY visit_date
        ON DUPLICATE KEY UPDATE
          unique_users = VALUES(unique_users),
          visit_count  = VALUES(visit_count),
          source       = VALUES(source),
          changed_by   = VALUES(changed_by)
        """,
        (SOURCE_LIVE, get_current_user(), visit_date),
    )


def record_visit(client_ip: str, visit_date: date | None = None) -> None:
    """접속 1건 기록. 실패해도 호출한 API 응답을 막지 않는다.

    같은 IP가 같은 날 여러 번 들어오면 ``hit_count`` 만 올라가고 접속자 수는
    1명으로 유지된다.
    """
    if not client_ip or client_ip == "unknown":
        return
    visit_date = visit_date or date.today()
    _ensure_tables()
    try:
        with managed_cursor(admin=True) as (conn, cursor):
            cursor.execute(
                """
                INSERT INTO access_visit (visit_date, client_ip, hit_count)
                VALUES (%s, %s, 1)
                ON DUPLICATE KEY UPDATE hit_count = hit_count + 1
                """,
                (visit_date, client_ip),
            )
            _rollup_from_visits(cursor, visit_date)
            conn.commit()
    except Exception as exc:
        logger.warning("access_stats record_visit failed (%s): %s", client_ip, exc)


def get_daily_counts(date_from: date, date_to: date) -> list[dict]:
    """기간 내 일자별 접속 집계. 기록이 없는 날은 행 자체가 없다."""
    _ensure_tables()
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    try:
        with managed_cursor(dictionary=True) as (_conn, cursor):
            cursor.execute(
                """
                SELECT visit_date, unique_users, visit_count, source
                  FROM access_daily
                 WHERE visit_date BETWEEN %s AND %s
                 ORDER BY visit_date
                """,
                (date_from, date_to),
            )
            rows = cursor.fetchall()
    except Exception as exc:
        logger.warning("access_stats get_daily_counts failed: %s", exc)
        return []
    return [
        {
            "date": row["visit_date"],
            "uniqueUsers": int(row["unique_users"] or 0),
            "visitCount": int(row["visit_count"] or 0),
            "source": str(row["source"] or SOURCE_LIVE),
        }
        for row in rows
    ]


def summarize(days: list[dict]) -> dict:
    """일자별 행에서 요약 지표를 만든다.

    평균은 기록이 있는 날만 분모로 삼는다 — 주말·공휴일처럼 애초에 가동하지
    않는 날까지 0으로 깔면 실제 이용 수준보다 낮게 보인다.
    """
    active = [row for row in days if row["uniqueUsers"] > 0]
    if not active:
        return {
            "activeDays": 0,
            "totalVisits": 0,
            "avgUniqueUsers": 0.0,
            "peakUniqueUsers": 0,
            "peakDate": None,
            "firstDate": None,
            "lastDate": None,
            "liveDays": 0,
            "backfilledDays": 0,
        }
    peak = max(active, key=lambda row: row["uniqueUsers"])
    return {
        "activeDays": len(active),
        "totalVisits": sum(row["visitCount"] for row in active),
        "avgUniqueUsers": round(sum(row["uniqueUsers"] for row in active) / len(active), 1),
        "peakUniqueUsers": peak["uniqueUsers"],
        "peakDate": peak["date"],
        "firstDate": active[0]["date"],
        "lastDate": active[-1]["date"],
        "liveDays": sum(1 for row in active if row["source"] == SOURCE_LIVE),
        "backfilledDays": sum(1 for row in active if row["source"] == SOURCE_BACKFILL),
    }


def default_window(today: date | None = None, days: int = 90) -> tuple[date, date]:
    """조회 기본 구간 — 오늘 포함 최근 ``days`` 일."""
    today = today or date.today()
    return today - timedelta(days=days - 1), today


def upsert_daily(
    rows: list[dict],
    source: str = SOURCE_BACKFILL,
    overwrite: bool = False,
) -> dict:
    """일자 단위 롤업을 직접 적재한다.

    Parameters
    ----------
    rows : list[dict]
        ``{"date": date, "uniqueUsers": int, "visitCount": int}`` 형태.
    source : str
        적재하는 행의 출처 표기. ``VALID_SOURCES`` 중 하나여야 한다.
    overwrite : bool
        False(기본)면 이미 있는 일자는 건너뛴다. 실제 기록(``live``)을
        나중 적재가 덮어쓰는 사고를 막기 위한 기본값이다.

    Returns
    -------
    dict
        ``{"inserted": int, "updated": int, "skipped": int}``
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}: {source!r}")
    if not rows:
        return {"inserted": 0, "updated": 0, "skipped": 0}

    _ensure_tables()
    user = get_current_user()
    inserted = updated = skipped = 0

    with managed_cursor(admin=True) as (conn, cursor):
        cursor.execute("SELECT visit_date FROM access_daily")
        existing = {row[0] for row in cursor.fetchall()}
        for row in rows:
            visit_date = row["date"]
            unique_users = int(row["uniqueUsers"])
            visit_count = int(row.get("visitCount", unique_users))
            if visit_date in existing and not overwrite:
                skipped += 1
                continue
            cursor.execute(
                """
                INSERT INTO access_daily (visit_date, unique_users, visit_count, source, changed_by)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  unique_users = VALUES(unique_users),
                  visit_count  = VALUES(visit_count),
                  source       = VALUES(source),
                  changed_by   = VALUES(changed_by)
                """,
                (visit_date, unique_users, visit_count, source, user),
            )
            if visit_date in existing:
                updated += 1
            else:
                inserted += 1
        conn.commit()

    return {"inserted": inserted, "updated": updated, "skipped": skipped}
