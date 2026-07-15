"""
Production Performance Page (DB-backed)
========================================
production_daily 테이블 기반 월별 생산실적 분석.

분류 모델 — 두 차원이 독립:
  category1 (보관유형) : 냉동, 냉장, 상온
  category2 (제품유형) : IC=Ice Cream, MY=Milk & Yogurt, FM=Fermented Milk, SN=Snack
  예) 김해 멸균 유음료 = (category1=상온, category2=MY)

기능:
  - 공장 / 보관유형(category1) / 제품유형(category2) 다중 필터
  - KPI(누계 계획·실적·월계획 달성률·기대 진도 대비·월말 착지 예상)
  - Burn-up S-커브 (누적 실적 vs 영업일 기준 계획 페이스)
  - 제품유형 비중 도넛
  - 일별 추이 라인 (제품유형별)
  - 계획 미달/초과 Top 품목
  - 인사이트 + 월별 추이 + 상세 테이블
"""
# 이 파일은 생산실적을 DB(production_daily)에서 조회해 시각화합니다.
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.domain.factories import (
    FACTORY_CODE_TO_KR,
    FACTORY_KR_TO_CODE,
    NAMYANGJU_PARENT_CODE,
)
from app.services.production_dw_service import (
    query_distinct_items,
    query_monthly_summary,
    query_production_daily,
    query_production_date_range,
    query_production_range,
)
from app.utils.df_format import numeric_column_config
from app.utils.page_common import csv_download, section_tone
from app.utils.page_state import persist_many
from app.components.production_modes import render_range_production_view


# ── 색상/라벨 매핑 ────────────────────────────────────────────
# category2 코드 → 화면 표시 라벨 (괄호로 한글 병기)
_CAT2_DISPLAY = {
    "IC": "IC (아이스크림)",
    "MY": "MY (유음료)",
    "FM": "FM (발효유)",
    "SN": "SN (스낵)",
}

# 차트 색상 (category2 기준)
_CAT2_COLORS = {
    "IC": "#7dd3fc",  # sky-300
    "MY": "#fbbf24",  # amber-400
    "FM": "#a78bfa",  # violet-400
    "SN": "#fb923c",  # orange-400
}

# category1 색상 (참고용)
_CAT1_COLORS = {
    "냉동": "#7dd3fc",
    "냉장": "#a78bfa",
    "상온": "#fb923c",
}

# 공장 코드 → 표시명 매핑 (production_daily 는 F-코드로 저장돼 있어 UI 표기 변환 필요)
# 남양주는 사내 DW 추출 시점에는 F10 통합이지만 production_dw_service.parse_sheet 가
# (냉장+MY → F10A, 그 외 → F10B) 룰로 자동 분리하므로 DB 에는 F10A/F10B 로 적재됨.
# 과거 데이터(아직 재동기화 전)에는 F10 코드가 남아있을 수 있어 fallback 매핑도 유지.
_FACTORY_DISPLAY_MAP = {**FACTORY_CODE_TO_KR, NAMYANGJU_PARENT_CODE: "남양주"}
_DISPLAY_TO_FACTORY_CODE = {**FACTORY_KR_TO_CODE, "남양주": NAMYANGJU_PARENT_CODE}


def _factory_display(code: str) -> str:
    """공장 코드(또는 이미 한글 이름)를 화면 표시용 한글로 변환."""
    return _FACTORY_DISPLAY_MAP.get(code, code)


# 테마 색 — 다크 모드 단일 (main.py DARK_VARS와 동기화) ──────
def _theme_colors():
    return dict(
        TEXT_PRIMARY="#e9f0fb",
        TEXT_SECONDARY="#9db1cf",
        ACCENT="#38bdf8",
        GRID="rgba(120,160,220,0.14)",
    )


# ── 캐시된 DB 쿼리 래퍼 ──────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def _load_month(
    year: int, month: int,
    factories: tuple[str, ...],
    cat1_vals: tuple[str, ...],
    cat2_vals: tuple[str | None, ...],
) -> pd.DataFrame:
    return query_production_daily(
        year, month,
        factories=list(factories) if factories else None,
        category1_values=list(cat1_vals) if cat1_vals else None,
        category2_values=list(cat2_vals) if cat2_vals else None,
    )


@st.cache_data(ttl=600, show_spinner=False)
def _load_range(
    date_from: str,
    date_to: str,
    factories: tuple[str, ...],
    cat1_vals: tuple[str, ...],
    cat2_vals: tuple[str | None, ...],
) -> pd.DataFrame:
    return query_production_range(
        date_from,
        date_to,
        factories=list(factories) if factories else None,
        category1_values=list(cat1_vals) if cat1_vals else None,
        category2_values=list(cat2_vals) if cat2_vals else None,
    )


@st.cache_data(ttl=600, show_spinner=False)
def _load_production_bounds() -> tuple[str | None, str | None]:
    return query_production_date_range()

