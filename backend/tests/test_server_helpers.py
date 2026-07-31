from __future__ import annotations

import asyncio
import os
import re
import sys
import unittest
from datetime import date, timedelta
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
        async def inner_app(scope, receive, send):
            raise AssertionError("blocked request reached the handler")

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        middleware = server.RejectUntrustedUnsafeOrigins(inner_app)
        scope = self._request("DELETE", "http://evil.example:3000").scope
        asyncio.run(middleware(scope, receive, send))
        start = next(m for m in messages if m["type"] == "http.response.start")
        self.assertEqual(start["status"], 403)

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
            operational_production_sum_sql=lambda: ("SUM(actual_qty)", ()),
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

    def test_gwangju_production_recorded_wip_policy_uses_expected_factors(self) -> None:
        service = server.import_core("app.services.production_correction_service")
        recorded = service.PRODUCTION_RECORDED_WIP_MIX_CONVERSION["광주"]
        self.assertEqual(recorded, {"129998": 10.91954, "129999": 1.0})

        finished_filter, finished_params = service.finished_production_filter_sql()
        self.assertIn("NOT", finished_filter)
        self.assertIn("129998", finished_params)
        self.assertIn("129999", finished_params)

        expression, params = service.operational_production_sum_sql()
        self.assertIn("actual_qty * %s", expression)
        self.assertIn(10.91954, params)
        self.assertIn(1.0, params)

    def test_gwangju_production_api_reclassifies_recorded_wip(self) -> None:
        correction = server.import_core("app.services.production_correction_service")
        first = date(2026, 7, 1)
        second = date(2026, 7, 2)
        service = SimpleNamespace(
            finished_production_filter_sql=lambda: (
                " AND NOT (factory = %s AND item_code IN (%s,%s))",
                ("F30", "129998", "129999"),
            ),
            get_wip_daily=lambda factory: [
                {"date": first, "total_wip_kg": 300.0},
            ],
            get_wip_item_totals=lambda factory, date_from, date_to: [
                {"item_code": "260014", "name": "탈지분유", "kg": 300.0},
            ],
            PRODUCTION_RECORDED_WIP_MIX_CONVERSION={
                "광주": {"129998": 10.91954, "129999": 1.0},
            },
            WIP_ITEM_LABELS={
                "광주": {
                    "129998": "탈지분유(수)",
                    "129999": "생크림(35%)(수)",
                },
            },
            wip_mix_from_totals=correction.wip_mix_from_totals,
        )
        with (
            patch.object(server, "import_core", return_value=service),
            patch.object(server, "fetch_one", side_effect=[
                {"max_date": date(2026, 7, 31)},
                {"actual": 1.0, "items": 1},
                {"plan": 1.2},
            ]) as fetch_one,
            patch.object(server, "fetch_all", side_effect=[
                [{"date": first, "IC": 0, "MY": 0, "FM": 1.0, "SN": 0, "ETC": 0}],
                [{"name": "FM", "value": 1_000.0}],
                [
                    {"date": first, "item_code": "129998", "name": "탈지분유(수)", "actual_qty": 100.0},
                    {"date": second, "item_code": "129999", "name": "생크림(35%)(수)", "actual_qty": 200.0},
                ],
                [{"name": "완제품", "plan": 1.2, "actual": 1.0}],
                [],
                [],
            ]) as fetch_all,
        ):
            result = server.production(
                factory="광주", requested_date=date(2026, 7, 15), mode="month",
            )

        self.assertIn("NOT", fetch_one.call_args_list[1].args[0])
        self.assertIn("NOT", fetch_all.call_args_list[0].args[0])
        self.assertEqual(result["daily"][0]["utilityProd"], 2.392)
        self.assertEqual(result["daily"][1]["date"], "07.02")
        self.assertEqual(result["daily"][1]["FM"], 0.0)
        self.assertEqual(result["daily"][1]["utilityProd"], 0.2)
        mix = {row["name"]: row["value"] for row in result["wipMix"]}
        self.assertEqual(mix["탈지분유(수)"], 68.6)
        self.assertEqual(mix["탈지분유"], 18.8)
        self.assertEqual(mix["생크림(35%)(수)"], 12.6)

    def test_gwangju_wip_mix_uses_mix_kg_conversion_before_ratio(self) -> None:
        service = server.import_core("app.services.production_correction_service")
        source = service.pd.DataFrame({
            "날짜": [date(2026, 7, 1), date(2026, 6, 30)],
            # 7월 원본 kg 비율은 약 4.8 / 88.9 / 6.3이지만,
            # 환산 후에는 화면 기준 31.3 / 53.5 / 15.2가 된다.
            "260014": [31.3 / 10.91954, 1_000.0],
            "260039": [53.5, 1_000.0],
            "260042": [15.2 / 4.0, 1_000.0],
            # 환산표에 없는 포장재는 구성비 분모에서 제외한다.
            "220999": [100_000.0, 100_000.0],
        })
        service._WIP_ITEM_CACHE = {}
        service._WIP_ITEM_CACHE_MTIME = None
        try:
            with (
                patch.object(service, "PATH_WIP_SUMMARY", __file__),
                patch.object(service.pd, "read_excel", return_value={"광주": source}),
            ):
                rows = service.get_wip_mix("광주", date(2026, 7, 1), date(2026, 7, 31))
        finally:
            service._WIP_ITEM_CACHE = {}
            service._WIP_ITEM_CACHE_MTIME = None

        self.assertEqual(rows, [
            {"name": "살균유", "value": 53.5},
            {"name": "탈지분유", "value": 31.3},
            {"name": "유크림믹스", "value": 15.2},
        ])

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
        # .env는 서버 PC에만 존재하는 배포 자산(git 미추적)이다. 개발 샌드박스처럼
        # 파일이 없는 환경에서는 코드 결함이 아니므로 skip으로 구분한다.
        env_path = server.LOCAL_CORE_ROOT / ".env"
        if not env_path.exists():
            self.skipTest("backend/.env는 서버 PC 전용 배포 자산입니다 (샌드박스 미보유).")
        self.assertTrue(env_path.exists())

    def test_sync_run_rejects_viewer_before_loading_services(self) -> None:
        payload = server.SyncRunRequest(force=True)
        with (
            patch.object(server, "client_is_admin", return_value=False),
            patch.object(server, "import_core") as import_core,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.sync_run(payload, self._request("POST"))
        self.assertEqual(raised.exception.status_code, 403)
        import_core.assert_not_called()

    def test_retrain_lock_conflict_maps_to_409(self) -> None:
        service = SimpleNamespace(
            trigger_v5_retrain=lambda trigger_mode: {"started": False, "message": "이미 학습 작업이 실행 중입니다."},
        )
        with (
            patch.object(server, "client_is_admin", return_value=True),
            patch.object(server, "import_core", return_value=service),
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.trigger_retrain(self._request("POST"))
        self.assertEqual(raised.exception.status_code, 409)

    def test_manual_sync_runs_both_sources_and_records_state(self) -> None:
        energy_service = SimpleNamespace(
            force_resync=Mock(return_value={"inserted": 6, "updated": 0}),
            auto_sync_once=Mock(return_value={"inserted": 0, "updated": 0}),
        )
        production_service = SimpleNamespace(
            auto_sync_production_once=Mock(return_value={"status": "unchanged"}),
        )
        with patch.object(
            server, "import_core", side_effect=[energy_service, production_service],
        ):
            result = server.run_excel_sync(force=True)
        energy_service.force_resync.assert_called_once()
        energy_service.auto_sync_once.assert_not_called()
        production_service.auto_sync_production_once.assert_called_once_with(force=True)
        self.assertEqual(result["energy"], {"inserted": 6, "updated": 0})
        self.assertIsNotNone(server._scheduler_state["lastRunAt"])

    # ── 생산실적 기간·연간 모드 (Phase 4) ─────────────────────

    def test_complete_month_span_matches_legacy_rule(self) -> None:
        self.assertTrue(server.is_complete_month_span(date(2026, 6, 1), date(2026, 6, 30)))
        self.assertTrue(server.is_complete_month_span(date(2026, 1, 1), date(2026, 3, 31)))
        self.assertFalse(server.is_complete_month_span(date(2026, 6, 2), date(2026, 6, 30)))
        self.assertFalse(server.is_complete_month_span(date(2026, 6, 1), date(2026, 6, 29)))
        self.assertFalse(server.is_complete_month_span(date(2026, 7, 1), date(2026, 6, 30)))
        # 윤년 2월
        self.assertTrue(server.is_complete_month_span(date(2024, 2, 1), date(2024, 2, 29)))
        self.assertTrue(server.is_complete_month_span(date(2023, 2, 1), date(2023, 2, 28)))
        self.assertFalse(server.is_complete_month_span(date(2024, 2, 1), date(2024, 2, 28)))

    def test_production_period_resolution_per_mode(self) -> None:
        base = date(2026, 7, 15)
        # 월별 모드는 base가 그 달의 며칠이든 항상 1일~말일 전체를 반환한다
        # (base.day=15를 골랐어도 31일까지 보여야 한다 — 2026-07-21 버그 수정).
        self.assertEqual(
            server.resolve_production_period("month", base, None, None),
            (date(2026, 7, 1), date(2026, 7, 31)),
        )
        # 실적 데이터가 그 달 말일 전에 끊겨 있으면(max_date) 그 날짜까지로 제한한다.
        self.assertEqual(
            server.resolve_production_period("month", base, None, None, date(2026, 7, 20)),
            (date(2026, 7, 1), date(2026, 7, 20)),
        )
        self.assertEqual(
            server.resolve_production_period("year", base, None, None),
            (date(2026, 1, 1), date(2026, 12, 31)),
        )
        self.assertEqual(
            server.resolve_production_period("range", base, date(2026, 6, 1), date(2026, 6, 30)),
            (date(2026, 6, 1), date(2026, 6, 30)),
        )
        # range 기본값: 기준일 포함 최근 31일
        self.assertEqual(
            server.resolve_production_period("range", base, None, None),
            (base - server.timedelta(days=30), base),
        )

    def test_production_month_mode_shows_full_month_regardless_of_selected_day(self) -> None:
        """31일짜리 달에서 말일이 아닌 날을 골라도 daily에 31일 전부 나와야 한다."""
        with (
            patch.object(server, "fetch_one", side_effect=[
                {"max_date": date(2026, 7, 31)},
                {"actual": 310.0, "items": 3},
                {"plan": 1200.0},
            ]),
            patch.object(server, "fetch_all", side_effect=[
                [{"date": date(2026, 7, day), "IC": 10.0, "MY": 0, "FM": 0, "SN": 0, "ETC": 0}
                 for day in range(1, 32)],                       # 일별 실적 31행
                [{"name": "IC", "value": 310.0}],                # 제품 믹스
                [{"name": "품목", "plan": 10.0, "actual": 9.0}], # 품목 순위
                [],                                              # 미달/초과 gap
                [],                                              # 제품유형 계획
            ]),
        ):
            # 기준일로 15일(말일이 아님)을 고른다 — 예전엔 이 경우 16~31일이 잘렸다.
            result = server.production(factory="전사", requested_date=date(2026, 7, 15), mode="month")
        self.assertEqual(len(result["daily"]), 31)
        self.assertEqual(result["daily"][0]["date"], "07.01")
        self.assertEqual(result["daily"][30]["date"], "07.31")

    def test_production_range_rejects_inverted_or_excessive_period(self) -> None:
        base = date(2026, 7, 15)
        with self.assertRaises(server.HTTPException) as raised:
            server.resolve_production_period("range", base, date(2026, 7, 10), date(2026, 7, 1))
        self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(server.HTTPException) as raised:
            server.resolve_production_period(
                "range", base, date(2020, 1, 1), date(2026, 7, 15),
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_production_rejects_unknown_mode_before_query(self) -> None:
        with (
            patch.object(server, "fetch_one") as fetch_one,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.production(factory="전사", requested_date=None, mode="weekly")
        self.assertEqual(raised.exception.status_code, 400)
        fetch_one.assert_not_called()

    def test_production_rejects_bad_explicit_range_before_query(self) -> None:
        with (
            patch.object(server, "fetch_one") as fetch_one,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.production(
                factory="전사", requested_date=None, mode="range",
                date_from=date(2026, 7, 10), date_to=date(2026, 7, 1),
            )
        self.assertEqual(raised.exception.status_code, 400)
        fetch_one.assert_not_called()

    def test_annual_elapsed_ratio_clamps_to_unit_interval(self) -> None:
        self.assertEqual(server.annual_elapsed_ratio(2026, date(2025, 12, 31)), 0.0)
        self.assertEqual(server.annual_elapsed_ratio(2026, date(2027, 1, 1)), 1.0)
        mid = server.annual_elapsed_ratio(2026, date(2026, 7, 2))
        self.assertAlmostEqual(mid, 183 / 365, places=6)

    def test_mail_tools_and_impact_lookup_live_inside_new_backend(self) -> None:
        """메일 자동화·영향계수 룩업이 legacy 폴더 없이 new/backend에서 자체 해석되는지."""
        mail_dir = server.LOCAL_CORE_ROOT / "tools" / "mail"
        for name in ("run_mail.py", "run_daily_mail.py", "config.py",
                     "daily_report_builder.py", "period_report_builder.py",
                     "mail_service.py"):
            self.assertTrue((mail_dir / name).exists(), name)
        self.assertTrue((mail_dir / "templates" / "daily_energy_report.html").exists())
        # config.PROJECT_ROOT = parents[2] → tools/mail 기준 2단계 위 = new/backend
        config_source = (mail_dir / "config.py").read_text(encoding="utf-8")
        self.assertIn("parents[2]", config_source)
        # 이상 진단이 참조하는 회귀계수 룩업 (legacy/analysis_results에서 복사)
        lookup = server.LOCAL_CORE_ROOT / "analysis_results" / "item_energy_impact_lookup.json"
        self.assertTrue(lookup.exists())

    def test_annual_monthly_plan_rate_stops_after_last_actual_month(self) -> None:
        """연간 모드는 누계 Burn-up이 아니라 월별 계획 대비 실적을 낸다.

        생산계획이 주 단위로 수립·집계되므로 연 누계보다 월 달성률이 현장 주기와 맞는다.
        미래 월 실적을 0으로 두면 달성률 0%로 오독되므로 None이어야 한다.
        """
        def month_row(month: int, ic: float) -> dict[str, object]:
            return {"month_no": month, "IC": ic, "MY": 0, "FM": 0, "SN": 0, "ETC": 0}

        with (
            patch.object(server, "fetch_one", side_effect=[
                {"max_date": date(2026, 7, 15)},          # MAX(date)
                {"actual": 300.0, "items": 3},            # 기간 실적 요약
                {"plan": 1200.0},                          # 기간 계획 합계
            ]),
            patch.object(server, "fetch_all", side_effect=[
                [month_row(month, 50.0) for month in (1, 2, 3)],  # 월별 실적 (3월까지)
                [{"name": "IC", "value": 300.0}],                  # 제품 믹스
                [{"name": "품목", "plan": 10.0, "actual": 9.0}],   # 품목 순위
                [month_row(month, 40.0) for month in (1, 2, 3)],  # 전년 월별 실적
                [{"m": month, "plan": 100.0} for month in range(1, 13)],  # 월별 계획
                [],                                                # 미달/초과 gap
                [],                                                # 제품유형 계획
                [],                                                # 월별 전년비
            ]),
        ):
            result = server.production(factory="전사", requested_date=date(2026, 7, 15), mode="year")
        monthly_plan = result["monthlyPlan"]
        self.assertEqual(len(monthly_plan), 12)
        # 계획은 12월까지 나오지만 실적·달성률은 마지막 실적 월(3월)까지만
        self.assertEqual(monthly_plan[0]["plan"], 100.0)
        self.assertEqual(monthly_plan[11]["plan"], 100.0)
        self.assertEqual(monthly_plan[2]["actual"], 50.0)
        self.assertEqual(monthly_plan[2]["rate"], 50.0)
        self.assertIsNone(monthly_plan[3]["actual"])
        self.assertIsNone(monthly_plan[3]["rate"])
        # 유형별 전년비를 위해 전년 동월 값이 daily에 함께 실린다
        self.assertEqual(result["daily"][0]["IC"], 50.0)
        self.assertEqual(result["daily"][0]["prevIC"], 40.0)

    def test_energy_window_defaults_to_current_month(self) -> None:
        base = date(2026, 7, 15)
        self.assertEqual(
            server.resolve_energy_window(base, None, None),
            (date(2026, 7, 1), base),
        )
        # 31일인 달을 말일 기준으로 조회하면 1일부터 말일까지 전부 포함된다
        month_end = date(2026, 5, 31)
        self.assertEqual(
            server.resolve_energy_window(month_end, None, None),
            (date(2026, 5, 1), month_end),
        )

    def test_production_month_mode_keeps_every_day_of_month(self) -> None:
        period_from, period_to = server.resolve_production_period(
            "month", date(2026, 5, 31), None, None,
        )
        self.assertEqual((period_from, period_to), (date(2026, 5, 1), date(2026, 5, 31)))
        with (
            patch.object(server, "fetch_one", side_effect=[
                {"max_date": date(2026, 7, 15)},
                {"actual": 310.0, "items": 3},
                {"plan": 1200.0},
            ]),
            patch.object(server, "fetch_all", side_effect=[
                [{"date": date(2026, 5, day), "IC": 10.0, "MY": 0, "FM": 0, "SN": 0, "ETC": 0}
                 for day in range(1, 32)],                       # 일별 실적 31행
                [{"name": "IC", "value": 310.0}],                # 제품 믹스
                [{"name": "품목", "plan": 10.0, "actual": 9.0}], # 품목 순위
                [],                                              # 미달/초과 gap
                [],                                              # 제품유형 계획
            ]),
        ):
            result = server.production(factory="전사", requested_date=date(2026, 5, 31), mode="month")
        # 과거 [-14:] 절단 결함 회귀 방지 — 31일 전부 반환돼야 한다
        self.assertEqual(len(result["daily"]), 31)
        self.assertEqual(result["daily"][0]["date"], "05.01")
        self.assertEqual(result["daily"][30]["date"], "05.31")

    def test_energy_window_rejects_partial_or_reversed_range(self) -> None:
        base = date(2026, 7, 15)
        with self.assertRaises(server.HTTPException) as raised:
            server.resolve_energy_window(base, date(2026, 7, 1), None)
        self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(server.HTTPException) as raised:
            server.resolve_energy_window(base, date(2026, 7, 10), date(2026, 7, 1))
        self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(server.HTTPException) as raised:
            server.resolve_energy_window(base, date(2023, 1, 1), date(2026, 7, 15))
        self.assertEqual(raised.exception.status_code, 400)

    def test_energy_yoy_builds_12_months_with_missing_as_none(self) -> None:
        rows = [
            {"y": 2025, "m": 1, "power": 100.0, "fuel": 10.0, "water": 5.0, "wastewater": 2.0},
            {"y": 2025, "m": 2, "power": 110.0, "fuel": 11.0, "water": 6.0, "wastewater": 2.5},
            {"y": 2026, "m": 1, "power": 90.0, "fuel": 9.5, "water": 4.5, "wastewater": 1.8},
        ]
        yoy = server.build_energy_yoy(rows, 2026)
        self.assertEqual(len(yoy), 12)
        self.assertEqual(yoy[0]["month"], "1월")
        self.assertEqual(yoy[0]["power"], {"current": 90.0, "previous": 100.0})
        # 금년 2월 데이터 없음 → current None, 전년만 존재
        self.assertEqual(yoy[1]["power"], {"current": None, "previous": 110.0})
        # 양쪽 모두 없는 월은 전부 None
        self.assertEqual(yoy[11]["power"], {"current": None, "previous": None})
        self.assertEqual(yoy[0]["wastewater"], {"current": 1.8, "previous": 2.0})

    def test_weighted_intensity_cumulative_uses_same_period_weighted_average(self) -> None:
        usage = {
            (2026, 1): 1000.0, (2026, 2): 1200.0,
            (2025, 1): 1100.0, (2025, 2): 1300.0, (2025, 3): 900.0,
        }
        production_kg = {
            (2026, 1): 10_000.0, (2026, 2): 8_000.0,
            (2025, 1): 10_000.0, (2025, 2): 10_000.0, (2025, 3): 9_000.0,
        }
        result = server.weighted_intensity_yoy(usage, production_kg, 2026)
        self.assertIsNotNone(result)
        # 금년 실적이 있는 1~2월만 합산: (1000+1200)/(18톤) vs (1100+1300)/(20톤)
        self.assertEqual(result["months"], 2)
        self.assertEqual(result["lastMonth"], 2)
        self.assertAlmostEqual(result["current"], round(2200.0 / 18.0, 2))
        self.assertAlmostEqual(result["previous"], round(2400.0 / 20.0, 2))
        # 3월(전년만 존재)은 누계에서 제외된다
        self.assertIsNone(server.weighted_intensity_yoy({}, production_kg, 2026))

    def test_factory_yoy_entry_builds_intensity_usage_production(self) -> None:
        current = {"power": 100_000.0, "fuel": 5_000.0, "water": 2_000.0, "wastewater": 1_000.0, "production": 50_000.0}
        previous = {"power": 120_000.0, "fuel": 6_000.0, "water": 0.0, "wastewater": 500.0, "production": 60_000.0}
        entry = server.factory_yoy_entry("김해", current, previous)
        self.assertEqual(entry["factory"], "김해")
        self.assertAlmostEqual(entry["intensity"]["power"]["current"], 2000.0)   # 100000/50톤
        self.assertAlmostEqual(entry["intensity"]["power"]["previous"], 2000.0)  # 120000/60톤
        self.assertAlmostEqual(entry["intensity"]["wwratio"]["current"], 0.5)
        self.assertIsNone(entry["intensity"]["wwratio"]["previous"])             # 용수 0 → None
        self.assertAlmostEqual(entry["usage"]["power"]["current"], 100.0)        # kWh → MWh
        self.assertAlmostEqual(entry["production"]["current"], 50.0)             # kg → ton

    def test_feature_importance_validates_factory_and_target(self) -> None:
        with patch.object(server, "import_core") as import_core:
            with self.assertRaises(server.HTTPException) as raised:
                server.model_feature_importance(factory="전사", target="전력")
            self.assertEqual(raised.exception.status_code, 400)
            with self.assertRaises(server.HTTPException) as raised:
                server.model_feature_importance(factory="김해", target="폐수")
            self.assertEqual(raised.exception.status_code, 400)
        import_core.assert_not_called()

    def test_production_insights_follow_legacy_rules(self) -> None:
        # 계획 없음 → 실적만
        no_plan = server.build_production_insights(
            plan=None, actual=1234.0, progress=None, cat2_plan={}, cat2_actual={},
        )
        self.assertIn("계획 데이터 없음", no_plan[0])
        # 진척 구간 + 최대/부진 제품유형
        messages = server.build_production_insights(
            plan=1000.0, actual=750.0, progress=75.0,
            cat2_plan={"IC": 500.0, "MY": 300.0},
            cat2_actual={"IC": 480.0, "MY": 210.0, "FM": 60.0},
        )
        self.assertIn("잔여 기간 주의", messages[0])          # 70~90 구간
        self.assertIn("최대 제품유형: IC", messages[1])        # 실적 1위 + 진척 96.0%
        self.assertIn("부진 제품유형: MY", messages[2])        # 진척 70% < 80
        # 부진 없음(모두 80% 이상)이면 부진 문장은 생략
        healthy = server.build_production_insights(
            plan=1000.0, actual=950.0, progress=95.0,
            cat2_plan={"IC": 500.0}, cat2_actual={"IC": 450.0},
        )
        self.assertEqual(len(healthy), 2)

    def test_shift_month_handles_year_boundaries(self) -> None:
        self.assertEqual(server.shift_month(2026, 7, -12), (2025, 7))
        self.assertEqual(server.shift_month(2026, 1, -1), (2025, 12))
        self.assertEqual(server.shift_month(2025, 12, 1), (2026, 1))
        self.assertEqual(server.shift_month(2026, 7, -24), (2024, 7))

    def test_item_trend_requires_item_codes(self) -> None:
        with self.assertRaises(server.HTTPException) as raised:
            server.production_item_trend(items="  , ", factory="전사")
        self.assertEqual(raised.exception.status_code, 400)

    def test_item_trend_axis_follows_selected_mode(self) -> None:
        """품목 추이 x축은 그 탭의 시간 범위·단위를 따른다 — 연간은 1년(12개월), 월간은 그 달(일별)."""
        with (
            patch.object(server, "fetch_one", return_value={"max_date": date(2026, 7, 31)}),
            patch.object(server, "fetch_all", return_value=[
                {"code": "A1", "name": "테스트품목", "y": 2026, "m": 7, "actual": 120.0},
                {"code": "A1", "name": "테스트품목", "y": 2025, "m": 7, "actual": 100.0},
            ]),
        ):
            yearly = server.production_item_trend(items="A1", factory="전사", requested_date=None, mode="year")
        self.assertEqual(yearly["granularity"], "month")
        yearly_series = yearly["items"][0]["series"]
        self.assertEqual(len(yearly_series), 12)
        self.assertEqual(yearly_series[0]["period"], "1월")
        self.assertEqual(yearly_series[11]["period"], "12월")
        # 7월 지점에 금년 값과 전년 동월 값이 함께 붙는다
        self.assertEqual(yearly_series[6]["actual"], 120.0)
        self.assertEqual(yearly_series[6]["prevYear"], 100.0)

        with (
            patch.object(server, "fetch_one", return_value={"max_date": date(2026, 7, 31)}),
            patch.object(server, "fetch_all", return_value=[
                {"code": "A1", "name": "테스트품목", "d": date(2026, 7, 3), "actual": 55.0},
                {"code": "A1", "name": "테스트품목", "d": date(2025, 7, 3), "actual": 50.0},
            ]),
        ):
            monthly = server.production_item_trend(items="A1", factory="전사", requested_date=None, mode="month")
        self.assertEqual(monthly["granularity"], "day")
        monthly_series = monthly["items"][0]["series"]
        self.assertEqual(len(monthly_series), 31)  # 7월 1~31일
        self.assertEqual(monthly_series[0]["period"], "07.01")
        self.assertEqual(monthly_series[30]["period"], "07.31")
        self.assertEqual(monthly_series[2]["actual"], 55.0)
        self.assertEqual(monthly_series[2]["prevYear"], 50.0)  # 전년 동일자(2025-07-03)

    def test_item_trend_range_mode_spans_selected_days(self) -> None:
        with (
            patch.object(server, "fetch_one", return_value={"max_date": date(2026, 7, 31)}),
            patch.object(server, "fetch_all", return_value=[]),
        ):
            ranged = server.production_item_trend(
                items="A1", factory="전사", requested_date=None, mode="range",
                date_from=date(2026, 3, 10), date_to=date(2026, 3, 13),
            )
        self.assertEqual(ranged["granularity"], "day")
        self.assertEqual(
            [point["period"] for point in ranged["items"][0]["series"]],
            ["03.10", "03.11", "03.12", "03.13"],
        )

    def test_mail_preview_replaces_cid_with_data_uri(self) -> None:
        image = SimpleNamespace(cid="chart1", data=b"\x89PNG", mime_subtype="png")
        html = server.inline_images_to_data_uris('<img src="cid:chart1">', [image])
        self.assertNotIn("cid:chart1", html)
        self.assertIn("data:image/png;base64,", html)

    def test_mail_period_normalization(self) -> None:
        self.assertEqual(server.normalize_mail_period(" Daily "), "daily")
        self.assertEqual(server.normalize_mail_period("weekly"), "weekly")
        self.assertEqual(server.normalize_mail_period("MONTHLY"), "monthly")
        for invalid in ("", "yearly", "매일", None):
            with self.assertRaises(server.HTTPException) as raised:
                server.normalize_mail_period(invalid)  # type: ignore[arg-type]
            self.assertEqual(raised.exception.status_code, 400)

    def test_mail_send_requires_admin_before_touching_mail_stack(self) -> None:
        request = self._request(method="POST")
        with patch.object(server, "import_core") as import_core:
            with self.assertRaises(server.HTTPException) as raised:
                server.send_mail_report(server.MailSendRequest(period="daily"), request)
        self.assertEqual(raised.exception.status_code, 403)
        import_core.assert_not_called()

    def test_mail_send_reports_missing_configuration_keys(self) -> None:
        request = self._request(method="POST")
        config = SimpleNamespace(
            is_valid=False,
            missing_keys=lambda: ["SMTP_HOST", "MAIL_RECIPIENTS"],
            recipients=[],
        )
        with (
            patch.object(server, "client_is_admin", return_value=True),
            patch.object(server, "import_core", return_value=SimpleNamespace(get_mail_config=lambda: config)),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                server.send_mail_report(server.MailSendRequest(period="daily"), request)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("SMTP_HOST", str(raised.exception.detail))

    def test_dashboard_trend_includes_all_energy_sources(self) -> None:
        with (
            patch.object(server, "fetch_one", return_value={"max_date": date(2026, 7, 15), "updated_at": None}),
            patch.object(server, "fetch_all", side_effect=[
                [],                                                        # 기간·공장별 홈 집계 원본
                [{"date": date(2026, 7, 15), "actual": 191.0,
                  "fuel": 1200.0, "water": 340.0, "wastewater": 150.0}],  # 7일 추이
                [],                                                        # YoY rows
                [],                                                        # 구성비 (YTD)
                [],                                                        # events
                [],                                                        # 판정 규칙용 prediction_log
            ]),
            patch.object(server, "aggregate_prediction_rows", return_value=[]) as predictions,
            patch.object(server, "fetch_actual_production_frame", return_value=None),
            patch.object(server, "actual_production_records", return_value=[]),
            patch.object(server, "actual_production_daily_kg", return_value={date(2026, 7, 15): 52340.0}),
            patch.object(server, "aggregate_period", return_value={"power": 0.0, "fuel": 0.0, "water": 0.0, "wastewater": 0.0, "production": 0.0}),
        ):
            result = server.dashboard(factory="전사", requested_date=date(2026, 7, 15))
        row = result["trend"][0]
        self.assertEqual(row["actual"], 191.0)
        self.assertEqual(row["fuel"], 1200.0)
        self.assertEqual(row["water"], 340.0)
        self.assertEqual(row["wastewater"], 150.0)
        self.assertEqual(row["production"], 52.3)
        self.assertEqual(result["operationTrends"]["power"][0]["actual"], 191.0)
        self.assertEqual(result["operationTrends"]["fuel"][0]["actual"], 1.2)
        self.assertEqual(result["operationTrends"]["water"][0]["actual"], 0.34)
        self.assertEqual(set(result["performance"]), {"mtd", "ytd"})
        predictions.assert_called_once_with(
            "전사", date(2026, 7, 15), date_from=date(2026, 7, 9), limit=21,
        )

    def test_single_day_band_exit_does_not_raise_dashboard_warning(self) -> None:
        """단발 이탈은 경고로 올리지 않는다 — 90% 밴드에서 통계적으로 흔해 알람 피로를 만든다."""
        banner = server.band_alert_banner(
            {"alertCount": 0, "watchCount": 6, "driftCount": 0, "signals": [], "flags": []}
        )
        self.assertEqual(banner["level"], "normal")
        self.assertIn("단발 이탈 6건", banner["description"])

        repeated = server.band_alert_banner(
            {"alertCount": 2, "watchCount": 6, "driftCount": 1, "signals": [], "flags": []}
        )
        self.assertEqual(repeated["level"], "warning")
        self.assertIn("반복 이탈 2건", repeated["title"])

        drift_only = server.band_alert_banner(
            {"alertCount": 0, "watchCount": 0, "driftCount": 3, "signals": [], "flags": []}
        )
        self.assertEqual(drift_only["level"], "warning")
        self.assertIn("지속 편차 3건", drift_only["title"])

    def test_band_rule_evaluation_groups_alerts_by_series(self) -> None:
        """같은 (공장, 지표)의 연속 이탈은 날짜별로 쪼개지 않고 한 건으로 묶는다."""
        rows = [
            {"factory": "논산", "target": "연료", "pred_date": date(2026, 7, 13) + timedelta(days=offset),
             "band_status": "over", "band_position": 1.4, "actual_value": 110.0, "pred_value": 100.0}
            for offset in range(4)
        ]
        with patch.object(server, "fetch_all", return_value=rows):
            evaluation = server.band_rule_evaluation("논산", date(2026, 7, 16))
        alert_signals = [signal for signal in evaluation["signals"] if signal["kind"] == "alert"]
        self.assertEqual(len(alert_signals), 1)
        self.assertEqual(alert_signals[0]["factory"], "논산")
        self.assertEqual(alert_signals[0]["date"], date(2026, 7, 16))
        self.assertGreater(evaluation["alertCount"], 1)

    def test_intensity_bridge_splits_change_without_residual(self) -> None:
        """원단위 변동 분해는 사용량효과 + 생산량효과가 정확히 전체 변동과 같아야 한다."""
        usage_prev, ton_prev = 1_000.0, 10.0   # 원단위 100
        usage_curr, ton_curr = 1_100.0, 10.5   # 원단위 약 104.76
        usage_effect = (usage_curr - usage_prev) / ton_curr
        production_effect = usage_prev * (1 / ton_curr - 1 / ton_prev)
        total_change = usage_curr / ton_curr - usage_prev / ton_prev
        self.assertAlmostEqual(usage_effect + production_effect, total_change, places=9)

    # ── 에너지 비용 집계 규칙 ────────────────────────────────────

    def test_cost_bridge_splits_change_without_residual(self) -> None:
        """비용 3단 분해는 세 효과의 합이 정확히 비용 증감과 같아야 한다."""
        service = server.import_core("app.services.energy_cost_service")
        bridge = service.cost_bridge(
            {"cost": 1_050_000_000.0, "usage": 6_000_000.0, "production_ton": 17_000.0},
            {"cost": 900_000_000.0, "usage": 5_600_000.0, "production_ton": 16_000.0},
        )
        self.assertAlmostEqual(service.bridge_residual(bridge), 0.0, places=4)
        # 항등식이 우연히 맞은 게 아님을 보이려고 각 효과의 부호도 확인한다.
        # 세 요인이 모두 비용을 밀어올린 사례다 — 부호가 뒤집히면 분해가 잘못된 것이다.
        self.assertGreater(bridge["productionEffect"], 0)   # 생산 16,000 → 17,000 ton
        self.assertGreater(bridge["efficiencyEffect"], 0)   # 원단위 350.00 → 352.94 (악화)
        self.assertGreater(bridge["priceEffect"], 0)        # 단가 160.71 → 175.00
        self.assertAlmostEqual(bridge["intensityPrev"], 350.0)
        self.assertAlmostEqual(bridge["pricePrev"], 900_000_000 / 5_600_000)

    def test_cost_bridge_refuses_when_denominator_missing(self) -> None:
        """분모가 0인 구간은 0이 아니라 None — 비율을 만들 수 없는 구간을 위장하지 않는다."""
        service = server.import_core("app.services.energy_cost_service")
        empty = {"cost": 0.0, "usage": 0.0, "production_ton": 0.0}
        full = {"cost": 1.0, "usage": 1.0, "production_ton": 1.0}
        self.assertIsNone(service.cost_bridge(full, empty))
        self.assertIsNone(service.cost_bridge(empty, full))

    def test_weighted_price_denominator_excludes_unpriced_usage(self) -> None:
        """단가 분모는 비용이 매겨진 사용량만 — 섞이면 가중평균이 구성원 범위를 벗어난다.

        실측 회귀: 경산은 사용량이 2021년부터 있으나 비용은 2026-04부터라, 전체 합으로
        나누면 전사 단가가 모든 개별 공장보다 낮게 나왔다(166 vs 180~184).
        """
        service = server.import_core("app.services.energy_cost_service")
        priced_cost, priced_usage = 1_800_000.0, 10_000.0    # 180원/kWh
        unpriced_usage = 5_000.0                              # 비용 미적재분
        self.assertAlmostEqual(service.weighted_price(priced_cost, priced_usage), 180.0)
        # 오염된 분모를 쓰면 단가가 구성원(180) 아래로 내려간다 — 이것이 회귀의 증상.
        self.assertLess(
            service.weighted_price(priced_cost, priced_usage + unpriced_usage), 180.0,
        )
        self.assertAlmostEqual(
            service.cost_coverage(priced_usage, priced_usage + unpriced_usage), 2 / 3,
        )

    def test_bridge_reliability_requires_every_period(self) -> None:
        service = server.import_core("app.services.energy_cost_service")
        self.assertTrue(service.is_bridge_reliable(1.0, 1.0))
        self.assertFalse(service.is_bridge_reliable(1.0, 0.5))
        self.assertFalse(service.is_bridge_reliable(1.0, None))

    def test_cod_average_ignores_unmeasured_zero(self) -> None:
        """COD는 합이 아니라 평균이고, 미측정 공장의 0(경산 원수)은 평균을 끌어내리면 안 된다."""
        service = server.import_core("app.services.energy_cost_service")
        self.assertAlmostEqual(service.average_concentration([520.0, 480.0]), 500.0)
        self.assertAlmostEqual(service.average_concentration([520.0, 0.0, 480.0, None]), 500.0)
        self.assertIsNone(service.average_concentration([0.0, None]))

    def test_cost_metric_scope_excludes_water(self) -> None:
        """용수·폐수 처리비는 시스템 관리 대상이 아니다 — 지표 목록에 없어야 한다."""
        service = server.import_core("app.services.energy_cost_service")
        self.assertEqual(set(service.COST_METRICS), {"power", "fuel"})
        self.assertEqual(service.COST_METRIC_KEYS, ("power", "fuel", "total"))
        self.assertIsNone(service.metric_spec(service.TOTAL_METRIC))
        for absent in ("water", "wastewater"):
            self.assertFalse(service.is_supported_metric(absent))

    def test_energy_cost_rejects_unknown_metric_before_query(self) -> None:
        with (
            patch.object(server, "fetch_one") as fetch_one,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.energy_cost(factory="전사", metric="water")
        self.assertEqual(raised.exception.status_code, 400)
        fetch_one.assert_not_called()

    # ── 에너지 비용·단가·COD 적재 (2026-07 MIS 화면 개편) ─────────────
    # 이 6개 컬럼은 컬럼 목록 3곳·한글 매핑·마이그레이션에 나뉘어 등록된다.
    # 어긋나면 예외가 아니라 "값이 조용히 사라지거나 다른 컬럼에 들어가는" 형태로
    # 실패하므로, 사람이 눈으로 맞추는 대신 불변식으로 고정한다.

    ENERGY_COST_COLUMNS = (
        "power_cost_krw", "power_price_krw_kwh",
        "fuel_cost_krw", "fuel_price_krw_nm3",
        "influent_cod_ppm", "effluent_cod_ppm",
    )

    def test_energy_cost_columns_registered_in_every_list(self) -> None:
        parser = server.import_core("app.utils.excel_parser")
        sync = server.import_core("app.services.daily_energy_sync_service")
        upload = server.import_core("app.services.upload_service")
        common = server.import_core("app.services.v5_common")

        for label, columns in (
            ("EXPECTED_COLUMNS", parser.EXPECTED_COLUMNS),
            ("sync _INSERT_COLUMNS", sync._INSERT_COLUMNS),
            ("upload INSERT_COLUMNS", upload.INSERT_COLUMNS),
            ("ENERGY_UPLOAD_TO_MODEL_COLUMNS", list(common.ENERGY_UPLOAD_TO_MODEL_COLUMNS)),
        ):
            with self.subTest(list=label):
                missing = [c for c in self.ENERGY_COST_COLUMNS if c not in columns]
                self.assertEqual(missing, [], f"{label} 누락")

    def test_insert_column_order_matches_numeric_columns(self) -> None:
        """INSERT 값 튜플은 NUMERIC_COLUMNS 순서로 만들어진다.

        두 INSERT 목록의 순서가 NUMERIC_COLUMNS와 어긋나면 전력비 값이 전력단가
        컬럼에 들어가는 식으로 조용히 뒤바뀐다 — 예외가 나지 않아 더 위험하다.
        """
        parser = server.import_core("app.utils.excel_parser")
        sync = server.import_core("app.services.daily_energy_sync_service")
        upload = server.import_core("app.services.upload_service")

        self.assertEqual(parser.NUMERIC_COLUMNS, parser.EXPECTED_COLUMNS[1:])
        for label, columns in (
            ("sync", sync._INSERT_COLUMNS),
            ("upload", upload.INSERT_COLUMNS),
        ):
            with self.subTest(path=label):
                self.assertEqual(columns[:2], ["factory", "date"])
                self.assertEqual(columns[2:], parser.NUMERIC_COLUMNS)

    def test_substring_label_map_checks_specific_keys_first(self) -> None:
        """부분매칭 표는 삽입 순서에 의존한다 — 구체적인 키가 먼저 와야 한다.

        `냉동전력량[kWh]` 은 `냉동전력량` 과 `전력량` 두 키에 모두 걸리므로,
        `전력량` 이 앞서면 냉동 전력이 전체 전력으로 들어간다.
        """
        parser = server.import_core("app.utils.excel_parser")
        keys = list(parser.KOR_SUBSTR_MAP)
        for general in keys:
            for specific in keys:
                if general != specific and general in specific:
                    with self.subTest(general=general, specific=specific):
                        self.assertLess(
                            keys.index(specific), keys.index(general),
                            f"{specific}(구체)가 {general}(일반)보다 뒤에 있어 매칭되지 않는다",
                        )

        # 전치형 행 필터가 같은 표에서 파생되어야 신규 라벨이 함께 살아남는다.
        for key in ("전력비", "전력단가", "연료비", "연료단가", "원수cod", "배출수cod"):
            self.assertIn(key, parser.METRIC_ROW_KEYS)
        self.assertNotIn("날짜", parser.METRIC_ROW_KEYS)

    def test_energy_cost_columns_never_become_model_features(self) -> None:
        """비용·단가는 모델 피처가 되어선 안 된다 — 전력비 = 전력량 × 단가 라 타깃 누출이다."""
        common = server.import_core("app.services.v5_common")
        frame = common.pd.DataFrame(
            {**{column: [1.0] for column in self.ENERGY_COST_COLUMNS}, "dow": [1]}
        )
        features = list(common.get_safe_features(frame).columns)
        leaked = [c for c in features if any(n in c for n in self.ENERGY_COST_COLUMNS)]
        self.assertEqual(leaked, [], "비용·단가가 피처 허용목록을 통과했다")

    def test_energy_cost_migration_chain_matches_schema_order(self) -> None:
        """ALTER AFTER 사슬(기존 DB)과 CREATE 순서(신규 설치)의 컬럼 배치가 같아야 한다."""
        connection = server.import_core("app.database.db_connection")
        migrations = [
            (column, fragment)
            for table, column, fragment in connection._PENDING_COLUMN_MIGRATIONS
            if table == "energy_daily" and column in self.ENERGY_COST_COLUMNS
        ]
        self.assertEqual([c for c, _ in migrations], list(self.ENERGY_COST_COLUMNS))
        anchors = ["water_per_ton_ton", *self.ENERGY_COST_COLUMNS[:-1]]
        for (column, fragment), anchor in zip(migrations, anchors):
            with self.subTest(column=column):
                self.assertIn(f"AFTER {anchor}", fragment)

        schema = (server.LOCAL_CORE_ROOT / "app" / "database" / "schema.sql").read_text(encoding="utf-8")
        create = schema.split("CREATE TABLE IF NOT EXISTS energy_daily")[1].split(") ENGINE")[0]
        defined = [
            match.group(1)
            for match in (
                re.match(r"([a-z_0-9]+)\s+(?:DOUBLE|INT|DATE|DATETIME|VARCHAR|TEXT)", line.strip())
                for line in create.splitlines()
                if line.strip() and not line.strip().startswith("--")
            )
            if match
        ]
        self.assertEqual(
            [c for c in defined if c in self.ENERGY_COST_COLUMNS],
            list(self.ENERGY_COST_COLUMNS),
        )
        self.assertEqual(defined[defined.index("water_per_ton_ton") + 1], "power_cost_krw")

    def test_energy_sync_drops_gyeongsan_rows_before_factory_start(self) -> None:
        """경산 실적은 2026-04-01 이전 행을 동기화 경계에서 다시 유입시키지 않는다."""
        parser = server.import_core("app.utils.excel_parser")
        sync = server.import_core("app.services.daily_energy_sync_service")
        frame = sync.pd.DataFrame({
            "date": sync.pd.to_datetime(["2026-03-31", "2026-04-01", "2026-04-02"]),
            **{column: [1.0, 2.0, 3.0] for column in parser.NUMERIC_COLUMNS},
        })

        cleaned, errors = sync._validate({"경산": frame})

        self.assertEqual(errors, [])
        self.assertEqual(
            [str(value)[:10] for value in cleaned["경산"]["date"]],
            ["2026-04-01", "2026-04-02"],
        )
        self.assertEqual(cleaned["경산"]["total_power_kwh"].tolist(), [2.0, 3.0])

    def test_manual_upload_rejects_gyeongsan_rows_before_factory_start(self) -> None:
        """수동 Excel 업로드도 자동 동기화와 같은 경산 시작일을 강제한다."""
        parser = server.import_core("app.utils.excel_parser")
        validation = server.import_core("app.services.validation_service")
        frame = validation.pd.DataFrame({
            "date": validation.pd.to_datetime(["2026-03-31", "2026-04-01"]),
            **{column: [1.0, 2.0] for column in parser.NUMERIC_COLUMNS},
        })

        cleaned, errors = validation.validate_all({"경산": frame})

        self.assertEqual(cleaned, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("2026-04-01", errors[0].reason)

    # ── 전사(경산 제외) 집계 라벨 ──────────────────────────────
    # 경산은 실적 시작일이 2026-04라 5개 공장과 전년비 동일 기준 비교가 안 된다
    # (경산만 전년 실적이 없어 증가분이 통째로 얹힘). 새 라벨이 기존 "전사"와
    # 같은 딕셔너리 조회 경로를 타는지, 그리고 실제로 경산만 빠지는지를 고정한다.

    GYEONGSAN_EXCLUDED_LABEL = "전사(경산 제외)"

    def test_company_wide_label_registered_everywhere_needed(self) -> None:
        domain = server.import_core("app.domain.factories")
        label = self.GYEONGSAN_EXCLUDED_LABEL

        self.assertTrue(server.is_company_wide(label))
        self.assertTrue(server.is_company_wide("전사"))
        self.assertTrue(server.is_company_wide("전체"))
        self.assertFalse(server.is_company_wide("김해"))
        self.assertTrue(domain.is_company_wide(label))

        self.assertIn(label, server.FACTORY_MEMBERS)
        self.assertNotIn("경산", server.FACTORY_MEMBERS[label])
        self.assertEqual(set(server.FACTORY_MEMBERS[label]), {"남양주1", "남양주2", "김해", "광주", "논산"})

        self.assertIn(label, server.PRODUCTION_FACTORY_CODES)
        self.assertNotIn("F50", server.PRODUCTION_FACTORY_CODES[label])

        self.assertIn(label, domain.AGGREGATE_FACTORY_MEMBERS)
        self.assertNotIn("경산", domain.AGGREGATE_FACTORY_MEMBERS[label])

    def test_physical_factory_members_untouched_by_design(self) -> None:
        """physical_factory_members()는 손대지 않는다 — 딕셔너리 조회라 등록만으로 옳게 동작.

        여기서 "전사"에 6개(경산 포함)가, 새 라벨에 5개(경산 제외)가 나오는 것이
        핵심 불변식이다 — 뒤바뀌면 다른 모든 헬퍼(actual_production_kg 등)가 함께 깨진다.
        """
        label = self.GYEONGSAN_EXCLUDED_LABEL
        self.assertEqual(set(server.physical_factory_members("전사")), set(server.PHYSICAL_FACTORIES))
        self.assertEqual(
            set(server.physical_factory_members(label)),
            set(server.PHYSICAL_FACTORIES) - {"경산"},
        )

    def test_factory_clause_excludes_gyeongsan_for_new_label(self) -> None:
        label = self.GYEONGSAN_EXCLUDED_LABEL
        clause, values = server.factory_clause(label)
        self.assertIn("factory IN", clause)
        self.assertNotIn("경산", values)
        self.assertEqual(set(values), {"남양주1", "남양주2", "김해", "광주", "논산"})
        # 회귀 방지 — 기존 "전사"는 필터 없음(빈 문자열)이어야 한다(현행 동작 불변).
        self.assertEqual(server.factory_clause("전사"), ("", []))

    def test_target_factory_lookup_shares_all_row_with_plain_company_wide(self) -> None:
        """savings_target 조회는 "경산 제외"도 "전사"와 같은 'ALL' 행을 본다(전용 목표 없음)."""
        label = self.GYEONGSAN_EXCLUDED_LABEL
        self.assertEqual("ALL" if server.is_company_wide(label) else label, "ALL")
        self.assertEqual("ALL" if server.is_company_wide("전사") else "전사", "ALL")
        self.assertEqual("ALL" if server.is_company_wide("김해") else "김해", "김해")

    def test_production_correction_energy_codes_need_no_explicit_branch(self) -> None:
        """_energy_factory_codes 는 expand_factory_filter 로 폴백해 새 라벨도 자동 처리한다.

        production_actual_service.get_actual_production_kg 와 같은 이유로 손대지
        않았다 — 이 테스트가 그 판단이 틀리지 않았음을 고정한다.
        """
        service = server.import_core("app.services.production_correction_service")
        label = self.GYEONGSAN_EXCLUDED_LABEL
        codes = service._energy_factory_codes(label)
        self.assertNotIn("경산", codes)
        self.assertEqual(set(codes), {"남양주1", "남양주2", "김해", "광주", "논산"})

    def test_production_correction_prod_codes_exclude_gyeongsan_f_code(self) -> None:
        """_prod_factory_codes 는 폴백이 없어(마지막 줄이 리터럴 반환) 명시 분기가 필요했다."""
        service = server.import_core("app.services.production_correction_service")
        label = self.GYEONGSAN_EXCLUDED_LABEL
        codes = service._prod_factory_codes(label)
        self.assertNotIn("F50", codes)
        self.assertEqual(set(codes), {"F10", "F10A", "F10B", "F20", "F30", "F40"})
        # 대조군 — 기존 "전사"는 F50(경산)을 포함해야 한다(회귀 방지).
        self.assertIn("F50", service._prod_factory_codes("전사"))

    def test_actual_production_service_drops_hardcoded_company_wide_branch(self) -> None:
        """get_actual_production_kg 는 이제 하드코딩 분기 없이 expand_factory_members 하나로 처리한다.

        예전 코드는 "전사"/"전체"를 별도로 FACTORY_PHYSICAL_DISPLAY_ORDER 리터럴로
        반환했다 — 그 분기가 남아 있었다면 새 라벨이 안 걸려도 결과가 우연히 같아
        버그가 안 드러났을 것이다. 소스에 하드코딩 분기가 없는지까지 확인한다.
        """
        import inspect

        service = server.import_core("app.services.production_actual_service")
        source = inspect.getsource(service.get_actual_production_kg)
        self.assertNotIn('"전사", "전체"', source)
        # 함수 본문(주석 제외)이 expand_factory_members 하나만 무조건 호출하는지 —
        # 주석에는 예전 분기를 설명하려고 상수 이름을 그대로 남겼으므로, 코드
        # 줄만 걸러서 확인한다.
        code_lines = [line for line in source.splitlines() if not line.strip().startswith("#")]
        self.assertNotIn("FACTORY_PHYSICAL_DISPLAY_ORDER", "\n".join(code_lines))
        self.assertIn("expand_factory_members(factory)", source)

    def test_events_endpoint_filters_gyeongsan_for_new_label_without_code_change(self) -> None:
        """이벤트 목록도 factory_clause 딕셔너리 조회 하나로 경산이 자동 제외된다."""
        label = self.GYEONGSAN_EXCLUDED_LABEL
        with patch.object(server, "fetch_all", return_value=[]) as fetch_all:
            server.list_events(factory=label)
        sql = fetch_all.call_args.args[0]
        params = fetch_all.call_args.args[1]
        self.assertIn("factory IN", sql)
        self.assertNotIn("경산", params)

    def test_compare_factory_lines_query_filters_gyeongsan_for_new_label(self) -> None:
        """energy() 의 공장별 비교 라인 쿼리는 반드시 factory_clause 필터가 붙어야 한다.

        조건만 is_company_wide 로 바꾸고 쿼리에 필터를 안 붙이면 "경산 제외"를
        골라도 비교 라인에 경산이 그대로 나온다 — 이게 이번 작업의 핵심 함정이었다.
        """
        label = self.GYEONGSAN_EXCLUDED_LABEL
        with (
            patch.object(server, "fetch_one", return_value={"max_date": date(2026, 7, 28)}),
            patch.object(server, "fetch_all", return_value=[]) as fetch_all,
            patch.object(server, "period_coverage", return_value={"expectedDays": 0, "presentDays": 0, "missingDays": 0}),
        ):
            # date_from/date_to는 FastAPI Query() 기본값을 갖는다 — 라우트 밖에서
            # 직접 호출할 때는 명시적으로 None을 넘겨야 실제 None으로 들어간다
            # (안 넘기면 Query 마커 객체 그대로 남아 날짜 비교에서 TypeError가 난다).
            server.energy(factory=label, requested_date=date(2026, 7, 28), date_from=None, date_to=None)
        compare_call = next(
            call for call in fetch_all.call_args_list
            if "GROUP BY date, factory" in call.args[0]
        )
        self.assertIn("factory IN", compare_call.args[0])
        self.assertNotIn("경산", compare_call.args[1])

    def test_compare_factory_lines_still_unfiltered_for_plain_company_wide(self) -> None:
        """대조군 — 기존 "전사"는 여전히 필터 없이 6개 전 공장을 가져와야 한다(회귀 방지)."""
        with (
            patch.object(server, "fetch_one", return_value={"max_date": date(2026, 7, 28)}),
            patch.object(server, "fetch_all", return_value=[]) as fetch_all,
            patch.object(server, "period_coverage", return_value={"expectedDays": 0, "presentDays": 0, "missingDays": 0}),
        ):
            server.energy(factory="전사", requested_date=date(2026, 7, 28), date_from=None, date_to=None)
        compare_call = next(
            call for call in fetch_all.call_args_list
            if "GROUP BY date, factory" in call.args[0]
        )
        self.assertNotIn("factory IN", compare_call.args[0])
        self.assertEqual(compare_call.args[1], (date(2026, 7, 1), date(2026, 7, 28)))

    # ── monthly_fallback_service ────────────────────────────────
    # 경산 월별 폴백의 세 규칙(전사_경산구분_및_월별적재_계획.md B-5)을 고정한다.
    # managed_cursor 를 페이크 컨텍스트 매니저로 바꿔치기해 순수하게 병합 로직만
    # 검증한다 — 실DB 검증은 별도로 이미 수행했다(빈 테이블에서 회귀 없음 확인).

    class _FakeCursor:
        def __init__(self, batches: list[list[dict]]) -> None:
            self._batches = list(batches)
            self._current: list[dict] = []

        def execute(self, _sql, _params=None) -> None:
            self._current = self._batches.pop(0) if self._batches else []

        def fetchall(self) -> list[dict]:
            return self._current

    class _FakeManagedCursor:
        """monthly_fallback_service.managed_cursor 를 대체하는 페이크.

        호출 순서대로 batches 를 하나씩 소비한다 — 이 서비스가 쿼리를 부르는
        순서(daily 먼저, monthly 나중)에 맞춰 batches 를 준비해야 한다.
        """

        def __init__(self, *all_batches: list[dict]) -> None:
            self._cursor = ServerHelperTests._FakeCursor(list(all_batches))

        def __call__(self, *_args, **_kwargs):
            return self

        def __enter__(self):
            return (None, self._cursor)

        def __exit__(self, *_exc) -> bool:
            return False

    def test_month_range_crosses_year_boundary(self) -> None:
        service = server.import_core("app.services.monthly_fallback_service")
        months = service._month_range(date(2025, 11, 1), date(2026, 2, 1))
        self.assertEqual(months, [(2025, 11), (2025, 12), (2026, 1), (2026, 2)])

    def test_monthly_energy_rule1_prefers_daily_over_monthly_table(self) -> None:
        """규칙 1 — 같은 (공장,월)에 일별 행이 있으면 월별 테이블 값은 무시된다."""
        service = server.import_core("app.services.monthly_fallback_service")
        daily_rows = [{
            "factory": "경산", "y": 2026, "m": 4, "n": 30,
            "total_power_kwh": 100.0, "fuel_nm3": 10.0, "water_ton": 1.0,
            "wastewater_ton": 1.0, "power_cost_krw": 1000.0, "fuel_cost_krw": 100.0,
        }]
        # 월별 테이블에 같은 (경산, 2026-04) 행이 있어도(이중 계상 방지 검증용
        # 함정 데이터), 일별이 있으므로 이 값은 절대 쓰이면 안 된다.
        monthly_rows = [{
            "factory": "경산", "month_key": "2026-04",
            "total_power_kwh": 999999.0, "fuel_nm3": 999999.0, "water_ton": 999999.0,
            "wastewater_ton": 999999.0, "power_cost_krw": 999999.0, "fuel_cost_krw": 999999.0,
        }]
        fake = self._FakeManagedCursor(daily_rows, monthly_rows)
        with patch.object(service, "managed_cursor", fake), \
             patch.object(service, "expand_factory_members", return_value=("경산",)):
            result = service.monthly_energy("경산", date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual(result[(2026, 4)]["total_power_kwh"], 100.0)

    def test_monthly_energy_rule1_falls_back_when_no_daily_rows(self) -> None:
        """규칙 1 — 일별 행이 0건인 달만 월별 테이블 값을 쓴다."""
        service = server.import_core("app.services.monthly_fallback_service")
        fake = self._FakeManagedCursor([], [{
            "factory": "경산", "month_key": "2025-06",
            "total_power_kwh": 50.0, "fuel_nm3": 5.0, "water_ton": 0.5,
            "wastewater_ton": 0.5, "power_cost_krw": 500.0, "fuel_cost_krw": 50.0,
        }])
        with patch.object(service, "managed_cursor", fake), \
             patch.object(service, "expand_factory_members", return_value=("경산",)):
            result = service.monthly_energy("경산", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result[(2025, 6)]["total_power_kwh"], 50.0)

    def test_monthly_energy_rule2_aggregates_per_physical_factory(self) -> None:
        """규칙 2 — 집계 라벨은 물리 공장별로 각각 판단한 뒤 합산해야 한다.

        여기서는 남양주1은 일별(정상 실적), 남양주2는 폴백(월별 테이블)인
        섞인 상황을 만들어, 합계가 두 값의 합인지 확인한다. 전사 단위로
        "일별 데이터가 있다"고 뭉뚱그려 판정했다면 남양주2도 일별 취급되어
        (없는 데이터라) 0으로 빠졌을 것이다.
        """
        service = server.import_core("app.services.monthly_fallback_service")
        daily_rows = [{
            "factory": "남양주1", "y": 2025, "m": 6, "n": 30,
            "total_power_kwh": 100.0, "fuel_nm3": 0.0, "water_ton": 0.0,
            "wastewater_ton": 0.0, "power_cost_krw": 0.0, "fuel_cost_krw": 0.0,
        }]
        monthly_rows = [{
            "factory": "남양주2", "month_key": "2025-06",
            "total_power_kwh": 40.0, "fuel_nm3": 0.0, "water_ton": 0.0,
            "wastewater_ton": 0.0, "power_cost_krw": 0.0, "fuel_cost_krw": 0.0,
        }]
        fake = self._FakeManagedCursor(daily_rows, monthly_rows)
        with patch.object(service, "managed_cursor", fake), \
             patch.object(service, "expand_factory_members", return_value=("남양주1", "남양주2")):
            result = service.monthly_energy("남양주", date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(result[(2025, 6)]["total_power_kwh"], 140.0)

    def test_fallback_months_reports_only_table_backed_months(self) -> None:
        service = server.import_core("app.services.monthly_fallback_service")
        fake = self._FakeManagedCursor([], [{
            "factory": "경산", "month_key": "2025-03",
            "total_power_kwh": 1.0, "fuel_nm3": 0.0, "water_ton": 0.0,
            "wastewater_ton": 0.0, "power_cost_krw": 0.0, "fuel_cost_krw": 0.0,
        }])
        with patch.object(service, "managed_cursor", fake), \
             patch.object(service, "expand_factory_members", return_value=("경산",)):
            months = service.fallback_months("경산", date(2025, 3, 1), date(2025, 3, 31))
        self.assertEqual(months, {(2025, 3)})

    def test_month_key_sql_avoids_percent_placeholder_collision(self) -> None:
        """'YYYY-MM' 생성에 DATE_FORMAT('%Y-%m') 을 쓰면 안 된다.

        파라미터가 있는 쿼리에서 드라이버가 '%' 를 자기 자리표시자로 해석해,
        '%%' 로 이스케이프하면 MySQL 에는 '%%Y-%%m' 이 도착하고 리터럴 문자열
        '%Y-%m' 이 돌아온다 — 커버리지 판정이 통째로 무력화되면서도 예외는 나지
        않아 조용히 실패한다(2026-07-31 실측). CONCAT 방식인지 고정한다.
        """
        service = server.import_core("app.services.monthly_input_service")
        fragment = service._MONTH_KEY_SQL.format(column="date")
        self.assertNotIn("%", fragment)
        self.assertIn("CONCAT", fragment)
        self.assertIn("LPAD", fragment)

    def test_production_daily_code_uses_kr_to_code_mapping(self) -> None:
        service = server.import_core("app.services.monthly_fallback_service")
        self.assertEqual(service._production_daily_code("경산"), "F50")
        self.assertEqual(service._production_daily_code("남양주1"), "F10A")
        self.assertIsNone(service._production_daily_code("존재안함"))

    # ── server.py 배선: _apply_monthly_energy_fallback ────────────
    # B-6 의 핵심 함정 — 폴백 달을 원본 행에 "더하면" 이미 일별로 잡힌 다른
    # 공장분이 두 번 들어간다. 반드시 그 달의 행을 통째로 "교체"해야 한다.

    def test_monthly_fallback_wiring_skips_query_when_nothing_to_backfill(self) -> None:
        """폴백 대상 달이 없으면(가장 흔한 경우) monthly_energy() 자체를 부르지 않는다."""
        rows = [{"y": 2026, "m": 7, "power": 100.0}]
        fake_service = SimpleNamespace(
            fallback_months=Mock(return_value=set()),
            monthly_energy=Mock(side_effect=AssertionError("불필요한 조회")),
        )
        with patch.object(server, "import_core", return_value=fake_service):
            result = server._apply_monthly_energy_fallback(
                rows, "경산", date(2026, 1, 1), date(2026, 7, 31), {"power": "total_power_kwh"},
            )
        self.assertEqual(result, rows)

    def test_monthly_fallback_wiring_replaces_not_adds(self) -> None:
        """폴백 달의 행은 monthly_energy() 결과로 완전히 교체된다 — 원본 값과 더하지 않는다.

        원본 SQL 행에 이미 (2025,6) 이 100.0 으로 잡혀 있어도(다른 물리 공장의
        일별 합), monthly_energy() 가 이미 그 달의 전체 공장을 다시 합산해
        돌려주므로 그 값을 그대로 써야 한다. 더했다면 240.0 이 나왔을 것이다.
        """
        rows = [{"y": 2025, "m": 6, "power": 100.0}]
        fake_service = SimpleNamespace(
            fallback_months=Mock(return_value={(2025, 6)}),
            monthly_energy=Mock(return_value={(2025, 6): {"total_power_kwh": 140.0}}),
        )
        with patch.object(server, "import_core", return_value=fake_service):
            result = server._apply_monthly_energy_fallback(
                rows, "남양주", date(2025, 6, 1), date(2025, 6, 30), {"power": "total_power_kwh"},
            )
        self.assertEqual(result, [{"y": 2025, "m": 6, "power": 140.0}])

    def test_monthly_fallback_wiring_applies_scale(self) -> None:
        """energy() 의 yoy_rows 는 /1000 스케일이다 — 폴백값도 같은 배율이 적용돼야 한다."""
        fake_service = SimpleNamespace(
            fallback_months=Mock(return_value={(2025, 6)}),
            monthly_energy=Mock(return_value={(2025, 6): {"total_power_kwh": 140_000.0}}),
        )
        with patch.object(server, "import_core", return_value=fake_service):
            result = server._apply_monthly_energy_fallback(
                [], "경산", date(2025, 6, 1), date(2025, 6, 30),
                {"power": "total_power_kwh"}, scale=1 / 1000,
            )
        self.assertEqual(result, [{"y": 2025, "m": 6, "power": 140.0}])


if __name__ == "__main__":
    unittest.main()
