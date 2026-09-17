from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

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


class ClientNameTests(unittest.TestCase):
    def test_names_use_the_requested_prefix_and_count(self) -> None:
        names = backfill.build_client_names(15, "BPN", seed=1)
        self.assertEqual(len(names), 15)
        for name in names:
            self.assertRegex(name, r"^BPN\d{6}$")

    def test_names_are_unique_and_sorted(self) -> None:
        names = backfill.build_client_names(15, "BPN", seed=2)
        self.assertEqual(len(set(names)), 15)
        self.assertEqual(names, sorted(names))

    def test_same_seed_reproduces_the_same_names(self) -> None:
        self.assertEqual(
            backfill.build_client_names(15, "BPN", seed=99),
            backfill.build_client_names(15, "BPN", seed=99),
        )


class BuildRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.days = backfill.workdays(date(2026, 5, 1), date(2026, 9, 16), set())
        self.names = backfill.build_client_names(15, "BPN", seed=5)

    def _per_day(self, rows: list[dict]) -> dict[date, list[str]]:
        grouped: dict[date, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["date"], []).append(row["clientName"])
        return grouped

    def test_daily_user_counts_stay_within_requested_range(self) -> None:
        grouped = self._per_day(backfill.build_rows(self.days, self.names, 5, 15, seed=1))
        self.assertEqual(set(grouped), set(self.days))
        for day, clients in grouped.items():
            self.assertGreaterEqual(len(clients), 5, day)
            self.assertLessEqual(len(clients), 15, day)

    def test_a_pc_is_never_counted_twice_on_one_day(self) -> None:
        # (일자, PC) 가 중복되면 그 날 접속자 수가 실제보다 많아진다.
        for day, clients in self._per_day(backfill.build_rows(self.days, self.names, 5, 15, seed=3)).items():
            self.assertEqual(len(clients), len(set(clients)), day)

    def test_every_row_uses_a_generated_name(self) -> None:
        rows = backfill.build_rows(self.days, self.names, 5, 15, seed=4)
        self.assertTrue(set(row["clientName"] for row in rows) <= set(self.names))

    def test_visit_count_is_at_least_one(self) -> None:
        for row in backfill.build_rows(self.days, self.names, 5, 15, seed=6):
            self.assertGreaterEqual(row["visitCount"], 1)

    def test_same_seed_reproduces_same_rows(self) -> None:
        self.assertEqual(
            backfill.build_rows(self.days, self.names, 5, 15, seed=42),
            backfill.build_rows(self.days, self.names, 5, 15, seed=42),
        )

    def test_no_names_produces_no_rows(self) -> None:
        self.assertEqual(backfill.build_rows(self.days, [], 5, 15, seed=1), [])

    def test_daily_count_is_capped_by_available_pcs(self) -> None:
        names = backfill.build_client_names(6, "BPN", seed=8)
        grouped = self._per_day(backfill.build_rows(self.days, names, 5, 15, seed=9))
        for day, clients in grouped.items():
            self.assertLessEqual(len(clients), 6, day)


class HostNameTests(unittest.TestCase):
    def setUp(self) -> None:
        stats._NAME_CACHE.clear()

    tearDown = setUp

    def test_fqdn_keeps_only_the_first_label(self) -> None:
        # BPN123456 과 BPN123456.corp.local 이 서로 다른 PC 로 세어지면 안 된다.
        self.assertEqual(stats.normalize_client_name("bpn123456.corp.local"), "BPN123456")

    def test_unsafe_characters_are_stripped(self) -> None:
        self.assertEqual(stats.normalize_client_name("BPN123456'; DROP--"), "BPN123456DROP--")

    def test_resolved_hostname_becomes_the_identifier(self) -> None:
        with patch.object(stats.socket, "gethostbyaddr", return_value=("BPN123456.corp.local", [], [])):
            self.assertEqual(stats.resolve_client_name("192.168.0.21"), "BPN123456")

    def test_ip_is_kept_when_lookup_fails(self) -> None:
        with patch.object(stats.socket, "gethostbyaddr", side_effect=OSError("no PTR")):
            self.assertEqual(stats.resolve_client_name("192.168.0.22"), "192.168.0.22")

    def test_loopback_name_is_not_used_as_a_pc_name(self) -> None:
        with patch.object(stats.socket, "gethostbyaddr", return_value=("localhost", [], [])):
            self.assertEqual(stats.resolve_client_name("127.0.0.1"), "127.0.0.1")

    def test_lookup_runs_once_per_ip(self) -> None:
        with patch.object(stats.socket, "gethostbyaddr", return_value=("BPN1", [], [])) as lookup:
            stats.resolve_client_name("192.168.0.23")
            stats.resolve_client_name("192.168.0.23")
            self.assertEqual(lookup.call_count, 1)

    def test_empty_ip_resolves_to_nothing(self) -> None:
        self.assertEqual(stats.resolve_client_name(""), "")


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
        row = [{"date": date(2026, 9, 1), "clientName": "BPN1", "visitCount": 1}]
        with self.assertRaises(ValueError):
            stats.upsert_visits(row, source="guess")

    def test_empty_rows_do_not_touch_the_database(self) -> None:
        self.assertEqual(stats.upsert_visits([]), {"inserted": 0, "days": 0, "skippedDays": 0})


class WindowTests(unittest.TestCase):
    def test_default_window_is_inclusive_of_today(self) -> None:
        start, end = stats.default_window(today=date(2026, 9, 17), days=90)
        self.assertEqual(end, date(2026, 9, 17))
        self.assertEqual((end - start).days + 1, 90)

    def test_backfill_starts_from_may_of_the_current_year(self) -> None:
        self.assertEqual(backfill.default_start(date(2026, 9, 17)), date(2026, 5, 1))


class ArgumentTests(unittest.TestCase):
    def test_defaults_match_the_documented_values(self) -> None:
        args = backfill.parse_args([])
        self.assertEqual((args.low, args.high), (5, 15))
        self.assertEqual((args.count, args.prefix), (15, "BPN"))
        self.assertIsNone(args.date_from)
        self.assertIsNone(args.date_to)
        self.assertFalse(args.overwrite)

    def test_pc_count_below_the_daily_cap_is_rejected(self) -> None:
        # PC 가 상한보다 적으면 그 날 접속자 수를 채울 수 없다.
        self.assertEqual(backfill.main(["--count", "4", "--max", "15", "--dry-run"]), 1)

    def test_reversed_date_range_is_rejected(self) -> None:
        self.assertEqual(backfill.main(["--from", "2026-09-16", "--to", "2026-05-01", "--dry-run"]), 1)


if __name__ == "__main__":
    unittest.main()
