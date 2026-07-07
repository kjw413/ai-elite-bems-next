"""
Energy Factory Power Analysis Page
====================================
전력 사용량 분석 - 일별/월별/전년대비 섹션 구조.
"""
# 이 파일은 공장별 전력 분석 화면을 보여줍니다.

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


# 공장 전력 화면을 구성합니다.
def render_factory_power():
    # 페이지 이동 후 재방문에도 필터 값을 유지 (default=None → widget 자체 default 사용)
    persist_many({
        "fp_daily_factory":  None,
        "fp_daily_month":    None,
        "fp_period_factory": None,
        "fp_start_date":     None,
        "fp_end_date":       None,
        "fp_yoy_factory":    None,
        "fp_yoy_year":       None,
        "fp_yoy_show_val":   None,
    })

    # 테마 변수 로컬화
    theme_vars = get_theme_vars()
    L_FONT = theme_vars["FONT"]
    L_GRID = theme_vars["GRID"]
    L_TEXT = theme_vars["TEXT"]

    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">⚡</span>
        <div>
            <div class="sub-page-title">전력 사용량</div>
            <div class="sub-page-breadcrumb">에너지 모니터링 > 전력 사용량</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    db_factories = get_factories()
    db_min, db_max = get_date_range()

    if not db_factories or not db_min:
        st.info("데이터가 없습니다. Data Upload 탭에서 데이터를 업로드해 주세요.")
        return

    ref_date = get_ref_date()



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
            st.markdown('<div class="chart-title" style="font-size:1.05rem; margin-top:8px;"><div class="chart-title-dot"></div>📅 일별 추이 비교</div>', unsafe_allow_html=True)
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                daily_factory = st.selectbox("공장 선택 (일별)", options=["전사"] + db_factories, index=0, key="fp_daily_factory")
            with col_m2:
                selected_month = st.selectbox("조회 월 (일별)", options=months,
                    index=months.index(default_month) if default_month in months else len(months) - 1,
                    key="fp_daily_month")

            year_m, month_m = int(selected_month[:4]), int(selected_month[5:7])
            last_day = monthrange(year_m, month_m)[1]
            daily_df = get_daily_data(factories=[daily_factory], date_from=f"{selected_month}-01", date_to=f"{selected_month}-{last_day:02d}")

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            if daily_df.empty:
                st.warning("일별 데이터가 없습니다.")
            else:
                daily_df["other_power_kwh"] = daily_df["total_power_kwh"] - daily_df["freezing_power_kwh"] - daily_df["air_compressor_kwh"]
            
                dk1, dk2, dk3, dk4 = st.columns(4)
                with dk1:
                    st.metric("총 전력량", f"{daily_df['total_power_kwh'].sum():,.0f} kWh")
                with dk2:
                    st.metric("냉동전력량", f"{daily_df['freezing_power_kwh'].sum():,.0f} kWh")
                with dk3:
                    st.metric("공압기전력", f"{daily_df['air_compressor_kwh'].sum():,.0f} kWh")
                with dk4:
                    st.metric("기타전력량", f"{daily_df['other_power_kwh'].sum():,.0f} kWh")
                st.markdown("<div style='font-size:0.75rem; color:var(--text-secondary); margin-bottom:12px;'>* 기타: 생산설비, 보일러, 조명, 사무 전력 등 일반 부하 합계</div>", unsafe_allow_html=True)

                daily_agg = daily_df.groupby("date").agg({
                    "total_power_kwh": "sum", "freezing_power_kwh": "sum", "air_compressor_kwh": "sum", "other_power_kwh": "sum"
                }).reset_index().sort_values("date")
            
                fig = go.Figure()
                for col_k, nm, clr in [("total_power_kwh", "전체 전력량", "#00d4ff"), ("freezing_power_kwh", "냉동전력량", "#7b2ff7"), ("air_compressor_kwh", "공압기", "#f97316"), ("other_power_kwh", "기타", "#48bb78")]:
                    kwargs = dict(name=nm, x=daily_agg["date"], y=daily_agg[col_k], mode="lines+markers", line=dict(color=clr, width=2))
                    fig.add_trace(go.Scatter(**kwargs))
            
                fig.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=60),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
            
                # 일별 눈금은 3일 간격으로 표시 (D1으로 모든 날짜 표시 시 가독성 저하)
                fig.update_xaxes(gridcolor=L_GRID, title_text="날짜", tickformat="%-d일", dtick=86400000*3,
                                 tickangle=0, range=[f"{selected_month}-01", f"{selected_month}-{last_day:02d}"],
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
            
                fig.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="kWh",
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig, use_container_width=True, key="fp_daily_bar")

                with st.expander("📄 일별 상세 데이터 보기"):
                    tbl_df = daily_df.copy()
                    tbl_df["date"] = pd.to_datetime(tbl_df["date"]).dt.strftime("%Y-%m-%d")
                    tbl_df = tbl_df.sort_values(["date", "factory"])
                    disp_df = rename_columns_to_korean(tbl_df)
                    st.dataframe(
                        disp_df, use_container_width=True, hide_index=True,
                        column_config=numeric_column_config(disp_df),
                    )

        with col_c2:
            st.markdown('<div class="chart-title" style="font-size:1.05rem; margin-top:8px;"><div class="chart-title-dot"></div>📅 기간별 추이 비교</div>', unsafe_allow_html=True)
            col_p1, col_p2, col_p3 = st.columns([1, 1, 1])
            with col_p1:
                period_factory = st.selectbox("공장 선택 (기간별)", options=["전사"] + db_factories, index=0, key="fp_period_factory")
            with col_p2:
                start_date = st.date_input("시작일", value=default_start, min_value=db_min_date, max_value=db_max_date, key="fp_start_date")
            with col_p3:
                end_date = st.date_input("종료일", value=default_end, min_value=db_min_date, max_value=db_max_date, key="fp_end_date")

            period_df = get_daily_data(factories=[period_factory], date_from=start_date.strftime("%Y-%m-%d"), date_to=end_date.strftime("%Y-%m-%d"))

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            if period_df.empty:
                st.warning("기간별 데이터가 없습니다.")
            else:
                period_df["other_power_kwh"] = period_df["total_power_kwh"] - period_df["freezing_power_kwh"] - period_df["air_compressor_kwh"]
            
                k1, k2, k3, k4 = st.columns(4)
                with k1:
                    st.metric("총 전력량", f"{period_df['total_power_kwh'].sum():,.0f} kWh")
                with k2:
                    st.metric("냉동전력량", f"{period_df['freezing_power_kwh'].sum():,.0f} kWh")
                with k3:
                    st.metric("공압기전력", f"{period_df['air_compressor_kwh'].sum():,.0f} kWh")
                with k4:
                    st.metric("기타전력량", f"{period_df['other_power_kwh'].sum():,.0f} kWh")
                st.markdown("<div style='font-size:0.75rem; color:var(--text-secondary); margin-bottom:12px;'>* 기타: 생산설비, 보일러, 조명, 사무 전력 등 일반 부하 합계</div>", unsafe_allow_html=True)

                p_agg = period_df.groupby("date").agg({
                    "total_power_kwh": "sum", "freezing_power_kwh": "sum", "air_compressor_kwh": "sum", "other_power_kwh": "sum"
                }).reset_index().sort_values("date")
            
                fig2 = go.Figure()
                for col_k, nm, clr in [("total_power_kwh", "전체 전력량", "#00d4ff"), ("freezing_power_kwh", "냉동전력량", "#7b2ff7"), ("air_compressor_kwh", "공압기", "#f97316"), ("other_power_kwh", "기타", "#48bb78")]:
                    kwargs = dict(name=nm, x=p_agg["date"], y=p_agg[col_k], mode="lines+markers", line=dict(color=clr, width=2))
                    fig2.add_trace(go.Scatter(**kwargs))
            
                fig2.update_layout(height=400, margin=dict(l=40, r=20, t=10, b=40),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
            
                fig2.update_xaxes(gridcolor=L_GRID, tickangle=0, title_text="", tickformat="%m/%d", tickfont=dict(color=L_FONT, size=16))
                fig2.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="kWh", tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig2, use_container_width=True, key="fp_period_line")

                with st.expander("📄 기간별 상세 데이터 보기"):
                    p_tbl = period_df.copy()
                    p_tbl["date"] = pd.to_datetime(p_tbl["date"]).dt.strftime("%Y-%m-%d")
                    p_tbl = p_tbl.sort_values(["date", "factory"])
                    disp_p = rename_columns_to_korean(p_tbl)
                    st.dataframe(
                        disp_p, use_container_width=True, hide_index=True,
                        column_config=numeric_column_config(disp_p),
                    )

    with st.container(border=True):
        # ── 섹션 3: 전년대비 사용 분석 ─────────────────────────────────────
        st.markdown('<div class="chart-title" style="font-size:1.05rem;"><div class="chart-title-dot"></div>📈 전년대비 전력 사용량</div>', unsafe_allow_html=True)

        col_y1, col_y2 = st.columns([1, 3])
        with col_y1:
            yoy_factory = st.selectbox("공장", options=["전사"] + db_factories, key="fp_yoy_factory")
            yoy_year = st.selectbox("기준연도", options=list(range(datetime.now().year, 1999, -1)), key="fp_yoy_year")
            show_val = st.checkbox("데이터 값 표시", value=False, key="fp_yoy_show_val")

        with col_y2:
            yoy_df = get_yoy_data(yoy_factory, yoy_year, "total_power_kwh")
            if not yoy_df.empty:
                fig5 = go.Figure()
                for yr in sorted(yoy_df["year"].unique()):
                    yr_data = yoy_df[yoy_df["year"] == yr].copy()
                    yr_data["month_label"] = yr_data["month"].astype(str) + "월"
                    fig5.add_trace(go.Scatter(x=yr_data["month_label"], y=yr_data["total_power_kwh"],
                        name=f"{yr}년", mode="lines+markers+text" if show_val else "lines+markers",
                        text=yr_data["total_power_kwh"] if show_val else None, textposition="top center", texttemplate="%{text:,.0f}",
                        textfont=dict(size=18, color=L_TEXT),
                        line=dict(dash="solid" if yr == yoy_year else "dash", width=2)))
                fig5.update_layout(height=350, margin=dict(l=40, r=20, t=10, b=40),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=L_FONT, size=16),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    colorway=["#00d4ff", "#3a5abb"])
            
                fig5.update_xaxes(gridcolor=L_GRID, title_text="월", tickangle=0, tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                fig5.update_yaxes(tickformat="~s", gridcolor=L_GRID, title_text="kWh",
                                 tickfont=dict(color=L_FONT, size=16), title_font=dict(color=L_FONT))
                st.plotly_chart(fig5, use_container_width=True, key="fp_yoy_line")

                # 하단 전년대비 데이터 테이블 추가
                curr_year_data = yoy_df[yoy_df["year"] == yoy_year].set_index("month")["total_power_kwh"]
                prev_year_data = yoy_df[yoy_df["year"] == yoy_year - 1].set_index("month")["total_power_kwh"]
            
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
            
                st.markdown('<div class="chart-subtitle"><div class="chart-subtitle-bar"></div>전년대비 월별 데이터 테이블</div>', unsafe_allow_html=True)
            
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
