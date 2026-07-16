from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import server


class ServerHelperTests(unittest.TestCase):
    @staticmethod
    def _request(method: str = "GET", origin: str | None = None) -> server.Request:
        headers = [] if origin is None else [(b"origin", origin.encode("ascii"))]
        return server.Request({
            "type": "http",
            "method": method,
            "path": "/api/v1/events",
            "headers": headers,
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("203.0.113.10", 12345),
        })

    def test_previous_year_date_clamps_leap_day(self) -> None:
        self.assertEqual(server.previous_year_date(date(2024, 2, 29)), date(2023, 2, 28))
        self.assertEqual(server.previous_year_date(date(2025, 7, 15)), date(2024, 7, 15))

    def test_requested_date_is_bounded_by_latest_database_date(self) -> None:
        latest = date(2026, 7, 10)
        self.assertEqual(server.bounded_base_date(date(2026, 7, 15), latest), latest)
        self.assertEqual(server.bounded_base_date(date(2026, 7, 1), latest), date(2026, 7, 1))

    def test_direct_queries_never_select_admin_database_credentials(self) -> None:
        connection = object()
        with (
            patch.dict(os.environ, {
                "DB_ADMIN_USER": "root",
                "DB_ADMIN_PASSWORD": "admin-secret",
                "DB_VIEWER_USER": "bems_reader",
                "DB_VIEWER_PASSWORD": "reader-secret",
            }),
            patch.object(server.pymysql, "connect", return_value=connection) as connect,
        ):
            self.assertIs(server.db_connect(), connection)
        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["user"], "bems_reader")
        self.assertEqual(kwargs["password"], "reader-secret")
        self.assertNotEqual(kwargs["user"], "root")

    def test_direct_queries_require_explicit_viewer_credentials(self) -> None:
        with (
            patch.dict(os.environ, {"DB_VIEWER_USER": "", "DB_VIEWER_PASSWORD": ""}),
            patch.object(server.pymysql, "connect") as connect,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.db_connect()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "데이터베이스에 연결할 수 없습니다.")
        connect.assert_not_called()

    def test_origin_canonicalization_is_exact(self) -> None:
        self.assertEqual(
            server._canonical_origin("HTTP://BEMS-PC:3000/"),
            "http://bems-pc:3000",
        )
        self.assertIsNone(server._canonical_origin("http://bems-pc:3000/path"))
        self.assertIsNone(server._canonical_origin("http://user@bems-pc:3000"))

    def test_configured_origins_extend_the_default_allowlist(self) -> None:
        with (
            patch.dict(os.environ, {"BEMS_ALLOWED_ORIGINS": "http://bems-alias:3000"}),
            patch.object(
                server,
                "_default_allowed_origins",
                return_value={"http://localhost:3000"},
            ),
        ):
            origins = server._configured_allowed_origins()
        self.assertEqual(
            origins,
            {"http://localhost:3000", "http://bems-alias:3000"},
        )

    def test_untrusted_unsafe_origin_is_rejected(self) -> None:
        async def call_next(_: server.Request):
            raise AssertionError("blocked request reached the handler")

        response = asyncio.run(server.reject_untrusted_unsafe_origins(
            self._request("DELETE", "http://evil.example:3000"),
            call_next,
        ))
        self.assertEqual(response.status_code, 403)

    def test_operational_production_uses_physical_factory_members(self) -> None:
        records = [
            {"date": date(2026, 7, 1), "factory": "남양주1", "actual_prod_kg": 1_000.0},
            {"date": date(2026, 7, 1), "factory": "남양주2", "actual_prod_kg": 2_000.0},
            {"date": date(2026, 7, 1), "factory": "광주", "actual_prod_kg": 3_000.0},
        ]
        self.assertEqual(
            server.actual_production_kg(
                records, "남양주", date(2026, 7, 1), date(2026, 7, 1),
            ),
            3_000.0,
        )
        self.assertEqual(
            server.actual_production_kg(
                records, "전사", date(2026, 7, 1), date(2026, 7, 1),
            ),
            6_000.0,
        )

    def test_operational_production_uses_viewer_query_and_f10_once(self) -> None:
        first = date(2026, 7, 1)
        second = date(2026, 7, 2)
        service = SimpleNamespace(
            WIP_MIX_CONVERSION={"광주": {"260014": 10.91954}},
            get_wip_daily=lambda factory: [
                {"date": second, "total_wip_kg": 400.0},
            ],
        )
        rows = [
            {"date": first, "factory": "F10", "actual_prod_kg": 1_000.0},
            {"date": first, "factory": "F10A", "actual_prod_kg": 600.0},
            {"date": second, "factory": "F10", "actual_prod_kg": 1_200.0},
            {"date": first, "factory": "F30", "actual_prod_kg": 3_000.0},
        ]
        with (
            patch.object(server, "import_core", return_value=service),
            patch.object(server, "fetch_all", return_value=rows) as fetch,
        ):
            records = server.fetch_actual_production_frame(first, second)

        fetch.assert_called_once()
        self.assertNotIn(
            {"date": first, "factory": "남양주1", "actual_prod_kg": 1_000.0},
            records,
        )
        self.assertIn(
            {"date": first, "factory": "남양주1", "actual_prod_kg": 600.0},
            records,
        )
        self.assertIn(
            {"date": second, "factory": "남양주1", "actual_prod_kg": 1_200.0},
            records,
        )
        self.assertIn(
            {"date": second, "factory": "광주", "actual_prod_kg": 400.0},
            records,
        )

    def test_prediction_aggregate_requires_every_member(self) -> None:
        rows = [
            {
                "pred_date": date(2026, 7, 15),
                "target": "전력",
                "factory": "남양주1",
                "predicted": 100.0,
                "lower_band": 90.0,
                "upper_band": 110.0,
                "actual": 105.0,
            },
        ]
        with patch.object(server, "fetch_all", return_value=rows):
            result = server.aggregate_prediction_rows("남양주", date(2026, 7, 15))
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["predicted"])
        self.assertIsNone(result[0]["actual"])
        self.assertEqual(result[0]["band_status"], "unknown")

    def test_prediction_aggregate_sums_complete_members(self) -> None:
        rows = [
            {
                "pred_date": date(2026, 7, 15),
                "target": "전력",
                "factory": "남양주1",
                "predicted": 100.0,
                "lower_band": 90.0,
                "upper_band": 110.0,
                "actual": 105.0,
            },
            {
                "pred_date": date(2026, 7, 15),
                "target": "전력",
                "factory": "남양주2",
                "predicted": 200.0,
                "lower_band": 180.0,
                "upper_band": 220.0,
                "actual": 230.0,
            },
        ]
        with patch.object(server, "fetch_all", return_value=rows):
            result = server.aggregate_prediction_rows("남양주", date(2026, 7, 15))
        self.assertEqual(result[0]["predicted"], 300.0)
        self.assertEqual(result[0]["lower_band"], 270.0)
        self.assertEqual(result[0]["upper_band"], 330.0)
        self.assertEqual(result[0]["actual"], 335.0)
        self.assertEqual(result[0]["band_status"], "over")

    def test_company_prediction_aggregate_excludes_untrained_gyeongsan(self) -> None:
        rows = [
            {
                "pred_date": date(2026, 7, 15),
                "target": "전력",
                "factory": factory,
                "predicted": 100.0,
                "lower_band": 90.0,
                "upper_band": 110.0,
                "actual": 105.0,
            }
            for factory in ("남양주1", "남양주2", "김해", "광주", "논산")
        ]
        with patch.object(server, "fetch_all", return_value=rows):
            result = server.aggregate_prediction_rows("전사", date(2026, 7, 15))
        self.assertEqual(result[0]["predicted"], 500.0)
        self.assertEqual(result[0]["actual"], 525.0)
        self.assertEqual(result[0]["band_status"], "inside")

    def test_prediction_run_rejects_untrained_factory_before_loading_model(self) -> None:
        payload = server.PredictionRequest(
            factory="경산",
            date=date(2026, 7, 15),
            mix_prod_kg=1_000.0,
        )
        with (
            patch.object(server, "client_is_admin", return_value=True),
            patch.object(server, "import_core") as import_core,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.run_prediction(payload, self._request("POST"))
        self.assertEqual(raised.exception.status_code, 400)
        import_core.assert_not_called()

    def test_point_prediction_remains_visible_without_quantile_band(self) -> None:
        rows = [
            {
                "pred_date": date(2026, 7, 15),
                "target": "전력",
                "factory": factory,
                "predicted": predicted,
                "lower_band": None,
                "upper_band": None,
                "actual": actual,
            }
            for factory, predicted, actual in (
                ("남양주1", 100.0, 105.0),
                ("남양주2", 200.0, 205.0),
            )
        ]
        with patch.object(server, "fetch_all", return_value=rows):
            result = server.aggregate_prediction_rows("남양주", date(2026, 7, 15))
        self.assertEqual(result[0]["predicted"], 300.0)
        self.assertIsNone(result[0]["lower_band"])
        self.assertIsNone(result[0]["upper_band"])
        self.assertEqual(result[0]["band_status"], "unknown")

    def test_run_result_does_not_invent_missing_band(self) -> None:
        result = server._format_prediction_results(
            date(2026, 7, 15),
            {"전력": {"pred": 123_000.0}},
        )
        self.assertEqual(result[0]["predicted"], 123.0)
        self.assertIsNone(result[0]["lower"])
        self.assertIsNone(result[0]["upper"])
        self.assertEqual(result[0]["status"], "unknown")

    def test_namyangju_production_filters_include_historical_parent_code(self) -> None:
        self.assertEqual(server.PRODUCTION_FACTORY_CODES["남양주1"], ("F10A", "F10"))
        self.assertEqual(server.PRODUCTION_FACTORY_CODES["남양주2"], ("F10B", "F10"))

    def test_audit_endpoint_rejects_viewer_before_query(self) -> None:
        with (
            patch.object(server, "client_is_admin", return_value=False),
            patch.object(server, "fetch_all") as fetch_all,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.audit(self._request())
        self.assertEqual(raised.exception.status_code, 403)
        fetch_all.assert_not_called()

    def test_prediction_run_rejects_viewer_before_loading_model(self) -> None:
        payload = server.PredictionRequest(
            factory="김해",
            date=date(2026, 7, 15),
            mix_prod_kg=1_000.0,
        )
        with (
            patch.object(server, "client_is_admin", return_value=False),
            patch.object(server, "import_core") as import_core,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.run_prediction(payload, self._request("POST"))
        self.assertEqual(raised.exception.status_code, 403)
        import_core.assert_not_called()

    def test_generate_missing_rejects_unsafe_range_before_loading_model(self) -> None:
        payloads = (
            server.HistoryBackfillRequest(
                factory="김해",
                date_from=date(2026, 7, 2),
                date_to=date(2026, 7, 1),
            ),
            server.HistoryBackfillRequest(
                factory="김해",
                date_from=date(2026, 1, 1),
                date_to=date(2026, 4, 4),
            ),
        )
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                patch.object(server, "client_is_admin", return_value=True),
                patch.object(server, "import_core") as import_core,
                self.assertRaises(server.HTTPException) as raised,
            ):
                server.generate_missing_history(payload, self._request("POST"))
            self.assertEqual(raised.exception.status_code, 400)
            import_core.assert_not_called()

    def test_event_create_rejects_aggregate_factory_before_loading_service(self) -> None:
        payload = server.EventCreateRequest(
            factory="전사",
            event_date=date(2026, 7, 15),
            note="테스트 이벤트",
        )
        with (
            patch.object(server, "client_is_admin", return_value=True),
            patch.object(server, "import_core") as import_core,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.create_event(payload, self._request("POST"))
        self.assertEqual(raised.exception.status_code, 400)
        import_core.assert_not_called()

    def test_failed_ai_report_is_not_saved(self) -> None:
        agent_service = SimpleNamespace(
            run_agent_report=lambda *_: "AI Agent 분석 중 오류가 발생했습니다.",
        )
        save_report = Mock()
        report_service = SimpleNamespace(save_report=save_report)
        payload = server.ReportRequest(factory="김해", year=2026, month=7)

        with (
            patch.object(server, "client_is_admin", return_value=True),
            patch.object(
                server,
                "import_core",
                side_effect=[agent_service, report_service],
            ),
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.generate_report(payload, self._request("POST"))

        self.assertEqual(raised.exception.status_code, 502)
        save_report.assert_not_called()

    def test_legacy_imports_cannot_write_bytecode(self) -> None:
        self.assertTrue(sys.dont_write_bytecode)

    def test_core_modules_come_from_local_copy(self) -> None:
        prediction_source = (
            server.LOCAL_CORE_ROOT / "app" / "services" / "usage_prediction_v5_service.py"
        )
        self.assertTrue(prediction_source.exists())
        fetch_section = prediction_source.read_text(encoding="utf-8").split(
            "def _fetch_energy_history"
        )[1][:900]
        # 발견·수정 로그 №1: overlay가 요구하는 factory 컬럼을 SELECT에 포함해야 한다.
        self.assertIn('"factory"', fetch_section)
        self.assertIn('drop(columns=["factory"])', fetch_section)

    def test_local_env_file_exists_for_standalone_run(self) -> None:
        self.assertTrue((server.LOCAL_CORE_ROOT / ".env").exists())


if __name__ == "__main__":
    unittest.main()
