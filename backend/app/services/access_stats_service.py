"""
Access Stats Service
====================
일 접속자 수 집계. 사내망 이용 현황을 운영 지표로 남기기 위한 모듈이다.

집계 단위
---------
- ``access_visit``  : (일자, 클라이언트 PC) 단위 상세.
- ``access_daily``  : 일자 단위 롤업. 화면·CSV가 읽는 단일 출처.

식별자는 PC 이름이다. 배포 주소부터 IP 대신 호스트명을 쓰고 있어, 운영자가
"누가 쓰는지" 를 확인할 때 보는 값과 집계에 남는 값이 같아야 읽힌다. 서버는
요청에서 IP 밖에 볼 수 없으므로 역방향 조회로 호스트명을 얻고(``resolve_client_name``),
실패하면 IP 문자열을 그대로 식별자로 쓴다 — 이름을 못 얻었다고 접속을 누락시키는
쪽이 더 나쁘다. 원본 IP 는 ``client_ip`` 에 따로 남겨 대조할 수 있게 한다.

롤업을 따로 두는 이유는 조회할 때마다 ``COUNT(DISTINCT)`` 를 돌리지 않기 위해서다.
롤업은 기록할 때마다 상세에서 다시 계산하므로 두 표가 어긋나지 않는다.

``source`` 가 그 행이 어디서 왔는지를 남긴다. 두 표 모두 같은 값을 쓴다.
  - ``live``     : 이 서비스가 실제 요청을 받아 적재한 값
  - ``backfill`` : 로깅 도입 이전 구간을 ``tools/backfill_access_daily.py`` 로 채운 값
집계·표시 계층은 두 값을 구분해서 다룰 수 있어야 하므로 API 응답까지 그대로 올린다.
적재 경로가 서로를 덮어쓰지 않게 하는 안전장치이기도 하다.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
from datetime import date, timedelta

from app.database.db_connection import DB_NAME, managed_cursor
from app.services.audit_service import get_current_user

logger = logging.getLogger(__name__)

# 행의 출처 값. 저장 시 이 목록 밖의 값은 거부한다.
SOURCE_LIVE = "live"
SOURCE_BACKFILL = "backfill"
VALID_SOURCES = (SOURCE_LIVE, SOURCE_BACKFILL)

# 호스트명 역방향 조회 캐시. 사내망 PC 이름은 세션 중 바뀌지 않으므로
# 프로세스 수명 동안 들고 있는다. 실패도 캐시한다 — 이름이 없는 IP 는 계속
# 없을 가능성이 높고, 매 요청마다 조회를 재시도하면 응답이 그만큼 느려진다.
_NAME_CACHE: dict[str, str] = {}
_NAME_CACHE_LOCK = threading.Lock()
_NAME_CACHE_LIMIT = 512

# 호스트명에 허용할 문자. 역방향 조회 결과는 외부 입력이므로 그대로 신뢰하지 않는다.
_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")

_VISIT_DDL = """
CREATE TABLE IF NOT EXISTS access_visit (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    visit_date    DATE         NOT NULL,
    client_name   VARCHAR(100) NOT NULL,
    client_ip     VARCHAR(45)  DEFAULT NULL,
    hit_count     INT          NOT NULL DEFAULT 1,
    source        VARCHAR(20)  NOT NULL DEFAULT 'live',
    first_seen_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_access_visit (visit_date, client_name),
    INDEX idx_access_visit_date (visit_date),
    INDEX idx_access_visit_name (client_name)
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

# 적재 경로가 실제로 쓰는 컬럼. 테이블이 있어도 구성이 다르면 INSERT 가
# "Unknown column" 으로 죽으므로, 적재 전에 이 목록으로 먼저 확인한다.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "access_visit": ("visit_date", "client_name", "client_ip", "hit_count", "source"),
    "access_daily": ("visit_date", "unique_users", "visit_count", "source"),
}

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


