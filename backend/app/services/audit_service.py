"""
Audit Service
=============
변경 이력(감사) 관리.
"""
# 이 파일은 데이터 변경 이력을 기록하고 조회합니다.

import getpass
from datetime import datetime
from app.database.db_connection import managed_cursor


# 현재 사용자 값을 가져옵니다.
def get_current_user() -> str:
    """현재 Windows OS 사용자 이름 반환."""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


# 변경 이력을 기록합니다.
def record_audit(
    factory: str,
    date: str,
    column_name: str,
    old_value,
    new_value,
    change_type: str = "MANUAL",
    changed_by: str = None,
):
    """
    변경 이력 1건 기록.

    Parameters
    ----------
    factory : str
    date : str
    column_name : str
    old_value : 이전 값
    new_value : 새 값
    change_type : UPLOAD / MANUAL / WEB_ROW_EDIT
    changed_by : 변경자 (None이면 OS 사용자)
    """
    if changed_by is None:
        changed_by = get_current_user()

    with managed_cursor() as (conn, cursor):
        cursor.execute(
            """
            INSERT INTO energy_daily_audit
                (factory, date, column_name, old_value, new_value, change_type, changed_at, changed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                factory,
                date,
                column_name,
                str(old_value) if old_value is not None else None,
                str(new_value) if new_value is not None else None,
                change_type,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                changed_by,
            ),
        )
        conn.commit()


# 변경 이력 일괄을 기록합니다.
def record_audit_batch(audit_records: list[dict]):
    """
    변경 이력 일괄 기록.

    Parameters
    ----------
    audit_records : list[dict]
        각 dict에는 factory, date, column_name, old_value, new_value, change_type 포함
    """
    if not audit_records:
        return

    changed_by = get_current_user()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with managed_cursor() as (conn, cursor):
        cursor.executemany(
            """
            INSERT INTO energy_daily_audit
                (factory, date, column_name, old_value, new_value, change_type, changed_at, changed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    r["factory"],
                    r["date"],
                    r["column_name"],
                    str(r.get("old_value", "")) if r.get("old_value") is not None else None,
                    str(r.get("new_value", "")) if r.get("new_value") is not None else None,
                    r.get("change_type", "MANUAL"),
                    now,
                    changed_by,
                )
                for r in audit_records
            ],
        )
        conn.commit()


# 변경 이력 이력 값을 가져옵니다.
def get_audit_history(
    factory: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 500,
) -> list[dict]:
    """
    변경 이력 조회.

    Returns
    -------
    list[dict]
    """
    query = "SELECT * FROM energy_daily_audit WHERE 1=1"
    params = []

    if factory and factory != "전체":
        query += " AND factory = %s"
        params.append(factory)
    if date_from:
        query += " AND date >= %s"
        params.append(date_from)
    if date_to:
        query += " AND date <= %s"
        params.append(date_to)

    query += " ORDER BY changed_at DESC LIMIT %s"
    params.append(limit)

    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(query, tuple(params))
        result = cursor.fetchall()
        return result
