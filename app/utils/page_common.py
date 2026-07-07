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


# 기준 날짜 값을 가져옵니다.
def get_ref_date() -> date:
    return st.session_state.get("filter_date_to", date.today() - timedelta(days=1))


# 월 목록을 만듭니다.
def month_list(db_min: str, db_max: str):
    months = []
    cur = datetime.strptime(db_min, "%Y-%m-%d").date().replace(day=1)
    end = datetime.strptime(db_max, "%Y-%m-%d").date().replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        cur = cur.replace(month=cur.month + 1) if cur.month < 12 else cur.replace(year=cur.year + 1, month=1)
    return months
