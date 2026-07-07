"""공장 코드 한글 변환 마이그레이션 실행기.

`app/database/migrations/2026_04_30_rename_factory_codes.sql` 을 읽어
관리자 권한으로 한 번에 실행합니다.

사용법:
    py -3 tools/run_factory_code_migration.py
    또는
    .venv\\Scripts\\python.exe tools\\run_factory_code_migration.py

전제: 프로젝트 루트의 .env 가 정상이고 MySQL 이 실행 중이어야 합니다.
멱등성: 두 번 실행해도 결과가 같습니다 (이미 한글로 변환된 환경에서는 0건 처리).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.database.db_connection import get_connection  # noqa: E402

MIGRATION_SQL = PROJECT_ROOT / "app" / "database" / "migrations" / "2026_04_30_rename_factory_codes.sql"

OLD_CODES = ("F10A", "F10B", "F10", "F20", "F30", "F40")
TABLES = ("energy_daily", "energy_daily_audit", "ai_reports", "prediction_log")


def _count_old_codes(cur) -> dict[str, int]:
    """각 테이블의 잔존 F-코드 행 수 집계."""
    placeholders = ", ".join(["%s"] * len(OLD_CODES))
    counts: dict[str, int] = {}
    for tbl in TABLES:
        cur.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE factory IN ({placeholders})",
            OLD_CODES,
        )
        counts[tbl] = int(cur.fetchone()[0])
    return counts


def main() -> int:
    if not MIGRATION_SQL.exists():
        print(f"[ERR] 마이그레이션 SQL을 찾을 수 없습니다: {MIGRATION_SQL}")
        return 2

    sql_text = MIGRATION_SQL.read_text(encoding="utf-8")
    # MySQL 클라이언트는 -- 한 줄 주석을 처리하지만, executemany 단위로
    # 분리할 때 BEGIN/COMMIT 까지 수동으로 처리해야 합니다. 여기서는
    # mysql.connector 가 한 번에 multi=True 로 받지만, 안정성을 위해
    # 의미 있는 SQL 만 추려 한 줄씩 실행합니다.
    statements: list[str] = []
    buf: list[str] = []
    for raw in sql_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        buf.append(line)
        if line.endswith(";"):
            stmt = " ".join(buf).rstrip(";").strip()
            buf = []
            if stmt:
                statements.append(stmt)

    # START TRANSACTION / COMMIT 은 connector 가 직접 다룹니다.
    statements = [s for s in statements if s.upper() not in {"START TRANSACTION", "COMMIT"}]

    conn = get_connection(admin=True)
    cur = conn.cursor()
    try:
        before = _count_old_codes(cur)
        print("=== BEFORE 잔존 F-코드 행 수 ===")
        for k, v in before.items():
            print(f"  {k:24s} {v:>6}")

        print(f"\n=== {len(statements)}개 SQL 실행 중 ===")
        for i, stmt in enumerate(statements, 1):
            cur.execute(stmt)
            print(f"  [{i:>3}/{len(statements)}] rowcount={cur.rowcount:>5}  {stmt[:90]}")
        conn.commit()

        after = _count_old_codes(cur)
        print("\n=== AFTER 잔존 F-코드 행 수 ===")
        ok = True
        for k, v in after.items():
            mark = "OK" if v == 0 else "FAIL"
            if v != 0:
                ok = False
            print(f"  {k:24s} {v:>6}   [{mark}]")

        print("\n검증: production_daily 는 의도적으로 F-코드 유지 (UI 표시 매핑 사용).")
        return 0 if ok else 1
    except Exception as exc:
        conn.rollback()
        print(f"[ERR] 마이그레이션 실패, 롤백: {exc}")
        return 1
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