@st.cache_data(ttl=600, show_spinner=False)
def _load_annual_factory_cat2(
    year: int,
    factories: tuple[str, ...],
    cat1_vals: tuple[str, ...],
    cat2_vals: tuple[str | None, ...],
) -> pd.DataFrame:
    """당해년·전년 1~12월 (factory × category2) 월별 실적 — 전년비 분석용."""
    return query_monthly_summary(
        year - 1, 1, year, 12,
        factories=list(factories) if factories else None,
        category1_values=list(cat1_vals) if cat1_vals else None,
        category2_values=list(cat2_vals) if cat2_vals else None,
        by=("factory", "category2"),
    )


@st.cache_data(ttl=600, show_spinner=False)
def _load_combos() -> pd.DataFrame:
    """가용 (factory × category1 × category2) 조합 — 필터 옵션 채우기용."""
    return query_distinct_items()


# ── KPI 계산 ──────────────────────────────────────────────────
def _calc_summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total_planned": 0.0, "total_actual": 0.0, "progress_pct": 0.0,
                "items_count": 0, "cat2_breakdown": {}}
    df_yr = df.assign(y=df["date"].dt.year, m=df["date"].dt.month)
    # plan distinct 키에 factory 포함 — 같은 item_code 가 여러 공장에서 생산되는 경우(예: 110388 → 김해+논산)
    # factory를 빼면 한 공장의 plan만 살아남아 합계가 누락됨
    plan_unique_rows = df_yr.drop_duplicates(["item_code", "factory", "y", "m"])
    plan_distinct = plan_unique_rows["planned_qty"].sum()
    actual_sum = df["actual_qty"].sum()
    pct = (actual_sum / plan_distinct * 100.0) if plan_distinct > 0 else 0.0

    cat2_actual = df.groupby("category2", dropna=False)["actual_qty"].sum()
    cat2_plan = plan_unique_rows.groupby("category2", dropna=False)["planned_qty"].sum()
    breakdown = {}
    for key in set(cat2_actual.index) | set(cat2_plan.index):
        a = float(cat2_actual.get(key, 0.0))
        p = float(cat2_plan.get(key, 0.0))
        breakdown[key if key is not None else "(미분류)"] = {
            "actual": a, "planned": p,
            "progress_pct": (a / p * 100.0) if p > 0 else 0.0,
        }

    return {
        "total_planned": float(plan_distinct),
        "total_actual": float(actual_sum),
        "progress_pct": float(pct),
        "items_count": int(df["item_code"].nunique()),
        "cat2_breakdown": breakdown,
    }


def _business_day_context(year: int, month: int, as_of: datetime) -> dict[str, float | int]:
    """Business-day progress context for the selected month (Mon-Fri basis)."""
    last_day = calendar.monthrange(year, month)[1]
    days = pd.date_range(f"{year}-{month:02d}-01", periods=last_day, freq="D")
    is_business = days.weekday < 5
    total_business = int(is_business.sum()) or last_day

    as_of_date = as_of.date()
    if as_of_date < days[0].date():
        elapsed_business = 0
    elif as_of_date > days[-1].date():
        elapsed_business = total_business
    else:
        elapsed_business = int(((days <= pd.Timestamp(as_of_date)) & is_business).sum())

    elapsed_ratio = (elapsed_business / total_business) if total_business > 0 else 0.0
    return {
        "elapsed_business_days": elapsed_business,
        "total_business_days": total_business,
        "elapsed_ratio": elapsed_ratio,
    }


def _calc_pace_summary(summary: dict, year: int, month: int, as_of: datetime) -> dict[str, float | int]:
    """Calculate pace-adjusted achievement and month-end forecast."""
    ctx = _business_day_context(year, month, as_of)
    planned = float(summary.get("total_planned", 0.0) or 0.0)
    actual = float(summary.get("total_actual", 0.0) or 0.0)
    elapsed_ratio = float(ctx["elapsed_ratio"])
    expected_to_date = planned * elapsed_ratio
    pace_pct = (actual / expected_to_date * 100.0) if expected_to_date > 0 else 0.0
    forecast_actual = (actual / elapsed_ratio) if elapsed_ratio > 0 else 0.0
    ctx.update({
        "expected_to_date": expected_to_date,
        "pace_pct": pace_pct,
        "forecast_actual": forecast_actual,
    })
    return ctx


def _build_burnup_curve(df: pd.DataFrame, year: int, month: int, total_plan: float) -> pd.DataFrame:
    """Daily cumulative actual vs business-day-distributed monthly plan."""
    last_day = calendar.monthrange(year, month)[1]
    days = pd.date_range(f"{year}-{month:02d}-01", periods=last_day, freq="D")
    out = pd.DataFrame({"date": days})
    out["day"] = out["date"].dt.day

    actual_by_day = (
        df.assign(day=df["date"].dt.day)
        .groupby("day")["actual_qty"].sum()
    )
    out["daily_actual"] = out["day"].map(actual_by_day).fillna(0.0)
    out["cum_actual"] = out["daily_actual"].cumsum()

    is_business = out["date"].dt.weekday < 5
    business_days = int(is_business.sum()) or last_day
    plan_per_business_day = (float(total_plan) / business_days) if business_days > 0 else 0.0
    out["daily_plan_pace"] = 0.0
    out.loc[is_business, "daily_plan_pace"] = plan_per_business_day
    out["cum_plan_pace"] = out["daily_plan_pace"].cumsum()
    return out


