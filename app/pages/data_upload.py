"""
Data Upload Page
==================
데이터 업로드 페이지 (라이트 테마).
"""
# 이 파일은 데이터 업로드 화면을 보여줍니다.

import streamlit as st
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.database.db_connection import is_admin
from app.services.upload_service import upload_excel, get_upload_history
from app.services.daily_energy_sync_service import (
    force_resync,
    get_daily_energy_sync_status,
)
from app.utils.df_format import numeric_column_config


# 자동 동기화 상태 패널을 렌더링합니다.
def _render_auto_sync_panel():
    """학습 소스(`E:\\Sampled DB\\RawDB_에너지.xlsx`) 자동 동기화 상태와 수동 트리거."""
    status = get_daily_energy_sync_status()
    file_exists = status["file_exists"]
    is_up_to_date = status["is_up_to_date"]

    # 헤더 색상: 초록(최신) / 주황(파일 변경 감지) / 회색(파일 없음)
    if not file_exists:
        accent = "#64748b"
        accent_bg = "rgba(100,116,139,0.08)"
        icon = "⚠️"
        headline = "원본 파일 없음"
    elif is_up_to_date:
        accent = "#5eead4"
        accent_bg = "rgba(20,184,166,0.16)"
        icon = "✅"
        headline = "최신 (마지막 동기화 시점과 동일)"
    else:
        accent = "#fbbf24"
        accent_bg = "rgba(245,158,11,0.16)"
        icon = "🔄"
        headline = "원본 파일이 변경되었습니다 — 동기화 권장"

    last_sync = status.get("last_sync_at") or "(아직 없음)"
    last_inserted = status.get("last_inserted", 0)
    last_updated = status.get("last_updated", 0)
    file_mtime = status.get("file_mtime") or "-"
    src_path = status.get("source_path") or "-"

    st.markdown(f"""
    <div style="background:var(--bg-card); border:1px solid var(--border); border-left:4px solid {accent};
                border-radius:14px; padding:16px 18px; margin-bottom:14px;
                box-shadow:0 1px 2px rgba(15,23,42,0.04);">
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
        <span style="font-size:1.2rem;">{icon}</span>
        <span style="color:{accent}; font-weight:700; font-size:0.95rem;">자동 동기화 — 일일 에너지 소스</span>
        <span style="display:inline-block;padding:2px 10px;border-radius:10px;
                     font-size:0.74rem;font-weight:600;
                     color:{accent};background:{accent_bg};">{headline}</span>
      </div>
      <div style="color:var(--text-secondary); font-size:0.85rem; line-height:1.7;">
        <div><b style="color:var(--text-primary);">소스 파일</b>: <code style="color:var(--accent);background:var(--accent-soft);padding:1px 6px;border-radius:4px;">{src_path}</code></div>
        <div><b style="color:var(--text-primary);">파일 수정시각</b>: {file_mtime}</div>
        <div><b style="color:var(--text-primary);">마지막 동기화</b>: {last_sync}
            &nbsp;&nbsp;<span style="color:#64748b;">(직전 신규 {last_inserted}건 / 갱신 {last_updated}건)</span>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    cols = st.columns([3, 1])
    with cols[1]:
        if st.button("🔁 지금 동기화", use_container_width=True, key="btn_force_resync_daily_energy"):
            with st.spinner("학습 소스에서 일일 에너지 데이터를 동기화 중..."):
                result = force_resync()
            if result.get("success"):
                if result.get("file_unchanged"):
                    st.info(result.get("message", "변경 없음"))
                else:
                    st.success(result.get("message", "동기화 완료"))
                    if result.get("errors"):
                        with st.expander(f"⚠️ 검증 경고 {len(result['errors'])}건"):
                            for e in result["errors"]:
                                st.warning(str(e))
                st.rerun()
            else:
                st.error(result.get("message", "동기화 실패"))
                if result.get("errors"):
                    with st.expander(f"오류 상세 ({len(result['errors'])}건)"):
                        for e in result["errors"]:
                            st.warning(str(e))


# 업로드 화면을 보여줍니다.
def render_upload_page():
    """데이터 업로드 페이지."""
    if not is_admin():
        st.warning("⚠️ 이 페이지는 관리자 전용입니다.")
        return
    
    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">📤</span>
        <div>
            <div class="sub-page-title">데이터 업로드</div>
            <div class="sub-page-breadcrumb">데이터 관리 > 업로드</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── 자동 동기화 패널 ─────────────────────────────────────────
    _render_auto_sync_panel()

    _code_style = (
        "color:var(--accent);background:var(--accent-soft);"
        "padding:1px 6px;border-radius:4px;font-size:0.82rem;"
    )
    st.markdown(f"""
    <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:14px;
                padding:18px 20px; margin-bottom:20px;
                box-shadow:0 1px 2px rgba(15,23,42,0.04);">
        <div style="color:var(--text-primary); font-weight:700; margin-bottom:10px; font-size:0.95rem;">
            📋 업로드 안내
        </div>
        <ul style="color:var(--text-secondary); line-height:1.8; margin:0; padding-left:18px;">
            <li>지원 형식: <code style="{_code_style}">.xlsx</code>, <code style="{_code_style}">.xls</code></li>
            <li>시트명: 공장 코드 (<code style="{_code_style}">남양주1</code>, <code style="{_code_style}">남양주2</code>, <code style="{_code_style}">김해</code>, <code style="{_code_style}">광주</code>, <code style="{_code_style}">논산</code>)</li>
            <li>동일 (공장+날짜) 데이터가 이미 존재하면 <strong style="color:#fbbf24;">덮어쓰기(UPSERT)</strong> 됩니다.</li>
            <li>변경 항목은 자동으로 변경 이력에 기록됩니다.</li>
        </ul>
    </div>
    """, unsafe_allow_html=True)
    
    uploaded_file = st.file_uploader(
        "Excel 파일 선택",
        type=["xlsx", "xls"],
        key="file_uploader",
    )
    
    if uploaded_file is not None:
        st.markdown(f"""
        <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:10px;
                    padding:12px 14px; margin:10px 0;
                    box-shadow:0 1px 2px rgba(15,23,42,0.04);">
            <span style="color:var(--accent);">📄</span>
            <strong style="color:var(--text-primary);">{uploaded_file.name}</strong>
            <span style="color:#64748b;">({uploaded_file.size:,} bytes)</span>
        </div>
        """, unsafe_allow_html=True)
        
        if st.button("🚀 업로드 실행", type="primary", use_container_width=True):
            with st.spinner("데이터를 처리 중입니다..."):
                result = upload_excel(uploaded_file, uploaded_file.name, save_original=False)
                
                if result["success"]:
                    st.success(f"✅ {result['message']}")
                    st.balloons()
                else:
                    st.error(f"❌ {result['message']}")
                    if result["errors"]:
                        st.subheader("오류 상세")
                        error_df = pd.DataFrame(result["errors"])
                        st.dataframe(error_df, use_container_width=True,
                                     column_config=numeric_column_config(error_df))
    
    # Upload history
    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="chart-title"><div class="chart-title-dot"></div>업로드 이력</div>', unsafe_allow_html=True)
    
    history = get_upload_history()
    if history:
        hist_df = pd.DataFrame(history)
        hist_df = hist_df.rename(columns={
            "id": "ID", "filename": "파일명", "uploaded_at": "업로드 시간",
            "uploaded_by": "업로드자", "record_count": "건수",
            "status": "상태", "error_message": "오류 메시지",
        })
        st.dataframe(hist_df, use_container_width=True, hide_index=True,
                     column_config=numeric_column_config(hist_df, skip_columns=["ID"]))
    else:
        st.info("업로드 이력이 없습니다.")
