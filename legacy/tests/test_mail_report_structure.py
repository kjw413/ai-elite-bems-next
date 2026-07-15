from datetime import date, timedelta

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tools.mail import daily_report_builder as daily_builder
from tools.mail import period_report_builder as period_builder
from tools.mail.config import DailyReportConfig, TEMPLATE_DIR
from tools.mail.daily_report_builder import (
    FACTORY_TABLE_METRICS,
    _build_daily_factory_rows,
    _build_daily_snapshot_table,
    _daily_direction_signal,
)
from tools.mail.period_report_builder import (
    _build_monthly_factory_rows,
    _build_monthly_mtd_charts,
    _build_weekly_snapshot_table,
    last_complete_week,
)


def _energy_row(factory: str, production_kg: float, power: float) -> dict:
    return {
        "factory": factory,
        "mix_prod_kg": production_kg,
        "total_power_kwh": power,
        "freezing_power_kwh": power * 0.4,
        "air_compressor_kwh": power * 0.1,
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


def test_daily_factory_warning_compares_raw_usage_not_unit_rate():
    ref_date = date(2026, 7, 8)
    prev_date = ref_date - timedelta(days=7)
    previous = _energy_row("남양주1", 1000.0, 100.0)
    current = _energy_row("남양주1", 500.0, 90.0)
    previous["date"] = prev_date
    current["date"] = ref_date

    factory_rows, warning_items, _good_count = _build_daily_factory_rows(
        [previous, current], ref_date, prev_date,
    )

    ny1 = next(row for row in factory_rows if row["factory"] == "남양주1")
    power = next(signal for signal in ny1["signals"] if signal["energy"] == "전력")
    # 사용량은 100→90으로 감소했다. 생산량 감소 때문에 원단위는 상승하지만
    # 섹션 2는 원단위가 아니라 원시 사용량을 비교하므로 경고가 아니다.
    assert power["label"] == "동반 감소"
    assert not any(item["energy"] == "전력" for item in warning_items)


def test_monthly_factory_rows_include_mtd_ytd_and_delta():
    current = [_energy_row("남양주1", 1200.0, 110.0)]
    previous = [_energy_row("남양주1", 1000.0, 100.0)]

    rows = _build_monthly_factory_rows(current, previous, current, previous)
    total = rows[0]
    production = total["metric_cells"][0]["mtd"]
    power = total["metric_cells"][1]["mtd"]

    assert len(rows) == 7  # 전사 + 남양주1·남양주2·김해·광주·논산·경산
    assert production["delta"] == "+20.0%"
    assert production["color"] == "#2563eb"
    assert power["delta"] == "−8.3%"
    assert power["color"] == "#2563eb"


def test_monthly_mtd_chart_uses_two_row_value_tables(monkeypatch):
    import plotly.graph_objects as go

    current = []
    previous = []
    for month, power in ((1, 110.0), (2, 120.0)):
        row = _energy_row("남양주1", 1000.0, power)
        row["date"] = date(2026, month, 15)
        current.append(row)
    for month, power in ((1, 100.0), (2, 105.0)):
        row = _energy_row("남양주1", 1000.0, power)
        row["date"] = date(2025, month, 15)
        previous.append(row)

    figures = []

    def fake_to_image(self, **_kwargs):
        figures.append(self)
        return b"png"

    monkeypatch.setattr(go.Figure, "to_image", fake_to_image)
    images, blocks = _build_monthly_mtd_charts(2026, 12, current, previous)

    assert images
    assert blocks[0]["cid"].startswith("mtd_chart_")
    previous_power, current_power = figures[0].data[:2]
    assert previous_power.mode == "lines+markers"
    assert previous_power.text is None
    assert previous_power.line.color == "#64748b"
    assert current_power.mode == "lines+markers"
    assert current_power.text is None
    water_index = next(
        i for i, metric in enumerate(daily_builder.INTENSITY_CHART_METRICS)
        if metric["key"] == "water"
    )
    fuel_index = next(
        i for i, metric in enumerate(daily_builder.INTENSITY_CHART_METRICS)
        if metric["key"] == "fuel"
    )
    previous_water, current_water = figures[0].data[water_index * 2:water_index * 2 + 2]
    assert previous_water.text is None
    assert current_water.text is None
    value_tables = [trace for trace in figures[0].data if trace.type == "table"]
    assert len(value_tables) == len(daily_builder.INTENSITY_CHART_METRICS)
    # 12월까지 확장해도 월별 값 공간을 확보하도록 좌측 연도 라벨 열은 두지 않는다.
    # 전년/당해 행은 범례와 동일한 글자색으로 구분한다.
    assert list(value_tables[0].header.values) == [f"{month}월" for month in range(1, 13)]
    assert len(value_tables[0].cells.values) == 12
    assert list(value_tables[0].cells.values[0]) == ["100.0", "110.0"]
    assert list(value_tables[0].cells.values[1]) == ["105.0", "120.0"]
    assert list(value_tables[0].cells.font.color[0]) == [
        daily_builder.PREV_YEAR_LINE_COLOR,
        daily_builder.INTENSITY_CHART_METRICS[0]["chart_color"],
    ]
    assert list(value_tables[fuel_index].cells.values[0]) == ["50.0", "50.0"]
    assert list(value_tables[water_index].cells.values[0]) == ["20.00", "20.00"]
    assert figures[0].layout.xaxis.gridcolor == "#e2e8f0"
    assert figures[0].layout.yaxis.gridcolor == "#e2e8f0"
    assert figures[0].layout.yaxis5.tickformat == ",.2f"
    assert figures[0].layout.yaxis.domain[0] > value_tables[0].domain.y[1]
    assert value_tables[0].domain.y[0] > figures[0].layout.yaxis3.domain[1]


def test_overlay_axis_ticks_only_show_on_row_right_chart(monkeypatch):
    import plotly.graph_objects as go

    figures = []

    def fake_to_image(self, **_kwargs):
        figures.append(self)
        return b"png"

    monkeypatch.setattr(go.Figure, "to_image", fake_to_image)
    metrics = daily_builder.INTENSITY_CHART_METRICS[:2]
    series_by_col = {
        metric["unit_col"]: [367.0, 348.0, 361.0, 375.0]
        for metric in metrics
    }

    image = daily_builder._render_metric_grid_chart(
        metrics=metrics,
        x_labels=["W25", "W26", "W27", "W28"],
        series_by_col=series_by_col,
        overlay_series=[2300.0, 2500.0, 2400.0, 2450.0],
        overlay_metric=daily_builder.PRODUCTION_METRIC,
        cur_legend="원단위",
        cols=2,
        include_value_table=True,
        include_overlay_in_value_table=False,
        include_value_table_label_column=False,
    )

    assert image == b"png"
    production_overlay = figures[0].data[0]
    assert production_overlay.mode == "lines+markers"
    assert production_overlay.text is None
    value_tables = [trace for trace in figures[0].data if trace.type == "table"]
    assert len(value_tables) == 3
    production_table, *unit_tables = value_tables
    # 생산량은 그리드 좌상단에 한 번만 표시한다.
    assert len(production_table.cells.values) == 5
    assert list(production_table.header.values) == ["", "W25", "W26", "W27", "W28"]
    assert list(production_table.cells.values[0]) == ["생산량"]
    assert list(production_table.cells.values[1]) == ["2,300"]
    assert list(production_table.cells.values[4]) == ["2,450"]
    assert production_table.domain.x[1] <= 0.5
    assert production_table.domain.y[0] > figures[0].layout.yaxis.domain[1]
    assert [annotation.text for annotation in figures[0].layout.annotations] == [
        "전력 원단위 (kWh/ton)",
        "냉동전력 원단위 (kWh/ton)",
    ]
    assert figures[0].layout.margin.t == 20
    assert figures[0].layout.legend.yanchor == "top"
    # 각 지표는 차트 제목으로 식별되므로 반복되는 빈 헤더/원단위 라벨 열을 제거한다.
    assert list(unit_tables[0].header.values) == ["W25", "W26", "W27", "W28"]
    assert len(unit_tables[0].cells.values) == 4
    assert list(unit_tables[0].cells.values[0]) == ["367.0"]
    assert list(unit_tables[0].cells.values[3]) == ["375.0"]
    # 값 표가 기간 라벨을 담으므로 차트 x축 눈금 라벨은 숨긴다(회전 라벨-표 겹침 제거).
    assert figures[0].layout.xaxis.showticklabels is False
    assert figures[0].layout.yaxis2.showticklabels is False
    assert figures[0].layout.yaxis4.showticklabels is True
    assert figures[0].layout.yaxis4.tickfont.color == "#4D7C0F"
    assert figures[0].layout.xaxis.gridcolor == "#e2e8f0"
    assert figures[0].layout.xaxis2.gridcolor == "#e2e8f0"
    assert figures[0].layout.yaxis.gridcolor == "#e2e8f0"
    assert figures[0].layout.yaxis3.gridcolor == "#e2e8f0"


def test_last_complete_week_is_previous_monday_to_sunday():
    assert last_complete_week(date(2026, 7, 10)) == (
        date(2026, 6, 29),
        date(2026, 7, 5),
    )
    assert last_complete_week(date(2026, 7, 13)) == (
        date(2026, 7, 6),
        date(2026, 7, 12),
    )


def test_daily_snapshot_compares_same_weekday_from_previous_week():
    ref_date = date(2026, 7, 8)
    compare_date = ref_date - timedelta(days=7)
    rows = [
        dict(_energy_row("남양주1", 1000.0, 100.0), date=compare_date),
        dict(_energy_row("남양주1", 900.0, 110.0), date=ref_date),
    ]

    snapshot = _build_daily_snapshot_table(rows, ref_date, compare_date)
    production_cell = snapshot["metric_rows"][0]["cells"][1]
    power_cell = snapshot["metric_rows"][1]["cells"][1]

    assert production_cell["value"] == "1"
    assert production_cell["delta"] == "−10.0%"
    assert power_cell["value"] == "122.22"
    assert power_cell["delta"] == "+22.2%"


def test_weekly_builder_uses_four_week_trend(monkeypatch):
    chart_calls = []

    def fake_fetch(_date_from, date_to, *_args, **_kwargs):
        row = _energy_row("남양주1", 1000.0, 100.0)
        row["date"] = date_to
        return [row]

    def fake_chart(**kwargs):
        chart_calls.append(kwargs)
        return b"png"

    monkeypatch.setattr(period_builder, "_fetch_rows_range", fake_fetch)
    monkeypatch.setattr(period_builder, "_render_metric_grid_chart", fake_chart)

    report = period_builder.build_weekly_report(ref_date=date(2026, 7, 8))

    assert chart_calls
    assert chart_calls[0]["x_labels"] == [
        "W25<br>(06/15~06/21)",
        "W26<br>(06/22~06/28)",
        "W27<br>(06/29~07/05)",
        "W28<br>(07/06~07/12)",
    ]
    assert chart_calls[0]["cur_legend"] == "원단위"
    assert chart_calls[0]["include_value_table"] is True
    assert chart_calls[0]["include_overlay_in_value_table"] is False
    assert chart_calls[0]["include_value_table_label_column"] is False
    assert chart_calls[0]["overlay_series"] == [1.0, 1.0, 1.0, 1.0]
    assert report.subject == "[생산기술팀] 주간 에너지 원단위 alert 07/06~07/12"
    assert "남양주1" in report.html
    assert "2. 주차별(WTD) 원단위 추이 (직전 4주)" in report.html
    assert "직전 5주" not in report.html
    assert "()" not in report.html


def test_all_three_mail_templates_render_with_strict_context():
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=True,
    )

    ref_date = date(2026, 7, 8)
    raw_daily = []
    for offset in range(8):
        row = _energy_row(
            "남양주1",
            900.0 if offset == 7 else 1000.0,
            110.0 if offset == 7 else 100.0,
        )
        row["date"] = ref_date - timedelta(days=7 - offset)
        raw_daily.append(row)
    compare_date = ref_date - timedelta(days=7)
    daily_rows, warning_items, good_count = _build_daily_factory_rows(
        raw_daily, ref_date, compare_date,
    )
    snapshot_table = _build_daily_snapshot_table(raw_daily, ref_date, compare_date)

    current = [_energy_row("남양주1", 1200.0, 110.0)]
    previous = [_energy_row("남양주1", 1000.0, 100.0)]
    weekly_snapshot_table = _build_weekly_snapshot_table(current, previous)
    monthly_rows = _build_monthly_factory_rows(current, previous, current, previous)
    daily = env.get_template("daily_energy_report.html").render(
        subject="daily",
        ref_date="2026-07-08",
        prev_date="2026-07-01",
        ref_weekday="수",
        generated_at="2026-07-10 12:00:00",
        trend_from="2026-07-02",
        warning_count=len(warning_items),
        warning_factory_count=len({item["factory"] for item in warning_items}),
        warning_items=warning_items,
        factory_rows=daily_rows,
        snapshot_table=snapshot_table,
    )
    weekly = env.get_template("weekly_energy_report.html").render(
        subject="weekly",
        report_title="주간",
        period_label="기간",
        generated_at="2026-07-10 12:00:00",
        section1_note="note",
        weekly_snapshot_table=weekly_snapshot_table,
        trend_charts=[],
        trend_week_count=4,
        trend_chart_note="note",
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
        factory_rows=monthly_rows,
        mtd_current_label="2026.06",
        mtd_previous_label="2025.06",
        ytd_current_label="2026.01~06",
        ytd_previous_label="2025.01~06",
        mtd_charts=[],
    )

    assert "1. 전일 원단위 실적 (vs 직전주 동일 요일)" in daily
    assert "2. 생산량↓ · 사용량↑ (vs 직전주 동일 요일)" in daily
    assert "2. 주차별(WTD) 원단위 추이 (직전 4주)" in weekly
    assert "1. 당월 실적 비교" in monthly
    assert "2. 월별(MTD) 원단위 추이" in monthly
    assert "table-layout:auto" in monthly
    assert monthly.count('nowrap="nowrap"') == len(monthly_rows) * len(FACTORY_TABLE_METRICS) * 2
    assert "white-space:nowrap; word-break:keep-all" in monthly