def _generate_insights(df: pd.DataFrame, summary: dict) -> list[str]:
    msgs: list[str] = []
    tp, ta, pct = summary["total_planned"], summary["total_actual"], summary["progress_pct"]
    if tp <= 0:
        msgs.append(f"📊 누계 실적: **{ta:,.0f}** (계획 데이터 없음)")
    elif pct >= 100:
        msgs.append(f"🎯 누계 진척률 **{pct:.1f}%** — 계획 초과 달성")
    elif pct >= 90:
        msgs.append(f"✅ 누계 진척률 **{pct:.1f}%** — 정상 추세")
    elif pct >= 70:
        msgs.append(f"🟡 누계 진척률 **{pct:.1f}%** — 잔여 기간 주의")
    else:
        msgs.append(f"⚠️ 누계 진척률 **{pct:.1f}%** — 가속 필요")

    cb = summary.get("cat2_breakdown", {})
    if cb:
        ranked = sorted(cb.items(), key=lambda x: x[1]["actual"], reverse=True)
        top_key, top_info = ranked[0]
        msgs.append(
            f"🏭 최대 제품유형: **{top_key}** "
            f"(실적 {top_info['actual']:,.0f}"
            + (f", 진척 {top_info['progress_pct']:.1f}%" if top_info['planned'] > 0 else "")
            + ")"
        )
        worst = min(
            (it for it in cb.items() if it[1]["planned"] > 0),
            key=lambda x: x[1]["progress_pct"], default=None,
        )
        if worst and worst[1]["progress_pct"] < 80:
            msgs.append(f"📉 부진 제품유형: **{worst[0]}** (진척 {worst[1]['progress_pct']:.1f}%)")
    return msgs


