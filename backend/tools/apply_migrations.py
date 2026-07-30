r"""energy_daily 등 멱등 ALTER 마이그레이션 적용 스크립트.

배경
----
``db_connection._apply_idempotent_migrations()`` 는 ``init_db()`` 안에서만 호출되는데,
``init_db()`` 를 부르는 코드가 저장소에 없다 — legacy Streamlit 앱이 기동 시 호출하던
함수이고, 현재 배포는 ``uvicorn backend.server:app`` 뿐이라 아무도 부르지 않는다.
그래서 ``_PENDING_COLUMN_MIGRATIONS`` 에 항목을 추가해도 **자동으로 적용되지 않는다.**

컬럼을 추가한 뒤 이 스크립트를 실행하지 않으면, 다음 동기화의 INSERT 가
``Unknown column 'power_cost_krw' in 'field list'`` 로 실패한다. 조용히 넘어가지 않고
동기화 전체가 멈추므로, 신규 컬럼 작업의 마지막 단계로 반드시 실행해야 한다.

멱등하다 — INFORMATION_SCHEMA 로 컬럼 존재를 확인한 뒤 없는 것만 ALTER 한다.
몇 번 실행해도 안전하고, 이미 적용됐으면 아무 것도 하지 않는다.

실행 (쓰기에는 관리자 계정 필요 — backend/.env 의 DB_ADMIN_* 를 읽는다)
----------------------------------------------------------------------
Git Bash::

    .venv/Scripts/python.exe backend/tools/apply_migrations.py

PowerShell::

    .venv\Scripts\python.exe backend\tools\apply_migrations.py

``--dry-run`` 을 주면 적용될 항목만 출력하고 DB 를 바꾸지 않는다.

되돌리기
--------
``ADD COLUMN ... NOT NULL DEFAULT 0`` 은 기존 행의 다른 값을 건드리지 않으므로 데이터
손실 위험은 없다. 그래도 되돌려야 하면::

    ALTER TABLE energy_daily
      DROP COLUMN power_cost_krw,      DROP COLUMN power_price_krw_kwh,
      DROP COLUMN fuel_cost_krw,       DROP COLUMN fuel_price_krw_nm3,
      DROP COLUMN influent_cod_ppm,    DROP COLUMN effluent_cod_ppm;
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.db_connection import (  # noqa: E402
    DB_NAME,
    _apply_idempotent_migrations,
    _PENDING_COLUMN_DROPS,
    _PENDING_COLUMN_MIGRATIONS,
    managed_cursor,
)


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (DB_NAME, table, column),
    )
    (count,) = cursor.fetchone()
    return count > 0


def report_pending() -> int:
    """적용 대기 중인 항목을 출력하고 그 개수를 반환한다."""
    pending = 0
    with managed_cursor(with_db=True, admin=True) as (_conn, cursor):
        for table, column, _fragment in _PENDING_COLUMN_MIGRATIONS:
            if not _column_exists(cursor, table, column):
                print(f"  ADD  {table}.{column}")
                pending += 1
        for table, column, _fragment in _PENDING_COLUMN_DROPS:
            if _column_exists(cursor, table, column):
                print(f"  DROP {table}.{column}")
                pending += 1
    return pending


def main() -> int:
    parser = argparse.ArgumentParser(description="멱등 ALTER 마이그레이션 적용")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="적용될 항목만 출력하고 DB 는 바꾸지 않는다",
    )
    args = parser.parse_args()

    print(f"대상 스키마: {DB_NAME}")
    print("적용 대기 항목:")
    try:
        pending = report_pending()
    except Exception as exc:
        print(f"\n실패: DB 에 연결하거나 스키마를 읽을 수 없습니다 — {exc}")
        print("backend/.env 의 DB_ADMIN_USER / DB_ADMIN_PASSWORD / DB_HOST 를 확인하세요.")
        return 1

    if pending == 0:
        print("  (없음 - 이미 모두 적용됨)")
        return 0

    if args.dry_run:
        print(f"\ndry-run: {pending}건이 적용 대기 중입니다. DB 는 바꾸지 않았습니다.")
        return 0

    print()
    try:
        _apply_idempotent_migrations()
    except Exception as exc:
        print(f"실패: 마이그레이션 적용 중 오류 — {exc}")
        return 1
    print(f"\n완료: {pending}건 적용")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
