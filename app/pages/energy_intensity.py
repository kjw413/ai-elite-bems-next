"""
Energy Intensity Analysis Page
================================
원단위 - 일별/월별/전년대비 섹션 구조.
"""
# 이 파일은 에너지 원단위 화면을 보여줍니다.

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
from calendar import monthrange
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.query_service import (
    get_daily_data, get_yoy_data,
    get_factories, get_date_range,
)
from app.utils.excel_parser import rename_columns_to_korean
from app.utils.df_format import numeric_column_config
from app.utils.page_state import persist_many
from app.utils.page_common import get_theme_vars, get_ref_date, month_list


# 원단위 = SUM(사용량)/SUM(생산톤) 구조의 지표만 노출.
# 폐수는 '원단위'가 아닌 폐수/용수 비로 관리하므로 본 페이지에서 제외하고
# 연료·용수 사용량 페이지의 '공장별 폐수/용수' 섹션에서 다룬다.
INTENSITY_METRICS = {
    "전력 원단위 (kWh/ton)": {"unit_col": "power_per_ton_kwh", "usage_col": "total_power_kwh", "unit_label": "kWh/ton"},
    "연료 원단위 (Nm³/ton)": {"unit_col": "fuel_per_ton_nm3", "usage_col": "fuel_nm3", "unit_label": "Nm³/ton"},
    "용수 원단위 (ton/ton)": {"unit_col": "water_per_ton_ton", "usage_col": "water_ton", "unit_label": "ton/ton"},
}


