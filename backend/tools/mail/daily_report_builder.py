"""
Daily Energy Alert Report Builder
=================================
기준일(평일 근무일 기준 D-N, 기본 N=1)의 당일 운영 이상을 빠르게 확인하는 일일 메일.
기준일 자체도 오늘로부터 주말·공휴일을 건너뛴 N번째 이전 근무일이다.
예: N=1일 때 오늘이 월요일이면 기준일은 지난주 금요일, 오늘이 화요일이면
기준일은 월요일이다(토·일요일은 세지 않음).

  1) 공장×지표 단일 스냅샷 표 — 가로축(컬럼)=사업장(전사·남양주1·남양주2·김해·
     광주·논산·경산), 세로축(행)=생산량+원단위 6종(전력·냉동전력·공압기전력·연료·
     용수·폐수/용수). 각 셀은 기준일 실적과 전주 동일 요일 대비 증감률.
  2) 즉시 점검 대상
     - 최근 7일 중앙값의 1%를 유효 변화 기준으로 사용
     - 생산량 감소 + 에너지 사용량 증가 조합만 경고 테이블로 요약.
       폐수/용수는 사용량이 아닌 비율이므로 비교 대상에서 제외.

주간/월간 메일은 period_report_builder.py가 본 모듈의 공용 집계 함수 +
차트 렌더러(_render_metric_grid_chart)를 재사용한다(일간 자체는 표만 사용, 차트 없음).
생산량은 DB_생산실적을 기준으로 하되 광주는 판매용 재공품 7개 품목의
믹스 환산 생산량을 합산한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from statistics import median
from typing import Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tools.mail.config import (
    TEMPLATE_DIR,
    DailyReportConfig,
    get_daily_report_config,
)
from tools.mail.logger import get_logger
from tools.mail.mail_service import InlineImage
from app.database.db_connection import get_connection
from app.services.production_actual_service import overlay_actual_production_rows
from app.services.v5_common import load_holidays_excel

log = get_logger("daily_report")


# 공장 코드 → 표시명 (raw factory 값을 그대로 보여줄 때 가독성 보조)
FACTORY_LABELS = {
    "F10A": "남양주1",
    "F10B": "남양주2",
    "F20":  "김해공장",
    "F30":  "광주공장",
    "F40":  "논산공장",
    "F50":  "경산",
}

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 표 표시 순서. None = 전체 합산, 리스트 = 해당 raw factory 코드만 필터링.
# 남양주1/남양주2는 별도 사업장으로 세분화 표시(과거의 남양주 통합 행은 더 이상 쓰지 않음).
FACTORY_DISPLAY_ORDER: List[Tuple[str, Optional[List[str]]]] = [
    ("전사",   None),
    ("남양주1", ["남양주1"]),
    ("남양주2", ["남양주2"]),
    ("김해",   ["김해"]),
    ("광주",   ["광주"]),
    ("논산",   ["논산"]),
    ("경산",   ["경산"]),
]

# 원단위 메트릭 정의.
# color          : 표 헤더/셀 배경 톤 산출용 상징색(파스텔 배경과 궁합이 맞는 원색).
# chart_color    : 차트 라인 색 — 흰 배경 위 얇은 선으로도 잘 보이도록 color보다 진하게
#                  조정한 값(예: 전력 노랑 F6C90E → 앰버 D97706). 표·차트 어디서 와도
#                  "같은 지표 = 같은 계열색"이 되도록 이 두 색상만 단일 소스로 관리한다.
#   header_bg    : 헤더(colspan=2) 셀 배경 — 상징색 ~20% + 흰색 80%
#   cell_bg      : 본문 값/델타 셀 배경 — 상징색 ~6% + 흰색 94% (텍스트 가독성 유지)
#   invert       : True = 증가가 개선(생산량). False = 증가가 악화(원단위/사용량 계열).
#   decimals     : 표 셀의 표시 소수자릿수.
#   chart_decimals : 차트 데이터 라벨 소수자릿수.
#   table_decimals : 주간·월간 추이 차트 아래 값 표의 소수자릿수.
#   axis_decimals  : 차트 y축 눈금 소수자릿수. 생략 시 chart_decimals를 사용.
# 이전 버전의 icon(⚡🔥💧🚿🍦)은 사내 그룹웨어 "전달" 시 Namo 에디터의 sanitizer가
# Supplementary Plane 이모지를 통째로 잘라내 빈 칸으로 보이는 이슈가 있어 제거.
# 시각 구분은 header_bg + border-top color 만으로 유지한다.
# SUM(사용량)/SUM(생산톤) 구조의 원단위 3종.
INTENSITY_METRICS = [
    {"key": "power",      "label": "전력 원단위", "unit": "kWh/ton",
     "color": "#F6C90E", "chart_color": "#D97706", "header_bg": "#FDF4CF", "cell_bg": "#FEFAEC",
     "usage_col": "total_power_kwh", "unit_col": "power_per_ton_kwh",
     "decimals": 2, "chart_decimals": 0, "table_decimals": 1, "invert": False},
    {"key": "freezing_power", "label": "냉동전력 원단위", "unit": "kWh/ton",
     "color": "#F6C90E", "chart_color": "#D97706", "header_bg": "#FDF4CF", "cell_bg": "#FEFAEC",
     "usage_col": "freezing_power_kwh", "unit_col": "freezing_power_per_ton_kwh",
     "decimals": 2, "chart_decimals": 0, "table_decimals": 1, "invert": False},
    {"key": "air_compressor", "label": "공압기전력 원단위", "unit": "kWh/ton",
     "color": "#F6C90E", "chart_color": "#D97706", "header_bg": "#FDF4CF", "cell_bg": "#FEFAEC",
     "usage_col": "air_compressor_kwh", "unit_col": "air_compressor_per_ton_kwh",
     "decimals": 2, "chart_decimals": 0, "table_decimals": 1, "invert": False},
    {"key": "fuel",       "label": "연료 원단위", "unit": "Nm³/ton",
     "color": "#E8450A", "chart_color": "#E8450A", "header_bg": "#FADACE", "cell_bg": "#FDF0EB",
     "usage_col": "fuel_nm3",        "unit_col": "fuel_per_ton_nm3",
     "decimals": 2, "chart_decimals": 1, "table_decimals": 1, "invert": False},
    {"key": "water",      "label": "용수 원단위", "unit": "ton/ton",
     "color": "#0EA5E9", "chart_color": "#0EA5E9", "header_bg": "#CFEDFB", "cell_bg": "#ECF7FD",
     "usage_col": "water_ton",       "unit_col": "water_per_ton_ton",
     "decimals": 2, "chart_decimals": 2, "axis_decimals": 2, "invert": False},
]

# 폐수/용수 = 폐수량 / 용수량 (소수점 비). '원단위'(사용량/생산톤)가 아니며
# 사업장별 표에만 노출한다.
# unit_col 은 _aggregate_weighted 가 별도로 채우는 파생 키 'wastewater_ratio'.
WASTEWATER_RATIO_METRIC = {
    "key": "wastewater_ratio", "label": "폐수/용수", "unit": "폐수량/용수량",
    "color": "#6B7280", "chart_color": "#6B7280", "header_bg": "#E1E3E6", "cell_bg": "#F3F4F5",
    "usage_col": None, "unit_col": "wastewater_ratio",
    "decimals": 2, "chart_decimals": 2, "invert": False,
}

# 생산량 — 원단위와 부호 해석이 반대이며(증가=개선), 사업장별 표의 5번째 컬럼으로
# 노출한다. 원단위는 고정부하 영향으로 생산량 변동분에 종속적이라
# 같은 표에서 함께 봐야 해석 가능 (생산↓ → 고정부하 분담 ↑ → 원단위 악화 등).
PRODUCTION_METRIC = {
    "key": "production", "label": "생산량", "unit": "ton",
    # 메로나 연녹색 — 대시보드 PROD_COLOR와 동기 (#A4D65E). chart_color는 라인 가독성을
    # 위해 더 진한 녹색(#65A30D)을 쓴다.
    # header_bg / cell_bg = 상징색을 흰색에 ~20% / ~6% 혼합 (다른 메트릭과 동일 톤 규칙).
    "color": "#A4D65E", "chart_color": "#65A30D", "axis_tick_color": "#4D7C0F",
    "header_bg": "#EDF6DE", "cell_bg": "#FAFCF5",
    "usage_col": "mix_prod_kg", "unit_col": "production_ton",
    "decimals": 0, "chart_decimals": 0, "invert": True,
}

# 일일·주간·월간 메일 공통 — 사업장별 원단위 표 & 추이 차트의 지표 컬럼 정의.
FACTORY_TABLE_METRICS = [PRODUCTION_METRIC] + INTENSITY_METRICS + [WASTEWATER_RATIO_METRIC]
# 원단위 4종(생산량 제외) — 추이 차트 전용. 일간/주간/월간 차트 모두 생산량을 별도
# 서브플롯으로 그리지 않고, 각 원단위 차트에 배경 막대로 함께 표시해(_render_metric_grid_chart
# 의 overlay_series) 생산↕·원단위↕ 조합을 한 차트에서 바로 비교할 수 있게 한다.
INTENSITY_CHART_METRICS = [m for m in FACTORY_TABLE_METRICS if m["key"] != "production"]
# 섹션2(즉시 점검) 사용량 비교 대상. INTENSITY_METRICS의 usage_col을 재사용하며,
# 폐수/용수는 원시 사용량이 아닌 비율이므로 제외한다.
DAILY_SIGNAL_METRICS = [m for m in INTENSITY_CHART_METRICS if m["key"] != "wastewater_ratio"]
DAILY_FACTORY_DISPLAY_ORDER = FACTORY_DISPLAY_ORDER[1:]

# 비교(전년 등) 라인 색 — 흰 배경에서 잘 보이도록 진한 슬레이트 사용.
PREV_YEAR_LINE_COLOR = "#64748b"
# 주간·월간 공통 차트 배경 격자선 — 기존 색보다 한 단계 진하게 조정.
CHART_GRID_COLOR = "#e2e8f0"
CHART_VERTICAL_SPACING = 0.18
# 값 표 1개 묶음(헤더 22 + 값 2행 각 20)의 세로 픽셀 — 차트 아래 표 도메인 높이 산정.
# 두 행(생산량/원단위)이 잘리지 않게 실제 셀 합보다 넉넉히 잡는다.
VALUE_TABLE_ROW_PX = 92
SINGLE_VALUE_TABLE_ROW_PX = 60
OVERLAY_SUMMARY_TABLE_ROW_PX = 54


# ─────────────────────────────────────────────────────────────────────────────
# 차트 렌더링 (공용) — 일간/주간/월간 메일이 모두 이 함수로 지표 그리드 차트를 그린다.
# ─────────────────────────────────────────────────────────────────────────────
def _render_metric_grid_chart(
    metrics: List[dict],
    x_labels: List[str],
    series_by_col: Dict[str, List[Optional[float]]],
    prev_series_by_col: Optional[Dict[str, List[Optional[float]]]] = None,
    overlay_series: Optional[List[Optional[float]]] = None,
    overlay_metric: Optional[dict] = None,
    cur_legend: str = "당해",
    prev_legend: str = "전년",
    cols: int = 2,
    cell_width: int = 300,
    cell_height: int = 210,
    include_value_table: bool = False,
    include_overlay_in_value_table: bool = True,
    include_value_table_label_column: bool = True,
) -> Optional[bytes]:
    """지표별 실적 값-라벨 꺾은선 그리드 PNG.

    - 데이터 라벨: 당해·전년 선 모두 각 점 위/아래에 실제 값을 표시.
    - overlay_series/overlay_metric: 주어지면 모든 서브플롯 배경에 보조축(secondary y)
      막대로 함께 그린다(예: 생산량). 원단위 선과 생산량 막대를 한 차트에서 봐야
      "생산↓·원단위↑" 같은 이상일 조합을 바로 짚어낼 수 있다는 요청 반영. 막대는
      보조축(오른쪽) 꺾은선으로 함께 그린다 — 원단위 선과 나란히 봐야 "생산↓·원단위↑"
      같은 이상일 조합을 바로 짚어낼 수 있다는 요청 반영.
    - autoscale: 원단위(기본축)·생산량(보조축) 모두 y축을 0부터 강제 고정
      (rangemode=tozero)하지 않고, 실제 값 범위에 여백만 살짝 둬 변화 폭이 잘 보이게
      한다 — Streamlit 웹 차트의 기본 autoscale과 동일한 방식.
    - 색상: metric["chart_color"]를 그대로 사용 — 표/범례에서 쓰는 색과 동일 계열이라
      메일 전체에서 "같은 지표 = 같은 색"이 유지된다.
    - include_value_table=True: 점 위 데이터 라벨 대신 각 차트 아래에 값 표를
      표시한다. 생산량 오버레이가 있으면 생산량/원단위, 전년 계열이 있으면
      전년/당해 순서이며 행의 글자색은 범례·선 색상과 동일하다.
      include_overlay_in_value_table=False이면 생산량 선은 유지하되 표에서는
      생산량 행을 빼고 현재 원단위 한 행만 표시하며, 생산량 값은 그리드
      좌상단 표에 한 번만 표시한다.
      include_value_table_label_column=False이면 차트 제목으로 지표를 식별할 수 있는
      주간 표에서 반복되는 계열명 열(빈 헤더/원단위 셀)을 제거한다.
    plotly 미가용/렌더 실패 시 None → 호출부가 차트를 생략하고 안내 문구로 대체한다.
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception as exc:
        log.warning(f"plotly 미설치 → 추이 차트 생략: {exc}")
        return None

    n = len(metrics)
    if n == 0:
        return None
    cols = max(1, min(cols, n))
    chart_rows = (n + cols - 1) // cols
    has_overlay = bool(overlay_series and overlay_metric and any(v is not None for v in overlay_series))
    show_overlay_summary_table = bool(
        include_value_table
        and not include_overlay_in_value_table
        and overlay_metric is not None
        and overlay_series is not None
    )
    table_has_comparison_row = (
        (include_overlay_in_value_table and overlay_metric is not None and overlay_series is not None)
        or prev_series_by_col is not None
    )
    value_table_row_px = (
        VALUE_TABLE_ROW_PX if table_has_comparison_row else SINGLE_VALUE_TABLE_ROW_PX
    )
    if include_value_table:
        plot_rows = chart_rows * 2 + (1 if show_overlay_summary_table else 0)
        specs = []
        titles = []
        row_heights = []
        if show_overlay_summary_table:
            specs.append([{"type": "table"}] + [None] * (cols - 1))
            # subplot_titles는 specs의 None 셀을 건너뛰므로 실제 table 셀 1개분만 추가한다.
            # 열 수만큼 빈 제목을 넣으면 이후 차트 제목이 오른쪽/아래로 한 칸씩 밀린다.
            titles.append("")
            row_heights.append(OVERLAY_SUMMARY_TABLE_ROW_PX)
        for chart_row in range(chart_rows):
            chart_specs = []
            table_specs = []
            chart_titles = []
            for col_idx in range(cols):
                metric_idx = chart_row * cols + col_idx
                if metric_idx < n:
                    chart_specs.append({"secondary_y": True} if has_overlay else {"type": "xy"})
                    table_specs.append({"type": "table"})
                    metric = metrics[metric_idx]
                    chart_titles.append(f"{metric['label']} ({metric['unit']})")
                else:
                    chart_specs.append(None)
                    table_specs.append(None)
            specs.extend([chart_specs, table_specs])
            # 제목 목록도 실제 생성된 subplot 수와 동일하게 맞춘다.
            titles.extend(chart_titles + [""] * len(chart_titles))
            row_heights.extend([cell_height, value_table_row_px])
        fig = make_subplots(
            rows=plot_rows, cols=cols, subplot_titles=titles, specs=specs,
            row_heights=row_heights,
            vertical_spacing=(
                0.025 if show_overlay_summary_table and chart_rows > 1
                else 0.035 if chart_rows > 1
                else 0.07
            ),
            horizontal_spacing=0.08,
        )
    else:
        plot_rows = chart_rows
        titles = [f"{m['label']} ({m['unit']})" for m in metrics] + [""] * (chart_rows * cols - n)
        specs = [[{"secondary_y": True}] * cols for _ in range(chart_rows)] if has_overlay else None
        fig = make_subplots(
            rows=plot_rows, cols=cols, subplot_titles=titles, specs=specs,
            vertical_spacing=CHART_VERTICAL_SPACING if chart_rows > 1 else 0.14,
            horizontal_spacing=0.08,
        )

    overlay_color = (overlay_metric or {}).get("chart_color", "#94a3b8")
    overlay_axis_tick_color = (overlay_metric or {}).get("axis_tick_color", overlay_color)
    overlay_label = (overlay_metric or {}).get("label", "생산량")
    overlay_decimals = (overlay_metric or {}).get("chart_decimals", (overlay_metric or {}).get("decimals", 0))
    overlay_vals = [v for v in (overlay_series or []) if v is not None]

    has_any = False
    value_tables = []
    for idx, m in enumerate(metrics):
        chart_row, c = idx // cols + 1, idx % cols + 1
        if include_value_table:
            r = chart_row * 2 - 1 + (1 if show_overlay_summary_table else 0)
        else:
            r = chart_row
        col_key = m["unit_col"]
        decimals = m.get("chart_decimals", m.get("decimals", 1))
        table_decimals = m.get("table_decimals", decimals)
        axis_decimals = m.get("axis_decimals", decimals)
        color = m.get("chart_color", m.get("color", "#2563eb"))
        cur = list(series_by_col.get(col_key) or [])
        prev = list((prev_series_by_col or {}).get(col_key) or [])

        # 생산량 보조축 꺾은선 — 원단위 선보다 먼저 그려 뒤에 깔리게 한다.
        # 2열 차트 중앙에서는 왼쪽 차트의 보조축 눈금과 오른쪽 차트의 기본축
        # 눈금이 겹치므로, 각 행의 마지막 차트에만 중복 생산량 눈금을 표시한다.
        if has_overlay:
            overlay_trace = dict(
                x=x_labels, y=overlay_series,
                mode="lines+markers" if include_value_table else "lines+markers+text",
                line=dict(color=overlay_color, width=2, dash="dash"),
                marker=dict(size=5, color=overlay_color, symbol="diamond"),
                name=overlay_label, legendgroup="overlay", showlegend=(idx == 0),
                connectgaps=False,
            )
            if not include_value_table:
                overlay_trace.update(
                    text=[
                        f"{v:,.{overlay_decimals}f}" if v is not None else ""
                        for v in (overlay_series or [])
                    ],
                    textposition="bottom center",
                    textfont=dict(color=overlay_axis_tick_color, size=8),
                )
            fig.add_trace(go.Scatter(**overlay_trace), row=r, col=c, secondary_y=True)
            if overlay_vals:
                ov_min, ov_max = min(overlay_vals), max(overlay_vals)
                ov_span = ov_max - ov_min
                ov_pad = ov_span * 0.25 if ov_span > 0 else max(abs(ov_max) * 0.15, 1)
                fig.update_yaxes(
                    secondary_y=True, range=[max(0, ov_min - ov_pad), ov_max + ov_pad],
                    showticklabels=(c == cols or idx == n - 1),
                    showgrid=False, zeroline=False,
                    tickformat=f",.{overlay_decimals}f",
                    tickfont=dict(size=8, color=overlay_axis_tick_color),
                    row=r, col=c,
                )

        if prev and any(v is not None for v in prev):
            prev_trace = dict(
                x=x_labels, y=prev,
                mode="lines+markers" if include_value_table else "lines+markers+text",
                line=dict(color=PREV_YEAR_LINE_COLOR, width=2, dash="dot"),
                marker=dict(size=5, color=PREV_YEAR_LINE_COLOR),
                name=prev_legend, legendgroup="prev", showlegend=(idx == 0),
                connectgaps=False, cliponaxis=False,
            )
            if not include_value_table:
                prev_trace.update(
                    text=[f"{v:,.{decimals}f}" if v is not None else "" for v in prev],
                    textposition="bottom center",
                    textfont=dict(color=PREV_YEAR_LINE_COLOR, size=9),
                )
            fig.add_trace(go.Scatter(**prev_trace), row=r, col=c, secondary_y=False)
        if cur and any(v is not None for v in cur):
            has_any = True
            cur_trace = dict(
                x=x_labels, y=cur,
                mode="lines+markers" if include_value_table else "lines+markers+text",
                line=dict(color=color, width=2.4),
                marker=dict(size=6, color=color),
                name=cur_legend, legendgroup="cur", showlegend=(idx == 0),
                connectgaps=False, cliponaxis=False,
            )
            if not include_value_table:
                cur_trace.update(
                    text=[f"{v:,.{decimals}f}" if v is not None else "" for v in cur],
                    textposition="top center",
                    textfont=dict(color=color, size=9),
                )
            fig.add_trace(go.Scatter(**cur_trace), row=r, col=c, secondary_y=False)

        if include_value_table:
            if (
                include_overlay_in_value_table
                and overlay_metric is not None
                and overlay_series is not None
            ):
                first_values = list(overlay_series)
                first_decimals = overlay_metric.get("table_decimals", overlay_decimals)
                first_color = overlay_axis_tick_color
                first_label = overlay_label
            elif prev_series_by_col is not None:
                first_values = prev
                first_decimals = table_decimals
                first_color = PREV_YEAR_LINE_COLOR
                first_label = prev_legend

            def _table_value(values, pos, places):
                value = values[pos] if pos < len(values) else None
                return "-" if value is None else f"{value:,.{places}f}"

            # 표가 차트의 x축(기간 라벨)을 대신한다 — 헤더=기간(W25…), 좌측 라벨 컬럼=
            # 계열명(생산량/원단위), 각 행=계열 값. 아래 레이아웃에서 차트 x축 눈금은 숨겨
            # 회전 라벨이 표와 겹치던 문제를 없앤다. plotly 기본 다크 헤더 대신 밝은 헤더 지정.
            if table_has_comparison_row:
                row_labels = [first_label, cur_legend]
                data_columns = [
                    [
                        _table_value(first_values, pos, first_decimals),
                        _table_value(cur, pos, table_decimals),
                    ]
                    for pos in range(len(x_labels))
                ]
                row_font_colors = [first_color, color]
            else:
                row_labels = [cur_legend]
                data_columns = [
                    [_table_value(cur, pos, table_decimals)]
                    for pos in range(len(x_labels))
                ]
                row_font_colors = [color]
            # 헤더는 기간 축약(W25 등)만 — 셀 폭을 넘는 날짜 범위는 표시하지 않는다
            # (정확한 기간은 메일 본문 안내 문구가 담당). "W25<br>(06/15~06/21)" → "W25".
            period_headers = [str(lbl).split("<br>")[0] for lbl in x_labels]
            if include_value_table_label_column:
                all_columns = [row_labels] + data_columns
                header_values = [""] + period_headers
                cell_font_colors = [row_font_colors] + [row_font_colors for _ in x_labels]
                cell_fill_colors = ["#eef2f7"] + ["#ffffff"] * len(x_labels)
                # 좌측 라벨 컬럼은 "2025년"·"생산량" 등 최대 5자가 안 잘리도록 데이터 칸과
                # 동등 폭으로 둔다(월간 6열에서도 왼쪽 잘림 방지).
                col_widths = [1.0] + [1.0] * len(x_labels)
            else:
                all_columns = data_columns
                header_values = period_headers
                cell_font_colors = [row_font_colors for _ in x_labels]
                cell_fill_colors = ["#ffffff"] * len(x_labels)
                col_widths = [1.0] * len(x_labels)
            value_tables.append((
                go.Table(
                    columnwidth=col_widths,
                    header=dict(
                        values=header_values,
                        align="center",
                        height=22,
                        fill_color="#f1f5f9",
                        line=dict(color="#cbd5e1", width=0.7),
                        font=dict(size=8.5, color="#475569"),
                    ),
                    cells=dict(
                        values=all_columns,
                        align="center",
                        height=20,
                        line=dict(color="#e2e8f0", width=0.7),
                        fill_color=cell_fill_colors,
                        font=dict(size=8.5, color=cell_font_colors),
                    ),
                ),
                r + 1,
                c,
            ))

        fig.update_yaxes(tickformat=f",.{axis_decimals}f", row=r, col=c, secondary_y=False)
        y_vals = [v for v in (cur + prev) if v is not None]
        if y_vals:
            y_min, y_max = min(y_vals), max(y_vals)
            span = y_max - y_min
            pad = span * 0.25 if span > 0 else max(abs(y_max) * 0.15, 1)
            fig.update_yaxes(range=[y_min - pad, y_max + pad], row=r, col=c, secondary_y=False)

    if not has_any:
        return None

    if show_overlay_summary_table:
        summary_decimals = overlay_metric.get("table_decimals", overlay_decimals)
        summary_values = [
            "-" if value is None else f"{value:,.{summary_decimals}f}"
            for value in overlay_series
        ]
        fig.add_trace(
            go.Table(
                columnwidth=[1.0] * (len(x_labels) + 1),
                header=dict(
                    values=[""] + [str(label).split("<br>")[0] for label in x_labels],
                    align="center",
                    height=22,
                    fill_color="#f1f5f9",
                    line=dict(color="#cbd5e1", width=0.7),
                    font=dict(size=8.5, color="#475569"),
                ),
                cells=dict(
                    values=[[overlay_label]] + [[value] for value in summary_values],
                    align="center",
                    height=20,
                    fill_color=["#eef2f7"] + ["#ffffff"] * len(x_labels),
                    line=dict(color="#e2e8f0", width=0.7),
                    font=dict(
                        size=8.5,
                        color=[[overlay_axis_tick_color]] * (len(x_labels) + 1),
                    ),
                ),
            ),
            row=1,
            col=1,
        )

    for table, table_row, table_col in value_tables:
        fig.add_trace(table, row=table_row, col=table_col)

    table_height = value_table_row_px * chart_rows if include_value_table else 0
    summary_table_height = OVERLAY_SUMMARY_TABLE_ROW_PX if show_overlay_summary_table else 0
    width_px = cell_width * cols
    height_px = cell_height * chart_rows + table_height + summary_table_height + 60
    legend_layout = (
        dict(
            orientation="h", yanchor="top", y=0.995,
            xanchor="right", x=1, font=dict(size=10),
        )
        if show_overlay_summary_table
        else dict(
            orientation="h", yanchor="bottom", y=1.03,
            xanchor="right", x=1, font=dict(size=10),
        )
    )
    fig.update_layout(
        height=height_px, width=width_px,
        margin=dict(l=42, r=14, t=20 if show_overlay_summary_table else 50, b=26),
        paper_bgcolor="white", plot_bgcolor="white",
        font=dict(color="#1f2937", size=11),
        legend=legend_layout,
    )
    # 값 표가 있으면 기간 라벨은 표 헤더가 담당하므로 차트 x축 눈금 라벨은 숨긴다
    # (회전된 2줄 라벨이 표와 겹치던 문제 제거).
    fig.update_xaxes(
        showgrid=True, gridcolor=CHART_GRID_COLOR,
        tickfont=dict(size=9), automargin=True,
        showticklabels=not include_value_table,
    )
    fig.update_yaxes(
        showgrid=True, gridcolor=CHART_GRID_COLOR,
        tickfont=dict(size=9), secondary_y=False,
    )
    for ann in fig.layout.annotations:  # 서브플롯 제목 크기 축소
        ann.font.size = 10.5

    try:
        return fig.to_image(format="png", width=width_px, height=height_px, scale=2)
    except Exception as exc:
        log.warning(f"추이 차트 PNG 변환 실패: {exc}")
        return None