def table_status() -> dict:
    """집계 테이블의 존재·컬럼 구성 점검 결과.

    Returns
    -------
    dict
        ``{"connected": bool, "error": str | None, "database": str,
           "tables": {이름: {"exists": bool, "missing": [컬럼...]}}}``

    ``missing`` 이 비어 있지 않으면 예전 구성의 테이블이 남아 있다는 뜻이다 —
    ``CREATE TABLE IF NOT EXISTS`` 는 이미 있는 테이블의 구조를 바꾸지 않으므로
    그대로 두면 적재가 실패한다.
    """
    result: dict = {"connected": False, "error": None, "database": DB_NAME, "tables": {}}
    try:
        with managed_cursor(admin=True) as (_conn, cursor):
            for table, required in REQUIRED_COLUMNS.items():
                cursor.execute(
                    """
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    """,
                    (DB_NAME, table),
                )
                columns = {str(row[0]) for row in cursor.fetchall()}
                result["tables"][table] = {
                    "exists": bool(columns),
                    "missing": [name for name in required if name not in columns] if columns else list(required),
                }
        result["connected"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def recreate_tables() -> None:
    """집계 테이블을 지우고 현재 구성으로 다시 만든다.

    쌓인 기록도 함께 사라진다. 예전 구성의 테이블이 남아 적재가 막힐 때의
    탈출구이며, 호출부가 사용자 확인을 받은 뒤에만 부른다.
    """
    global _TABLES_READY
    with managed_cursor(admin=True) as (conn, cursor):
        cursor.execute("DROP TABLE IF EXISTS access_visit")
        cursor.execute("DROP TABLE IF EXISTS access_daily")
        cursor.execute(_VISIT_DDL)
        cursor.execute(_DAILY_DDL)
        conn.commit()
    _TABLES_READY = True


def normalize_client_name(raw: str) -> str:
    """역방향 조회 결과를 식별자로 쓸 수 있게 다듬는다.

    FQDN 은 첫 라벨만 남긴다 — ``BPN123456.corp.local`` 과 ``BPN123456`` 이 서로 다른
    PC 로 세어지면 접속자 수가 부풀려진다. 대문자 표기는 사내 PC 명명 규칙을 따른다.
    """
    name = _NAME_SAFE.sub("", (raw or "").strip()).split(".")[0]
    return name.upper()[:100]


def resolve_client_name(client_ip: str) -> str:
    """IP 에 대응하는 PC 이름. 얻지 못하면 IP 문자열을 그대로 돌려준다."""
    if not client_ip:
        return ""
    with _NAME_CACHE_LOCK:
        cached = _NAME_CACHE.get(client_ip)
    if cached is not None:
        return cached

    resolved = client_ip
    try:
        name = normalize_client_name(socket.gethostbyaddr(client_ip)[0])
        # localhost 류는 PC 이름으로 쓸 값이 아니다 — IP 를 그대로 둔다.
        if name and name not in {"LOCALHOST", "IP6-LOCALHOST", "IP6-LOOPBACK"}:
            resolved = name
    except Exception as exc:
        logger.debug("reverse lookup failed for %s: %s", client_ip, exc)

    with _NAME_CACHE_LOCK:
        if len(_NAME_CACHE) >= _NAME_CACHE_LIMIT:
            _NAME_CACHE.clear()
        _NAME_CACHE[client_ip] = resolved
    return resolved


def _rollup_from_visits(cursor, visit_date: date, source: str) -> None:
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
        (source, get_current_user(), visit_date),
    )


def record_visit(client_ip: str, visit_date: date | None = None) -> None:
    """접속 1건 기록. 실패해도 호출한 API 응답을 막지 않는다.

    같은 PC 가 같은 날 여러 번 들어오면 ``hit_count`` 만 올라가고 접속자 수는
    1대로 유지된다.
    """
    if not client_ip or client_ip == "unknown":
        return
    client_name = resolve_client_name(client_ip)
    if not client_name:
        return
    visit_date = visit_date or date.today()
    _ensure_tables()
    try:
        with managed_cursor(admin=True) as (conn, cursor):
            cursor.execute(
                """
                INSERT INTO access_visit (visit_date, client_name, client_ip, hit_count, source)
                VALUES (%s, %s, %s, 1, %s)
                ON DUPLICATE KEY UPDATE
                  hit_count = hit_count + 1,
                  client_ip = VALUES(client_ip),
                  source    = VALUES(source)
                """,
                (visit_date, client_name, client_ip, SOURCE_LIVE),
            )
            _rollup_from_visits(cursor, visit_date, SOURCE_LIVE)
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


