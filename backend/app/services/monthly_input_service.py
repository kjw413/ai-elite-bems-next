"""
Monthly Input Service
======================
경산처럼 일별 실적이 없는 (공장, 월) 구간의 energy_monthly/production_monthly
관리자 수기 입력 CRUD.

target_service.py 의 절감 목표 저장 패턴(연/공장 단위 조회 + 항목 일괄
upsert/삭제)을 그대로 따른다. 값이 전부 빈 항목은 저장이 아니라 해당
(공장, 월) 행 삭제로 처리한다 — upsert_targets() 의 관례와 동일.

이 서비스는 monthly_fallback_service 의 규칙 1(일별 우선)을 그대로 존중한다 —
여기서 저장한 값이라도 energy_daily/production_daily 에 그 달 행이 이미 있으면
화면에는 반영되지 않는다. 착오 입력을 조용히 숨기지 않도록 list_* 함수가
"그 달에 일별 데이터가 이미 있는지" 플래그를 함께 반환한다.
"""
from __future__ import annotations

import logging
from typing import Any

from app.database.db_connection import managed_cursor
from app.services.audit_service import get_current_user

logger = logging.getLogger(__name__)

ENERGY_COLUMNS: tuple[str, ...] = (
    "total_power_kwh", "fuel_nm3", "water_ton", "wastewater_ton",
    "power_cost_krw", "fuel_cost_krw",
)


def _month_key_valid(month_key: str) -> bool:
    parts = month_key.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        return False
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 1 <= month <= 12 and 2000 <= year <= 2100


# 'YYYY-MM' 문자열을 만드는 SQL 조각. DATE_FORMAT(date, '%Y-%m') 을 쓰지 않는다 —
# 파라미터가 있는 쿼리에서 드라이버가 '%' 를 자기 자리표시자로 해석해, '%%' 로
# 이스케이프하면 MySQL 에는 '%%Y-%%m' 이 그대로 도착해 리터럴 '%Y-%m' 이 돌아온다
# (2026-07-31 실측 — 커버리지 판정이 통째로 무력화됐다). CONCAT 은 '%' 가 없어 안전하다.
_MONTH_KEY_SQL = "CONCAT(YEAR({column}), '-', LPAD(MONTH({column}), 2, '0'))"


def _daily_covered_months(table: str, date_column: str, factory_column: str, factory: str) -> set[str]:
    """이미 일별 실적이 있는 (연,월) — 'YYYY-MM' 문자열 집합.

    입력 화면이 이 달을 수기로 채워도 monthly_fallback_service 규칙 1에 의해
    무시된다는 걸 사용자에게 미리 알리기 위함이다.
    """
    month_key = _MONTH_KEY_SQL.format(column=date_column)
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"SELECT DISTINCT {month_key} month_key FROM {table} WHERE {factory_column} = %s",
            (factory,),
        )
        return {str(row["month_key"]) for row in cursor.fetchall()}


def energy_daily_covered_months(factory: str) -> set[str]:
    """energy_daily 에 이미 일별 실적이 있는 (연,월) — 'YYYY-MM' 집합.

    저장된 월별 입력이 있든 없든 알아야 한다 — 입력 화면은 사용자가 값을 넣기
    **전에** "이 달은 일별이 우선이라 반영 안 된다"를 알려줘야 하기 때문이다.
    """
    return _daily_covered_months("energy_daily", "date", "factory", factory)


def list_energy_monthly(factory: str) -> list[dict[str, Any]]:
    """공장의 등록된 월별 에너지 입력 + 이미 일별 데이터가 있는 달 표시."""
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"""
            SELECT factory, month_key, {', '.join(ENERGY_COLUMNS)}, source, updated_at, changed_by
            FROM energy_monthly WHERE factory = %s ORDER BY month_key
            """,
            (factory,),
        )
        rows = cursor.fetchall()
    covered = energy_daily_covered_months(factory)
    for row in rows:
        row["hasDailyData"] = row["month_key"] in covered
    return rows