@dataclass
class BuiltReport:
    subject: str
    html: str
    inline_images: List[InlineImage] = field(default_factory=list)
    ref_date: date = field(default_factory=date.today)
    record_count: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# DB 조회 (직접 SQL — query_service 의 streamlit 캐시 의존 회피)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_rows_range(
    date_from: date,
    date_to: date,
    factories: Optional[List[str]] = None,
) -> List[dict]:
    """기간 내 에너지 행을 조회하고 생산량은 운영 기준 실적으로 교체."""
    sql = """
        SELECT factory, date,
               total_power_kwh, freezing_power_kwh, air_compressor_kwh,
               fuel_nm3, water_ton, wastewater_ton, mix_prod_kg
        FROM energy_daily
        WHERE date BETWEEN %s AND %s
    """
    params: list = [date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d")]
    if factories:
        placeholders = ", ".join(["%s"] * len(factories))
        sql += f" AND factory IN ({placeholders})"
        params.extend(factories)
    sql += " ORDER BY date ASC, factory ASC"

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, tuple(params))
        rows = list(cursor.fetchall())
    finally:
        cursor.close()
        conn.close()

    return overlay_actual_production_rows(rows, date_from, date_to)


# ─────────────────────────────────────────────────────────────────────────────
# 집계 / 포맷팅
# ─────────────────────────────────────────────────────────────────────────────
def _aggregate_weighted(rows: List[dict]) -> Optional[dict]:
    """공장 행들을 합산해 전사 가중평균 원단위 산출.

    공식: SUM(usage) / (SUM(mix_prod_kg) / 1000) — 대시보드 _calc_unit_rate 와 동일.
    mix_prod_kg=0 인 날(주말/유휴)의 사용량도 분자에 포함해야 MTD/YTD가 실제 운영
    데이터와 정합하므로 행 필터링하지 않는다. 대시보드와 동일한 원단위 값을 보장.
    """
    if not rows:
        return None
    agg = {
        "n_factories":     len({r["factory"] for r in rows}),
        "total_power_kwh": sum(float(r.get("total_power_kwh") or 0) for r in rows),
        "freezing_power_kwh": sum(float(r.get("freezing_power_kwh") or 0) for r in rows),
        "air_compressor_kwh": sum(float(r.get("air_compressor_kwh") or 0) for r in rows),
        "fuel_nm3":        sum(float(r.get("fuel_nm3") or 0) for r in rows),
        "water_ton":       sum(float(r.get("water_ton") or 0) for r in rows),
        "wastewater_ton":  sum(float(r.get("wastewater_ton") or 0) for r in rows),
        "mix_prod_kg":     sum(float(r.get("mix_prod_kg") or 0) for r in rows),
    }
    prod_ton = agg["mix_prod_kg"] / 1000.0
    for m in INTENSITY_METRICS:
        agg[m["unit_col"]] = (agg[m["usage_col"]] / prod_ton) if prod_ton > 0 else None
    # 폐수/용수 = 폐수량 / 용수량 (소수점 비). 용수량 0이면 산출 불가(None).
    water = agg["water_ton"]
    agg[WASTEWATER_RATIO_METRIC["unit_col"]] = (
        agg["wastewater_ton"] / water if water > 0 else None
    )
    # 생산량 자체도 셀로 노출 — 0이면 비조업으로 의미 있는 값이므로 그대로 보존.
    agg[PRODUCTION_METRIC["unit_col"]] = prod_ton
    return agg


