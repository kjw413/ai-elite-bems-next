"""
Weekly / Monthly Energy Report Builder
======================================
일일 메일의 공용 집계 함수를 재사용하되 주기별 목적에 맞춰 별도 구조로 렌더링한다.

  · 주간 — 전주 대비와 전년 동일 ISO 주차를 비교하고 최근 4주 추이를 제공한다.
             templates/period_energy_report.html 사용.
  · 월간 — 직전 완결 월의 MTD와 YTD를 각각 전년 동일 기간과 비교한다.
             templates/monthly_energy_report.html 사용.

신설 공장(예: 경산 2026-04~)은 전년 데이터가 없는 기간에 전년비가 '-'로 표시된다.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tools.mail.config import TEMPLATE_DIR
from tools.mail.logger import get_logger
from tools.mail.daily_report_builder import (
    BuiltReport,
    FACTORY_DISPLAY_ORDER,
    FACTORY_TABLE_METRICS,
    _aggregate_weighted,
    _fetch_rows_range,
    _filter_rows_by_factory,
    _fmt,
    _pct_delta,
)

log = get_logger("period_report")


# ─────────────────────────────────────────────────────────────────────────────
# 기간 계산
# ─────────────────────────────────────────────────────────────────────────────
def last_complete_week(today: Optional[date] = None) -> Tuple[date, date]:
    """실행일 기준 가장 최근의 '완결된' 월~일 주를 반환."""
    today = today or date.today()
    monday_this_week = today - timedelta(days=today.weekday())
    return monday_this_week - timedelta(days=7), monday_this_week - timedelta(days=1)


def week_of(ref: date) -> Tuple[date, date]:
    """임의 일자가 속한 월~일 주."""
    monday = ref - timedelta(days=ref.weekday())
    return monday, monday + timedelta(days=6)


def same_iso_week_prev_year(week_from: date) -> Tuple[date, date]:
    """전년 동일 ISO 주차 (월~일). 전년에 W53이 없으면 W52로 폴백."""
    iso_year, iso_week, _ = week_from.isocalendar()
    try:
        start = date.fromisocalendar(iso_year - 1, iso_week, 1)
    except ValueError:
        start = date.fromisocalendar(iso_year - 1, 52, 1)
    return start, start + timedelta(days=6)


def last_complete_month(today: Optional[date] = None) -> Tuple[int, int]:
    """실행일 기준 직전 완결 월 (year, month)."""
    today = today or date.today()
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return y, m


def _month_range(year: int, month: int) -> Tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


# ─────────────────────────────────────────────────────────────────────────────
# 표 행 빌더 — 공용 템플릿 형식
#   row  = {factory, is_total, axes_cells: [축0 cells, 축1 cells]}
#   cell = {value, delta, color, delta2, delta2_color}  (delta2 = 회색 참고값, 선택)
# ─────────────────────────────────────────────────────────────────────────────
def _axis_cells(
    value_rows: List[dict],
    cmp_curr_rows: List[dict],
    cmp_base_rows: List[dict],
    codes: Optional[List[str]],
    ref2_rows: Optional[List[dict]] = None,
    ref2_label: str = "",
) -> List[dict]:
    """한 축의 메트릭별 셀 목록.

    value_rows    : '값' 컬럼에 표시할 기간의 raw 행
    cmp_curr/base : 델타(%) 계산용 (curr vs base)
    ref2_rows     : 회색 참고 델타의 base 기간 (curr 는 cmp_curr 와 동일)
    """
    v_agg = _aggregate_weighted(_filter_rows_by_factory(value_rows, codes))
    c_agg = _aggregate_weighted(_filter_rows_by_factory(cmp_curr_rows, codes))
    b_agg = _aggregate_weighted(_filter_rows_by_factory(cmp_base_rows, codes))
    r2_agg = (
        _aggregate_weighted(_filter_rows_by_factory(ref2_rows, codes))
        if ref2_rows is not None else None
    )

    cells = []
    for m in FACTORY_TABLE_METRICS:
        col = m["unit_col"]
        invert = m.get("invert", False)
        d_txt, d_color, _ = _pct_delta(
            c_agg.get(col) if c_agg else None,
            b_agg.get(col) if b_agg else None,
            invert=invert,
        )
        cell = {
            "value": _fmt(v_agg.get(col) if v_agg else None, m.get("decimals", 2)),
            "delta": d_txt,
            "color": d_color,
            "delta2": "",
            "delta2_color": "#94a3b8",
        }
        if r2_agg is not None:
            r2_txt, _r2_color, r2_pct = _pct_delta(
                c_agg.get(col) if c_agg else None,
                r2_agg.get(col) if r2_agg else None,
                invert=invert,
            )
            # 참고값은 판정 색상 없이 회색 고정 — 계절성 미보정 비교라 착시 방지
            cell["delta2"] = f"{ref2_label} {r2_txt}" if r2_pct is not None else ""
        cells.append(cell)
    return cells


def _build_period_factory_rows(axis_specs: List[dict]) -> List[dict]:
    """사업장(전사+실공장)별 × 축별 셀 행 생성. axis_specs[i] 는 _axis_cells kwargs."""
    rows = []
    for label, codes in FACTORY_DISPLAY_ORDER:
        rows.append({
            "factory": label,
            "is_total": codes is None,
            "axes_cells": [_axis_cells(codes=codes, **spec) for spec in axis_specs],
        })
    return rows

def _build_monthly_comparison_rows(
    curr_rows: List[dict], prev_rows: List[dict]
) -> List[dict]:
    """월간 전용: 사업장별 현재·전년 실적·증감률을 한 셀에 묶어 생성."""
    rows = []
    for label, codes in FACTORY_DISPLAY_ORDER:
        curr = _aggregate_weighted(_filter_rows_by_factory(curr_rows, codes))
        prev = _aggregate_weighted(_filter_rows_by_factory(prev_rows, codes))
        cells = []
        for metric in FACTORY_TABLE_METRICS:
            col = metric["unit_col"]
            curr_v = curr.get(col) if curr else None
            prev_v = prev.get(col) if prev else None
            delta, delta_color, _ = _pct_delta(
                curr_v, prev_v, invert=metric.get("invert", False),
            )
            cells.append({
                "current": _fmt(curr_v, metric.get("decimals", 2)),
                "previous": _fmt(prev_v, metric.get("decimals", 2)),
                "delta": delta,
                "delta_color": delta_color,
            })
        rows.append({
            "factory": label,
            "is_total": codes is None,
            "cells": cells,
        })
    return rows

def _trend_row(
    label: str,
    curr_rows: List[dict],
    base_rows: Optional[List[dict]],
) -> dict:
    """추이 표(전사) 한 행 — 메트릭별 값 + (base 대비) 델타."""
    c_agg = _aggregate_weighted(curr_rows)
    b_agg = _aggregate_weighted(base_rows) if base_rows is not None else None
    cells = []
    for m in FACTORY_TABLE_METRICS:
        col = m["unit_col"]
        d_txt, d_color, _ = _pct_delta(
            c_agg.get(col) if c_agg else None,
            b_agg.get(col) if b_agg else None,
            invert=m.get("invert", False),
        )
        cells.append({
            "value": _fmt(c_agg.get(col) if c_agg else None, m.get("decimals", 2)),
            "delta": d_txt,
            "color": d_color,
        })
    return {"label": label, "cells": cells}


# ─────────────────────────────────────────────────────────────────────────────
# 렌더 공통
# ─────────────────────────────────────────────────────────────────────────────
def _render(context: dict, template_name: str = "period_energy_report.html") -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env.get_template(template_name).render(**context)


# ─────────────────────────────────────────────────────────────────────────────
# 주간 리포트
# ─────────────────────────────────────────────────────────────────────────────
def build_weekly_report(ref_date: Optional[date] = None) -> BuiltReport:
    """주간 메일. ref_date 는 대상 주에 속한 임의 일자 (기본: 직전 완결 주)."""
    if ref_date is None:
        wk_from, wk_to = last_complete_week()
    else:
        wk_from, wk_to = week_of(ref_date)

    prev_from, prev_to = wk_from - timedelta(days=7), wk_from - timedelta(days=1)
    yoy_from, yoy_to = same_iso_week_prev_year(wk_from)
    iso_year, iso_week, _ = wk_from.isocalendar()

    log.info(f"주간 리포트 시작 — 대상 {wk_from}~{wk_to} (W{iso_week}), "
             f"전주 {prev_from}~{prev_to}, 전년동주 {yoy_from}~{yoy_to}")

    rows_wk = _fetch_rows_range(wk_from, wk_to)
    rows_prev = _fetch_rows_range(prev_from, prev_to)
    rows_yoy = _fetch_rows_range(yoy_from, yoy_to)

    factory_rows = _build_period_factory_rows([
        # 축0: 당주 실적 + 전주 대비
        dict(value_rows=rows_wk, cmp_curr_rows=rows_wk, cmp_base_rows=rows_prev),
        # 축1: 전년 동주 실적 + (당주 vs 전년동주) 전년비
        dict(value_rows=rows_yoy, cmp_curr_rows=rows_wk, cmp_base_rows=rows_yoy),
    ])

    # 최근 4주 전사 추이 (당주 포함, 최신이 위) — 각 주는 직전 주 대비 델타
    trend_rows = []
    week_windows = [
        (wk_from - timedelta(days=7 * i), wk_to - timedelta(days=7 * i))
        for i in range(4)
    ]
    fetched = {w: _fetch_rows_range(w[0], w[1]) for w in week_windows}
    prev_of = {
        w: _fetch_rows_range(w[0] - timedelta(days=7), w[1] - timedelta(days=7))
        for w in week_windows
    }
    for (w_from, w_to) in week_windows:
        w_iso = w_from.isocalendar()
        trend_rows.append(_trend_row(
            f"W{w_iso[1]:02d} ({w_from.strftime('%m/%d')}~{w_to.strftime('%m/%d')})",
            fetched[(w_from, w_to)],
            prev_of[(w_from, w_to)],
        ))

    n_factories = (_aggregate_weighted(rows_wk) or {}).get("n_factories", 0)
    subject = (
        f"[생산기술팀] 주간 에너지 원단위 alert {iso_year}-W{iso_week:02d} "
        f"({wk_from.strftime('%m/%d')}~{wk_to.strftime('%m/%d')})"
    )

    html = _render(dict(
        subject=subject,
        report_title="주간 에너지 원단위 Alert",
        period_label=f"대상 주 <b>{iso_year}-W{iso_week:02d}</b> "
                     f"({wk_from.strftime('%Y-%m-%d')} ~ {wk_to.strftime('%Y-%m-%d')})",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        table_note=(
            f"당주: {wk_from.strftime('%m/%d')}~{wk_to.strftime('%m/%d')} · "
            f"전주비: vs {prev_from.strftime('%m/%d')}~{prev_to.strftime('%m/%d')} · "
            f"전년동주: {yoy_from.strftime('%y/%m/%d')}~{yoy_to.strftime('%m/%d')} (ISO 주차 정렬). "
            "신설 공장은 전년 데이터가 없어 전년비가 '-' 로 표시됩니다."
        ),
        axis_defs=[
            {"label": "당주", "delta_header": "전주비"},
            {"label": "전년동주", "delta_header": "전년비"},
        ],
        factory_table_metrics=FACTORY_TABLE_METRICS,
        factory_rows=factory_rows,
        extra_table={
            "title": "최근 4주 전사 추이",
            "note": "각 행의 증감(%)은 직전 주 대비입니다. 최신 주가 맨 위.",
            "rows": trend_rows,
        },
    ))

    log.info(f"주간 리포트 생성 완료 — 표 {len(factory_rows)}행, 추이 {len(trend_rows)}주")
    return BuiltReport(
        subject=subject, html=html, inline_images=[],
        ref_date=wk_to, record_count=n_factories,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 월간 리포트
# ─────────────────────────────────────────────────────────────────────────────
def build_monthly_report(
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> BuiltReport:
    """성과 평가용 월간 메일. (year, month) 미지정 시 직전 완결 월."""
    if year is None or month is None:
        year, month = last_complete_month()

    m_from, m_to = _month_range(year, month)
    yoy_from, yoy_to = _month_range(year - 1, month)
    ytd_from, ytd_to = date(year, 1, 1), m_to
    ytd_y_from = date(year - 1, 1, 1)
    ytd_y_to = _month_range(year - 1, month)[1]

    log.info(
        f"월간 성과 리포트 시작 — 대상 {year}-{month:02d}, "
        f"전년동월 {yoy_from}~{yoy_to}, YTD {ytd_from}~{ytd_to}"
    )
    rows_m = _fetch_rows_range(m_from, m_to)
    rows_yoy = _fetch_rows_range(yoy_from, yoy_to)
    rows_ytd = _fetch_rows_range(ytd_from, ytd_to)
    rows_ytd_y = _fetch_rows_range(ytd_y_from, ytd_y_to)

    mtd_rows = _build_monthly_comparison_rows(rows_m, rows_yoy)
    ytd_rows = _build_monthly_comparison_rows(rows_ytd, rows_ytd_y)
    n_factories = (_aggregate_weighted(rows_m) or {}).get("n_factories", 0)
    subject = f"[생산기술팀] 월간 에너지 성과 리포트 {year}년 {month}월"

    html = _render({
        "subject": subject,
        "year": year,
        "previous_year": year - 1,
        "month": month,
        "period_from": m_from.strftime("%Y-%m-%d"),
        "period_to": m_to.strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "factory_table_metrics": FACTORY_TABLE_METRICS,
        "mtd_rows": mtd_rows,
        "ytd_rows": ytd_rows,
        "mtd_current_label": f"{year}.{month:02d}",
        "mtd_previous_label": f"{year - 1}.{month:02d}",
        "ytd_current_label": f"{year}.01~{month:02d}",
        "ytd_previous_label": f"{year - 1}.01~{month:02d}",
    }, template_name="monthly_energy_report.html")

    log.info(
        f"월간 성과 리포트 생성 완료 — MTD {len(mtd_rows)}행, YTD {len(ytd_rows)}행"
    )
    return BuiltReport(
        subject=subject,
        html=html,
        inline_images=[],
        ref_date=m_to,
        record_count=n_factories,
    )