def upsert_energy_monthly(items: list[dict[str, Any]]) -> int:
    """월별 에너지 실적 일괄 저장.

    items: [{factory, month_key, total_power_kwh, fuel_nm3, water_ton,
             wastewater_ton, power_cost_krw, fuel_cost_krw}, ...]
    전 항목이 비어 있거나 0이면(입력 취소) 해당 (공장, 월) 행을 삭제한다.
    반환: 적용된 행 수.
    """
    if not items:
        return 0
    user = get_current_user()
    affected = 0
    with managed_cursor(admin=True) as (conn, cursor):
        try:
            for item in items:
                factory = str(item.get("factory", "")).strip()
                month_key = str(item.get("month_key", "")).strip()
                if not factory or not _month_key_valid(month_key):
                    continue
                raw_values: dict[str, Any] = {col: item.get(col) for col in ENERGY_COLUMNS}
                numeric_values: dict[str, float] = {}
                for col, raw in raw_values.items():
                    if raw is None or raw == "":
                        numeric_values[col] = 0.0
                        continue
                    try:
                        numeric_values[col] = float(raw)
                    except (TypeError, ValueError):
                        numeric_values[col] = 0.0
                if all(value == 0.0 for value in numeric_values.values()):
                    cursor.execute(
                        "DELETE FROM energy_monthly WHERE factory=%s AND month_key=%s",
                        (factory, month_key),
                    )
                else:
                    cursor.execute(
                        f"""
                        INSERT INTO energy_monthly
                            (factory, month_key, {', '.join(ENERGY_COLUMNS)}, source, changed_by)
                        VALUES (%s, %s, {', '.join(['%s'] * len(ENERGY_COLUMNS))}, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            {', '.join(f'{col} = VALUES({col})' for col in ENERGY_COLUMNS)},
                            source = VALUES(source), changed_by = VALUES(changed_by)
                        """,
                        (
                            factory, month_key,
                            *[numeric_values[col] for col in ENERGY_COLUMNS],
                            "manual", user,
                        ),
                    )
                affected += 1
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise


def list_production_monthly(factory: str) -> list[dict[str, Any]]:
    """공장의 등록된 월별 생산 입력 목록(production_monthly, Korean label 기준).

    "이미 일별 데이터가 있는 달" 플래그는 여기 포함하지 않는다 — production_daily
    는 F-code 를 쓰므로 그 판단은 F-code 매핑을 아는 호출자가
    production_monthly_covered_months() 로 따로 구해 붙인다.
    """
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            """
            SELECT factory, month_key, category2, planned_qty, actual_qty,
                   source, updated_at, changed_by
            FROM production_monthly WHERE factory = %s ORDER BY month_key, category2
            """,
            (factory,),
        )
        return cursor.fetchall()


def production_monthly_covered_months(production_daily_codes: tuple[str, ...]) -> set[str]:
    """이미 production_daily 에 일별 실적이 있는 (연,월) — F-code 기준 조회.

    production_daily 는 F-code 를 쓰므로 Korean label -> F-code 매핑은 호출자
    (server.py)가 담당한다 — 이 서비스가 도메인 매핑을 중복해서 알 필요는 없다.
    """
    if not production_daily_codes:
        return set()
    placeholders = ",".join(["%s"] * len(production_daily_codes))
    month_key = _MONTH_KEY_SQL.format(column="date")
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            f"SELECT DISTINCT {month_key} month_key FROM production_daily WHERE factory IN ({placeholders})",
            production_daily_codes,
        )
        return {str(row["month_key"]) for row in cursor.fetchall()}


def upsert_production_monthly(items: list[dict[str, Any]]) -> int:
    """월별 생산 실적 일괄 저장.

    items: [{factory, month_key, category2, planned_qty, actual_qty}, ...]
    planned_qty·actual_qty 가 둘 다 비어 있으면(입력 취소) 해당 행을 삭제한다.
    단위는 kg — production_daily.actual_qty 와 동일(화면이 ton 입력을 받는다면
    ×1000 은 호출부에서 변환해서 넘긴다).
    """
    if not items:
        return 0
    user = get_current_user()
    affected = 0
    with managed_cursor(admin=True) as (conn, cursor):
        try:
            for item in items:
                factory = str(item.get("factory", "")).strip()
                month_key = str(item.get("month_key", "")).strip()
                category2 = str(item.get("category2", "")).strip()
                if not factory or not category2 or not _month_key_valid(month_key):
                    continue
                planned_raw, actual_raw = item.get("planned_qty"), item.get("actual_qty")
                try:
                    planned = float(planned_raw) if planned_raw not in (None, "") else 0.0
                except (TypeError, ValueError):
                    planned = 0.0
                try:
                    actual = float(actual_raw) if actual_raw not in (None, "") else 0.0
                except (TypeError, ValueError):
                    actual = 0.0
                if planned == 0.0 and actual == 0.0:
                    cursor.execute(
                        "DELETE FROM production_monthly WHERE factory=%s AND month_key=%s AND category2=%s",
                        (factory, month_key, category2),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO production_monthly
                            (factory, month_key, category2, planned_qty, actual_qty, source, changed_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            planned_qty = VALUES(planned_qty), actual_qty = VALUES(actual_qty),
                            source = VALUES(source), changed_by = VALUES(changed_by)
                        """,
                        (factory, month_key, category2, planned, actual, "manual", user),
                    )
                affected += 1
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise
