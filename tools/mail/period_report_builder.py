"""
Weekly / Monthly Energy Intensity Report Builder
================================================
일일 메일(daily_report_builder)과 동일한 집계·표 구조를 재사용해
주간/월간 alert 메일을 생성한다. 렌더링은 공용 템플릿
templates/period_energy_report.html 하나를 두 주기가 공유한다.

비교축 설계 (계절성이 강한 식품공장 특성 반영):
  · 주간 — 주 비교 = 전주 대비 (인접 주는 기온·생산 믹스가 유사해
            운영 변화가 잘 드러남), 보조 = 전년 동일 ISO 주차 (계절 보정).
  · 월간 — 주 비교 = 전년 동월 (전월 대비는 계절성 때문에 성과 판단 부적합
            → 회색 참고값으로만 표기), 보조 = YTD 누계 전년비 + 연간 목표 게이지.

신설 공장(예: 경산 2026-04~)은 전년 데이터가 없어 전년비가 '-' 로 표시되며,
주간의 전주 대비 / 월간의 전월 참고비가 유일한 비교축이 된다.
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
    _build_target_progress,
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
def _render(context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env.get_template("period_energy_report.html").render(**context)


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
        target_progress=[],
        target_note="",
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
    """월간 메일. (year, month) 미지정 시 직전 완결 월."""
    if year is None or month is None:
        year, month = last_complete_month()

    m_from, m_to = _month_range(year, month)
    pm_year, pm_month = (year - 1, 12) if month == 1 else (year, month - 1)
    pm_from, pm_to = _month_range(pm_year, pm_month)          # 전월 (회색 참고)
    yoy_from, yoy_to = _month_range(year - 1, month)          # 전년 동월 (주 비교)
    ytd_from, ytd_to = date(year, 1, 1), m_to
    ytd_y_from, ytd_y_to = date(year - 1, 1, 1), _month_range(year - 1, month)[1]
    prev_year_from, prev_year_to = date(year - 1, 1, 1), date(year - 1, 12, 31)

    log.info(f"월간 리포트 시작 — 대상 {year}-{month:02d}, "
             f"전년동월 {yoy_from}~{yoy_to}, 전월(참고) {pm_from}~{pm_to}")

    rows_m = _fetch_rows_range(m_from, m_to)
    rows_pm = _fetch_rows_range(pm_from, pm_to)
    rows_yoy = _fetch_rows_range(yoy_from, yoy_to)
    rows_ytd = _fetch_rows_range(ytd_from, ytd_to)
    rows_ytd_y = _fetch_rows_range(ytd_y_from, ytd_y_to)
    rows_prev_full = _fetch_rows_range(prev_year_from, prev_year_to)

    factory_rows = _build_period_factory_rows([
        # 축0: 당월 실적 + 전년 동월비 (주 비교) + 전월비 (회색 참고)
        dict(value_rows=rows_m, cmp_curr_rows=rows_m, cmp_base_rows=rows_yoy,
             ref2_rows=rows_pm, ref2_label="전월비"),
        # 축1: YTD 누계 + 전년 동기비
        dict(value_rows=rows_ytd, cmp_curr_rows=rows_ytd, cmp_base_rows=rows_ytd_y),
    ])

    # 당해 월별 전사 추이 (1월~당월, 최신이 위) — 각 월은 전년 동월 대비 델타
    trend_rows = []
    for mm in range(month, 0, -1):
        mm_from, mm_to = _month_range(year, mm)
        yy_from, yy_to = _month_range(year - 1, mm)
        trend_rows.append(_trend_row(
            f"{year}-{mm:02d}",
            _fetch_rows_range(mm_from, mm_to),
            _fetch_rows_range(yy_from, yy_to),
        ))

    # 연간 목표 게이지 — 일간 메일과 동일 로직 (YTD ÷ [전년 전체 × 0.95])
    ytd_curr = _aggregate_weighted(rows_ytd)
    prev_year_full = _aggregate_weighted(rows_prev_full)
    target_progress = _build_target_progress(m_to, ytd_curr, prev_year_full)

    n_factories = (_aggregate_weighted(rows_m) or {}).get("n_factories", 0)
    subject = f"[생산기술팀] 월간 에너지 원단위 alert {year}년 {month}월"

    html = _render(dict(
        subject=subject,
        report_title="월간 에너지 원단위 Alert",
        period_label=f"대상 월 <b>{year}년 {month}월</b> "
                     f"({m_from.strftime('%Y-%m-%d')} ~ {m_to.strftime('%Y-%m-%d')})",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        table_note=(
            f"당월: {year}-{month:02d} · 전년비: vs {year - 1}-{month:02d} (계절 정렬 주 비교) · "
            f"회색 전월비는 계절성 미보정 참고값 · YTD: {ytd_from.strftime('%m/%d')}~{ytd_to.strftime('%m/%d')} 누계. "
            "신설 공장은 전년 데이터가 없어 전년비가 '-' 로 표시됩니다."
        ),
        axis_defs=[
            {"label": f"{month}월", "delta_header": "전년비"},
            {"label": "YTD", "delta_header": "전년비"},
        ],
        factory_table_metrics=FACTORY_TABLE_METRICS,
        factory_rows=factory_rows,
        extra_table={
            "title": f"{year}년 월별 전사 추이",
            "note": "각 행의 증감(%)은 전년 동월 대비입니다. 최신 월이 맨 위.",
            "rows": trend_rows,
        },
        target_progress=target_progress,
        target_note=(
            f"사용률 = 누적 YTD/'{str(year)[2:]}년 목표, "
            f"'{str(year)[2:]}년 목표 = '{str(year - 1)[2:]}년 전체 누계 × 0.95 (전년비 5% 절감)"
        ),
    ))

    log.info(f"월간 리포트 생성 완료 — 표 {len(factory_rows)}행, "
             f"추이 {len(trend_rows)}개월, 목표 {len(target_progress)}건")
    return BuiltReport(
        subject=subject, html=html, inline_images=[],
        ref_date=m_to, record_count=n_factories,
    )
