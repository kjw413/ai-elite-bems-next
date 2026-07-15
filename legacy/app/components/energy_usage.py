"""Reusable components for energy usage pages."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.domain.factories import ordered_factory_labels
from app.services.query_service import get_daily_data, get_yoy_data
from app.utils.df_format import numeric_column_config
from app.utils.excel_parser import rename_columns_to_korean
from app.utils.page_common import csv_download


@dataclass(frozen=True)
class UsageMetricSpec:
    """Display metadata for a simple SUM-based energy usage metric."""

    key: str
    label: str
    metric_col: str
    unit: str
    icon: str
    color: str
    yaxis_title: str


USAGE_METRICS: dict[str, UsageMetricSpec] = {
    "전력": UsageMetricSpec(
        key="power",
        label="전력",
        metric_col="total_power_kwh",
        unit="kWh",
        icon="⚡",
        color="#00d4ff",
        yaxis_title="kWh",
    ),
    "연료": UsageMetricSpec(
        key="fuel",
        label="연료",
        metric_col="fuel_nm3",
        unit="Nm³",
        icon="🔥",
        color="#E8450A",
        yaxis_title="Nm³",
    ),
    "용수": UsageMetricSpec(
        key="water",
        label="용수",
        metric_col="water_ton",
        unit="ton",
        icon="💧",
        color="#0EA5E9",
        yaxis_title="ton",
    ),
    "폐수": UsageMetricSpec(
        key="wastewater",
        label="폐수",
        metric_col="wastewater_ton",
        unit="ton",
        icon="🌫️",
        color="#6B7280",
        yaxis_title="ton",
    ),
}


_POWER_TRACE_SPECS = [
    ("total_power_kwh", "전체 전력량", "#00d4ff"),
    ("freezing_power_kwh", "냉동전력량", "#7b2ff7"),
    ("air_compressor_kwh", "공압기", "#f97316"),
    ("other_power_kwh", "기타", "#48bb78"),
]

_WATER_TRACE_SPECS = [
    ("water_ton", "용수량", "#0EA5E9"),
    ("wastewater_ton", "폐수량", "#6B7280"),
]


def _format_number(value: float, unit: str, digits: int = 0) -> str:
    if pd.isna(value):
        return "-"
    if digits:
        return f"{value:,.{digits}f} {unit}"
    return f"{value:,.0f} {unit}"


def _with_power_other(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    required = {"total_power_kwh", "freezing_power_kwh", "air_compressor_kwh"}
    if required.issubset(out.columns):
        out["other_power_kwh"] = (
            out["total_power_kwh"]
            - out["freezing_power_kwh"]
            - out["air_compressor_kwh"]
        )
    return out


def _trace_specs_for(spec: UsageMetricSpec) -> list[tuple[str, str, str]]:
    if spec.key == "power":
        return _POWER_TRACE_SPECS
    if spec.key in ("water", "wastewater"):
        return _WATER_TRACE_SPECS
    return [(spec.metric_col, f"{spec.label}량", spec.color)]


def _base_line_layout(fig: go.Figure, theme: dict, *, height: int, yaxis_title: str) -> None:
    fig.update_layout(
        height=height,
        margin=dict(l=40, r=20, t=10, b=50),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=theme["FONT"], size=16),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_yaxes(
        tickformat="~s",
        gridcolor=theme["GRID"],
        title_text=yaxis_title,
        tickfont=dict(color=theme["FONT"], size=16),
        title_font=dict(color=theme["FONT"]),
    )


def _render_usage_line(
    df: pd.DataFrame,
    spec: UsageMetricSpec,
    theme: dict,
    *,
    x_col: str,
    key: str,
    x_tickformat: str,
    x_range: list[str] | None = None,
    x_dtick: int | None = None,
    group_by_factory: bool = False,
) -> None:
    df = _with_power_other(df)
    traces = _trace_specs_for(spec)
    fig = go.Figure()

    # 공장별 비교 모드 — 에너지원 종류와 무관하게 대표 지표(metric_col)를
    # 공장별 라인으로 분리해 그린다. (기존에는 전력/용수/폐수가 제외되어
    # 공장 간 비교가 불가능했음)
    if group_by_factory:
        agg = (
            df.groupby(["factory", x_col], as_index=False)[spec.metric_col]
            .sum()
            .sort_values([x_col, "factory"])
        )
        colors = ["#00d4ff", "#f97316", "#7b2ff7", "#48bb78", "#ecc94b", "#f56565", "#a78bfa"]
        for i, factory in enumerate(ordered_factory_labels(agg["factory"].unique())):
            part = agg[agg["factory"] == factory]
            fig.add_trace(go.Scatter(
                name=factory,
                x=part[x_col],
                y=part[spec.metric_col],
                mode="lines+markers",
                line=dict(color=colors[i % len(colors)], width=2),
            ))
    else:
        agg_cols = [col for col, _, _ in traces if col in df.columns]
        agg = df.groupby(x_col, as_index=False)[agg_cols].sum().sort_values(x_col)
        for col, name, color in traces:
            if col not in agg.columns:
                continue
            fig.add_trace(go.Scatter(
                name=name,
                x=agg[x_col],
                y=agg[col],
                mode="lines+markers",
                line=dict(color=color, width=2),
            ))

    _base_line_layout(fig, theme, height=400, yaxis_title=spec.yaxis_title)
    fig.update_xaxes(
        gridcolor=theme["GRID"],
        title_text="",
        tickformat=x_tickformat,
        dtick=x_dtick,
        range=x_range,
        tickangle=0,
        tickfont=dict(color=theme["FONT"], size=16),
        title_font=dict(color=theme["FONT"]),
    )
    st.plotly_chart(fig, use_container_width=True, key=key)


def render_usage_kpis(df: pd.DataFrame, spec: UsageMetricSpec, *, prefix: str = "") -> None:
    """Render SUM-only KPI cards for the selected usage metric."""
    if df.empty:
        return
    df = _with_power_other(df)
    if spec.key == "power":
        k1, k2, k3, k4 = st.columns(4)
        with k1:
            st.metric(f"{prefix}총 전력량", _format_number(df["total_power_kwh"].sum(), "kWh"))
        with k2:
            st.metric(f"{prefix}냉동전력량", _format_number(df["freezing_power_kwh"].sum(), "kWh"))
        with k3:
            st.metric(f"{prefix}공압기전력", _format_number(df["air_compressor_kwh"].sum(), "kWh"))
        with k4:
            st.metric(f"{prefix}기타전력량", _format_number(df["other_power_kwh"].sum(), "kWh"))
        st.caption("* 기타: 생산설비, 보일러, 조명, 사무 전력 등 일반 부하 합계")
        return

    total = float(df[spec.metric_col].sum()) if spec.metric_col in df.columns else 0.0
    daily = df.groupby("date")[spec.metric_col].sum() if spec.metric_col in df.columns else pd.Series(dtype=float)
    prod_ton = float(df["mix_prod_kg"].sum() / 1000.0) if "mix_prod_kg" in df.columns else 0.0

    if spec.key in ("water", "wastewater"):
        water = float(df["water_ton"].sum()) if "water_ton" in df.columns else 0.0
        wastewater = float(df["wastewater_ton"].sum()) if "wastewater_ton" in df.columns else 0.0
        ratio = wastewater / water if water > 0 else None
        k1, k2, k3, k4 = st.columns(4)
        with k1:
            st.metric(f"{prefix}총 용수량", _format_number(water, "ton"))
        with k2:
            st.metric(f"{prefix}총 폐수량", _format_number(wastewater, "ton"))
        with k3:
            st.metric("폐수/용수", "-" if ratio is None else f"{ratio:.2f}")
        with k4:
            st.metric("생산량", _format_number(prod_ton, "ton"))
        return

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric(f"{prefix}총 {spec.label}량", _format_number(total, spec.unit))
    with k2:
        st.metric("일평균", _format_number(float(daily.mean()) if not daily.empty else 0.0, spec.unit))
    with k3:
        st.metric("최대 일 사용량", _format_number(float(daily.max()) if not daily.empty else 0.0, spec.unit))
    with k4:
        st.metric("생산량", _format_number(prod_ton, "ton"))


def render_usage_trend_panel(
    spec: UsageMetricSpec,
    db_factories: list[str],
    months: list[str],
    default_month: str,
    db_min_date,
    db_max_date,
    default_start,
    default_end,
    theme: dict,
) -> None:
    """사용량 추이 통합 패널 — 월 단위/기간 지정 + 공장별 비교.

    기존에는 '일별(월 선택)'과 '기간별(시작~종료)' 두 패널이 좌우로 나란히 있어
    사실상 같은 차트가 범위만 달리 두 번 그려졌음. 하나의 패널에 조회 방식
    토글을 두어 화면을 절반으로 줄이고, '공장별 비교' 체크로 공장 간 라인
    비교(기존 불가)를 지원한다.
    """
    st.markdown(
        f'<div class="chart-title" style="font-size:1.05rem; margin-top:8px;">'
        f'<div class="chart-title-dot"></div>📅 {spec.label} 사용량 추이</div>',
        unsafe_allow_html=True,
    )

    ctl1, ctl2, ctl3 = st.columns([1.3, 1.2, 1.3])
    with ctl1:
        view_mode = st.radio(
            "조회 방식",
            options=["월 단위", "기간 지정"],
            horizontal=True,
            key="eu_view_mode",
        )
    with ctl2:
        factory = st.selectbox(
            "공장",
            options=["전사"] + db_factories,
            index=0,
            key="eu_factory",
        )
    with ctl3:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        compare_factories = st.checkbox(
            "공장별 비교 (라인 분리)",
            key="eu_compare",
            help=(
                "체크하면 공장 선택과 무관하게 5개 실공장의 라인을 분리해 "
                f"{spec.label} 사용량을 공장 간 비교합니다."
            ),
        )

    if view_mode == "월 단위":
        selected_month = st.selectbox(
            "조회 월",
            options=months,
            index=months.index(default_month) if default_month in months else len(months) - 1,
            key="eu_month",
        )
        year_m, month_m = int(selected_month[:4]), int(selected_month[5:7])
        last_day = monthrange(year_m, month_m)[1]
        date_from = f"{selected_month}-01"
        date_to = f"{selected_month}-{last_day:02d}"
        x_tickformat = "%-d일"
        x_range: list[str] | None = [date_from, date_to]
        x_dtick: int | None = 86400000 * 3
    else:
        col_p1, col_p2 = st.columns(2)
        with col_p1:
            start_date = st.date_input(
                "시작일",
                value=default_start,
                min_value=db_min_date,
                max_value=db_max_date,
                key="eu_start_date",
            )
        with col_p2:
            end_date = st.date_input(
                "종료일",
                value=default_end,
                min_value=db_min_date,
                max_value=db_max_date,
                key="eu_end_date",
            )
        if start_date > end_date:
            st.warning("시작일이 종료일보다 늦어 종료일과 맞췄습니다.")
            start_date = end_date
        date_from = start_date.strftime("%Y-%m-%d")
        date_to = end_date.strftime("%Y-%m-%d")
        x_tickformat = "%m/%d"
        x_range = None
        x_dtick = None

    if compare_factories:
        # 실공장 전체를 각자 라인으로 — factories=None 은 DB 원본(실공장) 행 반환
        df = get_daily_data(factories=None, date_from=date_from, date_to=date_to)
    else:
        df = get_daily_data(factories=[factory], date_from=date_from, date_to=date_to)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    if df.empty:
        st.warning("선택한 조건의 데이터가 없습니다.")
        return

    if compare_factories:
        st.caption(
            f"공장별 {spec.label} 사용량 비교 — KPI 카드는 공장 단일 선택 시 표시됩니다."
        )
    else:
        render_usage_kpis(df, spec)

    _render_usage_line(
        df,
        spec,
        theme,
        x_col="date",
        key=f"eu_trend_{spec.key}",
        x_tickformat=x_tickformat,
        x_range=x_range,
        x_dtick=x_dtick,
        group_by_factory=compare_factories,
    )

    with st.expander("📄 상세 데이터 보기"):
        cols = ["factory", "date", spec.metric_col]
        if spec.key == "power":
            cols = ["factory", "date", "total_power_kwh", "freezing_power_kwh", "air_compressor_kwh", "other_power_kwh"]
            df = _with_power_other(df)
        elif spec.key in ("water", "wastewater"):
            cols = ["factory", "date", "water_ton", "wastewater_ton"]
        tbl = df[[c for c in cols if c in df.columns]].copy()
        tbl["date"] = pd.to_datetime(tbl["date"]).dt.strftime("%Y-%m-%d")
        tbl = tbl.sort_values(["date", "factory"])
        disp = rename_columns_to_korean(tbl)
        st.dataframe(disp, use_container_width=True, hide_index=True, column_config=numeric_column_config(disp))
        csv_download(
            disp,
            filename=f"usage_{spec.key}_{date_from}_{date_to}.csv",
            key=f"dl_usage_{spec.key}",
        )


def render_yoy_usage_section(
    spec: UsageMetricSpec,
    db_factories: list[str],
    db_min: str,
    db_max: str,
    theme: dict,
) -> None:
    st.markdown(
        f'<div class="chart-title" style="font-size:1.05rem;">'
        f'<div class="chart-title-dot"></div>📈 전년대비 {spec.label} 사용량</div>',
        unsafe_allow_html=True,
    )
    min_year = pd.to_datetime(db_min).year
    max_year = pd.to_datetime(db_max).year
    year_options = list(range(max_year, min_year, -1)) or [max_year]

    col_y1, col_y2 = st.columns([1, 3])
    with col_y1:
        yoy_factory = st.selectbox("공장", options=["전사"] + db_factories, key="eu_yoy_factory")
        yoy_year = st.selectbox("기준연도", options=year_options, key="eu_yoy_year")
        show_val = st.checkbox("데이터 값 표시", value=False, key="eu_yoy_show_val")

    with col_y2:
        yoy_df = get_yoy_data(yoy_factory, int(yoy_year), spec.metric_col)
        if yoy_df.empty or spec.metric_col not in yoy_df.columns:
            st.info("전년 대비 데이터가 없습니다.")
            return

        fig = go.Figure()
        for yr in sorted(yoy_df["year"].unique()):
            yr_data = yoy_df[yoy_df["year"] == yr].copy()
            yr_data["month_label"] = yr_data["month"].astype(str) + "월"
            fig.add_trace(go.Scatter(
                x=yr_data["month_label"],
                y=yr_data[spec.metric_col],
                name=f"{yr}년",
                mode="lines+markers+text" if show_val else "lines+markers",
                text=yr_data[spec.metric_col] if show_val else None,
                textposition="top center",
                texttemplate="%{text:,.0f}",
                textfont=dict(size=18, color=theme["TEXT"]),
                line=dict(dash="solid" if yr == int(yoy_year) else "dash", width=2),
            ))
        _base_line_layout(fig, theme, height=350, yaxis_title=spec.yaxis_title)
        fig.update_layout(colorway=[spec.color, "#3a5abb"])
        fig.update_xaxes(
            gridcolor=theme["GRID"],
            title_text="월",
            tickangle=0,
            tickfont=dict(color=theme["FONT"], size=16),
            title_font=dict(color=theme["FONT"]),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"eu_yoy_{spec.key}")

        curr = yoy_df[yoy_df["year"] == int(yoy_year)].set_index("month")[spec.metric_col]
        prev = yoy_df[yoy_df["year"] == int(yoy_year) - 1].set_index("month")[spec.metric_col]
        yoy_table = pd.DataFrame(index=range(1, 13))
        yoy_table.index.name = "월"
        yoy_table["전년 실적"] = prev
        yoy_table["금년 실적"] = curr
        yoy_table = yoy_table.fillna(0)
        yoy_table["증감량"] = yoy_table["금년 실적"] - yoy_table["전년 실적"]
        yoy_table["증감률(%)"] = yoy_table.apply(
            lambda r: (r["증감량"] / r["전년 실적"] * 100.0) if r["전년 실적"] > 0 else 0.0,
            axis=1,
        )
        sum_prev = float(yoy_table["전년 실적"].sum())
        sum_curr = float(yoy_table["금년 실적"].sum())
        diff_sum = sum_curr - sum_prev
        diff_pct = (diff_sum / sum_prev * 100.0) if sum_prev > 0 else 0.0
        yoy_table.loc["누계"] = [sum_prev, sum_curr, diff_sum, diff_pct]
        yoy_table["증감률(%)"] = yoy_table["증감률(%)"].round(1)

        st.markdown(
            f'<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>'
            f'전년대비 월별 데이터 테이블 ({spec.label})</div>',
            unsafe_allow_html=True,
        )
        # 증감률에 좋음/나쁨 색을 입히지 않는다 — 사용량 증감은 생산량 변동의
        # 영향을 함께 받으므로 "감소=파랑(개선)" 표시가 오독을 유발했음.
        styled = (
            yoy_table.reset_index()
            .style.set_properties(**{"font-size": "15px"})
            .format({
                "전년 실적": "{:,.0f}",
                "금년 실적": "{:,.0f}",
                "증감량": "{:,.0f}",
                "증감률(%)": "{:.1f}%",
            })
        )
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            column_config={
                "월": st.column_config.TextColumn("월"),
                "전년 실적": st.column_config.NumberColumn("전년 실적", format="%,.0f"),
                "금년 실적": st.column_config.NumberColumn("금년 실적", format="%,.0f"),
                "증감량": st.column_config.NumberColumn("증감량", format="%,.0f"),
                "증감률(%)": st.column_config.NumberColumn("증감률(%)", format="%.1f%%"),
            },
        )
        st.caption(
            "※ 사용량 증감은 생산량 변동의 영향을 함께 받아 증감만으로 개선/악화를 "
            "판단할 수 없습니다 — 효율 판단은 '원단위' 페이지를 이용하세요."
        )
        csv_download(
            yoy_table.reset_index(),
            filename=f"yoy_usage_{spec.key}_{yoy_year}.csv",
            key=f"dl_yoy_usage_{spec.key}",
        )


def render_wastewater_ratio_section(
    db_factories: list[str],
    months: list[str],
    default_month: str,
    theme: dict,
) -> None:
    """Render the legacy water/wastewater factory ratio section."""
    st.markdown(
        '<div class="chart-title" style="font-size:1.05rem; margin-top:16px;">'
        '<div class="chart-title-dot"></div>💧 공장별 폐수/용수</div>',
        unsafe_allow_html=True,
    )
    col_r1, col_r2, _ = st.columns([1, 1, 2])
    with col_r1:
        ratio_factory = st.selectbox(
            "공장 선택 (비율)",
            options=["전사"] + db_factories,
            index=0,
            key="eu_ratio_factory",
        )
    with col_r2:
        ratio_month = st.selectbox(
            "조회 월 (비율)",
            options=months,
            index=months.index(default_month) if default_month in months else len(months) - 1,
            key="eu_ratio_month",
        )

    year_r, month_r = int(ratio_month[:4]), int(ratio_month[5:7])
    last_day = monthrange(year_r, month_r)[1]
    factories = None if ratio_factory == "전사" else [ratio_factory]
    df = get_daily_data(
        factories=factories,
        date_from=f"{ratio_month}-01",
        date_to=f"{ratio_month}-{last_day:02d}",
    )
    if df.empty:
        st.info("폐수/용수 비율 데이터가 없습니다.")
        return

    summary = df.groupby("factory", as_index=False).agg({"water_ton": "sum", "wastewater_ton": "sum"})
    summary["ratio"] = summary["wastewater_ton"] / summary["water_ton"].replace(0, float("nan"))

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="용수량",
        x=summary["factory"],
        y=summary["water_ton"],
        marker_color="#0EA5E9",
        text=summary["water_ton"],
        textposition="outside",
        texttemplate="%{text:,.0f}",
    ))
    fig.add_trace(go.Bar(
        name="폐수량",
        x=summary["factory"],
        y=summary["wastewater_ton"],
        marker_color="#6B7280",
        text=summary["wastewater_ton"],
        textposition="outside",
        texttemplate="%{text:,.0f}",
    ))
    fig.update_layout(
        barmode="group",
        height=300,
        margin=dict(l=40, r=20, t=10, b=40),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=theme["FONT"], size=16),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(gridcolor=theme["GRID"], tickfont=dict(color=theme["FONT"], size=16))
    fig.update_yaxes(
        tickformat="~s",
        gridcolor=theme["GRID"],
        title_text="ton",
        tickfont=dict(color=theme["FONT"], size=16),
        title_font=dict(color=theme["FONT"]),
    )
    st.plotly_chart(fig, use_container_width=True, key="eu_ratio_bar")

    display = summary[["factory", "water_ton", "wastewater_ton", "ratio"]].copy()
    display.columns = ["공장", "용수량 (ton)", "폐수량 (ton)", "폐수/용수"]
    display["폐수/용수"] = display["폐수/용수"].map(lambda v: "-" if pd.isna(v) else f"{v:.2f}")
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "용수량 (ton)": st.column_config.NumberColumn(format="%,.0f"),
            "폐수량 (ton)": st.column_config.NumberColumn(format="%,.0f"),
        },
    )
    csv_download(display, filename=f"wastewater_ratio_{ratio_month}.csv", key="dl_ww_ratio")


def default_period_bounds(db_min: str, db_max: str):
    db_min_date = pd.to_datetime(db_min).date()
    db_max_date = pd.to_datetime(db_max).date()
    ref_date = min(datetime.today().date(), db_max_date)
    default_end = ref_date
    default_start = max(default_end - timedelta(days=6), db_min_date)
    return db_min_date, db_max_date, default_start, default_end