def get_client_totals(date_from: date, date_to: date, limit: int = 50) -> list[dict]:
    """기간 내 PC 별 집계 — 접속일 수와 총 접속 횟수."""
    _ensure_tables()
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    try:
        with managed_cursor(dictionary=True) as (_conn, cursor):
            cursor.execute(
                """
                SELECT client_name,
                       COUNT(*)                  AS active_days,
                       COALESCE(SUM(hit_count), 0) AS visit_count,
                       MAX(visit_date)           AS last_date
                  FROM access_visit
                 WHERE visit_date BETWEEN %s AND %s
                 GROUP BY client_name
                 ORDER BY visit_count DESC, client_name
                 LIMIT %s
                """,
                (date_from, date_to, int(limit)),
            )
            rows = cursor.fetchall()
    except Exception as exc:
        logger.warning("access_stats get_client_totals failed: %s", exc)
        return []
    return [
        {
            "clientName": str(row["client_name"]),
            "activeDays": int(row["active_days"] or 0),
            "visitCount": int(row["visit_count"] or 0),
            "lastDate": row["last_date"],
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


def upsert_visits(rows: list[dict], source: str = SOURCE_BACKFILL, overwrite: bool = False) -> dict:
    """상세 접속 행을 적재하고, 건드린 일자의 롤업을 다시 계산한다.

    Parameters
    ----------
    rows : list[dict]
        ``{"date": date, "clientName": str, "visitCount": int}`` 형태.
    source : str
        적재하는 행의 출처 표기. ``VALID_SOURCES`` 중 하나여야 한다.
    overwrite : bool
        False(기본)면 이미 상세나 롤업이 있는 일자는 통째로 건너뛴다. 실제 기록
        (``live``)을 나중 적재가 덮어쓰는 사고를 막기 위한 기본값이다.

    Returns
    -------
    dict
        ``{"inserted": int, "days": int, "skippedDays": int}``
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}: {source!r}")
    if not rows:
        return {"inserted": 0, "days": 0, "skippedDays": 0}

    _ensure_tables()
    wanted_days = sorted({row["date"] for row in rows})
    inserted = 0

    with managed_cursor(admin=True) as (conn, cursor):
        cursor.execute("SELECT DISTINCT visit_date FROM access_visit")
        taken = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT visit_date FROM access_daily")
        taken |= {row[0] for row in cursor.fetchall()}

        target_days = wanted_days if overwrite else [day for day in wanted_days if day not in taken]
        skipped_days = len(wanted_days) - len(target_days)
        if not target_days:
            return {"inserted": 0, "days": 0, "skippedDays": skipped_days}

        target_set = set(target_days)
        if overwrite:
            # 덮어쓸 일자의 기존 상세를 먼저 지운다. 남겨두면 이번에 넣지 않은
            # PC 가 그대로 살아남아 그 날 접속자 수가 실제보다 많아진다.
            cursor.executemany(
                "DELETE FROM access_visit WHERE visit_date = %s",
                [(day,) for day in target_days],
            )

        for row in rows:
            if row["date"] not in target_set:
                continue
            visit_count = max(1, int(row.get("visitCount", 1)))
            cursor.execute(
                """
                INSERT INTO access_visit (visit_date, client_name, client_ip, hit_count, source)
                VALUES (%s, %s, NULL, %s, %s)
                ON DUPLICATE KEY UPDATE
                  hit_count = VALUES(hit_count),
                  source    = VALUES(source)
                """,
                (row["date"], row["clientName"], visit_count, source),
            )
            inserted += 1

        for day in target_days:
            _rollup_from_visits(cursor, day, source)
        conn.commit()

    return {"inserted": inserted, "days": len(target_days), "skippedDays": skipped_days}
