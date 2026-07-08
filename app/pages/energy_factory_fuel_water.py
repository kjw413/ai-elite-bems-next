"""
Energy Factory Fuel & Water Analysis Page
==========================================
연료·용수 사용량 분석 - 일별/월별/전년대비 섹션 구조.
"""
# 이 파일은 공장별 연료·용수 분석 화면을 보여줍니다.

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
from app.utils.page_common import get_theme_vars, get_ref_date, month_list, section_tone


# 공장 연료 용수 화면을 구성합니다.
def render_factory_fuel_water():
    # 페이지 이동 후 재방문에도 필터 값을 유지
    persist_many({
        "ffw_daily_factory":  None,
        "ffw_daily_month":    None,
        "ffw_period_factory": None,
        "ffw_start_date":     None,
        "ffw_end_date":       None,
        "ffw_yoy_factory":    None,
        "ffw_yoy_year":       None,
        "ffw_yoy_metric":     None,
        "ffw_yoy_show_val":   None,
        "ffw_ratio_factory":  None,
        "ffw_ratio_month":    None,
    })

    # 테마 변수 로컬화
    theme_vars = get_theme_vars()
    L_FONT = theme_vars["FONT"]
    L_GRID = theme_vars["GRID"]
    L_TEXT = theme_vars["TEXT"]

    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">🔥💧</span>
        <div>
            <div class="sub-page-title">연료·용수 사용량</div>
            <div class="sub-page-breadcrumb">에너지 모니터링 > 연료·용수 사용량</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    db_factories = get_factories()
    db_min, db_max = get_date_range()

    if not db_factories or not db_min:
        st.info("데이터가 없습니다.")
        return

    ref_date = get_ref_date()

    with st.container(border=True):
        # ── 섹션 1: 일별 사용량 비교 ─────────────────────────────────────
        section_tone("cyan")
        st.markdown('<div class="chart-title" style="font-size:1.05rem; margin-top:8px;"><div class="chart-title-dot"></div>📅 일별 사용량 비교</div>', unsafe_allow_html=True)

        months = month_list(db_min, db_max)
        default_month = ref_date.strftime("%Y-%m")
        default_month = default_month if default_month in months else (months[-1] if months else default_month)

        col_m1, col_m2, _ = st.columns([1, 1, 2])
        with col_m1:
            daily_factory = st.selectbox("공장 선택 (일별)", options=["전사"] + db_factories, index=0, key="ffw_daily_factory")
        with col_m2:
            selected_month = st.selectbox("조회 월", options=months,
                index=months.index(default_month) if default_month in months else len(months) - 1,
                key="ffw_daily_month")

        d_factories = [daily_factory]

        year_m, month_m = int(selected_month[:4]), int(selected_month[5:7])
        last_day = monthrange(year_m, month_m)[1]
        daily_df = get_daily_data(factories=d_factories, date_from=f"{selected_month}-01", date_to=f"{selected_month}-{last_day:02d}")

        if daily_df.empty:
            st.warning("해당 월에 데이터가 없습니다.")
        else:
            col_k1, col_k2, col_k3, col_k4, col_k5 = st.columns(5)
            with col_k1:
                st.metric("총 연료량", f"{daily_df['fuel_nm3'].sum():,.0f} Nm³")
            with col_k2:
                st.metric("총 용수량", f"{daily_df['water_ton'].sum():,.0f} ton")
            with col_k3:
                st.metric("총 폐수량", f"{daily_df['wastewater_ton'].sum():,.0f} ton")
            with col_k4:
                prod = daily_df['mix_prod_kg'].sum()
                fuel_intensity = daily_df['fuel_nm3'].sum() / (prod / 1000) if prod > 0 else 0
                st.metric("연료 원단위", f"{fuel_intensity:,.1f} Nm³/ton")
            with col_k5:
                water_intensity = daily_df['water_ton'].sum() / (prod / 1000) if prod > 0 else 0
                st.metric("용수 원단위", f"{water_intensity:,.1f} ton/ton")

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            col_c1, col_c2 = st.columns(2)

            with col_c1:
                st.markdown('<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>일별 연료사용량</div>', unsafe_allow_html=True)
                d_agg = daily_df.groupby("date").agg({"fuel_nm3": "sum"}).reset_index().sort_values("date")
                fig = go.Figure()
                kwargs = dict(name="연료량", x=d_agg["date"], y=d_agg["fuel_nm3"], mode="lines+markers", line=dict(color="#E8450A", width=2))
                fig.add_trace(go.Scatter(**kwargs))
                fig.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=60),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16), showlegend=False)
                fig.update_xaxes(gridcolor=L_GRID, title_text="날짜", tickformat="%-d일", dtick=86400000*3,
                                 tickangle=0, range=[f"{selected_month}-01", f"{selected_month}-{last_day:02d}"],
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                fig.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="Nm³",
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig, use_container_width=True, key="ffw_daily_fuel")
                with st.expander("📄 일별 연료 상세 데이터 보기"):
                    _d1 = rename_columns_to_korean(daily_df[["factory", "date", "fuel_nm3"]])
                    st.dataframe(_d1, use_container_width=True, hide_index=True,
                                 column_config=numeric_column_config(_d1))

            with col_c2:
                st.markdown('<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>일별 용수/폐수량</div>', unsafe_allow_html=True)
                d_agg2 = daily_df.groupby("date").agg({"water_ton": "sum", "wastewater_ton": "sum"}).reset_index().sort_values("date")
                fig2 = go.Figure()
                for col_k, nm, clr in [("water_ton", "용수량", "#0EA5E9"), ("wastewater_ton", "폐수량", "#6B7280")]:
                    kwargs2 = dict(name=nm, x=d_agg2["date"], y=d_agg2[col_k], mode="lines+markers", line=dict(color=clr, width=2))
                    fig2.add_trace(go.Scatter(**kwargs2))
                fig2.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=60),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
                fig2.update_xaxes(gridcolor=L_GRID, title_text="날짜", tickformat="%-d일", dtick=86400000*3,
                                  tickangle=0, range=[f"{selected_month}-01", f"{selected_month}-{last_day:02d}"],
                                  tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                fig2.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="ton",
                                  tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig2, use_container_width=True, key="ffw_daily_water")
                with st.expander("📄 일별 용수/폐수 상세 데이터 보기"):
                    _d2 = rename_columns_to_korean(daily_df[["factory", "date", "water_ton", "wastewater_ton"]])
                    st.dataframe(_d2, use_container_width=True, hide_index=True,
                                 column_config=numeric_column_config(_d2))



    with st.container(border=True):
        # ── 섹션 2: 기간별 사용량 비교 ─────────────────────────────────────
        section_tone("emerald")
        st.markdown('<div class="chart-title" style="font-size:1.05rem;"><div class="chart-title-dot"></div>📆 기간별 사용량 비교</div>', unsafe_allow_html=True)

        db_min_date = pd.to_datetime(db_min).date()
        db_max_date = pd.to_datetime(db_max).date()
        default_end = min(ref_date, db_max_date)
        default_start = max(default_end - timedelta(days=6), db_min_date)

        col_y1, col_y2, col_y3, _ = st.columns([1, 1, 1, 1])
        with col_y1:
            period_factory = st.selectbox("공장 선택 (기간별)", options=["전사"] + db_factories, index=0, key="ffw_period_factory")
        with col_y2:
            start_date = st.date_input("시작일", value=default_start, min_value=db_min_date, max_value=db_max_date, key="ffw_start_date")
        with col_y3:
            end_date = st.date_input("종료일", value=default_end, min_value=db_min_date, max_value=db_max_date, key="ffw_end_date")

        period_df = get_daily_data(factories=[period_factory], date_from=start_date.strftime("%Y-%m-%d"), date_to=end_date.strftime("%Y-%m-%d"))

        if period_df.empty:
            st.warning("기간별 데이터가 없습니다.")
        else:
            col_pk1, col_pk2, col_pk3, col_pk4, col_pk5 = st.columns(5)
            with col_pk1:
                st.metric("기간 총 연료량", f"{period_df['fuel_nm3'].sum():,.0f} Nm³")
            with col_pk2:
                st.metric("기간 총 용수량", f"{period_df['water_ton'].sum():,.0f} ton")
            with col_pk3:
                st.metric("기간 총 폐수량", f"{period_df['wastewater_ton'].sum():,.0f} ton")
            with col_pk4:
                p_prod = period_df['mix_prod_kg'].sum()
                p_fuel_intensity = period_df['fuel_nm3'].sum() / (p_prod / 1000) if p_prod > 0 else 0
                st.metric("연료 원단위", f"{p_fuel_intensity:,.1f} Nm³/ton")
            with col_pk5:
                p_water_intensity = period_df['water_ton'].sum() / (p_prod / 1000) if p_prod > 0 else 0
                st.metric("용수 원단위", f"{p_water_intensity:,.1f} ton/ton")

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            col_c1, col_c2 = st.columns(2)
            with col_c1:
                st.markdown('<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>기간별 연료사용 추이</div>', unsafe_allow_html=True)
                p_fuel_agg = period_df.groupby(["factory", "date"]).agg({"fuel_nm3": "sum"}).reset_index().sort_values(["date", "factory"])
            
                colors_cycle = ["#E8450A", "#ecc94b", "#6B7280", "#48bb78", "#0EA5E9"]
                fig3 = go.Figure()
                for i, fac in enumerate(sorted(p_fuel_agg["factory"].unique())):
                    f_data = p_fuel_agg[p_fuel_agg["factory"] == fac]
                    clr = colors_cycle[i % len(colors_cycle)]
                    kwargs3 = dict(name=fac, x=f_data["date"], y=f_data["fuel_nm3"], mode="lines+markers", line=dict(color=clr, width=2), marker=dict(color=clr))
                    fig3.add_trace(go.Scatter(**kwargs3))
                fig3.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=40),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
                fig3.update_xaxes(gridcolor=L_GRID, tickangle=0, title_text="", tickformat="%m/%d", tickfont=dict(color=L_FONT, size=16))
                fig3.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="Nm³", tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig3, use_container_width=True, key="ffw_period_fuel")
                with st.expander("📄 기간별 연료 상세 데이터 보기"):
                    _p1 = rename_columns_to_korean(period_df[["factory", "date", "fuel_nm3"]])
                    st.dataframe(_p1, use_container_width=True, hide_index=True,
                                 column_config=numeric_column_config(_p1))

            with col_c2:
                st.markdown('<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>기간별 용수/폐수 추이</div>', unsafe_allow_html=True)
                p_agg = period_df.groupby("date").agg({"water_ton": "sum", "wastewater_ton": "sum"}).reset_index().sort_values("date")
            
                fig4 = go.Figure()
                for col_k, nm, clr in [("water_ton", "용수량", "#0EA5E9"), ("wastewater_ton", "폐수량", "#6B7280")]:
                    kwargs4 = dict(name=nm, x=p_agg["date"], y=p_agg[col_k], mode="lines+markers", line=dict(color=clr, width=2))
                    fig4.add_trace(go.Scatter(**kwargs4))
                fig4.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=40),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
                fig4.update_xaxes(gridcolor=L_GRID, tickangle=0, title_text="", tickformat="%m/%d", tickfont=dict(color=L_FONT, size=16))
                fig4.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="ton", tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig4, use_container_width=True, key="ffw_period_water")
                with st.expander("📄 기간별 용수/폐수 상세 데이터 보기"):
                    _p2 = rename_columns_to_korean(period_df[["factory", "date", "water_ton", "wastewater_ton"]])
                    st.dataframe(_p2, use_container_width=True, hide_index=True,
                                 column_config=numeric_column_config(_p2))

    with st.container(border=True):
        # ── 섹션 3: 전년대비 사용 분석 ─────────────────────────────────────
        section_tone("violet")
        st.markdown('<div class="chart-title" style="font-size:1.05rem;"><div class="chart-title-dot"></div>📈 전년대비 연료/용수 사용 분석</div>', unsafe_allow_html=True)

        col_y1, col_y2 = st.columns([1, 3])
        with col_y1:
            yoy_factory = st.selectbox("공장 (전년비교)", options=["전사"] + db_factories, key="ffw_yoy_factory")
            yoy_year = st.selectbox("기준연도", options=list(range(datetime.now().year, 1999, -1)), key="ffw_yoy_year")
            yoy_metric_name = st.selectbox("비교 항목", options=["연료량 (Nm³)", "용수량 (ton)", "폐수량 (ton)"], key="ffw_yoy_metric")
            show_val = st.checkbox("데이터 값 표시", value=False, key="ffw_yoy_show_val")

        metric_map = {"연료량 (Nm³)": "fuel_nm3", "용수량 (ton)": "water_ton", "폐수량 (ton)": "wastewater_ton"}
        metric_col = metric_map[yoy_metric_name]

        with col_y2:
            yoy_df = get_yoy_data(yoy_factory, yoy_year, metric_col)
            if not yoy_df.empty and metric_col in yoy_df.columns:
                fig5 = go.Figure()
                for yr in sorted(yoy_df["year"].unique()):
                    yr_data = yoy_df[yoy_df["year"] == yr].copy()
                    yr_data["month_label"] = yr_data["month"].astype(str) + "월"
                    fig5.add_trace(go.Scatter(x=yr_data["month_label"], y=yr_data[metric_col],
                        name=f"{yr}년", mode="lines+markers+text" if show_val else "lines+markers",
                        text=yr_data[metric_col] if show_val else None, textposition="top center", texttemplate="%{text:,.0f}",
                        textfont=dict(size=18, color=L_TEXT),
                        line=dict(dash="solid" if yr == yoy_year else "dash", width=2)))
                fig5.update_layout(height=350, margin=dict(l=40, r=20, t=10, b=40),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    colorway=["#E8450A", "#3a5abb"])
                fig5.update_xaxes(gridcolor=L_GRID, title_text="월", tickangle=0, tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                fig5.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text=yoy_metric_name,
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig5, use_container_width=True, key="ffw_yoy_line")

                # 하단 전년대비 데이터 테이블 추가
                curr_year_data = yoy_df[yoy_df["year"] == yoy_year].set_index("month")[metric_col]
                prev_year_data = yoy_df[yoy_df["year"] == yoy_year - 1].set_index("month")[metric_col]
            
                yoy_table = pd.DataFrame(index=range(1, 13))
                yoy_table.index.name = "월"
                yoy_table["전년 실적"] = prev_year_data
                yoy_table["금년 실적"] = curr_year_data
                yoy_table = yoy_table.fillna(0)
                yoy_table["증감량"] = yoy_table["금년 실적"] - yoy_table["전년 실적"]
                yoy_table["증감률(%)"] = yoy_table.apply(
                    lambda r: (r["증감량"] / r["전년 실적"] * 100) if r["전년 실적"] > 0 else 0, axis=1
                )
            
                # 누계 행 추가
                sum_prev = yoy_table["전년 실적"].sum()
                sum_curr = yoy_table["금년 실적"].sum()
                diff_sum = sum_curr - sum_prev
                diff_pct = (diff_sum / sum_prev * 100) if sum_prev > 0 else 0
            
                yoy_table.loc["누계"] = [sum_prev, sum_curr, diff_sum, diff_pct]
            
                # color rate 관련 처리를 담당합니다.
                def color_rate(val):
                    if isinstance(val, str) or pd.isna(val):
                        return ""
                    if val < 0:
                        return "color: #4da6ff"
                    elif val > 0:
                        return "color: #ff4d4d"
                    return ""
                
                yoy_table["증감률(%)"] = yoy_table["증감률(%)"].round(1)
            
                st.markdown(f'<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>전년대비 월별 데이터 테이블 ({yoy_metric_name})</div>', unsafe_allow_html=True)
            
                styled_table = yoy_table.reset_index().style.set_properties(**{
                    'font-size': '15px'
                }).map(
                    color_rate, subset=["증감률(%)"]
                ).format(
                    {
                        "전년 실적": "{:,.0f}",
                        "금년 실적": "{:,.0f}",
                        "증감량": "{:,.0f}",
                        "증감률(%)": "{:.1f}%"
                    }
                )
            
                st.dataframe(
                    styled_table,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "월": st.column_config.NumberColumn("월", format="%d"),
                        "전년 실적": st.column_config.NumberColumn("전년 실적", format="%,.0f"),
                        "금년 실적": st.column_config.NumberColumn("금년 실적", format="%,.0f"),
                        "증감량": st.column_config.NumberColumn("증감량", format="%,.0f"),
                        "증감률(%)": st.column_config.NumberColumn("증감률(%)", format="%.1f%%")
                    }
                )
            else:
                st.info("전년 대비 데이터가 없습니다.")

        # ── 하단: 공장별 폐수/용수 비 (폐수량/용수량, 당월 기준 혹은 별도 필터 적용) ──────────
        st.markdown('<div class="chart-title" style="font-size:1.05rem; margin-top:16px;"><div class="chart-title-dot"></div>💧 공장별 폐수/용수</div>', unsafe_allow_html=True)
    
        db_min, db_max = get_date_range()
    
        col_r1, col_r2, _ = st.columns([1, 1, 2])
        with col_r1:
            selected_ratio_factory = st.selectbox("공장 선택 (비율)", options=["전사"] + db_factories, index=0, key="ffw_ratio_factory")
        with col_r2:
            selected_ratio_month = st.selectbox("조회 월 (비율)", options=months,
                index=months.index(default_month) if default_month in months else len(months) - 1,
                key="ffw_ratio_month")

        r_factories = [selected_ratio_factory] if selected_ratio_factory != "전사" else None
    
        year_r, month_r = int(selected_ratio_month[:4]), int(selected_ratio_month[5:7])
        last_day_r = monthrange(year_r, month_r)[1]
    
        all_daily = get_daily_data(factories=r_factories, date_from=f"{selected_ratio_month}-01", date_to=f"{selected_ratio_month}-{last_day_r:02d}")
        if not all_daily.empty:
            factory_summary = all_daily.groupby("factory").agg({"water_ton": "sum", "wastewater_ton": "sum"}).reset_index()
            # 용수 합계가 0인 공장은 비 계산 불가 → 0 나눗셈(inf/NaN) 방어를 위해 분모를 결측 처리
            factory_summary["ratio"] = (factory_summary["wastewater_ton"] / factory_summary["water_ton"].replace(0, float("nan"))).round(2)

            fig6 = go.Figure()
            fig6.add_trace(go.Bar(name="용수량", x=factory_summary["factory"], y=factory_summary["water_ton"], marker_color="#0EA5E9", text=factory_summary["water_ton"], textposition="outside", texttemplate="%{text:,.0f}"))
            fig6.add_trace(go.Bar(name="폐수량", x=factory_summary["factory"], y=factory_summary["wastewater_ton"], marker_color="#6B7280", text=factory_summary["wastewater_ton"], textposition="outside", texttemplate="%{text:,.0f}"))
            fig6.update_layout(barmode="group", height=300, margin=dict(l=40, r=20, t=10, b=40),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=L_FONT, size=16),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
            fig6.update_xaxes(gridcolor=L_GRID, tickfont=dict(color=L_FONT, size=16))
            fig6.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="ton", tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
            st.plotly_chart(fig6, use_container_width=True, key="ffw_ratio_bar")

            ratio_display = factory_summary[["factory", "water_ton", "wastewater_ton", "ratio"]].copy()
            ratio_display.columns = ["공장", "용수량 (ton)", "폐수량 (ton)", "폐수/용수"]
            # 비 산출 불가(용수 0) 공장은 '-' 로 표시
            ratio_display["폐수/용수"] = ratio_display["폐수/용수"].map(lambda v: "-" if pd.isna(v) else f"{v:.2f}")
            st.dataframe(
                ratio_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "용수량 (ton)": st.column_config.NumberColumn(format="%,.0f"),
                    "폐수량 (ton)": st.column_config.NumberColumn(format="%,.0f"),
                }
            )
