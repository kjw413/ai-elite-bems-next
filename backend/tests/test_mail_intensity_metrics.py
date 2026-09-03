from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tools.mail import daily_report_builder as daily
from tools.mail import period_report_builder as period


class MailIntensityMetricTests(unittest.TestCase):
    EXPECTED_INTENSITY_KEYS = [
        "power",
        "freezing_power",
        "air_compressor",
        "fuel",
        "water",
    ]

    def test_all_mail_periods_share_restored_equipment_metrics(self) -> None:
        self.assertEqual(
            [metric["key"] for metric in daily.INTENSITY_METRICS],
            self.EXPECTED_INTENSITY_KEYS,
        )
        self.assertEqual(
            [metric["key"] for metric in daily.DAILY_SIGNAL_METRICS],
            self.EXPECTED_INTENSITY_KEYS,
        )
        self.assertEqual(
            [metric["key"] for metric in period.MONTHLY_CHART_METRICS],
            [*self.EXPECTED_INTENSITY_KEYS, "wastewater_ratio"],
        )

    def test_equipment_unit_aggregation_uses_stored_rawdb_values(self) -> None:
        rows = [
            {
                "factory": "김해",
                "mix_prod_kg": 1_000.0,
                "total_power_kwh": 999_999.0,
                "freezing_power_kwh": 999_999.0,
                "air_compressor_kwh": 999_999.0,
                "fuel_nm3": 999_999.0,
                "water_ton": 999_999.0,
                "wastewater_ton": 10.0,
                "power_per_ton_kwh": 100.0,
                "freezing_power_per_ton_kwh": 10.0,
                "air_compressor_per_ton_kwh": 20.0,
                "fuel_per_ton_nm3": 30.0,
                "water_per_ton_ton": 40.0,
            },
            {
                "factory": "김해",
                "mix_prod_kg": 3_000.0,
                "total_power_kwh": 1.0,
                "freezing_power_kwh": 1.0,
                "air_compressor_kwh": 1.0,
                "fuel_nm3": 1.0,
                "water_ton": 1.0,
                "wastewater_ton": 2.0,
                "power_per_ton_kwh": 300.0,
                "freezing_power_per_ton_kwh": 30.0,
                "air_compressor_per_ton_kwh": 60.0,
                "fuel_per_ton_nm3": 90.0,
                "water_per_ton_ton": 120.0,
            },
        ]

        result = daily._aggregate_weighted(rows)

        self.assertIsNotNone(result)
        self.assertEqual(result["freezing_power_per_ton_kwh"], 25.0)
        self.assertEqual(result["air_compressor_per_ton_kwh"], 50.0)

    def test_zero_production_day_usage_still_counts_toward_intensity(self) -> None:
        """생산량 0인 비조업일의 사용량도 기간 원단위 분자에 포함되어야 한다."""
        operating = {
            "factory": "김해",
            "mix_prod_kg": 2_000.0,       # 2 ton
            "total_power_kwh": 200.0,
            "freezing_power_kwh": 40.0,
            "air_compressor_kwh": 20.0,
            "fuel_nm3": 60.0,
            "water_ton": 10.0,
            "wastewater_ton": 8.0,
            "power_per_ton_kwh": 100.0,
            "freezing_power_per_ton_kwh": 20.0,
            "air_compressor_per_ton_kwh": 10.0,
            "fuel_per_ton_nm3": 30.0,
            "water_per_ton_ton": 5.0,
        }
        idle = {
            "factory": "김해",
            "mix_prod_kg": 0.0,           # 비조업 — 엑셀 원단위 수식 성립 안 함
            "total_power_kwh": 50.0,
            "freezing_power_kwh": 10.0,
            "air_compressor_kwh": 5.0,
            "fuel_nm3": 15.0,
            "water_ton": 2.0,
            "wastewater_ton": 1.0,
            "power_per_ton_kwh": None,
            "freezing_power_per_ton_kwh": None,
            "air_compressor_per_ton_kwh": None,
            "fuel_per_ton_nm3": None,
            "water_per_ton_ton": None,
        }

        result = daily._aggregate_weighted([operating, idle])

        self.assertIsNotNone(result)
        # 총사용량 / 총생산량 (2 ton) — 비조업일 고정부하가 원단위에 반영된다.
        self.assertAlmostEqual(result["power_per_ton_kwh"], 250.0 / 2)
        self.assertAlmostEqual(result["freezing_power_per_ton_kwh"], 50.0 / 2)
        self.assertAlmostEqual(result["air_compressor_per_ton_kwh"], 25.0 / 2)
        self.assertAlmostEqual(result["fuel_per_ton_nm3"], 75.0 / 2)
        self.assertAlmostEqual(result["water_per_ton_ton"], 12.0 / 2)

    def test_all_zero_production_rows_yield_no_intensity(self) -> None:
        """생산량이 전혀 없으면 분모가 0이므로 원단위는 '-'(None)로 남는다."""
        rows = [{
            "factory": "김해",
            "mix_prod_kg": 0.0,
            "total_power_kwh": 50.0,
            "freezing_power_kwh": 10.0,
            "air_compressor_kwh": 5.0,
            "fuel_nm3": 15.0,
            "water_ton": 2.0,
            "wastewater_ton": 1.0,
            "power_per_ton_kwh": None,
            "freezing_power_per_ton_kwh": None,
            "air_compressor_per_ton_kwh": None,
            "fuel_per_ton_nm3": None,
            "water_per_ton_ton": None,
        }]

        result = daily._aggregate_weighted(rows)

        self.assertIsNotNone(result)
        for metric in daily.INTENSITY_METRICS:
            self.assertIsNone(result[metric["unit_col"]], metric["key"])

    def test_mail_query_selects_stored_equipment_unit_columns(self) -> None:
        class FakeCursor:
            sql = ""

            def execute(self, sql, _params):
                self.sql = sql

            def fetchall(self):
                return []

            def close(self):
                pass

        class FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self, dictionary=False):
                self.dictionary = dictionary
                return self._cursor

            def close(self):
                pass

        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        with patch.object(daily, "get_connection", return_value=connection):
            daily._fetch_rows_range(date(2026, 7, 30), date(2026, 7, 30))

        self.assertIn("freezing_power_per_ton_kwh", cursor.sql)
        self.assertIn("air_compressor_per_ton_kwh", cursor.sql)

    def test_monthly_table_is_transposed_and_total_excludes_gyeongsan(self) -> None:
        current_rows = [
            {"factory": "김해", "mix_prod_kg": 1_000.0},
            {"factory": "경산", "mix_prod_kg": 9_000.0},
        ]

        table = period._build_monthly_snapshot_table(
            current_rows,
            current_rows,
            current_rows,
            current_rows,
        )

        self.assertEqual(
            [factory["factory"] for factory in table["factories"]],
            ["전사(경산제외)", "남양주1", "남양주2", "김해", "광주", "논산", "경산"],
        )
        self.assertEqual(
            [row["label"] for row in table["metric_rows"]],
            [metric["label"] for metric in daily.FACTORY_TABLE_METRICS],
        )

        production_row = table["metric_rows"][0]
        self.assertEqual(production_row["cells"][0]["mtd"]["value"], "1")
        self.assertEqual(production_row["cells"][3]["mtd"]["value"], "1")
        self.assertEqual(production_row["cells"][-1]["mtd"]["value"], "9")

        html = period._render(
            {
                "subject": "월간 메일 테스트",
                "year": 2026,
                "previous_year": 2025,
                "month": 8,
                "period_from": "2026-08-01",
                "period_to": "2026-08-31",
                "generated_at": "2026-09-01 00:00:00",
                "monthly_snapshot_table": table,
                "mtd_current_label": "8월",
                "mtd_previous_label": "2025.08",
                "ytd_current_label": "8월",
                "ytd_previous_label": "2025.01~08",
                "mtd_charts": [],
            },
            template_name="monthly_energy_report.html",
        )
        self.assertEqual(html.count("8월 실적"), len(table["factories"]))
        self.assertEqual(html.count("8월 누계"), len(table["factories"]))
        self.assertLess(html.index("전사(경산제외)"), html.index("남양주1"))
        self.assertLess(html.index("남양주1"), html.index("남양주2"))
        self.assertLess(html.index(">생산량</div>"), html.index(">전력 원단위</div>"))


if __name__ == "__main__":
    unittest.main()
