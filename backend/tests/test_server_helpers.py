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
        self.assertEqual(
            server.resolve_production_period("month", base, None, None),
            (date(2026, 7, 1), base),
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

    def test_annual_burnup_actual_line_stops_after_last_actual_month(self) -> None:
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
                [{"m": month, "plan": 100.0} for month in range(1, 13)],  # 월별 계획 (burnup)
                [],                                                # 미달/초과 gap
                [],                                                # 제품유형 계획
                [],                                                # 월별 전년비
            ]),
        ):
            result = server.production(factory="전사", requested_date=date(2026, 7, 15), mode="year")
        burnup = result["burnup"]
        self.assertEqual(len(burnup), 12)
        # 계획 누계는 12월까지 이어진다
        self.assertEqual(burnup[0]["cumPlan"], 100.0)
        self.assertEqual(burnup[11]["cumPlan"], 1200.0)
        # 실적 누계는 마지막 실적 월(3월)까지만 값이 있고 이후는 None (평탄선 방지)
        self.assertEqual(burnup[2]["cumActual"], 150.0)
        self.assertIsNone(burnup[3]["cumActual"])
        self.assertIsNone(burnup[11]["cumActual"])

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
            patch.object(server, "fetch_one", side_effect=[
                {"max_date": date(2026, 7, 15), "updated_at": None},   # MAX(date)
                {"count": 0},                                           # 이상 이탈 건수
            ]),
            patch.object(server, "fetch_all", side_effect=[
                [{"date": date(2026, 7, 15), "actual": 191.0,
                  "fuel": 1200.0, "water": 340.0, "wastewater": 150.0}],  # 7일 추이
                [],                                                        # YoY rows
                [],                                                        # 구성비 (YTD)
                [],                                                        # events
            ]),
            patch.object(server, "aggregate_prediction_rows", return_value=[]),
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


if __name__ == "__main__":
    unittest.main()
