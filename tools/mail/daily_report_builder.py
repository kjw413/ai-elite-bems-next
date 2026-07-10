"""
Daily Energy Alert Report Builder
=================================
기준일(D-N)의 당일 운영 이상을 빠르게 확인하는 일일 메일.

  1) 5개 사업장 생산량·사용량 방향 신호
     - 홈 대시보드와 동일하게 최근 7일 중앙값의 1%를 유효 변화 기준으로 사용
     - 생산량 감소 + 에너지 사용량 증가 조합을 즉시 점검 대상으로 요약
  2) 당일·전일·전일비 상세 실적
     - 생산량과 전력·연료·용수·폐수 원시 사용량을 사업장별로 표시

주간/월간 메일은 period_report_builder.py가 본 모듈의 공용 집계 함수를 재사용한다.
광주 생산량은 대시보드와 동일하게 판매용 재공품 환산량을 보정한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
# 남양주는 raw DB의 남양주1+남양주2 를 합산한 가상 사업장.
FACTORY_DISPLAY_ORDER: List[Tuple[str, Optional[List[str]]]] = [
    ("전사",   None),
    ("남양주", ["남양주1", "남양주2"]),
    ("김해",   ["김해"]),
    ("광주",   ["광주"]),
    ("논산",   ["논산"]),
    ("경산",   ["경산"]),
]

# 원단위 메트릭 정의.
# color: 대시보드 7일 원단위 추이 차트의 에너지원별 상징색과 동기.
#   header_bg : 헤더(colspan=2) 셀 배경 — 상징색 ~20% + 흰색 80%
#   cell_bg   : 본문 값/델타 셀 배경 — 상징색 ~6% + 흰색 94% (텍스트 가독성 유지)
#   invert    : True = 증가가 개선(생산량). False = 증가가 악화(원단위/사용량 계열).
#   decimals  : 본문 셀의 표시 소수자릿수.
# 이전 버전의 icon(⚡🔥💧🚿🍦)은 사내 그룹웨어 "전달" 시 Namo 에디터의 sanitizer가
# Supplementary Plane 이모지를 통째로 잘라내 빈 칸으로 보이는 이슈가 있어 제거.
# 시각 구분은 header_bg + border-top color 만으로 유지한다.
# SUM(사용량)/SUM(생산톤) 구조의 원단위 3종.
INTENSITY_METRICS = [
    {"key": "power",      "label": "전력 원단위", "unit": "kWh/ton",
     "color": "#F6C90E", "header_bg": "#FDF4CF", "cell_bg": "#FEFAEC",
     "usage_col": "total_power_kwh", "unit_col": "power_per_ton_kwh",
     "decimals": 2, "invert": False},
    {"key": "fuel",       "label": "연료 원단위", "unit": "Nm³/ton",
     "color": "#E8450A", "header_bg": "#FADACE", "cell_bg": "#FDF0EB",
     "usage_col": "fuel_nm3",        "unit_col": "fuel_per_ton_nm3",
     "decimals": 2, "invert": False},
    {"key": "water",      "label": "용수 원단위", "unit": "ton/ton",
     "color": "#0EA5E9", "header_bg": "#CFEDFB", "cell_bg": "#ECF7FD",
     "usage_col": "water_ton",       "unit_col": "water_per_ton_ton",
     "decimals": 2, "invert": False},
]

# 폐수/용수 = 폐수량 / 용수량 (소수점 비). '원단위'(사용량/생산톤)가 아니며
# 사업장별 표에만 노출한다.
# unit_col 은 _aggregate_weighted 가 별도로 채우는 파생 키 'wastewater_ratio'.
WASTEWATER_RATIO_METRIC = {
    "key": "wastewater_ratio", "label": "폐수/용수", "unit": "폐수량/용수량",
    "color": "#6B7280", "header_bg": "#E1E3E6", "cell_bg": "#F3F4F5",
    "usage_col": None, "unit_col": "wastewater_ratio",
    "decimals": 2, "invert": False,
}

# 생산량 — 원단위와 부호 해석이 반대이며(증가=개선), 사업장별 표의 5번째 컬럼으로
# 노출한다. 원단위는 고정부하 영향으로 생산량 변동분에 종속적이라
# 같은 표에서 함께 봐야 해석 가능 (생산↓ → 고정부하 분담 ↑ → 원단위 악화 등).
PRODUCTION_METRIC = {
    "key": "production", "label": "생산량", "unit": "ton",
    # 메로나 연녹색 — 대시보드 PROD_COLOR와 동기 (#A4D65E).
    # header_bg / cell_bg = 상징색을 흰색에 ~20% / ~6% 혼합 (다른 메트릭과 동일 톤 규칙).
    "color": "#A4D65E", "header_bg": "#EDF6DE", "cell_bg": "#FAFCF5",
    "usage_col": "mix_prod_kg", "unit_col": "production_ton",
    "decimals": 0, "invert": True,
}

# 주간·월간 메일의 사업장별 원단위 표 컬럼 정의.
FACTORY_TABLE_METRICS = [PRODUCTION_METRIC] + INTENSITY_METRICS + [WASTEWATER_RATIO_METRIC]
# 일일 메일은 원단위가 아니라 생산량과 원시 사용량을 비교한다.
DAILY_DETAIL_METRICS = [
    {"key": "production", "label": "생산량", "signal_label": "생산", "unit": "ton",
     "value_col": "production_ton", "color": "#A4D65E", "header_bg": "#EDF6DE",
     "cell_bg": "#FAFCF5", "decimals": 0, "invert": True},
    {"key": "power", "label": "전력 사용량", "signal_label": "전력", "unit": "kWh",
     "value_col": "total_power_kwh", "color": "#F6C90E", "header_bg": "#FDF4CF",
     "cell_bg": "#FEFAEC", "decimals": 0, "invert": False},
    {"key": "fuel", "label": "연료 사용량", "signal_label": "연료", "unit": "Nm³",
     "value_col": "fuel_nm3", "color": "#E8450A", "header_bg": "#FADACE",
     "cell_bg": "#FDF0EB", "decimals": 0, "invert": False},
    {"key": "water", "label": "용수 사용량", "signal_label": "용수", "unit": "ton",
     "value_col": "water_ton", "color": "#0EA5E9", "header_bg": "#CFEDFB",
     "cell_bg": "#ECF7FD", "decimals": 1, "invert": False},
    {"key": "wastewater", "label": "폐수 사용량", "signal_label": "폐수", "unit": "ton",
     "value_col": "wastewater_ton", "color": "#6B7280", "header_bg": "#E1E3E6",
     "cell_bg": "#F3F4F5", "decimals": 1, "invert": False},
]
DAILY_USAGE_METRICS = DAILY_DETAIL_METRICS[1:]
DAILY_FACTORY_DISPLAY_ORDER = FACTORY_DISPLAY_ORDER[1:]



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
def _apply_gwangju_correction_to_rows(
    rows: List[dict],
    date_from: date,
    date_to: date,
) -> List[dict]:
    """광주 행의 mix_prod_kg에 재공품(외부 판매분) 환산값을 가산.

    공식: mix_prod_kg ← raw_mix + wip_kg — 대시보드 query_service._apply_gwangju_correction
    과 동일. raw_mix(에너지팀 sync 값)에 DB_재공품의 광주 시트 환산 합계를 더해 외부
    판매 재공품(탈지분유·살균유·생크림·유크림믹스 등) 분량이 빠지지 않도록 한다.
    accounted_kg(=finished+wip)로 교체하는 방식은 production_daily 적재 누락 시 raw
    정보를 잃어버리므로 채택하지 않는다 (대시보드 docstring 참고).
    """
    if not rows or not any(r.get("factory") == "광주" for r in rows):
        return rows
    try:
        from app.services.production_correction_service import get_breakdown_daily
        bd = get_breakdown_daily("광주", date_from, date_to)
    except Exception as exc:
        log.warning(f"광주 보정 데이터 로드 실패 — 원본 유지: {exc}")
        return rows
    if bd is None or bd.empty:
        return rows
    bd = bd.copy()
    bd["_date_norm"] = bd["date"].apply(
        lambda d: d.date() if hasattr(d, "date") else d
    )
    wip_map: Dict = dict(zip(bd["_date_norm"], bd["wip_kg"].astype(float)))

    for r in rows:
        if r.get("factory") != "광주":
            continue
        d = r.get("date")
        if hasattr(d, "date"):
            d = d.date()
        wip = wip_map.get(d) or 0.0
        raw_mix = float(r.get("mix_prod_kg") or 0)
        r["mix_prod_kg"] = raw_mix + float(wip)
    return rows


def _fetch_rows_range(
    date_from: date,
    date_to: date,
    factories: Optional[List[str]] = None,
) -> List[dict]:
    """기간 내 (factory × date) 행을 모두 조회. 광주 행은 분모 보정을 적용."""
    sql = """
        SELECT factory, date,
               total_power_kwh, fuel_nm3, water_ton, wastewater_ton, mix_prod_kg
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

    return _apply_gwangju_correction_to_rows(rows, date_from, date_to)


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
    """생산량과 사용량 방향 조합을 대시보드와 동일한 라벨로 변환."""
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
    rows: List[dict], ref_date: date
) -> Tuple[List[dict], List[dict], int]:
    """5개 사업장의 방향 신호와 당일·전일 상세 실적을 생성."""
    history_dates = [ref_date - timedelta(days=i) for i in range(6, -1, -1)]
    prev_date = ref_date - timedelta(days=1)
    factory_rows: List[dict] = []
    warning_items: List[dict] = []
    good_count = 0

    for factory_label, codes in DAILY_FACTORY_DISPLAY_ORDER:
        history = {d: _aggregate_on_date(rows, d, codes) for d in history_dates}
        curr = history.get(ref_date)
        prev = history.get(prev_date)
        prod_col = "production_ton"
        prod_curr = _metric_value(curr, prod_col)
        prod_prev = _metric_value(prev, prod_col)
        prod_base = _median_nonzero([_metric_value(history[d], prod_col) for d in history_dates])
        prod_delta, prod_color, prod_pct = _pct_delta(prod_curr, prod_prev, invert=True)

        signals = []
        for metric in DAILY_USAGE_METRICS:
            col = metric["value_col"]
            usage_curr = _metric_value(curr, col)
            usage_prev = _metric_value(prev, col)
            usage_base = _median_nonzero([_metric_value(history[d], col) for d in history_dates])
            signal = _daily_direction_signal(
                prod_curr, prod_prev, usage_curr, usage_prev, prod_base, usage_base,
            )
            usage_delta, usage_color, usage_pct = _pct_delta(usage_curr, usage_prev)
            signal.update({
                "energy": metric["signal_label"],
                "usage_delta": usage_delta,
                "usage_delta_color": usage_color,
                "production_delta": prod_delta,
            })
            signals.append(signal)
            if signal["tone"] == "warn":
                warning_items.append({
                    "date": ref_date.strftime("%Y-%m-%d"),
                    "factory": factory_label,
                    "energy": metric["signal_label"],
                    "production_delta": prod_delta,
                    "usage_delta": usage_delta,
                })
            elif signal["tone"] == "good":
                good_count += 1

        detail_cells = []
        for metric in DAILY_DETAIL_METRICS:
            col = metric["value_col"]
            curr_v = _metric_value(curr, col)
            prev_v = _metric_value(prev, col)
            delta, delta_color, _ = _pct_delta(
                curr_v, prev_v, invert=metric.get("invert", False),
            )
            detail_cells.append({
                "current": _fmt(curr_v, metric["decimals"]),
                "previous": _fmt(prev_v, metric["decimals"]),
                "delta": delta,
                "delta_color": delta_color,
            })

        factory_rows.append({
            "factory": factory_label,
            "has_current": curr is not None,
            "has_previous": prev is not None,
            "production_delta": prod_delta,
            "production_delta_color": prod_color,
            "signals": signals,
            "detail_cells": detail_cells,
        })

    return factory_rows, warning_items, good_count