def test_daily_builder_renders_new_two_section_report(monkeypatch):
    ref_date = date(2026, 7, 8)
    raw_rows = []
    for offset in range(8):
        row = _energy_row(
            "남양주1",
            900.0 if offset == 7 else 1000.0,
            110.0 if offset == 7 else 100.0,
        )
        row["date"] = ref_date - timedelta(days=7 - offset)
        raw_rows.append(row)

    fetch_calls = []

    def fake_fetch(date_from, date_to, *_args, **_kwargs):
        fetch_calls.append((date_from, date_to))
        return raw_rows

    monkeypatch.setattr(daily_builder, "_fetch_rows_range", fake_fetch)
    monkeypatch.setattr(daily_builder, "_load_holiday_dates", lambda: set())
    config = DailyReportConfig(
        reference_offset_days=2,
        factories_filter=[],
        include_company_total=True,
        chart_recent_days=7,
    )
    report = daily_builder.build_daily_report(ref_date=ref_date, config=config)

    assert "일일 에너지 원단위 alert" in report.subject
    assert fetch_calls == [(date(2026, 7, 1), ref_date)]
    assert "1. 전일 원단위 실적 (vs 직전주 동일 요일)" in report.html
    assert "2. 생산량↓ · 사용량↑ (vs 직전주 동일 요일)" in report.html
    assert "생산↓ 사용↑" in report.html
    assert "전력 원단위" in report.html
    assert "연료 원단위" in report.html
    assert "용수 원단위" in report.html
    assert "폐수/용수" in report.html
    assert "방향 신호" not in report.html
    assert ">판정<" in report.html
    calm_rows = []
    for offset in range(8):
        row = _energy_row("남양주1", 1000.0, 100.0)
        row["date"] = ref_date - timedelta(days=7 - offset)
        calm_rows.append(row)
    monkeypatch.setattr(daily_builder, "_fetch_rows_range", lambda *_args, **_kwargs: calm_rows)
    calm_report = daily_builder.build_daily_report(ref_date=ref_date, config=config)
    assert "생산↓·사용↑ 조합 없음" in calm_report.html
    assert ">판정<" not in calm_report.html


def test_monthly_builder_renders_mtd_table_and_monthly_mtd_charts(monkeypatch):
    def fake_fetch(date_from, date_to, *_args, **_kwargs):
        is_current = date_from.year == 2026
        return [_energy_row("남양주1", 1200.0 if is_current else 1000.0, 110.0 if is_current else 100.0)]

    monkeypatch.setattr(period_builder, "_fetch_rows_range", fake_fetch)
    monkeypatch.setattr(period_builder, "_build_monthly_mtd_charts", lambda *_args: ([], []))
    report = period_builder.build_monthly_report(year=2026, month=6)

    assert "월간 에너지 원단위 alert" in report.subject
    assert "1. 당월 실적 비교" in report.html
    assert "2. 월별(MTD) 원단위 추이" in report.html
    assert "2. 연 누계(YTD) 원단위 추이" not in report.html
    assert "최근 4주 전사 추이" not in report.html
