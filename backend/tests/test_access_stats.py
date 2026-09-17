from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import access_stats_service as stats  # noqa: E402
from tools import backfill_access_daily as backfill  # noqa: E402


class WorkdayTests(unittest.TestCase):
    def test_weekends_are_excluded(self) -> None:
        # 2026-09-14(월) ~ 2026-09-20(일)
        days = backfill.workdays(date(2026, 9, 14), date(2026, 9, 20), set())
        self.assertEqual(days, [date(2026, 9, d) for d in range(14, 19)])

    def test_holidays_are_excluded(self) -> None:
        days = backfill.workdays(date(2026, 9, 14), date(2026, 9, 18), {date(2026, 9, 16)})
        self.assertNotIn(date(2026, 9, 16), days)
        self.assertEqual(len(days), 4)

    def test_single_weekend_day_range_is_empty(self) -> None:
        self.assertEqual(backfill.workdays(date(2026, 9, 19), date(2026, 9, 20), set()), [])


class BuildRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.days = backfill.workdays(date(2026, 7, 17), date(2026, 9, 16), set())

    def test_user_counts_stay_within_requested_range(self) -> None:
        rows = backfill.build_rows(self.days, 5, 15, seed=1)
        self.assertTrue(rows)
        for row in rows:
            self.assertGreaterEqual(row["uniqueUsers"], 5)
            self.assertLessEqual(row["uniqueUsers"], 15)

    def test_visit_count_is_never_below_user_count(self) -> None:
        rows = backfill.build_rows(self.days, 5, 15, seed=7)
        for row in rows:
            self.assertGreaterEqual(row["visitCount"], row["uniqueUsers"])

    def test_same_seed_reproduces_same_values(self) -> None:
        self.assertEqual(
            backfill.build_rows(self.days, 5, 15, seed=42),
            backfill.build_rows(self.days, 5, 15, seed=42),
        )

    def test_zero_user_day_reports_zero_visits(self) -> None:
        # 접속자 0명인데 접속 횟수가 잡히면 두 값이 서로 모순이다.
        rows = backfill.build_rows(self.days, 0, 0, seed=11)
        for row in rows:
            self.assertEqual((row["uniqueUsers"], row["visitCount"]), (0, 0))

    def test_row_dates_match_the_requested_days(self) -> None:
        rows = backfill.build_rows(self.days, 5, 15, seed=3)
        self.assertEqual([row["date"] for row in rows], self.days)


class SummaryTests(unittest.TestCase):
    @staticmethod
    def _day(day: int, users: int, visits: int, source: str = stats.SOURCE_LIVE) -> dict:
        return {"date": date(2026, 9, day), "uniqueUsers": users, "visitCount": visits, "source": source}

    def test_average_ignores_days_without_any_access(self) -> None:
        # 가동하지 않은 날까지 분모에 넣으면 실제 이용 수준보다 낮게 나온다.
        summary = stats.summarize([self._day(1, 10, 12), self._day(2, 0, 0), self._day(3, 20, 25)])
        self.assertEqual(summary["activeDays"], 2)
        self.assertEqual(summary["avgUniqueUsers"], 15.0)

    def test_peak_reports_the_busiest_day(self) -> None:
        summary = stats.summarize([self._day(1, 10, 12), self._day(2, 20, 25), self._day(3, 8, 8)])
        self.assertEqual(summary["peakUniqueUsers"], 20)
        self.assertEqual(summary["peakDate"], date(2026, 9, 2))

    def test_live_and_backfilled_days_are_counted_separately(self) -> None:
        summary = stats.summarize([
            self._day(1, 9, 11, stats.SOURCE_BACKFILL),
            self._day(2, 10, 12, stats.SOURCE_BACKFILL),
            self._day(3, 11, 13),
        ])
        self.assertEqual(summary["backfilledDays"], 2)
        self.assertEqual(summary["liveDays"], 1)

    def test_empty_input_reports_zeroes_without_raising(self) -> None:
        summary = stats.summarize([])
        self.assertEqual(summary["activeDays"], 0)
        self.assertIsNone(summary["peakDate"])


class UpsertGuardTests(unittest.TestCase):
    def test_unknown_source_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            stats.upsert_daily([{"date": date(2026, 9, 1), "uniqueUsers": 5}], source="guess")

    def test_empty_rows_do_not_touch_the_database(self) -> None:
        self.assertEqual(stats.upsert_daily([]), {"inserted": 0, "updated": 0, "skipped": 0})


class WindowTests(unittest.TestCase):
    def test_default_window_is_inclusive_of_today(self) -> None:
        start, end = stats.default_window(today=date(2026, 9, 17), days=90)
        self.assertEqual(end, date(2026, 9, 17))
        self.assertEqual((end - start).days + 1, 90)


class ArgumentTests(unittest.TestCase):
    def test_range_defaults_to_five_through_fifteen(self) -> None:
        args = backfill.parse_args(["--from", "2026-07-17"])
        self.assertEqual((args.low, args.high), (5, 15))
        self.assertEqual(args.date_from, date(2026, 7, 17))
        self.assertIsNone(args.date_to)
        self.assertFalse(args.overwrite)


if __name__ == "__main__":
    unittest.main()
