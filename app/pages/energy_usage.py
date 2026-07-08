"""
Integrated Energy Usage Page
============================
전력·연료·용수·폐수 사용량 통합 분석.
"""
from __future__ import annotations

import streamlit as st

from app.components.energy_usage import (
    USAGE_METRICS,
    default_period_bounds,
    render_daily_usage_panel,
    render_period_usage_panel,
    render_wastewater_ratio_section,
    render_yoy_usage_section,
)
from app.services.query_service import get_date_range, get_factories
from app.utils.page_common import get_ref_date, get_theme_vars, month_list, section_tone
from app.utils.page_state import persist_many


def render_energy_usage():
    """전력·연료·용수·폐수 사용량을 단일 페이지에서 조회합니다."""
    persist_many({
        "eu_source": None,
        "eu_daily_factory": None,
        "eu_daily_month": None,
        "eu_period_factory": None,
        "eu_start_date": None,
        "eu_end_date": None,
        "eu_yoy_factory": None,
        "eu_yoy_year": None,
        "eu_yoy_show_val": None,
        "eu_ratio_factory": None,
        "eu_ratio_month": None,
    })

    st.markdown(
        """
        <div class="sub-page-header">
            <span style="font-size:1.5rem;">⚡</span>
            <div>
                <div class="sub-page-title">사용량 통합</div>
                <div class="sub-page-breadcrumb">에너지 모니터링 > 사용량 통합</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    db_factories = get_factories()
    db_min, db_max = get_date_range()
    if not db_factories or not db_min:
        st.info("데이터가 없습니다. Data Upload 탭에서 데이터를 업로드해 주세요.")
        return

    theme = get_theme_vars()
    ref_date = get_ref_date()
    months = month_list(db_min, db_max)
    default_month = ref_date.strftime("%Y-%m")
    default_month = default_month if default_month in months else (months[-1] if months else default_month)
    db_min_date, db_max_date, default_start, default_end = default_period_bounds(db_min, db_max)

    source_names = list(USAGE_METRICS.keys())
    if st.session_state.get("eu_source") not in source_names:
        st.session_state["eu_source"] = "전력"

    with st.container(border=True):
        section_tone("cyan")
        src = st.radio(
            "에너지원 선택",
            options=source_names,
            horizontal=True,
            key="eu_source",
            captions=["설비 분해 포함", "도시가스 사용량", "용수 사용량", "폐수 배출량"],
        )
        spec = USAGE_METRICS[src]
        st.caption(
            "공장·기간 필터는 에너지원 간 공유됩니다. 전력에서 용수로 바꿔도 같은 조건으로 바로 비교할 수 있습니다."
        )

    with st.container(border=True):
        section_tone("cyan")
        col_daily, col_period = st.columns(2)
        with col_daily:
            render_daily_usage_panel(spec, db_factories, months, default_month, theme)
        with col_period:
            render_period_usage_panel(spec, db_factories, db_min_date, db_max_date, default_start, default_end, theme)

    with st.container(border=True):
        section_tone("violet")
        render_yoy_usage_section(spec, db_factories, db_min, db_max, theme)

    if spec.key in ("water", "wastewater"):
        with st.container(border=True):
            section_tone("emerald")
            render_wastewater_ratio_section(db_factories, months, default_month, theme)

    st.caption(
        "※ 원단위는 사용량과 산식이 달라 별도 '원단위' 페이지에서 관리합니다. "
        "기간/연 원단위는 일 원단위 평균이 아니라 사용량 ÷ 생산량으로 재계산됩니다."
    )
