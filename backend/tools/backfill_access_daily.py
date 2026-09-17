r"""접속 로깅 도입 이전 구간의 접속 기록 적재 스크립트.

배경
----
일별 접속자 집계(``app/services/access_stats_service.py``)는 2026-09 에 붙었다.
그 이전에도 사내망에서 화면은 쓰였지만 서버가 세지 않았으므로 기록이 남아 있지
않다. 이 스크립트는 그 구간의 접속 상세(``access_visit``)와 일자별 롤업
(``access_daily``)을 지정한 범위 안의 값으로 채워, 이용 추이 화면이 도입 이전까지
이어지게 한다.

적재하는 행은 ``source='backfill'`` 로 표시된다. 실제 요청에서 적재된
``source='live'`` 와 구분되며, API 응답(``/api/v1/stats/access``)과 관리자 화면,
CSV 내려받기까지 그대로 따라간다. 표시를 위해서만 있는 값이 아니라, 두 적재 경로가
서로를 덮어쓰지 않게 하는 안전장치이기도 하다 — 이 스크립트는 이미 기록이 있는
일자를 통째로 건너뛴다.

PC 이름
-------
실측 경로는 클라이언트 IP 를 역방향 조회해 사내 PC 이름을 식별자로 쓴다. 이 구간은
조회할 원본 요청이 없으므로 ``--prefix`` + 일련번호 형태의 이름을 ``--count`` 개
만들어 쓴다. 이름과 각 PC 의 활성도는 ``--seed`` 로 고정되므로, 같은 옵션이면 항상
같은 결과가 나온다 — ``--dry-run`` 으로 확인한 그대로 적재된다.
``client_ip`` 는 NULL 로 남는다. 실측이 아닌 행에 IP 를 지어넣지 않는다.

대상 일자
---------
평일(월~금) 중 휴일을 뺀 근무일만 채운다. 휴일 목록은 예측모델이 쓰는
``DB_holiday.xlsx`` 를 재사용한다(``app.services.v5_common.load_holidays_excel``).
그 자산이 없는 환경(git 미포함)에서는 주말 제외만 적용하고 경고를 남긴다 —
공휴일에도 값이 들어가므로, 이 경우 ``--dry-run`` 으로 대상 일자를 먼저 확인하는
편이 안전하다.

실행 (쓰기에는 관리자 계정 필요 — backend/.env 의 DB_ADMIN_* 를 읽는다)
----------------------------------------------------------------------
Git Bash::

    .venv/Scripts/python.exe backend/tools/backfill_access_daily.py --dry-run

PowerShell::

    .venv\Scripts\python.exe backend\tools\backfill_access_daily.py

``--from`` 을 주지 않으면 올해 5월 1일부터, ``--to`` 를 주지 않으면 어제까지 채운다
— 오늘은 실측이 계속 쌓이는 중이라 대상에서 뺀다.

되돌리기
--------
적재한 행만 골라 지울 수 있다. 실측 행은 ``source='live'`` 라 영향받지 않는다::

    DELETE FROM access_visit WHERE source = 'backfill';
    DELETE FROM access_daily WHERE source = 'backfill';
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


def build_client_names(count: int, prefix: str, seed: int) -> list[str]:
    """PC 이름 목록. 사내 자산번호처럼 접두어 + 6자리 일련번호 형태.

    번호는 겹치지 않게 뽑고 정렬해 돌려준다 — 목록 순서가 실행마다 달라지면
    dry-run 으로 확인한 내용과 적재 결과를 대조하기 어렵다.
    """
    rng = random.Random(seed)
    numbers = rng.sample(range(100000, 1000000), count)
    return [f"{prefix}{number}" for number in sorted(numbers)]


def _weighted_sample(rng: random.Random, pool: list[tuple[str, float]], size: int) -> list[str]:
    """가중치에 비례해 중복 없이 ``size`` 개를 뽑는다.

    ``random.sample`` 은 가중치를 받지 못한다. 매일 같은 확률로 고르면 PC 별
    접속일 수가 전부 비슷해져, 자주 쓰는 사람과 가끔 쓰는 사람이 나뉘는 실제
    분포와 멀어진다.
    """
    remaining = list(pool)
    chosen: list[str] = []
    for _ in range(min(size, len(remaining))):
        total = sum(weight for _, weight in remaining)
        if total <= 0:
            break
        threshold = rng.uniform(0, total)
        accumulated = 0.0
        for index, (name, weight) in enumerate(remaining):
            accumulated += weight
            if accumulated >= threshold:
                chosen.append(name)
                remaining.pop(index)
                break
    return chosen


def build_rows(days: list[date], names: list[str], low: int, high: int, seed: int) -> list[dict]:
    """일자 × PC 단위 접속 행 생성.

    하루 접속자 수는 ``low``~``high`` 사이에서 뽑고, 그만큼의 PC 를 활성도
    가중치로 고른다. 접속 횟수는 1회 이상 — 같은 사람이 하루에 화면을 두 번 이상
    여는 경우가 있어, 전원이 정확히 1회인 쪽이 오히려 실제 분포와 멀다.
    """
    if not names:
        return []
    rng = random.Random(seed)
    # PC 별 활성도. 담당자는 매일, 그 외는 가끔 들어온다.
    weights = [rng.uniform(0.2, 1.0) for _ in names]
    pool = list(zip(names, weights))

    rows: list[dict] = []
    for day in days:
        target = rng.randint(low, min(high, len(names)))
        for name in _weighted_sample(rng, pool, target):
            rows.append({
                "date": day,
                "clientName": name,
                # 대부분 1회, 가끔 2~3회.
                "visitCount": rng.choices([1, 2, 3], weights=[70, 22, 8])[0],
            })
    return rows


def default_start(today: date | None = None) -> date:
    """``--from`` 기본값 — 올해 5월 1일."""
    return date((today or date.today()).year, 5, 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="접속 로깅 이전 구간의 접속 기록 적재")
    parser.add_argument(
        "--from", dest="date_from", type=date.fromisoformat, default=None,
        help="시작일 (YYYY-MM-DD). 기본값은 올해 5월 1일",
    )
    parser.add_argument(
        "--to", dest="date_to", type=date.fromisoformat, default=None,
        help="종료일 (YYYY-MM-DD). 기본값은 어제 — 오늘은 실측이 쌓이는 중이라 제외",
    )
    parser.add_argument("--min", dest="low", type=int, default=5, help="일 접속자 수 하한 (기본 5)")
    parser.add_argument("--max", dest="high", type=int, default=15, help="일 접속자 수 상한 (기본 15)")
    parser.add_argument("--count", type=int, default=15, help="생성할 PC 이름 개수 (기본 15)")
    parser.add_argument("--prefix", default="BPN", help="PC 이름 접두어 (기본 BPN)")
    parser.add_argument("--seed", type=int, default=20260917, help="같은 옵션에서 같은 결과를 내기 위한 시드")
    parser.add_argument("--dry-run", action="store_true", help="대상과 값만 출력하고 DB 는 바꾸지 않는다")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="이미 기록이 있는 일자도 다시 쓴다. 실측(live) 집계까지 지워지므로 기본은 건너뛰기",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    date_from = args.date_from or default_start()
    date_to = args.date_to or (date.today() - timedelta(days=1))
    if date_from > date_to:
        print(f"실패: 시작일({date_from})이 종료일({date_to})보다 뒤입니다.")
        return 1
    if args.low < 1 or args.high < args.low:
        print(f"실패: 접속자 수 범위가 올바르지 않습니다 — min={args.low}, max={args.high}")
        return 1
    if args.count < args.high:
        print(f"실패: PC 이름 개수({args.count})가 일 접속자 수 상한({args.high})보다 적습니다.")
        return 1
    if not args.prefix.strip():
        print("실패: --prefix 가 비어 있습니다.")
        return 1

    holidays, holidays_loaded = load_holiday_set()
    if not holidays_loaded:
        print("경고: 휴일 목록을 읽지 못했습니다. 주말만 제외하므로 공휴일에도 값이 들어갑니다.")
        print("      DB_holiday.xlsx 가 있는 환경에서 실행하거나 --dry-run 으로 대상 일자를 먼저 확인하세요.")

    days = workdays(date_from, date_to, holidays)
    if not days:
        print(f"대상 없음: {date_from} ~ {date_to} 사이에 근무일이 없습니다.")
        return 0

    names = build_client_names(args.count, args.prefix.strip(), args.seed)
    rows = build_rows(days, names, args.low, args.high, args.seed)
    per_day = {day: 0 for day in days}
    for row in rows:
        per_day[row["date"]] += 1

    print(f"대상 구간: {date_from} ~ {date_to}")
    print(f"근무일 {len(days)}일 · 접속 상세 {len(rows)}행 · 일 평균 접속자 {len(rows) / len(days):.1f}대 · 시드 {args.seed}")
    print(f"PC {len(names)}대: {', '.join(names)}")

    if args.dry_run:
        print()
        for day in days:
            print(f"  {day}  접속자 {per_day[day]:>3}대")
        by_client: dict[str, int] = {}
        for row in rows:
            by_client[row["clientName"]] = by_client.get(row["clientName"], 0) + 1
        print("\n  PC 별 접속일 수")
        for name in names:
            print(f"    {name}  {by_client.get(name, 0):>3}일")
        print(f"\ndry-run: 상세 {len(rows)}행이 적재 대기 중입니다. DB 는 바꾸지 않았습니다.")
        return 0

    try:
        result = access_stats_service.upsert_visits(
            rows,
            source=access_stats_service.SOURCE_BACKFILL,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"실패: 적재 중 오류 — {exc}")
        print("backend/.env 의 DB_ADMIN_USER / DB_ADMIN_PASSWORD / DB_HOST 를 확인하세요.")
        return 1

    print(
        f"완료: 상세 {result['inserted']}행 · {result['days']}일 적재 · "
        f"건너뜀(이미 기록 있음) {result['skippedDays']}일"
    )
    if result["skippedDays"] and not args.overwrite:
        print("      건너뛴 일자는 이미 기록이 있는 날입니다. 다시 쓰려면 --overwrite 를 주세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
