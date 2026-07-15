"""
Weekly / Monthly Energy Report Builder
======================================
일일 메일의 공용 집계 함수 + 차트 렌더러(_render_metric_grid_chart)를 재사용하되
주기별 목적에 맞춰 별도 구조로 렌더링한다.

  · 주간 — 일간 메일과 동일한 구조(가로축=사업장, 세로축=지표)로 전사+6개 사업장의
             W-1(전주) 실적과 W-2(전전주) 대비 증감률 단일 스냅샷을 섹션1에 제공하고,
             섹션2에서 최근 4주 개별(누계 아님) 원단위 추이를 사업장별(남양주1~경산)
             차트로 제공한다. 생산량은 각 원단위 차트에 보조축 꺾은선으로 병기.
             templates/weekly_energy_report.html 사용.
  · 월간 — 당월 실적(전년 동월비, YTD 병기)과 공장별 월별 MTD 원단위 추이 차트를 제공한다.
             templates/monthly_energy_report.html 사용.

신설 공장(예: 경산 2026-04~)은 전년 데이터가 없는 기간에 전년비가 '-'로 표시된다.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tools.mail.config import TEMPLATE_DIR
from tools.mail.logger import get_logger
from tools.mail.mail_service import InlineImage
from tools.mail.daily_report_builder import (
    BuiltReport,
    DAILY_FACTORY_DISPLAY_ORDER,
    FACTORY_DISPLAY_ORDER,
    FACTORY_TABLE_METRICS,
    INTENSITY_CHART_METRICS,
    PRODUCTION_METRIC,
    _aggregate_weighted,
    _fetch_rows_range,
    _filter_rows_by_factory,
    _fmt,
    _metric_value,
    _normalize_row_date,
    _pct_delta,
    _render_metric_grid_chart,
)

log = get_logger("period_report")

# 월간 월별 MTD 추이 차트는 생산량을 제외한 원단위 4종만 2×2로 그린다
# (생산량 자체의 전년비는 이미 섹션1 표에 있어 중복 표시하지 않음). daily의
# INTENSITY_CHART_METRICS 와 동일 정의를 재사용해 필터링 로직 중복을 피한다.
MONTHLY_CHART_METRICS = INTENSITY_CHART_METRICS
WEEKLY_TREND_WEEK_COUNT = 4


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


def last_complete_month(today: Optional[date] = None) -> Tuple[int, int]:
    """실행일 기준 직전 완결 월 (year, month)."""
    today = today or date.today()
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return y, m


def _month_range(year: int, month: int) -> Tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _build_weekly_snapshot_table(curr_rows: List[dict], prev_rows: List[dict]) -> dict:
    """전사+6개 사업장 × 생산량+원단위 7종의 단일 스냅샷 표 데이터 (일간 표와 동일 구조).

    curr_rows: W-1(전주) 기간의 _fetch_rows_range 결과.
    prev_rows: W-2(전전주) 기간의 _fetch_rows_range 결과.
    반환: {factories: [{factory, is_total}], metric_rows: [{label, unit, color,
             header_bg, cell_bg, cells: [{value, delta, color} × 사업장 수]}]}
    """
    agg_by_factory = {
        label: (
            _aggregate_weighted(_filter_rows_by_factory(curr_rows, codes)),
            _aggregate_weighted(_filter_rows_by_factory(prev_rows, codes)),
        )
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


def _build_monthly_factory_rows(
    rows_mtd: List[dict],
    rows_mtd_prev: List[dict],
    rows_ytd: List[dict],
    rows_ytd_prev: List[dict],
) -> List[dict]:
    """월간 전용: 사업장별 × 지표별로 MTD/YTD 실적을 나란히(컬럼) 생성.

    각 지표마다 {mtd: {value, delta, color}, ytd: {value, delta, color}} 쌍을 만들어
    한 행(사업장)에서 당월(MTD)과 연누계(YTD) 실적·전년비를 한 번에 볼 수 있게 한다.
    """
    rows = []
    for label, codes in FACTORY_DISPLAY_ORDER:
        m_cur = _aggregate_weighted(_filter_rows_by_factory(rows_mtd, codes))
        m_prev = _aggregate_weighted(_filter_rows_by_factory(rows_mtd_prev, codes))
        y_cur = _aggregate_weighted(_filter_rows_by_factory(rows_ytd, codes))
        y_prev = _aggregate_weighted(_filter_rows_by_factory(rows_ytd_prev, codes))

        metric_cells = []
        for metric in FACTORY_TABLE_METRICS:
            col = metric["unit_col"]
            invert = metric.get("invert", False)
            decimals = metric.get("decimals", 2)

            m_delta, m_color, _ = _pct_delta(
                m_cur.get(col) if m_cur else None,
                m_prev.get(col) if m_prev else None,
                invert=invert,
            )
            y_delta, y_color, _ = _pct_delta(
                y_cur.get(col) if y_cur else None,
                y_prev.get(col) if y_prev else None,
                invert=invert,
            )
            metric_cells.append({
                "mtd": {
                    "value": _fmt(m_cur.get(col) if m_cur else None, decimals),
                    "delta": m_delta, "color": m_color,
                },
                "ytd": {
                    "value": _fmt(y_cur.get(col) if y_cur else None, decimals),
                    "delta": y_delta, "color": y_color,
                },
            })
        rows.append({
            "factory": label,
            "is_total": codes is None,
            "metric_cells": metric_cells,
        })
    return rows


def _build_monthly_mtd_charts(
    year: int,
    month: int,
    rows_cur: List[dict],
    rows_prev: List[dict],
) -> Tuple[List[InlineImage], List[dict]]:
    """공장별 월별 MTD(1월~대상월) 원단위 추이 차트를 PNG 인라인 이미지로 생성.

    한 공장당 2×2 그리드(전력/연료/용수/폐수·용수), 당해년(색 실선)과 전년(회색 점선)을
    함께 그려 계절 흐름을 대조한다. 렌더링은 공용 _render_metric_grid_chart 재사용.
    반환: (inline_images, blocks[{factory, cid}])
    """
    months = list(range(1, month + 1))
    month_labels = [f"{m}월" for m in months]

    def series(rows: List[dict], codes: Optional[List[str]], col: str, yr: int) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        for mm in months:
            m_start, m_end = _month_range(yr, mm)
            sel = [
                r for r in _filter_rows_by_factory(rows, codes)
                if m_start <= _normalize_row_date(r.get("date")) <= m_end
            ]
            agg = _aggregate_weighted(sel)
            out.append(agg.get(col) if agg else None)
        return out

    images: List[InlineImage] = []
    blocks: List[dict] = []
    for idx, (label, codes) in enumerate(FACTORY_DISPLAY_ORDER):
        series_by_col = {m["unit_col"]: series(rows_cur, codes, m["unit_col"], year) for m in MONTHLY_CHART_METRICS}
        prev_by_col = {m["unit_col"]: series(rows_prev, codes, m["unit_col"], year - 1) for m in MONTHLY_CHART_METRICS}
        png = _render_metric_grid_chart(
            metrics=MONTHLY_CHART_METRICS, x_labels=month_labels,
            series_by_col=series_by_col, prev_series_by_col=prev_by_col,
            cur_legend=f"{year}년", prev_legend=f"{year - 1}년", cols=2,
            include_value_table=True,
            include_value_table_label_column=False,
        )
        if png is None:
            continue  # 데이터 전무 공장(예: 미가동)은 차트 생략
        cid = f"mtd_chart_{idx}"
        images.append(InlineImage(cid=cid, data=png, mime_subtype="png"))
        blocks.append({"factory": label, "cid": cid})

    return images, blocks


# ─────────────────────────────────────────────────────────────────────────────
# 렌더 공통
# ─────────────────────────────────────────────────────────────────────────────
def _render(context: dict, template_name: str = "weekly_energy_report.html") -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env.get_template(template_name).render(**context)


# ─────────────────────────────────────────────────────────────────────────────
# 주간 리포트
# ─────────────────────────────────────────────────────────────────────────────
def build_weekly_report(ref_date: Optional[date] = None) -> BuiltReport:
    """주간 메일. ref_date 는 대상 주에 속한 임의 일자 (기본: 직전 완결 주).

    비교축은 전주 대비만 사용한다 — 전년 동주는 명절 이동(설/추석) 등으로 계절
    보정이 부정확해 인접 기간(전주) 비교보다 오탐 위험이 크다고 판단해 제외했다.
    """
    if ref_date is None:
        wk_from, wk_to = last_complete_week()
    else:
        wk_from, wk_to = week_of(ref_date)

    iso_year, iso_week, _ = wk_from.isocalendar()

    log.info(f"주간 리포트 시작 — 대상 {wk_from}~{wk_to} (W{iso_week})")

    rows_wk = _fetch_rows_range(wk_from, wk_to)

    # 섹션 1: 일일 메일과 동일한 구조(가로축=사업장, 세로축=지표)의 단일 스냅샷 —
    # W-1(전주, wk_from~wk_to) 실적과 W-2(전전주) 대비 증감률.
    prev_wk_from, prev_wk_to = wk_from - timedelta(days=7), wk_to - timedelta(days=7)
    rows_prev_wk = _fetch_rows_range(prev_wk_from, prev_wk_to)
    weekly_snapshot_table = _build_weekly_snapshot_table(rows_wk, rows_prev_wk)

    # 최근 4주 개별(누계 아님) 사업장별(남양주~경산) 원단위 추이 차트 — 오래된 주 → 최신 주.
    # 전사 합산 대신 5개 실사업장으로 나눠, 특정 공장만 이상인 경우도 놓치지 않게 한다.
    week_windows = [
        (wk_from - timedelta(days=7 * i), wk_to - timedelta(days=7 * i))
        for i in range(WEEKLY_TREND_WEEK_COUNT - 1, -1, -1)
    ]
    week_x_labels = [
        (
            f"W{w_from.isocalendar()[1]:02d}<br>"
            f"({w_from.strftime('%m/%d')}~{w_to.strftime('%m/%d')})"
        )
        for w_from, w_to in week_windows
    ]
    week_rows = [_fetch_rows_range(w_from, w_to) for w_from, w_to in week_windows]

    trend_images: List[InlineImage] = []
    trend_charts: List[dict] = []
    for idx, (factory_label, codes) in enumerate(DAILY_FACTORY_DISPLAY_ORDER):
        series_by_col: Dict[str, List[Optional[float]]] = {m["unit_col"]: [] for m in FACTORY_TABLE_METRICS}
        for wk_rows in week_rows:
            agg = _aggregate_weighted(_filter_rows_by_factory(wk_rows, codes))
            for m in FACTORY_TABLE_METRICS:
                series_by_col[m["unit_col"]].append(agg.get(m["unit_col"]) if agg else None)

        png = _render_metric_grid_chart(
            metrics=INTENSITY_CHART_METRICS, x_labels=week_x_labels,
            series_by_col=series_by_col,
            overlay_series=series_by_col.get(PRODUCTION_METRIC["unit_col"]),
            overlay_metric=PRODUCTION_METRIC,
            cur_legend="원단위",
            cols=2,
            include_value_table=True,
            include_overlay_in_value_table=False,
            include_value_table_label_column=False,
        )
        if png is None:
            continue
        cid = f"weekly_trend_chart_{idx}"
        trend_images.append(InlineImage(cid=cid, data=png, mime_subtype="png"))
        trend_charts.append({"factory": factory_label, "cid": cid})

    n_factories = (_aggregate_weighted(rows_wk) or {}).get("n_factories", 0)
    subject = (
        "[생산기술팀] 주간 에너지 원단위 alert "
        f"{wk_from.strftime('%m/%d')}~{wk_to.strftime('%m/%d')}"
    )

    html = _render(dict(
        subject=subject,
        report_title="주간 에너지 원단위 Alert",
        period_label=f"대상 주 <b>{iso_year}-W{iso_week:02d}</b> "
                     f"({wk_from.strftime('%Y-%m-%d')} ~ {wk_to.strftime('%Y-%m-%d')})",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        section1_note=(
            f"W-1(전주, {wk_from.strftime('%m/%d')}~{wk_to.strftime('%m/%d')}) 실적과 "
            f"W-2(전전주, {prev_wk_from.strftime('%m/%d')}~{prev_wk_to.strftime('%m/%d')}) 대비 증감률입니다. "
        ),
        weekly_snapshot_table=weekly_snapshot_table,
        trend_charts=trend_charts,
        trend_week_count=WEEKLY_TREND_WEEK_COUNT,
        trend_chart_note="",
    ))

    log.info(f"주간 리포트 생성 완료 — 스냅샷 지표 {len(weekly_snapshot_table['metric_rows'])}행, 추이 차트 {len(trend_charts)}개")
    return BuiltReport(
        subject=subject, html=html, inline_images=trend_images,
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
        f"월간 리포트 시작 — 대상 {year}-{month:02d}, "
        f"전년동월 {yoy_from}~{yoy_to}, YTD {ytd_from}~{ytd_to}"
    )
    rows_m = _fetch_rows_range(m_from, m_to)
    rows_yoy = _fetch_rows_range(yoy_from, yoy_to)
    rows_ytd = _fetch_rows_range(ytd_from, ytd_to)
    rows_ytd_y = _fetch_rows_range(ytd_y_from, ytd_y_to)

    # 섹션 1: 사업장 × 지표별 MTD(당월)·YTD(연누계) 실적+전년비를 나란히(컬럼) 표시
    factory_rows = _build_monthly_factory_rows(rows_m, rows_yoy, rows_ytd, rows_ytd_y)
    # 섹션 2: 공장별 월별 MTD 원단위 추이 꺾은선 (당해 vs 전년)
    mtd_images, mtd_charts = _build_monthly_mtd_charts(year, month, rows_ytd, rows_ytd_y)

    n_factories = (_aggregate_weighted(rows_m) or {}).get("n_factories", 0)
    subject = f"[생산기술팀] 월간 에너지 원단위 alert '{str(year)[2:]}년 {month}월"

    html = _render({
        "subject": subject,
        "year": year,
        "previous_year": year - 1,
        "month": month,
        "period_from": m_from.strftime("%Y-%m-%d"),
        "period_to": m_to.strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "factory_table_metrics": FACTORY_TABLE_METRICS,
        "factory_rows": factory_rows,
        "mtd_current_label": f"{month}월",
        "mtd_previous_label": f"{year - 1}.{month:02d}",
        "ytd_current_label": f"{month}월",
        "ytd_previous_label": f"{year - 1}.01~{month:02d}",
        "mtd_charts": mtd_charts,
    }, template_name="monthly_energy_report.html")

    log.info(
        f"월간 리포트 생성 완료 — 표 {len(factory_rows)}행, MTD 추이 차트 {len(mtd_charts)}개"
    )
    return BuiltReport(
        subject=subject,
        html=html,
        inline_images=mtd_images,
        ref_date=m_to,
        record_count=n_factories,
    )