def _fmt(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _pct_delta(
    curr: Optional[float],
    prev: Optional[float],
    invert: bool = False,
) -> Tuple[str, str, Optional[float]]:
    """(텍스트, 색상hex, raw_pct) 반환.

    invert=False(기본) — 원단위/사용량: 감소가 개선 → 음수=청색, 양수=적색.
    invert=True       — 생산량 등 증가가 개선인 지표: 양수=청색, 음수=적색.
    """
    if curr is None or prev is None or prev == 0:
        return ("-", "#6b7280", None)
    try:
        pct = (float(curr) - float(prev)) / float(prev) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return ("-", "#6b7280", None)

    if abs(pct) < 0.05:
        return ("±0.0%", "#6b7280", 0.0)
    sign = "+" if pct > 0 else "−"
    if invert:
        color = "#2563eb" if pct > 0 else "#dc2626"   # 생산량: 증가=개선=청색
    else:
        color = "#dc2626" if pct > 0 else "#2563eb"   # 원단위: 증가=악화=적색
    return (f"{sign}{abs(pct):.1f}%", color, pct)


def _normalize_row_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).split(" ")[0], "%Y-%m-%d").date()


@lru_cache(maxsize=1)
def _load_holiday_dates() -> frozenset:
    """DB_holiday.xlsx 기반 공휴일 집합(모델 파이프라인과 동일 기준, 프로세스 내 캐시)."""
    return frozenset(load_holidays_excel())


