"""Production performance renderers for non-monthly modes."""
from __future__ import annotations

import calendar
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.domain.factories import (
    FACTORY_CODE_TO_KR,
    NAMYANGJU_PARENT_CODE,
    expand_factory_members,
)
from app.services.production_correction_service import WIP_TRUSTED_FACTORIES, get_breakdown
from app.services.query_service import get_daily_data
from app.utils.df_format import numeric_column_config
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

_ENERGY_SOURCE_SPECS = {
    "전력": ("total_power_kwh", "전력 (kWh)", "kWh", "#f59e0b"),
    "연료": ("fuel_nm3", "연료 (Nm³)", "Nm³", "#ef4444"),
    "용수": ("water_ton", "용수 (ton)", "ton", "#38bdf8"),
}

_FACTORY_DISPLAY_MAP = {**FACTORY_CODE_TO_KR, NAMYANGJU_PARENT_CODE: "남양주"}


def _factory_display(code: str) -> str:
    return _FACTORY_DISPLAY_MAP.get(code, code)


def is_complete_month_span(date_from: str, date_to: str) -> bool:
    """True if the period starts on day 1 and ends on the last day of a month."""
    start = pd.to_datetime(date_from).date()
    end = pd.to_datetime(date_to).date()
    return start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1]


def _energy_factories_for(prod_factories: tuple[str, ...]) -> list[str]:
    labels: list[str] = []
    for code in prod_factories:
        labels.extend(expand_factory_members(_factory_display(code)))
    return list(dict.fromkeys(labels))


def _energy_cross_range(
    df_prod: pd.DataFrame,
    date_from: str,
    date_to: str,
    factories: tuple[str, ...],
) -> pd.DataFrame:
    """Join production with corrected energy data through get_daily_data()."""
    energy_labels = _energy_factories_for(factories)
    energy = get_daily_data(
        factories=energy_labels if energy_labels else None,
        date_from=date_from,
        date_to=date_to,
    )
    if energy.empty:
        return pd.DataFrame()

    energy = energy.copy()
    energy["date"] = pd.to_datetime(energy["date"]).dt.normalize()
    energy_cols = ["mix_prod_kg", "total_power_kwh", "fuel_nm3", "water_ton"]
    energy_daily = energy.groupby("date", as_index=False)[energy_cols].sum()

    if df_prod.empty:
        return energy_daily.assign(total_prod=0.0)

    prod = df_prod.copy()
    prod["date"] = pd.to_datetime(prod["date"]).dt.normalize()
    prod_daily = prod.groupby("date", as_index=False)["actual_qty"].sum().rename(
        columns={"actual_qty": "total_prod"}
    )
    out = energy_daily.merge(prod_daily, on="date", how="outer").fillna(0.0)
    return out.sort_values("date").reset_index(drop=True)


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


def _render_breakdown(date_from: str, date_to: str, sel_factories: list[str]) -> None:
    sel_display = [_factory_display(f) for f in sel_factories] if sel_factories else ["남양주", "김해", "광주", "논산"]
    targets: list[str] = []
    for factory in sel_display:
        targets.extend(expand_factory_members(factory))
    targets = sorted(set(targets))

    with st.container(border=True):
        section_tone("emerald")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🧮</span>에너지 원단위 생산량 (Mix-kg 환산)'
            '<span class="section-title-sub">완제품 · 재공품 · 외주(임가공)</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        rows = []
        for factory in targets:
            try:
                b = get_breakdown(factory, date_from, date_to)
            except Exception as exc:
                st.warning(f"[{factory}] 보정 계산 실패: {exc}")
                continue
            rows.append({
                "공장": factory,
                "에너지 믹스 (kg)": b.energy_mix_kg,
                "완제품 (kg)": b.finished_kg,
                "재공품 (kg)": b.wip_kg,
                "외주/잔차 (kg)": b.residual_kg,
                "외주 비중(%)": (b.residual_kg / b.energy_mix_kg * 100.0) if b.energy_mix_kg > 0 else 0.0,
                "WIP 신뢰": "✓" if factory in WIP_TRUSTED_FACTORIES else "—",
                "비고": b.notes,
            })
        if rows:
            view = pd.DataFrame(rows)
            st.dataframe(view, use_container_width=True, hide_index=True, column_config=numeric_column_config(view))
        else:
            st.info("보정 계산 결과가 없습니다.")


def _render_energy_cross(
    mode: str,
    df: pd.DataFrame,
    date_from: str,
    date_to: str,
    sel_factories: list[str],
    theme: dict,
) -> None:
    with st.container(border=True):
        section_tone("amber")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">⚡</span>에너지 사용량 vs 생산량'
            '<span class="section-title-sub">get_daily_data 보정 경유</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        source = st.selectbox("에너지원", list(_ENERGY_SOURCE_SPECS.keys()), key="prod_range_energy_src_db")
        src_col, src_axis_label, src_unit, src_color = _ENERGY_SOURCE_SPECS[source]
        ec = _energy_cross_range(df, date_from, date_to, tuple(sel_factories))
        if ec.empty:
            st.info("해당 기간 에너지 데이터가 없습니다.")
            return
        if mode == "연간":
            plot_df = (
                ec.assign(month_label=ec["date"].dt.to_period("M").astype(str))
                .groupby("month_label", as_index=False)[["total_prod", "mix_prod_kg", src_col]].sum()
            )
            x_vals = plot_df["month_label"]
        else:
            plot_df = ec
            x_vals = plot_df["date"]
        fig = go.Figure()
        fig.add_trace(go.Bar(name="총 생산량 (생산실적)", x=x_vals, y=plot_df["total_prod"],
                             marker_color="#10b981", yaxis="y1", opacity=0.6))
        fig.add_trace(go.Scatter(name=f"{source} 사용량 ({src_unit})", x=x_vals, y=plot_df[src_col],
                                 mode="lines+markers", line=dict(color=src_color, width=2), yaxis="y2"))
        fig.add_trace(go.Scatter(name="믹스 생산량 (kg, 에너지 DB)", x=x_vals, y=plot_df["mix_prod_kg"],
                                 mode="lines+markers", line=dict(color="#a78bfa", width=2, dash="dot"), yaxis="y1"))
        fig.update_layout(
            height=420, margin=dict(l=20, r=20, t=20, b=40),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=theme["TEXT_PRIMARY"]),
            yaxis=dict(title=dict(text="생산량", font=dict(color=theme["TEXT_PRIMARY"])),
                       side="left", gridcolor=theme["GRID"], tickformat="~s"),
            yaxis2=dict(title=dict(text=src_axis_label, font=dict(color=theme["TEXT_PRIMARY"])),
                        side="right", overlaying="y", tickformat="~s"),
            legend=dict(orientation="h", y=1.10, x=0.5, xanchor="center"),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"{mode}_energy_cross")


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
    _render_breakdown(date_from, date_to, sel_factories)
    _render_energy_cross(mode, df, date_from, date_to, sel_factories, theme)