# ─────────────────────────────────────────────────────────────
def render_production_performance():
    """생산실적 페이지 (DB 기반, 두 차원 독립 분류)."""
    # 페이지 이동 후 재방문에도 필터 값을 유지
    persist_many({
        "prod_mode_db":       None,
        "prod_year_db":       None,
        "prod_month_db":      None,
        "prod_start_date_db": None,
        "prod_end_date_db":   None,
        "prod_fac_db":        None,
        "prod_cat1_db":       None,
        "prod_cat2_db":       None,
        "prod_energy_fac_db": None,
        "prod_energy_cat_db": None,
        "prod_energy_src_db": None,
        "prod_range_energy_src_db": None,
    })

    _t = _theme_colors()

    st.markdown(
        """
        <div class="sub-page-header">
            <span style="font-size:1.5rem;">🏭</span>
            <div>
                <div class="sub-page-title">생산 실적</div>
                <div class="sub-page-breadcrumb">생산실적 > 월별 일일 생산 분석 (DB)</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    today = datetime.today()

    try:
        combos = _load_combos()
    except Exception as exc:
        st.error(f"DB 조회 실패: {exc}\n\nproduction_daily 테이블이 비어 있을 수 있습니다. "
                 "`py -3 tools/mis_rpa/build_production_dataset.py` 후 앱 재시작으로 동기화하세요.")
        return

    if combos.empty:
        st.warning("📂 production_daily 테이블에 데이터가 없습니다.\n\n"
                   "1) `py -3 tools/mis_rpa/build_production_dataset.py` 로 통합 파일 생성\n"
                   "2) 앱을 재시작하면 자동 동기화됩니다.")
        return

    factories_all = sorted(combos["factory"].unique())
    cat1_all = sorted(combos["category1"].unique())
    cat2_raw = combos["category2"].unique()
    cat2_all = sorted([s for s in cat2_raw if s is not None])
    has_null_cat2 = any(s is None for s in cat2_raw)

    prod_min, prod_max = _load_production_bounds()
    prod_min_date = pd.to_datetime(prod_min).date() if prod_min else datetime(today.year - 2, 1, 1).date()
    prod_max_date = pd.to_datetime(prod_max).date() if prod_max else today.date()
    min_year = prod_min_date.year
    max_year = max(prod_max_date.year, today.year)
    year_options = list(range(max_year, min_year - 1, -1))

    # ── 1) 조회 조건 ────────────────────────────────
    with st.container(border=True):
        section_tone("cyan")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">⚙️</span>조회 조건'
            '<span class="section-title-sub">월별 · 기간별 · 연간 · 공장 · 보관유형 · 제품유형</span>'
            "</div>", unsafe_allow_html=True,
        )
        mode = st.radio("조회 모드", ["월별", "기간별", "연간"], horizontal=True, key="prod_mode_db")
        default_year = today.year if today.year in year_options else max_year
        if st.session_state.get("prod_year_db") not in year_options:
            st.session_state["prod_year_db"] = default_year

        if mode == "월별":
            c1, c2, c3, c4, c5 = st.columns([0.6, 0.5, 1.3, 1.3, 1.3])
            with c1:
                year = st.selectbox("연도", options=year_options, index=year_options.index(default_year), key="prod_year_db")
            with c2:
                month = st.selectbox("월", options=list(range(1, 13)), index=today.month - 1, key="prod_month_db")
            date_from = f"{int(year)}-{int(month):02d}-01"
            date_to = f"{int(year)}-{int(month):02d}-{calendar.monthrange(int(year), int(month))[1]:02d}"
        elif mode == "기간별":
            c1, c2, c3, c4, c5 = st.columns([1.0, 1.0, 1.2, 1.2, 1.2])
            default_end = prod_max_date
            default_start = max(default_end - timedelta(days=30), prod_min_date)
            with c1:
                start_date = st.date_input("시작일", value=default_start, min_value=prod_min_date, max_value=prod_max_date, key="prod_start_date_db")
            with c2:
                end_date = st.date_input("종료일", value=default_end, min_value=prod_min_date, max_value=prod_max_date, key="prod_end_date_db")
            if start_date > end_date:
                st.warning("시작일이 종료일보다 늦어 종료일과 맞췄습니다.")
                start_date = end_date
            year = int(start_date.year)
            month = int(start_date.month)
            date_from = start_date.strftime("%Y-%m-%d")
            date_to = end_date.strftime("%Y-%m-%d")
        else:
            c1, c2, c3, c4, c5 = st.columns([0.7, 0.1, 1.3, 1.3, 1.3])
            with c1:
                year = st.selectbox("연도", options=year_options, index=year_options.index(default_year), key="prod_year_db")
            month = 1
            date_from = f"{int(year)}-01-01"
            date_to = f"{int(year)}-12-31"
        with c3:
            sel_factories = st.multiselect(
                "공장",
                options=factories_all,
                default=factories_all,
                key="prod_fac_db",
                format_func=_factory_display,
            )
        with c4:
            sel_cat1 = st.multiselect("보관유형 (category1)", options=cat1_all, default=cat1_all, key="prod_cat1_db")
        with c5:
            cat2_options = cat2_all + (["(미분류)"] if has_null_cat2 else [])
            sel_cat2_display = st.multiselect(
                "제품유형 (category2)",
                options=cat2_options,
                default=cat2_options,
                key="prod_cat2_db",
                help="IC=아이스크림, MY=유음료, FM=발효유, SN=스낵",
            )

    # 사용자 선택 → 쿼리 파라미터 변환
    cat2_query: list[str | None] = []
    for s in sel_cat2_display:
        if s == "(미분류)":
            cat2_query.append(None)
        else:
            cat2_query.append(s)

    if mode == "월별":
        df = _load_month(int(year), int(month),
                         tuple(sel_factories), tuple(sel_cat1), tuple(cat2_query))
    else:
        df = _load_range(date_from, date_to,
                         tuple(sel_factories), tuple(sel_cat1), tuple(cat2_query))
    if df.empty:
        period_label = f"{year}년 {month}월" if mode == "월별" else f"{date_from} ~ {date_to}"
        st.info(f"{period_label} — 선택한 필터 조건에 해당하는 데이터가 없습니다.")
        return

    summary = _calc_summary(df)

    if mode != "월별":
        render_range_production_view(
            mode=mode,
            df=df,
            summary=summary,
            date_from=date_from,
            date_to=date_to,
            selected_year=int(year),
            sel_factories=list(sel_factories),
            today=today,
            theme=_t,
        )
        return

    # ── 2) KPI ─────────────────────────────────────
    pace = _calc_pace_summary(summary, int(year), int(month), today)
    with st.container(border=True):
        section_tone("emerald")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📊</span>요약 KPI'
            f'<span class="section-title-sub">{year}년 {month}월 · 영업일 페이스 보정</span>'
            "</div>", unsafe_allow_html=True,
        )
        k1, k2, k3, k4, k5, k6 = st.columns([1.0, 1.0, 1.05, 1.25, 1.15, 0.8])
        with k1:
            if summary["total_planned"] > 0:
                st.metric("누계 계획", f"{summary['total_planned']:,.0f}")
            else:
                st.metric("누계 계획", "—")
        with k2:
            st.metric("누계 실적", f"{summary['total_actual']:,.0f}")
        with k3:
            if summary["total_planned"] > 0:
                delta_pct = summary["progress_pct"] - 100.0
                st.metric("월계획 달성률", f"{summary['progress_pct']:.1f}%",
                          delta=f"{delta_pct:+.1f}%p vs 월계획")
            else:
                st.metric("월계획 달성률", "N/A")
        with k4:
            if pace["expected_to_date"] > 0:
                pace_delta = float(pace["pace_pct"]) - 100.0
                pace_word = "앞섬" if pace_delta >= 0 else "뒤짐"
                st.metric("기대 진도 대비", f"{float(pace['pace_pct']):.1f}%",
                          delta=f"{pace_delta:+.1f}%p {pace_word}")
            else:
                st.metric("기대 진도 대비", "N/A")
        with k5:
            if pace["elapsed_ratio"] > 0:
                forecast = float(pace["forecast_actual"])
                forecast_delta = None
                if summary["total_planned"] > 0:
                    forecast_delta = f"{(forecast / summary['total_planned'] * 100.0 - 100.0):+.1f}% vs 계획"
                st.metric("월말 착지 예상", f"{forecast:,.0f}", delta=forecast_delta)
            else:
                st.metric("월말 착지 예상", "—")
        with k6:
            st.metric("품목 수", f"{summary['items_count']:,}")

        if summary["total_planned"] > 0:
            st.caption(
                f"영업일 기준 기대 누계: **{float(pace['expected_to_date']):,.0f}** · "
                f"경과 영업일 **{int(pace['elapsed_business_days'])}/{int(pace['total_business_days'])}일** "
                "(월~금 기준, 공휴일 미반영)"
            )

    # ── 2b) 이달 진도 Burn-up S-커브 ─────────────────
    with st.container(border=True):
        section_tone("violet")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📈</span>이달 진도 — Burn-up S-커브'
            '<span class="section-title-sub">누적 실적 vs 영업일 기준 누적 계획 페이스</span>'
            "</div>", unsafe_allow_html=True,
        )
        burn = _build_burnup_curve(df, int(year), int(month), summary["total_planned"])
        if burn.empty:
            st.info("표시할 일별 누적 데이터가 없습니다.")
        else:
            fig = go.Figure()
            if summary["total_planned"] > 0:
                fig.add_trace(go.Scatter(
                    name="누적 계획 페이스", x=burn["day"], y=burn["cum_plan_pace"],
                    mode="lines", line=dict(color="#64748b", width=3, dash="dash"),
                    hovertemplate="%{x}일<br>계획 페이스 %{y:,.0f}<extra></extra>",
                ))
            fig.add_trace(go.Scatter(
                name="누적 실적", x=burn["day"], y=burn["cum_actual"],
                mode="lines+markers", line=dict(color=_t["ACCENT"], width=3),
                marker=dict(size=6),
                hovertemplate="%{x}일<br>누적 실적 %{y:,.0f}<extra></extra>",
            ))
            if int(year) == today.year and int(month) == today.month:
                # 다크 배경에서 보이도록 밝은 색 사용 (기존 #0f172a는 배경색과 겹쳐 안 보였음)
                fig.add_vline(
                    x=today.day, line_width=1.5, line_dash="dot", line_color="#e9f0fb",
                    annotation_text="오늘", annotation_position="top",
                    annotation_font_color="#e9f0fb",
                )
            fig.update_layout(
                height=380, margin=dict(l=20, r=20, t=10, b=40),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=_t["TEXT_PRIMARY"]),
                legend=dict(orientation="h", y=1.10, x=0.5, xanchor="center"),
                xaxis=dict(title=dict(text="일", font=dict(color=_t["TEXT_PRIMARY"])),
                           gridcolor=_t["GRID"], tickfont=dict(color=_t["TEXT_PRIMARY"], size=16), dtick=3),
                yaxis=dict(title=dict(text="누적 생산량", font=dict(color=_t["TEXT_PRIMARY"])),
                           gridcolor=_t["GRID"], tickfont=dict(color=_t["TEXT_PRIMARY"], size=16),
                           tickformat="~s"),
            )
            st.plotly_chart(fig, use_container_width=True, key="burnup_pace_db")

            if summary["total_planned"] > 0 and pace["expected_to_date"] > 0:
                gap = summary["total_actual"] - float(pace["expected_to_date"])
                gap_word = "앞섬" if gap >= 0 else "뒤짐"
                st.caption(
                    f"현재 누계 실적은 계획 페이스 대비 **{abs(gap):,.0f} kg {gap_word}**입니다. "
                    f"월말 착지 예상: **{float(pace['forecast_actual']):,.0f} kg**"
                )

    # ── 3) 제품유형 비중 (도넛 2개) ─────────────────
    with st.container(border=True):
        section_tone("amber")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🍩</span>제품유형(category2)별 생산량 비중'
            '<span class="section-title-sub">실적 vs 계획</span>'
            "</div>", unsafe_allow_html=True,
        )
        cb = summary["cat2_breakdown"]
        if not cb:
            st.info("제품유형 데이터가 없습니다.")
        else:
            cat_data = pd.DataFrame([
                {"sub": k, "actual": v["actual"], "planned": v["planned"]}
                for k, v in cb.items()
            ])
            colors = [_CAT2_COLORS.get(c, "#94a3b8") for c in cat_data["sub"]]
            has_planned = bool(cat_data["planned"].sum() > 0)

            d1, d2 = (st.columns(2) if has_planned else (st.container(), None))
            # 작은 슬라이스가 잘리지 않도록 height 키우고 margin 여유, 범례 표시
            pie_layout = dict(
                height=400, margin=dict(l=20, r=20, t=50, b=60),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                showlegend=True,
                legend=dict(orientation="h", y=-0.05, x=0.5, xanchor="center",
                            font=dict(color=_t["TEXT_PRIMARY"], size=16)),
            )
            with d1:
                fig_a = go.Figure(go.Pie(
                    labels=[_CAT2_DISPLAY.get(c, c) for c in cat_data["sub"]],
                    values=cat_data["actual"], hole=0.55,
                    marker=dict(colors=colors), textinfo="percent", textposition="inside",
                    insidetextorientation="horizontal",
                    textfont=dict(color="#0f172a", size=18),
                ))
                fig_a.update_layout(
                    title=dict(text="누계 실적 비중", x=0.5, xanchor="center",
                               font=dict(color=_t["TEXT_PRIMARY"], size=20)),
                    **pie_layout,
                )
                st.plotly_chart(fig_a, use_container_width=True, key="cat2_pie_actual")
            if d2 is not None:
                with d2:
                    fig_p = go.Figure(go.Pie(
                        labels=[_CAT2_DISPLAY.get(c, c) for c in cat_data["sub"]],
                        values=cat_data["planned"], hole=0.55,
                        marker=dict(colors=colors), textinfo="percent", textposition="inside",
                        insidetextorientation="horizontal",
                        textfont=dict(color="#0f172a", size=18),
                    ))
                    fig_p.update_layout(
                        title=dict(text="누계 계획 비중", x=0.5, xanchor="center",
                                   font=dict(color=_t["TEXT_PRIMARY"], size=20)),
                        **pie_layout,
                    )
                    st.plotly_chart(fig_p, use_container_width=True, key="cat2_pie_planned")

    # ── 4) 일별 추이 ────────────────────────────────
    with st.container(border=True):
        section_tone("cyan")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📈</span>일별 생산량 추이 (제품유형별)'
            "</div>", unsafe_allow_html=True,
        )
        daily = (
            df.assign(day=df["date"].dt.day,
                      cat2_label=df["category2"].fillna("(미분류)"))
            .groupby(["day", "cat2_label"])["actual_qty"].sum().reset_index()
        )
        if daily.empty:
            st.info("일별 데이터가 없습니다.")
        else:
            fig = px.line(
                daily, x="day", y="actual_qty", color="cat2_label", markers=True,
                color_discrete_map=_CAT2_COLORS,
                labels={"day": "일", "actual_qty": "생산량", "cat2_label": "제품유형"},
            )
            fig.update_layout(
                height=380, margin=dict(l=20, r=20, t=10, b=40),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=_t["TEXT_PRIMARY"]),
                legend=dict(orientation="h", y=1.10, x=0.5, xanchor="center"),
                xaxis=dict(gridcolor=_t["GRID"], tickfont=dict(color=_t["TEXT_PRIMARY"], size=16),
                           dtick=3, title=dict(font=dict(color=_t["TEXT_PRIMARY"]))),
                yaxis=dict(gridcolor=_t["GRID"], tickfont=dict(color=_t["TEXT_PRIMARY"], size=16),
                           tickformat="~s",
                           title=dict(font=dict(color=_t["TEXT_PRIMARY"]))),
            )
            st.plotly_chart(fig, use_container_width=True, key="daily_trend_db")

        # 일별 상세 데이터
        with st.expander("📄 일별 상세 데이터 보기"):
            daily_detail = (
                df.assign(
                    날짜=df["date"].dt.strftime("%Y-%m-%d"),
                    cat2_label=df["category2"].fillna("(미분류)"),
                    공장=df["factory"].map(_factory_display),
                )
                .groupby(["날짜", "공장", "cat2_label"])["actual_qty"].sum()
                .reset_index()
                .rename(columns={"cat2_label": "제품유형", "actual_qty": "생산량"})
                .sort_values(["날짜", "공장"], ascending=[True, True])
            )
            st.dataframe(
                daily_detail, use_container_width=True, hide_index=True,
                column_config=numeric_column_config(daily_detail),
            )
            csv_download(
                daily_detail,
                filename=f"production_daily_{year}{int(month):02d}.csv",
                key="dl_prod_daily_detail",
            )

    # ── 5) 계획 미달/초과 품목 ─────────────────────
    with st.container(border=True):
        section_tone("rose")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🏅</span>계획 대비 품목 랭킹'
            '<span class="section-title-sub">미달 Top 10 · 초과 Top 10</span>'
            "</div>", unsafe_allow_html=True,
        )
        df_yr = df.assign(y=df["date"].dt.year, m=df["date"].dt.month)
        # plan distinct 키에 factory 포함 (같은 item이 여러 공장에 있으면 각 공장 plan 합산)
        plan_per_item = (
            df_yr.drop_duplicates(["item_code", "factory", "y", "m"])
            .groupby(["item_code"])["planned_qty"].sum().rename("planned")
        )
        actual_per_item = df.groupby(["item_code"])["actual_qty"].sum().rename("actual")
        names = df.drop_duplicates("item_code").set_index("item_code")["item_name"]
        cat2_per_item = df.drop_duplicates("item_code").set_index("item_code")["category2"].fillna("(미분류)")
        item_summary = pd.concat([names, cat2_per_item, plan_per_item, actual_per_item], axis=1).reset_index()
        item_summary.columns = ["item_code", "item_name", "cat2", "planned", "actual"]
        item_summary[["planned", "actual"]] = item_summary[["planned", "actual"]].fillna(0.0)
        item_summary["item_name"] = item_summary["item_name"].fillna("")
        item_summary["cat2"] = item_summary["cat2"].fillna("(미분류)")
        item_summary["variance"] = item_summary["actual"] - item_summary["planned"]
        item_summary["achievement_pct"] = item_summary.apply(
            lambda r: (r["actual"] / r["planned"] * 100.0) if r["planned"] > 0 else 0.0,
            axis=1,
        )

        under_top = (
            item_summary[(item_summary["planned"] > 0) & (item_summary["variance"] < 0)]
            .sort_values("variance", ascending=True)
            .head(10)
            .reset_index(drop=True)
        )
        over_top = (
            item_summary[item_summary["variance"] > 0]
            .sort_values("variance", ascending=False)
            .head(10)
            .reset_index(drop=True)
        )

        def _render_gap_items(tab_df: pd.DataFrame, *, key: str, empty_msg: str, actual_color: str) -> None:
            if tab_df.empty:
                st.info(empty_msg)
                return
            labels = [f"{r['item_code']} · {r['item_name']} ({r['cat2']})" for _, r in tab_df.iterrows()]
            fig = go.Figure()
            fig.add_trace(go.Bar(
                name="계획", y=labels, x=tab_df["planned"], orientation="h",
                marker_color="#94a3b8", text=[f"{v:,.0f}" for v in tab_df["planned"]],
                textposition="outside", textfont=dict(color=_t["TEXT_PRIMARY"], size=15),
            ))
            fig.add_trace(go.Bar(
                name="실적", y=labels, x=tab_df["actual"], orientation="h",
                marker_color=actual_color, text=[f"{v:,.0f}" for v in tab_df["actual"]],
                textposition="outside", textfont=dict(color=_t["TEXT_PRIMARY"], size=15),
            ))
            fig.update_layout(
                barmode="group",
                height=460, margin=dict(l=20, r=90, t=10, b=20),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=_t["TEXT_PRIMARY"]),
                xaxis=dict(gridcolor=_t["GRID"], tickformat="~s",
                           tickfont=dict(color=_t["TEXT_PRIMARY"], size=16)),
                yaxis=dict(autorange="reversed",
                           tickfont=dict(color=_t["TEXT_PRIMARY"], size=15)),
                legend=dict(orientation="h", y=1.05, x=0.5, xanchor="center"),
            )
            st.plotly_chart(fig, use_container_width=True, key=key)

            detail = tab_df[["item_code", "item_name", "cat2", "planned", "actual", "variance", "achievement_pct"]].rename(columns={
                "item_code": "Item Code", "item_name": "Item 명", "cat2": "제품유형",
                "planned": "누계 계획", "actual": "누계 실적", "variance": "편차", "achievement_pct": "달성률(%)",
            })
            with st.expander("📄 상세 데이터 보기"):
                st.dataframe(
                    detail, use_container_width=True, hide_index=True,
                    column_config=numeric_column_config(detail),
                )
                csv_download(detail, filename=f"item_ranking_{key}.csv", key=f"dl_{key}")

        under_tab, over_tab = st.tabs(["계획 미달 Top", "계획 초과 Top"])
        with under_tab:
            _render_gap_items(
                under_top, key="under_items_db",
                empty_msg="계획 대비 미달 품목이 없습니다.", actual_color="#ef4444",
            )
        with over_tab:
            _render_gap_items(
                over_top, key="over_items_db",
                empty_msg="계획 대비 초과 품목이 없습니다.", actual_color="#10b981",
            )

    # ── 6) 연간 월별 실적 — 월별 전년비 (공장·제품유형 single-select) ──
    with st.container(border=True):
        section_tone("violet")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📆</span>연간 월별 실적 — 전년비'
            f'<span class="section-title-sub">{year}년 vs {year - 1}년 · 월별 전년비</span>'
            "</div>", unsafe_allow_html=True,
        )

        # 이 섹션 전용 single-select 필터 (페이지 상단 멀티필터와 독립)
        fac_code_map = {_factory_display(f): f for f in factories_all}
        fac_opts = ["전체"] + list(fac_code_map.keys())
        fc1, fc2, _fc3 = st.columns([1, 1, 2])
        with fc1:
            sel_fac_one = st.selectbox("공장", fac_opts, key="annual_fac_one")

        # 선택한 공장이 실제 생산하는 제품유형만 노출 (전체 선택 시 전 품목)
        _cat_scope = combos if sel_fac_one == "전체" \
            else combos[combos["factory"] == fac_code_map[sel_fac_one]]
        _cats = sorted({c for c in _cat_scope["category2"].dropna().unique()})
        _has_null = bool(_cat_scope["category2"].isna().any())
        cat_opts = ["전체"] + _cats + (["(미분류)"] if _has_null else [])
        # 공장 변경으로 이전 제품유형이 새 옵션에 없으면 '전체'로 리셋
        if st.session_state.get("annual_cat_one") not in cat_opts:
            st.session_state["annual_cat_one"] = "전체"
        with fc2:
            sel_cat_one = st.selectbox(
                "제품유형", cat_opts, key="annual_cat_one",
                format_func=lambda c: _CAT2_DISPLAY.get(c, c),
            )

        fac_sel: tuple[str, ...] = () if sel_fac_one == "전체" else (fac_code_map[sel_fac_one],)
        if sel_cat_one == "전체":
            cat_sel: tuple[str | None, ...] = ()
        elif sel_cat_one == "(미분류)":
            cat_sel = (None,)
        else:
            cat_sel = (sel_cat_one,)

        try:
            df_ann = _load_annual_factory_cat2(int(year), fac_sel, (), cat_sel)
        except Exception as exc:
            df_ann = pd.DataFrame()
            st.caption(f"(연간 추이 로드 실패: {exc})")

        if df_ann.empty:
            st.info(f"{year - 1}~{year}년 데이터가 없습니다.")
        else:
            # production_daily.actual_qty 는 kg → Mix-Ton 환산 (÷1000)
            _TON = 1000.0
            mlabels = [f"{m}월" for m in range(1, 13)]
            cur_m = (
                df_ann[df_ann["year"] == int(year)]
                .groupby("month")["monthly_actual"].sum().reindex(range(1, 13)) / _TON
            )
            prev_m = (
                df_ann[df_ann["year"] == int(year) - 1]
                .groupby("month")["monthly_actual"].sum().reindex(range(1, 13)) / _TON
            )
            cur_total = float(cur_m.sum())   # 실적 있는 월만 합산 (NaN=0)
            prev_total = float(prev_m.sum())

            def _ratio(c: float, p: float) -> float | None:
                """전년비(%) — 전년 실적이 없으면 None, 당해 미실적은 0 으로 간주(=0.0%)."""
                if pd.isna(p) or p == 0:
                    return None
                return (0.0 if pd.isna(c) else c) / p * 100.0

            def _f_qty(v: float) -> str:
                return "-" if pd.isna(v) else f"{v:,.0f}"

            def _f_pct(r: float | None) -> str:
                return "-" if r is None else f"{r:.1f}%"

            row_prev = {"구분": f"'{str(year - 1)[2:]}년 생산량", "단위": "Mix-Ton"}
            row_cur = {"구분": f"'{str(year)[2:]}년 생산량", "단위": "Mix-Ton"}
            row_pct = {"구분": "전년비", "단위": "%"}
            ratios_by_col: dict[str, float | None] = {}
            for m in range(1, 13):
                lbl = f"{m}월"
                pv, cv = prev_m.loc[m], cur_m.loc[m]
                row_prev[lbl] = _f_qty(pv)
                row_cur[lbl] = _f_qty(cv)
                r = _ratio(cv, pv)
                row_pct[lbl] = _f_pct(r)
                ratios_by_col[lbl] = r
            row_prev["계"] = _f_qty(prev_total)
            row_cur["계"] = _f_qty(cur_total)
            r_total = _ratio(cur_total, prev_total)
            row_pct["계"] = _f_pct(r_total)
            ratios_by_col["계"] = r_total

            disp = pd.DataFrame(
                [row_prev, row_cur, row_pct],
                columns=["구분", "단위"] + mlabels + ["계"],
            )

            # 전년비 행 색상: ≥100% 파랑 / <100% 빨강 (사진과 동일)
            def _style(_df: pd.DataFrame) -> pd.DataFrame:
                sty = pd.DataFrame("", index=_df.index, columns=_df.columns)
                for col, rr in ratios_by_col.items():
                    if rr is None:
                        continue
                    clr = "#2563eb" if rr >= 100 else "#dc2626"
                    sty.iloc[2, _df.columns.get_loc(col)] = f"color:{clr}; font-weight:700"
                return sty

            st.dataframe(
                disp.style.apply(_style, axis=None),
                use_container_width=True, hide_index=True,
            )

            # 차트: 월별 당해/전년 생산량(꺾은선)
            fig_ann = go.Figure()
            fig_ann.add_trace(go.Scatter(
                name=f"{year - 1}년", x=mlabels,
                y=[prev_m.loc[m] for m in range(1, 13)],
                mode="lines+markers", line=dict(color="#94a3b8", width=2),
                connectgaps=False,
            ))
            fig_ann.add_trace(go.Scatter(
                name=f"{year}년", x=mlabels,
                y=[cur_m.loc[m] for m in range(1, 13)],
                mode="lines+markers", line=dict(color=_t["ACCENT"], width=3),
                connectgaps=False,
            ))
            fig_ann.update_layout(
                height=400, margin=dict(l=20, r=20, t=20, b=60),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=_t["TEXT_PRIMARY"]),
                legend=dict(orientation="h", y=-0.18, x=0.5, xanchor="center"),
                xaxis=dict(gridcolor=_t["GRID"], type="category",
                           tickfont=dict(color=_t["TEXT_PRIMARY"], size=16)),
                yaxis=dict(title=dict(text="생산량 (Mix-Ton)", font=dict(color=_t["TEXT_PRIMARY"])),
                           gridcolor=_t["GRID"], tickformat="~s",
                           tickfont=dict(color=_t["TEXT_PRIMARY"], size=16)),
            )
            st.plotly_chart(fig_ann, use_container_width=True, key="annual_yoy_db")
            st.caption(
                f"📌 {sel_fac_one} · "
                f"{_CAT2_DISPLAY.get(sel_cat_one, sel_cat_one)} 기준 · "
                "단위 Mix-Ton(생산실적 kg÷1000). "
                "월 전년비 = 당해월÷전년동월, 계 전년비 = 당해 누계÷전년 연간 합계."
            )