def _is_business_day(d: date, holiday_dates: frozenset) -> bool:
    return d.weekday() < 5 and d not in holiday_dates


def _previous_business_day(d: date, holiday_dates: frozenset) -> date:
    """d 하루 전부터 거슬러 올라가며 첫 평일 근무일(주말·공휴일 제외)을 반환."""
    prev = d - timedelta(days=1)
    while not _is_business_day(prev, holiday_dates):
        prev -= timedelta(days=1)
    return prev


def _filter_rows_by_factory(
    rows: List[dict], factory_codes: Optional[List[str]]
) -> List[dict]:
    """factory_codes=None이면 전체, 리스트이면 해당 원시 공장만 반환."""
    if factory_codes is None:
        return rows
    code_set = set(factory_codes)
    return [r for r in rows if r["factory"] in code_set]


def _aggregate_on_date(
    rows: List[dict], target_date: date, factory_codes: Optional[List[str]]
) -> Optional[dict]:
    selected = [
        r for r in _filter_rows_by_factory(rows, factory_codes)
        if _normalize_row_date(r.get("date")) == target_date
    ]
    return _aggregate_weighted(selected)


def _metric_value(agg: Optional[dict], column: str) -> Optional[float]:
    if not agg:
        return None
    value = agg.get(column)
    return float(value) if value is not None else None


