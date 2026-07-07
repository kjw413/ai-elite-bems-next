"""
Savings Plan Management Page
==============================
절감 계획 관리 (플레이스홀더).
"""
# 이 파일은 절감 계획 화면을 보여줍니다.

import streamlit as st


# 절감 plan 화면을 구성합니다.
def render_savings_plan():
    """절감 계획 관리 페이지."""
    
    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">📋</span>
        <div>
            <div class="sub-page-title">절감 계획 관리</div>
            <div class="sub-page-breadcrumb">에너지 절감관리 > 절감 계획 관리</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("""
    <div style="text-align:center; padding:60px 20px;">
        <div style="font-size:4rem; margin-bottom:20px;">📋</div>
        <div style="font-size:1.3rem; color:#e0e0e0; margin-bottom:10px;">절감 계획 관리</div>
        <div style="color:#8892b0; margin-bottom:30px;">이 기능은 향후 업데이트에서 제공될 예정입니다.</div>
        <div style="background: linear-gradient(135deg, #141937, #1a2050); border:1px solid #2a3a8a; border-radius:12px; padding:24px; max-width:600px; margin:0 auto; text-align:left;">
            <div style="color:#00d4ff; font-weight:600; margin-bottom:12px;">예정 기능:</div>
            <ul style="color:#a0aec0; line-height:2;">
                <li>연간/분기별 에너지 절감 목표 설정</li>
                <li>공장별 절감 계획 수립 및 추적</li>
                <li>절감 시나리오 시뮬레이션</li>
                <li>절감 계획 대비 실적 모니터링</li>
                <li>절감 활동 이력 관리</li>
            </ul>
        </div>
    </div>
    """, unsafe_allow_html=True)
