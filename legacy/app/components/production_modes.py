"""Production performance renderers for non-monthly modes."""
from __future__ import annotations

import calendar
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.utils.page_common import section_tone


_CAT2_DISPLAY = {
    "IC": "IC (아이스크림)",
    "MY": "MY (유음료)",
    "FM": "FM (발효유)",
    "SN": "SN (스낵)",
}

_CAT2_COLORS = {
    "IC": "#7dd3fc",
    "MY": "#fbbf24",
    "FM": "#a78bfa",
    "SN": "#fb923c",
}

def is_complete_month_span(date_from: str, date_to: str) -> bool:
    """True if the period starts on day 1 and ends on the last day of a month."""
    start = pd.to_datetime(date_from).date()
    end = pd.to_datetime(date_to).date()
    return start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1]


def _render_kpis(
    *,
    mode: str,
    summary: dict,
    date_from: str,
    date_to: str,
    selected_year: int,
    today: datetime,
    plan_allowed: bool,
) -> None:
    start = pd.to_datetime(date_from).date()
    end = pd.to_datetime(date_to).date()
    period_days = max((end - start).days + 1, 1)
    period_label = f"{date_from} ~ {date_to}" if mode == "기간별" else f"{selected_year}년"

    with st.container(border=True):
        section_tone("emerald")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📊</span>요약 KPI'
            f'<span class="section-title-sub">{mode} · {period_label}</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        if plan_allowed:
            k1, k2, k3, k4, k5 = st.columns([1, 1, 1.1, 0.8, 0.8])
            with k1:
                st.metric("누계 계획" if mode == "기간별" else "연계획", f"{summary['total_planned']:,.0f}")
            with k2:
                st.metric("누계 실적", f"{summary['total_actual']:,.0f}")
            with k3:
                if summary["total_planned"] > 0:
                    st.metric("계획 달성률", f"{summary['progress_pct']:.1f}%")
                else:
                    st.metric("계획 달성률", "N/A")
            with k4:
                st.metric("품목 수", f"{summary['items_count']:,}")
            with k5:
                st.metric("조회일수", f"{period_days:,}일")

            if mode == "연간" and summary["total_planned"] > 0:
                year_start = pd.Timestamp(f"{selected_year}-01-01")
                year_end = pd.Timestamp(f"{selected_year}-12-31")
                if today.date() < year_start.date():
                    elapsed_ratio = 0.0
                elif today.date() > year_end.date():
                    elapsed_ratio = 1.0
                else:
                    elapsed_ratio = ((pd.Timestamp(today.date()) - year_start).days + 1) / (
                        (year_end - year_start).days + 1
                    )
                expected = summary["total_planned"] * elapsed_ratio
                forecast = summary["total_actual"] / elapsed_ratio if elapsed_ratio > 0 else 0.0
                st.caption(f"연간 기대 누계: **{expected:,.0f}** · 연말 착지 예상: **{forecast:,.0f}**")
        else:
            k1, k2, k3 = st.columns(3)
            with k1:
                st.metric("누계 실적", f"{summary['total_actual']:,.0f}")
            with k2:
                st.metric("품목 수", f"{summary['items_count']:,}")
            with k3:
                st.metric("조회일수", f"{period_days:,}일")
            st.caption("기간별 계획 대비는 선택 범위가 완전한 월들로 구성될 때만 표시합니다.")


def _render_annual_burnup(df: pd.DataFrame, selected_year: int, theme: dict) -> None:
    with st.container(border=True):
        section_tone("violet")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📈</span>연간 Burn-up'
            '<span class="section-title-sub">월별 누적 실적 vs 월별 계획 누계</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        df_yr = df.assign(y=df["date"].dt.year, m=df["date"].dt.month)
        actual_m = df_yr.groupby("m")["actual_qty"].sum().reindex(range(1, 13), fill_value=0.0)
        plan_m = (
            df_yr.drop_duplicates(["item_code", "factory", "y", "m"])
            .groupby("m")["planned_qty"].sum().reindex(range(1, 13), fill_value=0.0)
        )
        labels = [f"{m}월" for m in range(1, 13)]
        fig = go.Figure()
        fig.add_trace(go.Scatter(name="누적 계획", x=labels, y=plan_m.cumsum(), mode="lines+markers",
                                 line=dict(color="#64748b", width=3, dash="dash")))
        fig.add_trace(go.Scatter(name="누적 실적", x=labels, y=actual_m.cumsum(), mode="lines+markers",
                                 line=dict(color=theme["ACCENT"], width=3)))
        fig.update_layout(
            height=380, margin=dict(l=20, r=20, t=10, b=40),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=theme["TEXT_PRIMARY"]),
            legend=dict(orientation="h", y=1.10, x=0.5, xanchor="center"),
            yaxis=dict(gridcolor=theme["GRID"], tickformat="~s",
                       title=dict(text="누적 생산량", font=dict(color=theme["TEXT_PRIMARY"]))),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"annual_burnup_{selected_year}")


def _render_trend(mode: str, df: pd.DataFrame, theme: dict) -> None:
    with st.container(border=True):
        section_tone("cyan")
        title = "월별 생산량 추이" if mode == "연간" else "일별 생산량 추이"
        st.markdown(
            '<div class="section-title">'
            f'<span class="section-title-icon">📈</span>{title} (제품유형별)'
            "</div>",
            unsafe_allow_html=True,
        )
        if mode == "연간":
            trend = (
                df.assign(month=df["date"].dt.month, cat2_label=df["category2"].fillna("(미분류)"))
                .groupby(["month", "cat2_label"])["actual_qty"].sum().reset_index()
            )
            trend["x_label"] = trend["month"].astype(str) + "월"
            x_col = "x_label"
        else:
            trend = (
                df.assign(dt_day=df["date"].dt.normalize(), cat2_label=df["category2"].fillna("(미분류)"))
                .groupby(["dt_day", "cat2_label"])["actual_qty"].sum().reset_index()
            )
            x_col = "dt_day"
        if trend.empty:
            st.info("표시할 생산량 추이가 없습니다.")
            return
        fig = px.line(
            trend, x=x_col, y="actual_qty", color="cat2_label", markers=True,
            color_discrete_map=_CAT2_COLORS,
            labels={x_col: "월" if mode == "연간" else "날짜", "actual_qty": "생산량", "cat2_label": "제품유형"},
        )
        fig.update_layout(
            height=390, margin=dict(l=20, r=20, t=10, b=40),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=theme["TEXT_PRIMARY"]),
            legend=dict(orientation="h", y=1.10, x=0.5, xanchor="center"),
            yaxis=dict(gridcolor=theme["GRID"], tickformat="~s"),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"{mode}_prod_trend")


def render_range_production_view(
    *,
    mode: str,
    df: pd.DataFrame,
    summary: dict,
    date_from: str,
    date_to: str,
    selected_year: int,
    sel_factories: list[str],
    today: datetime,
    theme: dict,
) -> None:
    """Render period/year production mode."""
    plan_allowed = mode == "연간" or is_complete_month_span(date_from, date_to)
    _render_kpis(
        mode=mode,
        summary=summary,
        date_from=date_from,
        date_to=date_to,
        selected_year=selected_year,
        today=today,
        plan_allowed=plan_allowed,
    )
    if mode == "연간":
        _render_annual_burnup(df, selected_year, theme)
    else:
        st.info("기간별 Burn-up은 월 계획 정의가 왜곡될 수 있어 숨깁니다.")
    _render_trend(mode, df, theme)
    if not plan_allowed:
        st.info("기간별 계획 대비 품목 랭킹은 선택 범위가 완전한 월들로 구성될 때만 표시합니다.")