def _median_nonzero(values: List[Optional[float]]) -> Optional[float]:
    nums = [float(v) for v in values if v is not None and abs(float(v)) > 1e-9]
    return float(median(nums)) if nums else None


def _daily_trend_direction(
    curr: Optional[float], prev: Optional[float], base: Optional[float]
) -> int:
    """홈 대시보드와 동일한 방향 판정: 7일 중앙값의 1% 이하는 작은 흔들림."""
    if curr is None or prev is None or base is None:
        return 0
    diff = float(curr) - float(prev)
    threshold = max(abs(float(base)) * 0.01, 1e-9)
    if diff > threshold:
        return 1
    if diff < -threshold:
        return -1
    return 0


def _daily_direction_signal(
    prod_curr: Optional[float],
    prod_prev: Optional[float],
    usage_curr: Optional[float],
    usage_prev: Optional[float],
    prod_base: Optional[float],
    usage_base: Optional[float],
) -> dict:
    """생산량과 에너지 사용량 방향 조합을 대시보드와 동일한 라벨로 변환."""
    if any(v is None for v in (prod_curr, prod_prev, usage_curr, usage_prev)):
        return {
            "label": "데이터 없음", "badge": "-", "tone": "missing",
            "color": "#64748b", "bg": "#f1f5f9",
        }

    p_dir = _daily_trend_direction(prod_curr, prod_prev, prod_base)
    u_dir = _daily_trend_direction(usage_curr, usage_prev, usage_base)
    if p_dir > 0 and u_dir < 0:
        label, badge, tone = "생산↑ 사용↓", "개선", "good"
    elif p_dir < 0 and u_dir > 0:
        label, badge, tone = "생산↓ 사용↑", "주의", "warn"
    elif p_dir > 0 and u_dir > 0:
        label, badge, tone = "동반 증가", "동행", "neutral"
    elif p_dir < 0 and u_dir < 0:
        label, badge, tone = "동반 감소", "동행", "neutral"
    elif p_dir == 0 and u_dir == 0:
        label, badge, tone = "변화 작음", "안정", "neutral"
    elif p_dir == 0:
        label, badge, tone = "생산 유지", "참고", "neutral"
    else:
        label, badge, tone = "사용 유지", "참고", "neutral"

    style = {
        "warn": ("#b91c1c", "#fef2f2"),
        "good": ("#047857", "#ecfdf5"),
        "neutral": ("#475569", "#f8fafc"),
    }[tone]
    return {"label": label, "badge": badge, "tone": tone, "color": style[0], "bg": style[1]}