# 에너지 원단위 화면을 구성합니다.
def render_energy_intensity():
    # 페이지 이동 후 재방문에도 필터 값을 유지
    persist_many({
        "ei_metric_select":  None,
        "ei_daily_factory":  None,
        "ei_daily_month":    None,
        "ei_period_factory": None,
        "ei_start_date":     None,
        "ei_end_date":       None,
        "ei_period_cum":     None,
        "ei_yoy_factory":    None,
        "ei_yoy_year":       None,
        "ei_yoy_show_val":   None,
        "ei_yoy_cum":        None,
    })

    # 테마 변수 로컬화
    theme_vars = get_theme_vars()
    L_FONT = theme_vars["FONT"]
    L_GRID = theme_vars["GRID"]
    L_TEXT = theme_vars["TEXT"]

    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">📊</span>
        <div>
            <div class="sub-page-title">원단위</div>
            <div class="sub-page-breadcrumb">에너지 모니터링 > 원단위</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    db_factories = get_factories()
    db_min, db_max = get_date_range()

    if not db_factories or not db_min:
        st.info("데이터가 없습니다.")
        return

    ref_date = get_ref_date()

    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        metric_selection = st.selectbox("원단위 선택", options=list(INTENSITY_METRICS.keys()), key="ei_metric_select")

    m_info = INTENSITY_METRICS[metric_selection]
    unit_col = m_info["unit_col"]
    usage_col = m_info["usage_col"]
    unit_label = m_info["unit_label"]
    short_name = metric_selection.split(" (")[0]



    with st.container(border=True):
        # ── 섹션 1: 일별 및 기간별 추이 ─────────────────────────────────────

        months = month_list(db_min, db_max)
        default_month = ref_date.strftime("%Y-%m")
        default_month = default_month if default_month in months else (months[-1] if months else default_month)

        db_min_date = pd.to_datetime(db_min).date()
        db_max_date = pd.to_datetime(db_max).date()
        default_end = min(ref_date, db_max_date)
        default_start = max(default_end - timedelta(days=6), db_min_date)

        col_c1, col_c2 = st.columns(2)

        with col_c1:
            st.markdown(f'<div class="chart-title" style="font-size:1.05rem; margin-top:8px;"><div class="chart-title-dot"></div>📅 일별 {short_name} 추이</div>', unsafe_allow_html=True)
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                daily_factory = st.selectbox("공장 선택 (일별)", ["전사"] + db_factories, index=0, key="ei_daily_factory")
            with col_m2:
                selected_month = st.selectbox("조회 월 (일별)", options=months,
                    index=months.index(default_month) if default_month in months else len(months) - 1,
                    key="ei_daily_month")

            year_m, month_m = int(selected_month[:4]), int(selected_month[5:7])
            last_day = monthrange(year_m, month_m)[1]
            daily_df = get_daily_data(factories=[daily_factory], date_from=f"{selected_month}-01", date_to=f"{selected_month}-{last_day:02d}")

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            if daily_df.empty:
                st.warning("일별 데이터가 없습니다.")
            else:
                total_prod = daily_df["mix_prod_kg"].sum() / 1000
                # 사이드바 열림 시 단위 잘림 방지를 위해 카드 폭을 넓게(2:1 비율)
                col_k1, _ = st.columns([2, 1])
                with col_k1:
                    total_usage = daily_df[usage_col].sum()
                    val = total_usage / total_prod if total_prod > 0 else 0
                    st.metric(f"해당 월 {short_name}", f"{val:,.2f} {unit_label}")
                st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

                daily_agg = daily_df.groupby(["factory", "date"]).agg({
                    usage_col: "sum", "mix_prod_kg": "sum"
                }).reset_index().sort_values(["date", "factory"])
                daily_agg["date"] = pd.to_datetime(daily_agg["date"])
                daily_agg["intensity"] = daily_agg.apply(lambda r: r[usage_col] / (r["mix_prod_kg"] / 1000) if r["mix_prod_kg"] > 0 else None, axis=1)
            
                fig = go.Figure()
                for fac in sorted(daily_agg["factory"].unique()):
                    f_data = daily_agg[daily_agg["factory"] == fac]
                    kwargs = dict(name=fac, x=f_data["date"], y=f_data["intensity"],
                        mode="lines+markers",
                        hovertemplate=f"<b>{fac}</b><br>날짜: %{{x|%m월 %d일}}<br>원단위: %{{y:,.2f}}<extra></extra>",
                        connectgaps=True, line=dict(width=2))
                    fig.add_trace(go.Scatter(**kwargs))
            
                fig.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=60),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
                    font=dict(color=L_FONT, size=16),
                    colorway=["#00d4ff", "#f97316", "#7b2ff7", "#48bb78", "#ecc94b", "#f56565"])
            
                fig.update_xaxes(gridcolor=L_GRID, title_text="날짜", tickformat="%-d일", dtick=86400000*3,
                                 tickangle=0, range=[f"{selected_month}-01", f"{selected_month}-{last_day:02d}"],
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
            
                fig.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="원단위",
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig, use_container_width=True, key="ei_daily_intensity")
            
                with st.expander("📄 일별 상세 데이터 보기"):
                    tbl_df = daily_df.copy()
                    tbl_df["date"] = pd.to_datetime(tbl_df["date"]).dt.strftime("%Y-%m-%d")
                    tbl_df = tbl_df.sort_values(["date", "factory"])
                    _d = rename_columns_to_korean(tbl_df)
                    st.dataframe(_d, use_container_width=True, hide_index=True,
                                 column_config=numeric_column_config(_d))

        with col_c2:
            st.markdown(f'<div class="chart-title" style="font-size:1.05rem; margin-top:8px;"><div class="chart-title-dot"></div>📅 기간별 {short_name} 추이</div>', unsafe_allow_html=True)
            col_p1, col_p2, col_p3 = st.columns([1, 1, 1])
            with col_p1:
                period_factory = st.selectbox("공장 선택 (기간별)", ["전사"] + db_factories, index=0, key="ei_period_factory")
            with col_p2:
                start_date = st.date_input("시작일", value=default_start, min_value=db_min_date, max_value=db_max_date, key="ei_start_date")
            with col_p3:
                end_date = st.date_input("종료일", value=default_end, min_value=db_min_date, max_value=db_max_date, key="ei_end_date")
            show_period_cum = st.checkbox(
                "누계 추이 보기",
                value=False,
                key="ei_period_cum",
                help="체크하면 각 날짜에서 시작일부터의 누계 원단위(SUM 사용량 ÷ SUM 생산톤)로 라인을 다시 그립니다.",
            )

            period_df = get_daily_data(factories=[period_factory], date_from=start_date.strftime("%Y-%m-%d"), date_to=end_date.strftime("%Y-%m-%d"))

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            if period_df.empty:
                st.warning("기간별 데이터가 없습니다.")
            else:
                total_p_prod = period_df["mix_prod_kg"].sum() / 1000
                total_p_usage = period_df[usage_col].sum()
                p_val = total_p_usage / total_p_prod if total_p_prod > 0 else 0

                # 사이드바 열림 시 단위 잘림 방지를 위해 카드 폭을 넓게(2:1 비율)
                col_pk1, _ = st.columns([2, 1])
                with col_pk1:
                    st.metric(f"기간 {short_name}", f"{p_val:,.2f} {unit_label}")

                p_agg = period_df.groupby(["factory", "date"]).agg({usage_col: "sum", "mix_prod_kg": "sum"}).reset_index()
                p_agg["date"] = pd.to_datetime(p_agg["date"])
                p_agg = p_agg.sort_values(["factory", "date"]).reset_index(drop=True)
                if show_period_cum:
                    # 누계 원단위: 공장별로 날짜순 사용량/생산량 누적합을 따로 잡고 나눠준다.
                    # daily 단위에서 단순 평균이 아니라 분자/분모 각각 누적해야 정합.
                    p_agg["cum_usage"] = p_agg.groupby("factory")[usage_col].cumsum()
                    p_agg["cum_prod"]  = p_agg.groupby("factory")["mix_prod_kg"].cumsum()
                    p_agg["intensity"] = p_agg.apply(
                        lambda r: r["cum_usage"] / (r["cum_prod"] / 1000) if r["cum_prod"] > 0 else None,
                        axis=1,
                    )
                else:
                    p_agg["intensity"] = p_agg.apply(
                        lambda r: r[usage_col] / (r["mix_prod_kg"] / 1000) if r["mix_prod_kg"] > 0 else None,
                        axis=1,
                    )
                p_agg = p_agg.sort_values(["date", "factory"])

                fig3 = go.Figure()
                for fac in sorted(p_agg["factory"].unique()):
                    f_data = p_agg[p_agg["factory"] == fac]
                    trace_name = f"{fac} (누계)" if show_period_cum else fac
                    kwargs = dict(name=trace_name, x=f_data["date"], y=f_data["intensity"], mode="lines+markers", line=dict(width=2))
                    fig3.add_trace(go.Scatter(**kwargs))

                fig3.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=40),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=""),
                    font=dict(color=L_FONT, size=16),
                    colorway=["#00d4ff", "#f97316", "#7b2ff7", "#48bb78", "#ecc94b", "#f56565"])
            
                fig3.update_xaxes(gridcolor=L_GRID, tickangle=0, title_text="", tickformat="%m/%d", tickfont=dict(color=L_FONT, size=16))
                fig3.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text=unit_label,
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig3, use_container_width=True, key="ei_period_intensity")

                with st.expander("📄 기간별 상세 데이터 보기"):
                    p_tbl = period_df.copy()
                    p_tbl["date"] = pd.to_datetime(p_tbl["date"]).dt.strftime("%Y-%m-%d")
                    p_tbl = p_tbl.sort_values(["date", "factory"])
                    _p = rename_columns_to_korean(p_tbl)
                    st.dataframe(_p, use_container_width=True, hide_index=True,
                                 column_config=numeric_column_config(_p))

    with st.container(border=True):
        # ── 섹션 3: 전년대비 사용 분석 ─────────────────────────────────────
        st.markdown('<div class="chart-title" style="font-size:1.05rem;"><div class="chart-title-dot"></div>📈 전년대비 원단위 분석</div>', unsafe_allow_html=True)

        col_y1, col_y2 = st.columns([1, 3])
        with col_y1:
            yoy_factory = st.selectbox("공장", options=["전사"] + db_factories, key="ei_yoy_factory")
            yoy_year = st.selectbox("기준연도", options=list(range(datetime.now().year, 1999, -1)), key="ei_yoy_year")
            show_val = st.checkbox("데이터 값 표시", value=False, key="ei_yoy_show_val")
            show_yoy_cum = st.checkbox(
                "누계 추이 보기",
                value=False,
                key="ei_yoy_cum",
                help="체크하면 각 월에서 1월부터의 누계 원단위(SUM 사용량 ÷ SUM 생산톤)로 라인을 다시 그립니다. 예) 6월 점 = 1~6월 누계.",
            )

        with col_y2:
            yoy_df = get_yoy_data(yoy_factory, yoy_year, usage_col)
            if not yoy_df.empty:
                # 원단위 컬럼이 있으면 우선 사용하고, 없으면 사용량 컬럼으로 대체
                chart_col = unit_col if unit_col in yoy_df.columns else usage_col
                # 누계 모드: 각 연도에 대해 월 정렬 → 사용량/생산량 누적합 → 누계 원단위 재계산
                if show_yoy_cum:
                    plot_df = yoy_df.sort_values(["year", "month"]).copy()
                    plot_df["cum_usage"] = plot_df.groupby("year")[usage_col].cumsum()
                    plot_df["cum_prod"]  = plot_df.groupby("year")["mix_prod_kg"].cumsum()
                    plot_df["_y"] = plot_df.apply(
                        lambda r: r["cum_usage"] / (r["cum_prod"] / 1000) if r["cum_prod"] > 0 else None,
                        axis=1,
                    )
                else:
                    plot_df = yoy_df.copy()
                    plot_df["_y"] = plot_df[chart_col] if chart_col in plot_df.columns else plot_df[usage_col]

                fig5 = go.Figure()
                for yr in sorted(plot_df["year"].unique()):
                    yr_data = plot_df[plot_df["year"] == yr].copy()
                    yr_data["month_label"] = yr_data["month"].astype(str) + "월"
                    y_vals = yr_data["_y"]
                    trace_name = f"{yr}년 (누계)" if show_yoy_cum else f"{yr}년"
                    fig5.add_trace(go.Scatter(x=yr_data["month_label"], y=y_vals,
                        name=trace_name, mode="lines+markers+text" if show_val else "lines+markers",
                        text=y_vals if show_val else None, textposition="top center", texttemplate="%{text:,.2f}",
                        textfont=dict(size=18, color=L_TEXT),
                        line=dict(dash="solid" if yr == yoy_year else "dash", width=2)))
                fig5.update_layout(height=350, margin=dict(l=40, r=20, t=10, b=40),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    colorway=["#00d4ff", "#3a5abb"])
            
                fig5.update_xaxes(gridcolor=L_GRID, title_text="월", tickangle=0, tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                fig5.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text=unit_label,
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig5, use_container_width=True, key="ei_yoy_line")

                # 하단 테이블용 데이터
                # 누계 추이 보기 ON → 월별 셀도 차트와 동일하게 1월~해당월 누계 원단위로 표시.
                #   누계값 = SUM(1~N월 사용량) / SUM(1~N월 생산톤). 분자·분모를 각각 누적해야
                #   원단위 정의(Σ사용량/Σ생산톤)와 정합 (단순 월 평균이 아님).
                #   usage_col/mix_prod_kg 원본은 보존 → 아래 '누계' 행의 전년 전체 집계가 그대로
                #   유지되며, 이는 마지막 누계월 값과 동일하다.
                tbl_src = yoy_df.sort_values(["year", "month"]).copy()
                if show_yoy_cum:
                    cum_usage = tbl_src.groupby("year")[usage_col].cumsum()
                    cum_prod  = tbl_src.groupby("year")["mix_prod_kg"].cumsum()
                    tbl_src["_tbl_val"] = (cum_usage / (cum_prod / 1000)).where(cum_prod > 0)
                else:
                    tbl_src["_tbl_val"] = tbl_src[chart_col]

                curr_year_df = tbl_src[tbl_src["year"] == yoy_year].set_index("month")
                prev_year_df = tbl_src[tbl_src["year"] == yoy_year - 1].set_index("month")

                yoy_table = pd.DataFrame(index=range(1, 13))
                yoy_table.index.name = "월"
                yoy_table["전년 실적"] = prev_year_df["_tbl_val"]
                yoy_table["금년 실적"] = curr_year_df["_tbl_val"]
                yoy_table = yoy_table.fillna(0)
                yoy_table["증감량"] = yoy_table["금년 실적"] - yoy_table["전년 실적"]
                yoy_table["증감률(%)"] = yoy_table.apply(
                    lambda r: (r["증감량"] / r["전년 실적"] * 100) if r["전년 실적"] > 0 else 0, axis=1
                )
            
                # 누계 행 추가 (합산이 아닌 사용량/생산량 재계산)
                sum_prev_usage = prev_year_df[usage_col].sum() if usage_col in prev_year_df.columns else 0
                sum_prev_prod = prev_year_df["mix_prod_kg"].sum() if "mix_prod_kg" in prev_year_df.columns else 0
            
                sum_curr_usage = curr_year_df[usage_col].sum() if usage_col in curr_year_df.columns else 0
                sum_curr_prod = curr_year_df["mix_prod_kg"].sum() if "mix_prod_kg" in curr_year_df.columns else 0
            
                avg_prev = sum_prev_usage / (sum_prev_prod / 1000) if sum_prev_prod > 0 else 0
                avg_curr = sum_curr_usage / (sum_curr_prod / 1000) if sum_curr_prod > 0 else 0
            
                diff_sum = avg_curr - avg_prev
                diff_pct = (diff_sum / avg_prev * 100) if avg_prev > 0 else 0
            
                yoy_table.loc["누계"] = [avg_prev, avg_curr, diff_sum, diff_pct]
            
                # color rate 관련 처리를 담당합니다.
                def color_rate(val):
                    if isinstance(val, str) or pd.isna(val):
                        return ""
                    if val < 0:
                        return "color: #4da6ff" # Blue for negative
                    elif val > 0:
                        return "color: #ff4d4d" # Red for positive
                    return ""
            
                yoy_table["증감률(%)"] = yoy_table["증감률(%)"].round(1)
            
                _tbl_mode = " · 1월부터 누계" if show_yoy_cum else ""
                st.markdown(f'<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>전년대비 월별 데이터 테이블 ({short_name}{_tbl_mode})</div>', unsafe_allow_html=True)
            
                styled_table = yoy_table.reset_index().style.set_properties(**{
                    'font-size': '15px'
                }).map(
                    color_rate, subset=["증감률(%)"]
                ).format(
                    {
                        "전년 실적": "{:,.2f}",
                        "금년 실적": "{:,.2f}",
                        "증감량": "{:,.2f}",
                        "증감률(%)": "{:.1f}%"
                    }
                )
            
                st.dataframe(
                    styled_table,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "월": st.column_config.NumberColumn("월", format="%d"),
                        "전년 실적": st.column_config.NumberColumn("전년 실적", format="%,.2f"),
                        "금년 실적": st.column_config.NumberColumn("금년 실적", format="%,.2f"),
                        "증감량": st.column_config.NumberColumn("증감량", format="%,.2f"),
                        "증감률(%)": st.column_config.NumberColumn("증감률(%)", format="%.1f%%")
                    }
                )

            else:
                st.info("전년 대비 데이터가 없습니다.")