# ─────────────────────────────────────────────────────────────────────────────
# 메인 엔트리
# ─────────────────────────────────────────────────────────────────────────────
def build_daily_report(
    ref_date: Optional[date] = None,
    config: Optional[DailyReportConfig] = None,
) -> BuiltReport:
    cfg = config or get_daily_report_config()
    if ref_date is None:
        ref_date = date.today() - timedelta(days=cfg.reference_offset_days)

    factories_filter = cfg.factories_filter or None
    trend_from = ref_date - timedelta(days=6)
    prev_date = ref_date - timedelta(days=1)
    log.info(
        f"일일 이상 리포트 시작 - 기준일: {ref_date}, "
        f"판정 이력: {trend_from}~{ref_date}"
    )

    rows = _fetch_rows_range(trend_from, ref_date, factories_filter)
    factory_rows, warning_items, good_count = _build_daily_factory_rows(rows, ref_date)
    n_current_factories = sum(1 for row in factory_rows if row["has_current"])
    warning_factory_count = len({item["factory"] for item in warning_items})

    subject = (
        f"[생산기술팀] 일일 에너지 이상 alert {ref_date} "
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
        n_factories=n_current_factories,
        warning_count=len(warning_items),
        warning_factory_count=warning_factory_count,
        good_count=good_count,
        warning_items=warning_items,
        factory_rows=factory_rows,
        signal_metrics=DAILY_USAGE_METRICS,
        detail_metrics=DAILY_DETAIL_METRICS,
    )

    log.info(
        f"일일 이상 리포트 생성 완료 - 사업장 {n_current_factories}/5, "
        f"즉시 점검 {len(warning_items)}건, 개선 신호 {good_count}건"
    )
    return BuiltReport(
        subject=subject,
        html=html,
        inline_images=[],
        ref_date=ref_date,
        record_count=n_current_factories,
    )
