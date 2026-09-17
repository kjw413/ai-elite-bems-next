r"""접속 로깅 도입 이전 구간의 ``access_daily`` 롤업 적재 스크립트.

배경
----
일별 접속자 집계(``app/services/access_stats_service.py``)는 2026-09 에 붙었다.
그 이전에도 사내망에서 화면은 쓰였지만 서버가 세지 않았으므로 상세
(``access_visit``)가 남아 있지 않다. 이 스크립트는 그 구간의 일자별 롤업을
지정한 범위 안의 값으로 채워 이용 추이 화면이 도입 이전까지 이어지게 한다.

적재하는 행은 ``source='backfill'`` 로 표시된다. 실제 요청에서 적재된
``source='live'`` 와 구분되며, API 응답(``/api/v1/stats/access``)과 관리자 화면,
CSV 내려받기까지 그대로 따라간다 — 실측 집계와 섞여 한 덩어리로 보이면 안 된다.

대상 일자
---------
평일(월~금) 중 휴일을 뺀 근무일만 채운다. 휴일 목록은 예측모델이 쓰는
``DB_holiday.xlsx`` 를 재사용한다(``app.services.v5_common.load_holidays_excel``).
그 자산이 없는 환경(git 미포함)에서는 주말 제외만 적용하고 경고를 남긴다 —
공휴일에도 값이 들어가므로, 이 경우 ``--dry-run`` 으로 대상 일자를 먼저 확인하는
편이 안전하다.

이미 행이 있는 일자는 건너뛴다. 실측으로 쌓인 값을 나중 실행이 덮어쓰는 사고를
막기 위한 기본 동작이며, 되돌리기 어렵다. 의도적으로 다시 쓰려면 ``--overwrite``.

실행 (쓰기에는 관리자 계정 필요 — backend/.env 의 DB_ADMIN_* 를 읽는다)
----------------------------------------------------------------------
Git Bash::

    .venv/Scripts/python.exe backend/tools/backfill_access_daily.py \
        --from 2026-07-17 --dry-run

PowerShell::

    .venv\Scripts\python.exe backend\tools\backfill_access_daily.py `
        --from 2026-07-17

``--to`` 를 주지 않으면 어제까지 채운다 — 오늘은 실측이 계속 쌓이는 중이라
대상에서 뺀다. ``--seed`` 를 고정하면 같은 범위·같은 옵션에서 항상 같은 결과가
나오므로, dry-run 으로 확인한 값을 그대로 적재할 수 있다.

되돌리기
--------
적재한 행만 골라 지울 수 있다. 실측 행은 ``source='live'`` 라 영향받지 않는다::

    DELETE FROM access_daily
     WHERE source = 'backfill' AND visit_date BETWEEN '2026-07-17' AND '2026-09-16';
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import date, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import access_stats_service  # noqa: E402

logger = logging.getLogger(__name__)


def load_holiday_set() -> tuple[set[date], bool]:
    """휴일 집합과 원본 엑셀을 실제로 읽었는지 여부.

    두 번째 값이 False 면 공휴일이 빠진 채 주말만 제외된다는 뜻이다.
    ``load_holidays_excel()`` 은 엑셀이 없어도 근로자의 날(5/1)만 채운 집합을
    돌려주므로, 집합이 비었는지로는 판정할 수 없다 — 파일 존재를 직접 본다.
    """
    try:
        from app.services.v5_common import PATH_HOLIDAY, load_holidays_excel
        holidays = {item for item in load_holidays_excel() if isinstance(item, date)}
        return holidays, PATH_HOLIDAY.exists()
    except Exception as exc:
        logger.debug("holiday source unavailable: %s", exc)
        return set(), False


def workdays(date_from: date, date_to: date, holidays: set[date]) -> list[date]:
    """범위 안의 근무일(주말·휴일 제외)."""
    days: list[date] = []
    cursor = date_from
    while cursor <= date_to:
        if cursor.weekday() < 5 and cursor not in holidays:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def build_rows(days: list[date], low: int, high: int, seed: int) -> list[dict]:
    """일자별 롤업 행 생성.

    접속 횟수는 접속자 수 이상이다 — 같은 사람이 하루에 화면을 두 번 이상 여는
    경우가 있어, 두 값이 같아지는 쪽이 오히려 실제 분포와 멀다.
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    for day in days:
        unique_users = rng.randint(low, high)
        revisits = rng.randint(0, max(1, unique_users // 3)) if unique_users else 0
        rows.append({
            "date": day,
            "uniqueUsers": unique_users,
            "visitCount": unique_users + revisits,
        })
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="접속 로깅 이전 구간의 일별 집계 적재")
    parser.add_argument(
        "--from", dest="date_from", required=True, type=date.fromisoformat,
        help="시작일 (YYYY-MM-DD). 서비스를 열어 사내에 공유한 날",
    )
    parser.add_argument(
        "--to", dest="date_to", type=date.fromisoformat, default=None,
        help="종료일 (YYYY-MM-DD). 기본값은 어제 — 오늘은 실측이 쌓이는 중이라 제외",
    )
    parser.add_argument("--min", dest="low", type=int, default=5, help="일 접속자 수 하한 (기본 5)")
    parser.add_argument("--max", dest="high", type=int, default=15, help="일 접속자 수 상한 (기본 15)")
    parser.add_argument("--seed", type=int, default=20260917, help="같은 옵션에서 같은 결과를 내기 위한 시드")
    parser.add_argument("--dry-run", action="store_true", help="대상 일자와 값만 출력하고 DB 는 바꾸지 않는다")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="이미 행이 있는 일자도 덮어쓴다. 실측(live) 집계까지 지워지므로 기본은 건너뛰기",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    date_to = args.date_to or (date.today() - timedelta(days=1))
    if args.date_from > date_to:
        print(f"실패: 시작일({args.date_from})이 종료일({date_to})보다 뒤입니다.")
        return 1
    if args.low < 0 or args.high < args.low:
        print(f"실패: 접속자 수 범위가 올바르지 않습니다 — min={args.low}, max={args.high}")
        return 1

    holidays, holidays_loaded = load_holiday_set()
    if not holidays_loaded:
        print("경고: 휴일 목록을 읽지 못했습니다. 주말만 제외하므로 공휴일에도 값이 들어갑니다.")
        print("      DB_holiday.xlsx 가 있는 환경에서 실행하거나 --dry-run 으로 대상 일자를 먼저 확인하세요.")

    days = workdays(args.date_from, date_to, holidays)
    if not days:
        print(f"대상 없음: {args.date_from} ~ {date_to} 사이에 근무일이 없습니다.")
        return 0

    rows = build_rows(days, args.low, args.high, args.seed)
    total_users = sum(row["uniqueUsers"] for row in rows)
    print(f"대상 구간: {args.date_from} ~ {date_to}")
    print(f"근무일 {len(rows)}일 · 일 평균 접속자 {total_users / len(rows):.1f}명 · 시드 {args.seed}")

    if args.dry_run:
        for row in rows:
            print(f"  {row['date']}  접속자 {row['uniqueUsers']:>3}  접속 {row['visitCount']:>3}")
        print(f"\ndry-run: {len(rows)}건이 적재 대기 중입니다. DB 는 바꾸지 않았습니다.")
        return 0

    try:
        result = access_stats_service.upsert_daily(
            rows,
            source=access_stats_service.SOURCE_BACKFILL,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"실패: 적재 중 오류 — {exc}")
        print("backend/.env 의 DB_ADMIN_USER / DB_ADMIN_PASSWORD / DB_HOST 를 확인하세요.")
        return 1

    print(
        f"완료: 신규 {result['inserted']}건 · 갱신 {result['updated']}건 · "
        f"건너뜀(이미 있음) {result['skipped']}건"
    )
    if result["skipped"] and not args.overwrite:
        print("      건너뛴 일자는 이미 집계가 있는 날입니다. 다시 쓰려면 --overwrite 를 주세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
