"""
Daily Energy Intensity Report Builder (v5 — v5.2 정상범주 밴드 적용)
======================================================================
기준일(D-N) 일일 에너지 원단위 실적을 3섹션 1페이지로 정리:

  1) 사업장별 원단위 표    — 5 사업장(전사/남양주/김해/광주/논산) × 4 원단위
                              각 셀: MTD 값 + MTD 전년비, YTD 값 + YTD 전년비
  2) 연 진척 게이지        — 전사 4개 원단위 연간 목표 대비 YTD 달성 페이스
  3) 정상범주 상한 초과    — 기준일 5개 실공장 × 3타겟(전력/연료/용수) 중
                              실측이 v5.2 정상범주 상한(P95)을 넘은 항목
                              (이전 "AI 이상 감지" 섹션을 v5.2 밴드 기반으로 교체)
  + 전력 원단위 7일 추이 차트(공장별 라인)

비교축 (식품공장 계절성 + 일별 noise 고려):
  · MTD = 이번 달 1일~기준일 가중평균 vs 전년 동월 1일~동일 day
  · YTD = 올해 1/1~기준일 가중평균 vs 전년 동기간 1/1~동일 day

원단위 계산: SUM(사용량) / SUM(생산량_kg/1000). mix_prod_kg ≤ 0 공장은 제외
(raw 데이터 결측 보정 — 분자/분모 정합성).

남양주 = 남양주1 + 남양주2 합산 (query_service 의 표시 컨벤션과 동일).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    "F20":  "2공장",
    "F30":  "3공장",
    "F40":  "4공장",
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
]

# 원단위 메트릭 정의. target_service 의 metric 키와 동기.
# color: 대시보드 7일 원단위 추이 차트의 에너지원별 상징색과 동기.
#   header_bg : 헤더(colspan=2) 셀 배경 — 상징색 ~20% + 흰색 80%
#   cell_bg   : 본문 값/델타 셀 배경 — 상징색 ~6% + 흰색 94% (텍스트 가독성 유지)
#   invert    : True = 증가가 개선(생산량). False = 증가가 악화(원단위/사용량 계열).
#   decimals  : 본문 셀의 표시 소수자릿수.
# 이전 버전의 icon(⚡🔥💧🚿🍦)은 사내 그룹웨어 "전달" 시 Namo 에디터의 sanitizer가
# Supplementary Plane 이모지를 통째로 잘라내 빈 칸으로 보이는 이슈가 있어 제거.
# 시각 구분은 header_bg + border-top color 만으로 유지한다.
# 연간 절감 목표(섹션 2)가 적용되는 진짜 '원단위' 3종. SUM(사용량)/SUM(생산톤) 구조.
INTENSITY_METRICS = [
    {"key": "power",      "label": "전력 원단위", "unit": "kWh/ton",
     "color": "#F6C90E", "header_bg": "#FDF4CF", "cell_bg": "#FEFAEC",
     "usage_col": "total_power_kwh", "unit_col": "power_per_ton_kwh",   "target_key": "power_per_ton",
     "decimals": 2, "invert": False},
    {"key": "fuel",       "label": "연료 원단위", "unit": "Nm³/ton",
     "color": "#E8450A", "header_bg": "#FADACE", "cell_bg": "#FDF0EB",
     "usage_col": "fuel_nm3",        "unit_col": "fuel_per_ton_nm3",    "target_key": "fuel_per_ton",
     "decimals": 2, "invert": False},
    {"key": "water",      "label": "용수 원단위", "unit": "ton/ton",
     "color": "#0EA5E9", "header_bg": "#CFEDFB", "cell_bg": "#ECF7FD",
     "usage_col": "water_ton",       "unit_col": "water_per_ton_ton",   "target_key": "water_per_ton",
     "decimals": 2, "invert": False},
]

# 폐수/용수 = 폐수량 / 용수량 (소수점 비). '원단위'(사용량/생산톤)가 아닌 비이므로
# 연간 절감 목표 대상에서 제외(target_key=None)하고 사업장별 표에만 노출한다.
# unit_col 은 _aggregate_weighted 가 별도로 채우는 파생 키 'wastewater_ratio'.
WASTEWATER_RATIO_METRIC = {
    "key": "wastewater_ratio", "label": "폐수/용수", "unit": "폐수량/용수량",
    "color": "#6B7280", "header_bg": "#E1E3E6", "cell_bg": "#F3F4F5",
    "usage_col": None, "unit_col": "wastewater_ratio", "target_key": None,
    "decimals": 2, "invert": False,
}

# 생산량 — 원단위와 부호 해석이 반대이며(증가=개선) 5% 절감 목표 대상이 아니므로
# 연간 목표 달성률 섹션(_build_target_progress)에는 포함하지 않고, 사업장별 표에만
# 5번째 컬럼으로 노출한다. 원단위는 고정부하 영향으로 생산량 변동분에 종속적이라
# 같은 표에서 함께 봐야 해석 가능 (생산↓ → 고정부하 분담 ↑ → 원단위 악화 등).
PRODUCTION_METRIC = {
    "key": "production", "label": "생산량", "unit": "ton",
    # 메로나 연녹색 — 대시보드 PROD_COLOR와 동기 (#A4D65E).
    # header_bg / cell_bg = 상징색을 흰색에 ~20% / ~6% 혼합 (다른 메트릭과 동일 톤 규칙).
    "color": "#A4D65E", "header_bg": "#EDF6DE", "cell_bg": "#FAFCF5",
    "usage_col": "mix_prod_kg", "unit_col": "production_ton", "target_key": None,
    "decimals": 0, "invert": True,
}

# 사업장별 표(섹션 1) 컬럼 정의 — 생산량을 맨 왼쪽에 배치해 원단위 해석의 기준값
# (고정부하 분담의 분모)이 먼저 눈에 들어오도록 하고, 폐수/용수 비를 맨 오른쪽에 둔다.
FACTORY_TABLE_METRICS = [PRODUCTION_METRIC] + INTENSITY_METRICS + [WASTEWATER_RATIO_METRIC]

# v5.2 정상범주 상한 초과 감지 — 기준일 1일치만, 5개 실공장 모두 확인
# (이상 판정은 더 이상 MAPE 임계가 아니라 "실측 > P95" 정성적 기준)
PHYSICAL_FACTORIES_FOR_EXCEEDANCE: List[str] = [
    "남양주1", "남양주2", "김해", "광주", "논산",
]

# v5.1(점추정) 폴백 임계 — pred_p95가 NULL인 행은 (legacy) 메일에서 표시 안 함.
# v5.2 모델 활성 + 충분한 예측 이력이 쌓이면 자연스럽게 폴백 케이스가 사라짐.


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


def _fetch_p95_exceedances(ref_date: date) -> List[dict]:
    """기준일 1일치, 5개 실공장에서 실측이 v5.2 정상범주 상한(P95)을 넘은 항목 조회.

    판정 기준 (v5.2 정성적):
      - prediction_log.pred_p95 가 존재 (= v5.2 예측이 저장된 행)
      - actual_value > pred_p95
    심각도 순(band_position 내림차순) 정렬.

    v5.1 legacy 행(pred_p95 NULL)은 본 메일에서 표시하지 않음 — 정상범주 개념이
    없는 이력은 "P95 초과" 라는 정의 자체가 성립하지 않으므로 의미 없음.
    """
    placeholders = ", ".join(["%s"] * len(PHYSICAL_FACTORIES_FOR_EXCEEDANCE))
    sql = f"""
        SELECT factory, pred_date, target,
               pred_value, pred_p05, pred_p95,
               actual_value, band_position, mape
        FROM prediction_log
        WHERE pred_date = %s
          AND factory IN ({placeholders})
          AND actual_value IS NOT NULL
          AND pred_p95 IS NOT NULL
          AND actual_value > pred_p95
        ORDER BY band_position DESC, factory ASC, target ASC
    """
    params: tuple = (ref_date.strftime("%Y-%m-%d"), *PHYSICAL_FACTORIES_FOR_EXCEEDANCE)
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()
        conn.close()


def _fetch_p95_coverage_summary(ref_date: date) -> dict:
    """기준일 5개 실공장의 P95 커버리지 요약 (얼마나 v5.2 예측이 있는지).

    Returns:
        {
          "expected": 15,                 # 5공장 × 3타겟
          "with_band": N,                 # pred_p95가 있는 행 수
          "exceeded":  M,                 # P95 초과 행 수
          "missing_factory_targets": [...]# v5.2 예측 없는 공장·타겟 페어
        }
    """
    placeholders = ", ".join(["%s"] * len(PHYSICAL_FACTORIES_FOR_EXCEEDANCE))
    sql = f"""
        SELECT factory, target,
               pred_p95 IS NOT NULL AS has_band,
               (actual_value IS NOT NULL AND pred_p95 IS NOT NULL
                AND actual_value > pred_p95) AS exceeded
        FROM prediction_log
        WHERE pred_date = %s
          AND factory IN ({placeholders})
    """
    params: tuple = (ref_date.strftime("%Y-%m-%d"), *PHYSICAL_FACTORIES_FOR_EXCEEDANCE)
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = list(cursor.fetchall())
    finally:
        cursor.close()
        conn.close()

    expected_pairs = {
        (f, t) for f in PHYSICAL_FACTORIES_FOR_EXCEEDANCE for t in ("전력", "연료", "용수")
    }
    present_pairs = {(r["factory"], r["target"]) for r in rows if r.get("has_band")}
    return {
        "expected": len(expected_pairs),
        "with_band": len(present_pairs),
        "exceeded":  sum(1 for r in rows if r.get("exceeded")),
        "missing_factory_targets": sorted(expected_pairs - present_pairs),
    }


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


def _yoy_window(ref_date: date) -> date:
    """1년 전 동일 일자. 윤년 (2/29)은 2/28 로 안전 폴백."""
    try:
        return ref_date.replace(year=ref_date.year - 1)
    except ValueError:
        return ref_date - timedelta(days=365)


# ─────────────────────────────────────────────────────────────────────────────
# 섹션 빌더
# ─────────────────────────────────────────────────────────────────────────────
def _filter_rows_by_factory(
    rows: List[dict], factory_codes: Optional[List[str]]
) -> List[dict]:
    """factory_codes=None 이면 전체 행 그대로. 리스트면 해당 raw factory 코드만."""
    if factory_codes is None:
        return rows
    code_set = set(factory_codes)
    return [r for r in rows if r["factory"] in code_set]


def _build_factory_rate_rows(
    rows_mtd_curr: List[dict],
    rows_mtd_prev: List[dict],
    rows_ytd_curr: List[dict],
    rows_ytd_prev: List[dict],
) -> List[dict]:
    """사업장별 원단위 표 행 데이터.

    행 = FACTORY_DISPLAY_ORDER (전사/남양주/김해/광주/논산)
    각 행의 각 메트릭 셀 = {mtd_value, mtd_delta, mtd_color, ytd_value, ytd_delta, ytd_color}
    """
    table_rows = []
    for label, codes in FACTORY_DISPLAY_ORDER:
        m_curr = _aggregate_weighted(_filter_rows_by_factory(rows_mtd_curr, codes))
        m_prev = _aggregate_weighted(_filter_rows_by_factory(rows_mtd_prev, codes))
        y_curr = _aggregate_weighted(_filter_rows_by_factory(rows_ytd_curr, codes))
        y_prev = _aggregate_weighted(_filter_rows_by_factory(rows_ytd_prev, codes))

        cells = []
        for m in FACTORY_TABLE_METRICS:
            cm = m_curr.get(m["unit_col"]) if m_curr else None
            pm = m_prev.get(m["unit_col"]) if m_prev else None
            cy = y_curr.get(m["unit_col"]) if y_curr else None
            py = y_prev.get(m["unit_col"]) if y_prev else None
            invert = m.get("invert", False)
            decimals = m.get("decimals", 2)
            m_txt, m_color, _ = _pct_delta(cm, pm, invert=invert)
            y_txt, y_color, _ = _pct_delta(cy, py, invert=invert)
            cells.append({
                "mtd_value": _fmt(cm, decimals),
                "mtd_delta": m_txt,
                "mtd_color": m_color,
                "ytd_value": _fmt(cy, decimals),
                "ytd_delta": y_txt,
                "ytd_color": y_color,
            })
        table_rows.append({
            "factory": label,
            "is_total": codes is None,   # 전사 행은 별도 강조 가능
            "cells":   cells,
        })
    return table_rows


# 연간 절감 목표 — 전사 기준 4대 원단위 모두 '전년 전체 누계 대비 5% 절감'.
# 2026년 목표값 = 2025년 전체 누계(1/1~12/31) × YTD_TARGET_FACTOR.
# 분모로 "전년 동기간 YTD" 가 아닌 "전년 전체 누계" 를 쓰는 이유: 연간 목표는
# 그 해 1년 전체 운영 결과를 기준으로 세우는 것이 회계·관리 컨벤션과 맞고,
# 동기간 YTD 를 쓰면 연중 시점에 따라 목표값이 흔들리는 부작용이 있다.
YTD_TARGET_FACTOR = 0.95


def _build_target_progress(
    ref_date: date,
    ytd_curr: Optional[dict],
    prev_year_full: Optional[dict],
) -> List[dict]:
    """전사(ALL) 기준 4대 원단위 연간 목표 달성률 (YTD 누계 기준).

    목표값  = 전년 전체 누계 × 0.95     (전년 대비 5% 절감)
    달성률  = 현재 YTD ÷ 목표값 × 100
      · ≤ 100% : 목표 달성 (원단위는 낮을수록 좋음)
      · > 100% : 목표 미달 — 절감 부족
    """
    if not ytd_curr or not prev_year_full:
        return []

    out = []
    for m in INTENSITY_METRICS:
        curr_v = ytd_curr.get(m["unit_col"])
        prev_v = prev_year_full.get(m["unit_col"])
        if curr_v is None or prev_v in (None, 0):
            continue

        target_value = float(prev_v) * YTD_TARGET_FACTOR
        if target_value == 0:
            continue
        ratio = float(curr_v) / target_value * 100.0   # YTD / 목표

        # 색상 (원단위는 낮을수록 좋음 → ratio가 작을수록 좋음)
        if ratio <= 100.0:
            color = "#10b981"   # 녹색: 목표 달성
        elif ratio <= 105.0:
            color = "#d97706"   # 앰버: 5% 이내 초과 — 진행 중
        else:
            color = "#dc2626"   # 빨강: 5% 초과 — 절감 미흡

        out.append({
            "label":        m["label"],
            "unit":         m["unit"],
            "prev_value":   float(prev_v),
            "target_value": target_value,
            "actual_value": float(curr_v),
            "achievement":  ratio,                              # YTD ÷ 목표 × 100
            "color":        color,
            "bar_width":    max(0.0, min(100.0, ratio)),        # 100% = 목표선
        })
    return out


def _build_p95_exceedance_items(rows: List[dict]) -> List[dict]:
    """AI 예상 사용 범위의 윗선을 실측이 초과한 항목을 메일 표 행으로 변환.

    각 행:
      - 공장 / 항목
      - AI 예상 정상 범위 (예측 하한~상한)
      - 실측값
      - 초과량 / 상한 대비 초과율
      - 이탈 등급(경미/주의/심각) — band_position 기반
    """
    out = []
    for r in rows:
        p05_raw = r.get("pred_p05")
        p05 = float(p05_raw) if p05_raw is not None else None
        p95 = float(r["pred_p95"])
        actual_v = float(r["actual_value"])

        over_amount = actual_v - p95
        over_pct = over_amount / max(p95, 1.0) * 100.0  # 상한 대비 초과 비율
        bp_raw = r.get("band_position")
        bp = float(bp_raw) if bp_raw is not None else None

        # 이탈 등급 (비전공자 친화 라벨): |bp| 기반 3단계
        if bp is None:
            level_label, color = "—", "#ea580c"
        elif abs(bp) >= 2.0:
            level_label, color = "심각", "#dc2626"   # 빨강
        elif abs(bp) >= 1.5:
            level_label, color = "주의", "#ea580c"   # 주황
        else:
            level_label, color = "경미", "#d97706"   # 앰버

        out.append({
            "factory":   r["factory"],
            "target":    r["target"],
            "band":      f"{p05:,.0f} ~ {p95:,.0f}" if p05 is not None else f"≤ {p95:,.0f}",
            "p95":       _fmt(p95, 0),
            "actual":    _fmt(actual_v, 0),
            "over":      f"+{over_amount:,.0f}",
            "over_pct":  f"+{over_pct:.1f}%",
            "level":     level_label,
            "color":     color,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 차트 (전력 원단위 7일 추이 1장만 — 메일 분량 압축)
# ─────────────────────────────────────────────────────────────────────────────
def _try_make_weekly_trend_chart(
    rows_weekly: List[dict],
    title: str,
) -> Optional[bytes]:
    """공장별 전력 원단위 7일 일자 추이 PNG. plotly/kaleido 없으면 None."""
    try:
        import plotly.graph_objects as go      # type: ignore
    except ImportError:
        log.warning("plotly 미설치 → 차트 생략")
        return None

    power_metric = next(m for m in INTENSITY_METRICS if m["key"] == "power")
    by_factory: Dict[str, List[Tuple[date, float]]] = {}
    for r in rows_weekly:
        prod_kg = float(r.get("mix_prod_kg") or 0)
        if prod_kg <= 0:
            continue
        intensity = float(r.get(power_metric["usage_col"]) or 0) / (prod_kg / 1000.0)
        d = r["date"]
        if isinstance(d, str):
            d = datetime.strptime(d, "%Y-%m-%d").date()
        by_factory.setdefault(r["factory"], []).append((d, intensity))

    if not by_factory:
        return None

    fig = go.Figure()
    palette = ["#2563eb", "#f97316", "#7b2ff7", "#10b981", "#ecc94b", "#ef4444"]
    for i, (fac, series) in enumerate(sorted(by_factory.items())):
        series.sort(key=lambda x: x[0])
        fig.add_trace(go.Scatter(
            x=[s[0] for s in series],
            y=[s[1] for s in series],
            mode="lines+markers",
            name=FACTORY_LABELS.get(fac, fac),
            line=dict(width=2, color=palette[i % len(palette)]),
        ))

    fig.update_layout(
        title=dict(text=title, x=0.02, font=dict(size=13, color="#1f2937")),
        height=280,
        margin=dict(l=40, r=20, t=44, b=36),
        paper_bgcolor="white", plot_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=10)),
        font=dict(color="#1f2937", size=11),
        xaxis=dict(showgrid=True, gridcolor="#e5e7eb", title=""),
        yaxis=dict(showgrid=True, gridcolor="#e5e7eb", title=power_metric["unit"]),
    )
    try:
        return fig.to_image(format="png", width=760, height=280, scale=2)
    except Exception as e:
        log.warning(f"차트 PNG 변환 실패(kaleido 미설치 가능): {e}")
        return None


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

    # 비교 기간 계산
    weekly_from   = ref_date - timedelta(days=6)
    weekly_to     = ref_date
    mtd_from      = date(ref_date.year, ref_date.month, 1)
    mtd_to        = ref_date
    mtd_y_from    = _yoy_window(mtd_from)
    mtd_y_to      = _yoy_window(mtd_to)
    ytd_from      = date(ref_date.year, 1, 1)
    ytd_to        = ref_date
    ytd_y_from    = _yoy_window(ytd_from)
    ytd_y_to      = _yoy_window(ytd_to)
    # 연간 목표(섹션 2) 계산용 — 전년 전체 누계(1/1 ~ 12/31).
    # 사업장별 표(섹션 1)의 YTD 전년비는 여전히 "전년 동기간 YTD"(ytd_y_*)를 사용.
    prev_year_from = date(ref_date.year - 1, 1, 1)
    prev_year_to   = date(ref_date.year - 1, 12, 31)

    log.info(
        f"리포트 빌더 v4 시작 - 기준일: {ref_date} "
        f"(MTD: {mtd_from}~{mtd_to}, YTD: {ytd_from}~{ytd_to}, "
        f"전년 전체: {prev_year_from}~{prev_year_to})"
    )

    # 데이터 페치
    rows_mtd           = _fetch_rows_range(mtd_from, mtd_to, factories_filter)
    rows_mtd_y         = _fetch_rows_range(mtd_y_from, mtd_y_to, factories_filter)
    rows_ytd           = _fetch_rows_range(ytd_from, ytd_to, factories_filter)
    rows_ytd_y         = _fetch_rows_range(ytd_y_from, ytd_y_to, factories_filter)
    rows_prev_year_full = _fetch_rows_range(prev_year_from, prev_year_to, factories_filter)
    p95_exceed_rows    = _fetch_p95_exceedances(ref_date)
    p95_summary        = _fetch_p95_coverage_summary(ref_date)

    # 섹션 데이터
    mtd_curr       = _aggregate_weighted(rows_mtd)
    ytd_curr       = _aggregate_weighted(rows_ytd)
    prev_year_full = _aggregate_weighted(rows_prev_year_full)

    factory_rate_rows = _build_factory_rate_rows(
        rows_mtd, rows_mtd_y, rows_ytd, rows_ytd_y,
    )
    target_progress = _build_target_progress(ref_date, ytd_curr, prev_year_full)
    p95_items       = _build_p95_exceedance_items(p95_exceed_rows)

    # 추이 차트는 메일 본문에서 제외 — HTML 템플릿이 더 이상 참조하지 않으므로
    # PNG 생성/inline 첨부도 생략 (참조 없는 orphan 첨부 방지).
    inline_images: List[InlineImage] = []
    chart_cid = None

    # 제목 (verdict 제거 — 가벼운 알림 톤)
    n_curr_factories = mtd_curr.get("n_factories", 0) if mtd_curr else 0
    subject = f"[생산기술팀] 일일 에너지 원단위 alert {ref_date} ({WEEKDAY_KR[ref_date.weekday()]})"

    # 템플릿 렌더
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("daily_energy_report.html")
    html = template.render(
        subject            = subject,
        ref_date           = ref_date.strftime("%Y-%m-%d"),
        ref_year_short     = ref_date.strftime("%y"),   # 사업장별 표 서브헤더("'YY년 누계")
        ref_weekday        = WEEKDAY_KR[ref_date.weekday()],
        generated_at       = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        weekly_from_str    = weekly_from.strftime("%m/%d"),
        weekly_to_str      = weekly_to.strftime("%m/%d"),
        mtd_from_str       = mtd_from.strftime("%m/%d"),
        mtd_to_str         = mtd_to.strftime("%m/%d"),
        ytd_from_str       = ytd_from.strftime("%m/%d"),
        ytd_to_str         = ytd_to.strftime("%m/%d"),
        n_factories        = n_curr_factories,
        factory_rate_rows  = factory_rate_rows,
        factory_table_metrics = FACTORY_TABLE_METRICS,
        target_progress    = target_progress,
        # v5.2 정상범주 상한 초과 섹션
        p95_items          = p95_items,
        n_p95_exceed       = p95_summary["exceeded"],
        n_with_band        = p95_summary["with_band"],
        n_expected_band    = p95_summary["expected"],
        missing_band_pairs = p95_summary["missing_factory_targets"],
        chart_cid          = chart_cid,
    )

    log.info(
        f"리포트 v5 생성 완료 - 표 {len(factory_rate_rows)}행, "
        f"P95 초과 {p95_summary['exceeded']}건 "
        f"(밴드 보유 {p95_summary['with_band']}/{p95_summary['expected']}), "
        f"목표 {len(target_progress)}건, 차트={'O' if chart_cid else 'X'}"
    )

    return BuiltReport(
        subject=subject,
        html=html,
        inline_images=inline_images,
        ref_date=ref_date,
        record_count=n_curr_factories,
    )