def _build_daily_factory_rows(
    rows: List[dict], ref_date: date, prev_date: date
) -> Tuple[List[dict], List[dict], int]:
    """사업장(전사 제외)의 생산량↔사용량 방향 경고를 생성 — 섹션2 전용.

    생산이 줄었는데 에너지 사용량이 오르면 "생산↓ 사용↑"(주의)로 표시한다.
    prev_date는 기준일의 전주 동일
    요일(ref_date-7일)이며 호출부(build_daily_report)에서 계산해 전달받는다.
    7일 중앙값 기준선(prod_base/usage_base)은 달력 기준 최근 7일을 그대로 쓴다.
    """
    history_dates = [ref_date - timedelta(days=i) for i in range(6, -1, -1)]
    factory_rows: List[dict] = []
    warning_items: List[dict] = []
    good_count = 0

    for factory_label, codes in DAILY_FACTORY_DISPLAY_ORDER:
        history = {d: _aggregate_on_date(rows, d, codes) for d in history_dates}
        curr = history.get(ref_date)
        prev = _aggregate_on_date(rows, prev_date, codes)
        prod_col = "production_ton"
        prod_curr = _metric_value(curr, prod_col)
        prod_prev = _metric_value(prev, prod_col)
        prod_base = _median_nonzero([_metric_value(history[d], prod_col) for d in history_dates])
        prod_delta, prod_color, prod_pct = _pct_delta(prod_curr, prod_prev, invert=True)

        signals = []
        for metric in DAILY_SIGNAL_METRICS:
            col = metric["usage_col"]
            usage_curr = _metric_value(curr, col)
            usage_prev = _metric_value(prev, col)
            usage_base = _median_nonzero([_metric_value(history[d], col) for d in history_dates])
            signal = _daily_direction_signal(
                prod_curr, prod_prev, usage_curr, usage_prev, prod_base, usage_base,
            )
            usage_delta, usage_color, usage_pct = _pct_delta(usage_curr, usage_prev)
            signal.update({
                "energy": metric["label"].replace(" 원단위", ""),
                "usage_delta": usage_delta,
                "usage_delta_color": usage_color,
                "production_delta": prod_delta,
            })
            signals.append(signal)
            if signal["tone"] == "warn":
                warning_items.append({
                    "date": ref_date.strftime("%Y-%m-%d"),
                    "factory": factory_label,
                    "energy": metric["label"].replace(" 원단위", ""),
                    "production_delta": prod_delta,
                    "usage_delta": usage_delta,
                })
            elif signal["tone"] == "good":
                good_count += 1

        factory_rows.append({
            "factory": factory_label,
            "has_current": curr is not None,
            "has_previous": prev is not None,
            "production_delta": prod_delta,
            "production_delta_color": prod_color,
            "signals": signals,
        })

    return factory_rows, warning_items, good_count


