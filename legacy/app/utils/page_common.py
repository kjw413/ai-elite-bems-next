"""
Page Common Helpers
===================
에너지 페이지(전력/연료·용수/원단위) 공통 헬퍼.
"""
# 이 파일은 에너지 페이지들이 공유하는 공통 헬퍼를 제공합니다.

import streamlit as st
from datetime import datetime, date, timedelta


# ─ 다크 모드 차트 색상 헬퍼 (main.py DARK_VARS와 동기화) ─
def get_theme_vars():
    return {
        "FONT": "#e9f0fb",
        "GRID": "rgba(120,160,220,0.14)",
        "TEXT": "#e9f0fb",
    }


# ─ 섹션 색조(tone) 마커 ─
# st.container(border=True) 블록의 첫 줄에서 호출하면 main.py의
# :has(.sec-tone-*) CSS가 그 컨테이너를 네이비 카드 + 색조 틴트로 전환합니다.
# 사용 가능 tone: cyan / emerald / violet / amber / rose
#
# Streamlit 1.49+에서는 모든 레이아웃 블록이 stLayoutWrapper로 감싸여
# CSS만으로는 "보더 컨테이너"를 구별할 수 없음 → 이 마커가 섹션 식별자 역할.
# 즉, 섹션 카드 스타일(네이비 배경/좌측 액센트/패딩)을 받으려면 호출이 필수.
# 매칭은 직계 체인(stLayoutWrapper > stVerticalBlock > stElementContainer)이라
# 조상 블록/중첩 컨테이너로 번지지 않음.
def section_tone(tone: str) -> None:
    st.markdown(f'<span class="sec-tone sec-tone-{tone}"></span>', unsafe_allow_html=True)


# 기준 날짜 값을 가져옵니다.
def get_ref_date() -> date:
    """페이지 기본 조회 기준일 — 어제 (수집 지연으로 당일 데이터는 아직 없음).

    과거에는 'filter_date_to' 세션 키를 우선 참조했으나 그 키를 설정하는 코드가
    없어 항상 기본값이 쓰였다. 죽은 분기를 제거하고 의도를 명시한다.
    """
    return date.today() - timedelta(days=1)


# CSV 다운로드 버튼 — 화면 표와 동일한 데이터를 보고서/엑셀 작업용으로 내려받기.
def csv_download(df, *, filename: str, key: str, label: str = "⬇️ CSV",
                 use_container_width: bool = False) -> None:
    """utf-8-sig(BOM) 인코딩으로 엑셀에서 한글이 깨지지 않는 CSV 다운로드 버튼."""
    st.download_button(
        label=label,
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name=filename,
        mime="text/csv",
        key=key,
        use_container_width=use_container_width,
        help="현재 화면 필터가 적용된 데이터를 CSV로 내려받습니다.",
    )


# 월 목록을 만듭니다.
def month_list(db_min: str, db_max: str):
    months = []
    cur = datetime.strptime(db_min, "%Y-%m-%d").date().replace(day=1)
    end = datetime.strptime(db_max, "%Y-%m-%d").date().replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        cur = cur.replace(month=cur.month + 1) if cur.month < 12 else cur.replace(year=cur.year + 1, month=1)
    return months
