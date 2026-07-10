from datetime import date, timedelta

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tools.mail import daily_report_builder as daily_builder
from tools.mail import period_report_builder as period_builder
from tools.mail.config import DailyReportConfig, TEMPLATE_DIR
from tools.mail.daily_report_builder import (
    DAILY_DETAIL_METRICS,
    DAILY_USAGE_METRICS,
    FACTORY_TABLE_METRICS,
    _build_daily_factory_rows,
    _daily_direction_signal,
)
from tools.mail.period_report_builder import (
    _build_monthly_comparison_rows,
    _build_period_factory_rows,
    _trend_row,
    last_complete_week,
)


def _energy_row(factory: str, production_kg: float, power: float) -> dict:
    return {
        "factory": factory,
        "mix_prod_kg": production_kg,
        "total_power_kwh": power,
        "fuel_nm3": 50.0,
        "water_ton": 20.0,
        "wastewater_ton": 10.0,
    }


def test_daily_direction_signal_matches_dashboard_rules():
    warn = _daily_direction_signal(90, 100, 110, 100, 100, 100)
    good = _daily_direction_signal(110, 100, 90, 100, 100, 100)
    small = _daily_direction_signal(99.5, 100, 100.5, 100, 100, 100)
    missing = _daily_direction_signal(None, 100, 110, 100, 100, 100)

    assert (warn["label"], warn["tone"]) == ("생산↓ 사용↑", "warn")
    assert (good["label"], good["tone"]) == ("생산↑ 사용↓", "good")
    assert (small["label"], small["tone"]) == ("변화 작음", "neutral")
    assert missing["tone"] == "missing"


def test_monthly_comparison_rows_include_current_previous_and_delta():
    current = [_energy_row("남양주1", 1200.0, 110.0)]
    previous = [_energy_row("남양주1", 1000.0, 100.0)]

    rows = _build_monthly_comparison_rows(current, previous)
    total = rows[0]
    production = total["cells"][0]
    power = total["cells"][1]

    assert len(rows) == 6  # 전사 + 남양주·김해·광주·논산·경산
    assert production["delta"] == "+20.0%"
    assert production["delta_color"] == "#2563eb"
    assert power["delta"] == "−8.3%"
    assert power["delta_color"] == "#2563eb"


def test_last_complete_week_is_previous_monday_to_sunday():
    assert last_complete_week(date(2026, 7, 10)) == (
        date(2026, 6, 29),
        date(2026, 7, 5),
    )
    assert last_complete_week(date(2026, 7, 13)) == (
        date(2026, 7, 6),
        date(2026, 7, 12),
    )


def test_all_three_mail_templates_render_with_strict_context():
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=True,
    )

    ref_date = date(2026, 7, 8)
    raw_daily = []
    for offset in range(7):
        row = _energy_row(
            "남양주1",
            900.0 if offset == 6 else 1000.0,
            110.0 if offset == 6 else 100.0,
        )
        row["date"] = ref_date - timedelta(days=6 - offset)
        raw_daily.append(row)
    daily_rows, warning_items, good_count = _build_daily_factory_rows(raw_daily, ref_date)

    current = [_energy_row("남양주1", 1200.0, 110.0)]
    previous = [_energy_row("남양주1", 1000.0, 100.0)]
    weekly_rows = _build_period_factory_rows([
        dict(value_rows=current, cmp_curr_rows=current, cmp_base_rows=previous),
    ])
    weekly_trend = [_trend_row("W27", current, previous)]
    monthly_rows = _build_monthly_comparison_rows(current, previous)
    daily = env.get_template("daily_energy_report.html").render(
        subject="daily",
        ref_date="2026-07-08",
        prev_date="2026-07-07",
        ref_weekday="수",
        generated_at="2026-07-10 12:00:00",
        trend_from="2026-07-02",
        n_factories=5,
        warning_count=len(warning_items),
        warning_factory_count=len({item["factory"] for item in warning_items}),
        good_count=good_count,
        warning_items=warning_items,
        factory_rows=daily_rows,
        signal_metrics=DAILY_USAGE_METRICS,
        detail_metrics=DAILY_DETAIL_METRICS,
    )
    weekly = env.get_template("period_energy_report.html").render(
        subject="weekly",
        report_title="주간",
        period_label="기간",
        generated_at="2026-07-10 12:00:00",
        table_note="note",
        axis_defs=[{"label": "당주", "delta_header": "전주비"}],
        factory_table_metrics=FACTORY_TABLE_METRICS,
        factory_rows=weekly_rows,
        extra_table={"title": "최근 4주", "note": "note", "rows": weekly_trend},
    )
    monthly = env.get_template("monthly_energy_report.html").render(
        subject="monthly",
        year=2026,
        previous_year=2025,
        month=6,
        period_from="2026-06-01",
        period_to="2026-06-30",
        generated_at="2026-07-10 12:00:00",
        factory_table_metrics=FACTORY_TABLE_METRICS,
        mtd_rows=monthly_rows,
        ytd_rows=monthly_rows,
        mtd_current_label="2026.06",
        mtd_previous_label="2025.06",
        ytd_current_label="2026.01~06",
        ytd_previous_label="2025.01~06",
    )

    assert "일일 생산량 · 에너지 이상 신호" in daily
    assert "최근 4주" in weekly
    assert "당월 실적 비교 (MTD)" in monthly
    assert "연 누계 실적 비교 (YTD)" in monthly

def test_daily_builder_renders_new_two_section_report(monkeypatch):
    ref_date = date(2026, 7, 8)
    raw_rows = []
    for offset in range(7):
        row = _energy_row(
            "남양주1",
            900.0 if offset == 6 else 1000.0,
            110.0 if offset == 6 else 100.0,
        )
        row["date"] = ref_date - timedelta(days=6 - offset)
        raw_rows.append(row)

    monkeypatch.setattr(daily_builder, "_fetch_rows_range", lambda *_args, **_kwargs: raw_rows)
    config = DailyReportConfig(
        reference_offset_days=2,
        factories_filter=[],
        include_company_total=True,
        chart_recent_days=7,
    )
    report = daily_builder.build_daily_report(ref_date=ref_date, config=config)

    assert "일일 에너지 이상 alert" in report.subject
    assert "1. 전일 대비 방향 이상 요약" in report.html
    assert "2. 당일 · 전일 상세 실적" in report.html
    assert "생산↓ 사용↑" in report.html
    assert "사업장별 원단위" not in report.html


def test_monthly_builder_renders_mtd_and_ytd_only(monkeypatch):
    def fake_fetch(date_from, date_to, *_args, **_kwargs):
        is_current = date_from.year == 2026
        return [_energy_row("남양주1", 1200.0 if is_current else 1000.0, 110.0 if is_current else 100.0)]

    monkeypatch.setattr(period_builder, "_fetch_rows_range", fake_fetch)
    report = period_builder.build_monthly_report(year=2026, month=6)

    assert "월간 에너지 성과 리포트" in report.subject
    assert "1. 당월 실적 비교 (MTD)" in report.html
    assert "2. 연 누계 실적 비교 (YTD)" in report.html
    assert "최근 4주 전사 추이" not in report.html