def _build_daily_snapshot_table(rows: List[dict], ref_date: date, prev_date: date) -> dict:
    """전사+6개 사업장 × 생산량+원단위 6종의 단일 스냅샷 표 데이터.

    가로축(컬럼)=사업장, 세로축(행)=지표. 각 셀은 기준일(ref_date) 실적과
    전주 동일 요일(prev_date=ref_date-7일) 대비 증감률을 담는다.
    반환: {factories: [{factory, is_total}], metric_rows: [{label, unit, color,
             header_bg, cell_bg, cells: [{value, delta, color} × 사업장 수]}]}
    """
    agg_by_factory = {
        label: (_aggregate_on_date(rows, ref_date, codes), _aggregate_on_date(rows, prev_date, codes))
        for label, codes in FACTORY_DISPLAY_ORDER
    }

    metric_rows = []
    for m in FACTORY_TABLE_METRICS:
        col = m["unit_col"]
        decimals = m["decimals"]
        cells = []
        for label, _codes in FACTORY_DISPLAY_ORDER:
            curr_agg, prev_agg = agg_by_factory[label]
            curr_v = _metric_value(curr_agg, col)
            prev_v = _metric_value(prev_agg, col)
            delta, delta_color, _ = _pct_delta(curr_v, prev_v, invert=m.get("invert", False))
            cells.append({
                "value": _fmt(curr_v, decimals),
                "delta": delta,
                "color": delta_color,
            })
        metric_rows.append({
            "label": m["label"], "unit": m["unit"], "color": m["chart_color"],
            "header_bg": m["header_bg"], "cell_bg": m["cell_bg"],
            "cells": cells,
        })

    return {
        "factories": [
            {"factory": label, "is_total": codes is None}
            for label, codes in FACTORY_DISPLAY_ORDER
        ],
        "metric_rows": metric_rows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 메인 엔트리
# ─────────────────────────────────────────────────────────────────────────────
def build_daily_report(
    ref_date: Optional[date] = None,
    config: Optional[DailyReportConfig] = None,
) -> BuiltReport:
    cfg = config or get_daily_report_config()
    holiday_dates = _load_holiday_dates()
    if ref_date is None:
        # 평일 근무일(주말·공휴일 제외) 기준 D-N. 예: 오늘이 월요일이고 N=2면
        # 직전주 목요일이 기준일(금요일→목요일 순으로 2회 역산).
        ref_date = date.today()
        for _ in range(cfg.reference_offset_days):
            ref_date = _previous_business_day(ref_date, holiday_dates)
    elif not _is_business_day(ref_date, holiday_dates):
        # 호출부(대시보드 '메일 송부' 등)가 넘긴 기준일이 주말·공휴일이면
        # 직전 근무일로 보정 — 일일 alert 기준일은 항상 근무일이어야 한다.
        ref_date = _previous_business_day(ref_date, holiday_dates)

    factories_filter = cfg.factories_filter or None
    trend_from = ref_date - timedelta(days=6)
    prev_date = ref_date - timedelta(days=7)  # 전주 동일 요일
    log.info(
        f"일일 이상 리포트 시작 - 기준일(근무일 기준 D-1): {ref_date}, "
        f"비교 기준일(전주 동일 요일): {prev_date}, 판정 이력: {trend_from}~{ref_date}"
    )

    # 섹션2(즉시 점검) 7일 중앙값 계산을 위해 trend_from 하루 전날 데이터까지 조회.
    # 전주 동일 요일의 비교값까지 포함하도록 ref_date-7일부터 조회한다.
    # 섹션1(스냅샷)은 이 중 ref_date/prev_date 두 날짜만 사용.
    fetch_from = min(trend_from - timedelta(days=1), prev_date)
    rows = _fetch_rows_range(fetch_from, ref_date, factories_filter)
    factory_rows, warning_items, good_count = _build_daily_factory_rows(rows, ref_date, prev_date)
    n_current_factories = sum(1 for row in factory_rows if row["has_current"])
    warning_factory_count = len({item["factory"] for item in warning_items})
    snapshot_table = _build_daily_snapshot_table(rows, ref_date, prev_date)

    subject = (
        f"[생산기술팀] 일일 에너지 원단위 alert {ref_date}"
        f"({WEEKDAY_KR[ref_date.weekday()]})"
    )
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    html = env.get_template("daily_energy_report.html").render(
        subject=subject,
        ref_date=ref_date.strftime("%Y-%m-%d"),
        prev_date=prev_date.strftime("%Y-%m-%d"),
        ref_weekday=WEEKDAY_KR[ref_date.weekday()],
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        trend_from=trend_from.strftime("%Y-%m-%d"),
        warning_count=len(warning_items),
        warning_factory_count=warning_factory_count,
        warning_items=warning_items,
        factory_rows=factory_rows,
        snapshot_table=snapshot_table,
    )

    log.info(
        f"일일 이상 리포트 생성 완료 - 사업장 {n_current_factories}/{len(DAILY_FACTORY_DISPLAY_ORDER)}, "
        f"스냅샷 지표 {len(snapshot_table['metric_rows'])}행, "
        f"즉시 점검 {len(warning_items)}건, 개선 신호 {good_count}건"
    )
    return BuiltReport(
        subject=subject,
        html=html,
        inline_images=[],
        ref_date=ref_date,
        record_count=n_current_factories,
    )
