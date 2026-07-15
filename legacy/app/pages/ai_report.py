"""
AI Energy Report Page
=======================
에너지 실적 보고서 생성 (OpenAI API 연동).
"""
# 이 파일은 AI 리포트 화면을 보여줍니다.

import streamlit as st
from datetime import datetime
from app.database.db_connection import is_admin
from app.services.query_service import get_factories
from app.services.ai_report_service import get_saved_report, save_report
from app.services.ai_db_service import run_agent_report
from app.utils.page_common import section_tone
from app.utils.page_state import persist_many


# AI 리포트 화면을 구성합니다.
def render_ai_report():
    """AI 에너지 실적 보고서 페이지."""

    # 페이지 이동 후 재방문에도 필터 값을 유지
    persist_many({
        "ai_report_factory": None,
        "ai_report_year":    None,
        "ai_report_month":   None,
    })

    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">📄</span>
        <div>
            <div class="sub-page-title">에너지 실적 보고서</div>
            <div class="sub-page-breadcrumb">AI 에너지 분석 > 에너지 실적 보고서</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    # 상단 컨트롤 패널
    with st.container(border=True):
        section_tone("cyan")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">⚙️</span>보고서 생성 조건'
            '<span class="section-title-sub">대상 · 기준 연월 · 트리거</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        col1, col2, col3 = st.columns([1, 1, 2])

        today = datetime.today()
        curr_year = today.year
        curr_month = today.month if today.day > 5 else (today.month - 1) or 12
        if curr_month == 12 and today.day <= 5:
            curr_year -= 1

        with col1:
            selected_factory = st.selectbox(
                "분석 대상",
                options=["전사"] + [f for f in get_factories() if f not in ("전사", "전체")],
                index=0,
                key="ai_report_factory",
            )
        with col2:
            selected_year = st.selectbox(
                "기준 연도", options=range(curr_year - 2, curr_year + 1), index=2,
                key="ai_report_year",
            )
            selected_month = st.selectbox(
                "기준 월", options=range(1, 13), index=curr_month - 1,
                key="ai_report_month",
            )

        with col3:
            st.write("")
            st.write("")

            # 기존 보고서 존재 여부 확인
            saved_report = get_saved_report(selected_factory, selected_year, selected_month)

            # 보고서 생성은 LLM(OpenAI) 비용이 발생하는 유일한 사용자 트리거 —
            # 관리자(host PC) 전용으로 게이트하고, viewer는 저장본 열람만 허용한다.
            # (viewer DB 계정은 SELECT 전용이라 저장 단계에서도 실패했음)
            if is_admin():
                btn_label = "보고서 재생성 (덮어쓰기)" if saved_report else "보고서 생성"
                btn_type = "secondary" if saved_report else "primary"
                generate_btn = st.button(btn_label, use_container_width=True, type=btn_type)
            else:
                generate_btn = False
                st.caption(
                    "🔒 보고서 생성/재생성은 관리자(host PC) 전용입니다. "
                    "이미 생성된 보고서는 아래에서 열람할 수 있습니다."
                )
    
    # 보고서 생성 액션
    if generate_btn:
        with st.spinner("AI 에이전트가 직접 데이터베이스를 조회하여 정밀 분석 중입니다..."):
            report_text = run_agent_report(
                factory=selected_factory,
                year=selected_year,
                month=selected_month
            )
            
            save_success = save_report(selected_factory, selected_year, selected_month, report_text)
            if save_success:
                st.success("보고서가 성공적으로 생성 및 저장되었습니다.")
                st.rerun()  # 저장 후 결과를 반영하기 위해 리프레시
            else:
                st.error("보고서 생성은 완료되었으나 DB 저장에 실패했습니다. 관리자에게 문의하세요.")
    
    # 보고서 출력 영역
    if saved_report:
        with st.container(border=True):
            section_tone("emerald")
            st.markdown(
                '<div class="section-title">'
                '<span class="section-title-icon">📋</span>'
                f'{selected_year}년 {selected_month}월 {selected_factory} 에너지 실적 종합 보고서'
                '</div>',
                unsafe_allow_html=True,
            )
            # HTML 태그 렌더링 지원을 위해 unsafe_allow_html=True
            # 본문은 단일 markdown 호출로 출력 (별도 div 래핑 시 빈 박스 발생)
            st.markdown(
                f'<div style="line-height:1.8; padding:8px 4px;">{saved_report["report_content"]}</div>',
                unsafe_allow_html=True,
            )

            created_at = saved_report["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            updated_at = saved_report["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
            st.caption(f"🕘 최초 생성일: {created_at}  |  🔄 최종 수정일: {updated_at}")
    elif not saved_report and not generate_btn:
        # 빈 상태 안내 — viewer에게는 없는 버튼을 가리키지 않도록 문구 분기
        empty_hint = (
            "우측 상단의 <b>[보고서 생성]</b> 버튼을 눌러 AI 인사이트를 도출해 보세요."
            if is_admin()
            else "관리자가 보고서를 생성하면 이 화면에서 열람할 수 있습니다."
        )
        with st.container(border=True):
            section_tone("violet")
            st.markdown(f"""
            <div style="text-align:center; padding:60px 20px;">
                <div style="font-size:3rem; margin-bottom:10px;">📄</div>
                <div style="color:var(--text-secondary); font-size:1.1rem;">
                    저장된 보고서가 없습니다.<br/>
                    {empty_hint}
                </div>
            </div>
            """, unsafe_allow_html=True)
