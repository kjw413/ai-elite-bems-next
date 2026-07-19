r"""energy_daily 재동기화 복구 스크립트 (원본 엑셀 -> DB).

배경
----
로컬 MySQL 복구 당시 ``energy_daily`` / ``prediction_log`` / ``anomaly_analysis`` 3개
테이블은 IMPORT TABLESPACE 실패로 **빈 테이블**로 남았다. 이 중 대시보드의 주
데이터원인 ``energy_daily`` 는 원본 에너지 엑셀(``RawDB_에너지.xlsx``)에서 앱의 정규
동기화 경로로 재생성할 수 있다.

주의 (2026-07-18 확인)
----------------------
- Downloads 의 ``Sampled DB`` 는 **샘플 export** 로, 에너지 엑셀은 2021-01-01 ~
  2026-05-12 까지만 담고 있다(생산실적 DB 는 2026-07-15 까지). 이 파일로 동기화하면
  energy_daily 가 2026-05-12 까지 채워지고 최근 약 2개월은 비어 있다. **완전/최신**
  복구는 사내 서버의 mysqldump(energy_daily/prediction_log/anomaly_analysis) 복원이
  정답이다.
- 같은 폴더의 ``DB_생산실적_DW.xlsx`` 는 구 시스템 형식이라 이 스크립트는 생산실적을
  건드리지 않는다(production_daily 는 이미 채워져 있음). ``--also-production`` 을
  명시했을 때만 시도한다.

실행 (쓰기에는 관리자 root 계정 필요)
-------------------------------------
Git Bash::

    DB_ADMIN_USER=root DB_ADMIN_PASSWORD='<root_pw>' \
    SAMPLED_DB_DIR='C:/Users/Jong_u/Downloads/Sampled DB/Sampled DB' \
    .venv/Scripts/python.exe backend/tools/restore_energy_from_excel.py

PowerShell::

    $env:DB_ADMIN_USER='root'; $env:DB_ADMIN_PASSWORD='<root_pw>'
    $env:SAMPLED_DB_DIR='C:\Users\Jong_u\Downloads\Sampled DB\Sampled DB'
    .venv\Scripts\python.exe backend\tools\restore_energy_from_excel.py

옵션: ``--source <에너지엑셀경로>`` 로 파일을 직접 지정할 수 있다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv

load_dotenv(BACKEND_ROOT / ".env")


def _resolve_source(cli_source: str | None) -> Path | None:
    """에너지 원본 엑셀 경로 확정 - CLI > ENERGY_SOURCE_XLSX > SAMPLED_DB_DIR > Downloads 사본."""
    candidates: list[Path] = []
    if cli_source:
        candidates.append(Path(cli_source))
    env_file = os.getenv("ENERGY_SOURCE_XLSX")
    if env_file:
        candidates.append(Path(env_file))
    sampled = os.getenv("SAMPLED_DB_DIR")
    if sampled:
        candidates.append(Path(sampled) / "RawDB_에너지.xlsx")
    candidates.append(Path(r"E:\Sampled DB") / "RawDB_에너지.xlsx")
    candidates.append(Path.home() / "Downloads" / "Sampled DB" / "Sampled DB" / "RawDB_에너지.xlsx")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _row_count(table: str) -> int | None:
    from app.database.db_connection import execute_query

    try:
        rows = execute_query(f"SELECT COUNT(*) AS n FROM {table}")
        return int(rows[0]["n"]) if rows else 0
    except Exception as exc:  # pragma: no cover - diagnostic path
        print(f"  ! {table} 조회 실패: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="원본 엑셀로 energy_daily 재동기화")
    parser.add_argument("--source", help="RawDB_에너지.xlsx 경로 (미지정 시 자동 탐색)")
    parser.add_argument("--also-production", action="store_true",
                        help="production_daily 도 동기화 시도 (기본 off - 구 형식 파일 주의)")
    args = parser.parse_args()

    admin_user = os.getenv("DB_ADMIN_USER", "root")
    if not os.getenv("DB_ADMIN_PASSWORD"):
        print("[중단] DB_ADMIN_PASSWORD 가 비어 있습니다. root(쓰기 권한) 비밀번호를 환경변수로 넘겨주세요.")
        return 2

    source = _resolve_source(args.source)
    if source is None:
        print("[중단] RawDB_에너지.xlsx 를 찾지 못했습니다. --source 로 경로를 지정하세요.")
        return 2
    # 서비스가 참조하는 경로를 원본 파일 위치로 맞춘다.
    os.environ["ENERGY_SOURCE_XLSX"] = str(source)
    os.environ.setdefault("SAMPLED_DB_DIR", str(source.parent))
    print(f"원본 엑셀 : {source}")
    print(f"쓰기 계정 : {admin_user}\n")

    from app.services.daily_energy_sync_service import sync_daily_energy_from_source

    print("[energy_daily] 재동기화 전 행 수 :", _row_count("energy_daily"))
    result = sync_daily_energy_from_source(source_path=source, force=True)
    if not result.get("success"):
        print("[실패] 에너지 동기화 오류:", result.get("message") or result.get("errors"))
        return 1
    after = _row_count("energy_daily")
    print("[energy_daily] 재동기화 후 행 수 :", after)
    print("  신규:", result.get("inserted"), "· 갱신:", result.get("updated"))

    if args.also_production:
        try:
            from app.services.production_dw_sync_service import auto_sync_production_once

            prod = auto_sync_production_once(force=False)
            print("\n[production_daily] 상태:", (prod or {}).get("status") or (prod or {}).get("message"))
        except Exception as exc:  # pragma: no cover
            print("\n[production_daily] 동기화 생략/실패:", exc)

    print("\n완료. 백엔드 재기동 후 대시보드를 새로고침하면 실데이터가 표시됩니다.")
    print("남는 것: prediction_log·anomaly_analysis 는 여전히 비어 있음 → 예측 실행(관리자 '예측 누락 생성')")
    print("        또는 사내 mysqldump 복원으로 별도 채워야 함. 에너지도 최신(>2026-05-12)은 mysqldump 필요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
