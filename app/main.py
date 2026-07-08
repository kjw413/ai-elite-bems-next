"""
BEMS - Binggrae Energy Management System
==========================================
사이드바 네비게이션, 다크 모드 단일 테마(EMS 관제 스타일), 에너지 색상 직관화.
딥 네이비 배경 + 시안/일렉트릭 블루 글로우 + 글래스 카드로 관제실 시각 위계 구축.
"""
# 이 파일은 웹 앱의 메인 화면, 사이드바 메뉴, 다크 테마 시스템을 구성합니다.

import streamlit as st
import streamlit.components.v1 as components
import sys, base64
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.database.db_connection import init_db, is_admin
from app.services.audit_service import get_current_user
from app.services.query_service import get_record_count

st.set_page_config(
    page_title="BEMS - 빙그레 에너지 관리시스템",
    page_icon="⚡", layout="wide",
    initial_sidebar_state="expanded",
)
# DB 초기화(DDL/계정/권한) — 매 rerun 반복 방지를 위해 프로세스당 1회만 실행
@st.cache_resource
def _init_db_once():
    init_db()
    return True
_init_db_once()

# 일일 에너지 데이터 자동 동기화 (매 rerun, mtime 변경 시에만 실제 sync)
# 학습 소스(E:\Sampled DB\RawDB_에너지.xlsx)가 갱신돼 있으면 energy_daily에 UPSERT.
# 파일이 없거나 잠겨 있으면 graceful skip — 앱 기동 자체는 막지 않음.
# 실제로 insert/update가 있었다면 query_service의 cache_data를 비워 즉시 화면 반영.
try:
    from app.services.daily_energy_sync_service import auto_sync_once
    _sync_result = auto_sync_once()
    if _sync_result and (_sync_result.get("inserted") or _sync_result.get("updated")):
        st.cache_data.clear()
except Exception as _exc:
    print(f"[startup] daily energy auto-sync 실패(계속 진행): {_exc}")

# 일별 생산실적 데이터 자동 동기화 (매 rerun, mtime 변경 시에만 실제 sync)
# 통합 파일(E:\Sampled DB\DB_생산실적.xlsx)이 갱신돼 있으면 production_daily에 UPSERT.
# 파일이 없으면 graceful skip — Raw 폴더 통합을 안 돌렸으면 그냥 건너뜀.
# 실제 UPSERT(status="synced")가 있었다면 query_service의 cache_data를 비워 즉시 화면 반영.
try:
    from app.services.production_dw_sync_service import auto_sync_production_once
    _prod_sync_result = auto_sync_production_once()
    if _prod_sync_result and _prod_sync_result.get("status") == "synced":
        st.cache_data.clear()
except Exception as _exc:
    print(f"[startup] production auto-sync 실패(계속 진행): {_exc}")

# 재공품(DB_재공품.xlsx) 변경 감지 → 즉시 캐시 무효화.
# 재공품은 DB 동기화 대상이 아니라 엑셀 직접 읽기(production_correction_service)라,
# 파일만 단독으로 바뀌면 query_service의 ttl=120s 캐시 만료 전까지 화면 반영이 지연됨.
# 파일 mtime 변경이 감지되면 여기서 cache_data를 비워 다음 렌더에 바로 반영.
try:
    from app.services.production_correction_service import wip_changed_needs_cache_clear
    if wip_changed_needs_cache_clear():
        st.cache_data.clear()
except Exception as _exc:
    print(f"[startup] 재공품 변경 감지 실패(계속 진행): {_exc}")

# ── Session State ──
# 권한(is_admin)은 클라이언트 IP 기반으로 매 호출 시 판단하므로 세션에 보관하지 않습니다.
# 호스트 PC(loopback) → root, 외부 PC → viewer 자동 분류 (db_connection.is_admin 참고).
for k, v in {
    "current_page": "dashboard", "current_submenu": None,
    "energy_submenu_open": False,
    "ai_submenu_open": False,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

@st.cache_data
def get_logo_base64():
    """assets 폴더의 로고를 base64로 반환합니다."""
    p = PROJECT_ROOT / "app" / "assets" / "web_logo.png"
    return base64.b64encode(p.read_bytes()).decode() if p.exists() else None


# 사이드바 표시용 — 매 rerun마다 DB COUNT(*) 쿼리를 막기 위한 캐시 wrapper.
# ttl=60초: 데이터 업로드/편집 후 길어도 1분 내 사이드바 카운트가 갱신됨.
@st.cache_data(ttl=60, show_spinner=False)
def _cached_record_count() -> int:
    return get_record_count()


# 사이드바 표시용 — getpass.getuser()는 가볍지만 매 rerun 호출 자체를 줄임.
# 사용자 컨텍스트는 프로세스 수명 내 변하지 않으므로 cache_resource가 적합.
@st.cache_resource(show_spinner=False)
def _cached_current_user() -> str:
    return get_current_user()

def navigate(page, submenu=None):
    """페이지 이동을 담당합니다."""
    st.session_state.current_page = page
    st.session_state.current_submenu = submenu


# 로고/미니 헤더 클릭 → ?nav=home 쿼리 파라미터 → 대시보드로 이동
def _consume_nav_query_param() -> None:
    qp = st.query_params
    nav_val = qp.get("nav")
    if nav_val == "home":
        st.session_state.current_page = "dashboard"
        st.session_state.current_submenu = None
        try:
            del st.query_params["nav"]
        except Exception:
            pass
        st.rerun()


_consume_nav_query_param()

# ────────────────────────────────────────────────────────────────
# 테마 CSS 변수 (다크 모드 단일 — EMS 관제 스타일)
# ────────────────────────────────────────────────────────────────
# 디자인 원칙: 딥 네이비 표면 + 시안/일렉트릭 블루 글로우 액센트, 반투명 글래스 카드,
# 얇은 광량 보더와 은은한 글로우로 관제실(control-room) 깊이감 구축.
# AX 전시회 EMS 화면 레퍼런스: 어두운 남색 배경, 네온 시안 강조, 유리질 패널.
DARK_VARS = """
    /* === 표면 (Surfaces) — 딥 네이비 글래스 === */
    --bg-app:           #070b16;            /* 관제 배경 — 거의 검정에 가까운 남색 */
    --bg-card:          #101a30;            /* 1차 글래스 카드 */
    --bg-card2:         #16223c;            /* 2차 표면 — 살짝 밝은 남색 */

    /* === 경계 (Borders) — 얇은 광량 라인 === */
    --border:           rgba(120,160,220,0.14);   /* 반투명 콜드 블루 라인 */
    --border-strong:    rgba(120,160,220,0.26);   /* 입력 영역용 강조 톤 */
    --border-light:     rgba(120,160,220,0.08);

    /* === 텍스트 (Text) === */
    --text-primary:     #e9f0fb;            /* 니어 화이트 — 강한 대비 */
    --text-secondary:   #9db1cf;            /* 콜드 슬레이트 */
    --text-muted:       #647695;            /* 뮤트 슬레이트 */

    /* === 액센트 (Accent: Electric Cyan-Blue) === */
    --accent:           #38bdf8;            /* sky-400 — 네온 시안 */
    --accent-strong:    #0ea5e9;            /* sky-500 — 호버/강조 */
    --accent-hover:     rgba(56,189,248,0.12);
    --accent-soft:      rgba(56,189,248,0.18);
    --accent-glow:      0 0 0 1px rgba(56,189,248,0.35), 0 0 18px rgba(56,189,248,0.28);

    /* === 스크롤바 === */
    --scrollbar-bg:     transparent;
    --scrollbar-thumb:  #2a3a58;

    /* === 입력 (Inputs) === */
    --input-bg:         #0d1730;
    --input-border:     rgba(120,160,220,0.24);
    --input-border-strong: rgba(140,175,225,0.45);  /* 멀티셀렉트 등 다중 입력용 강조 보더 */

    /* === 탭 === */
    --tab-active-bg:    rgba(56,189,248,0.10);

    /* === 버튼 === */
    --btn-bg:           linear-gradient(135deg,#0ea5e9 0%,#2563eb 100%);
    --btn-border:       rgba(56,189,248,0.55);
    --btn-primary:      linear-gradient(135deg,#22d3ee 0%,#0ea5e9 100%);

    /* === 메트릭 / 카드 / 알림 === */
    --metric-bg:        #101a30;
    --expander-bg:      #0d1730;
    --alert-bg:         rgba(56,189,248,0.07);
    --footer-pill:      #101a30;

    /* === 사이드바 === */
    --sb-bg:            #0a1122;
    --sb-border:        rgba(120,160,220,0.12);
    --sb-text:          #c3d2ea;
    --sb-text-muted:    #7286a6;
    --sb-text-active:   #6fd2fb;
    --sb-hover-bg:      rgba(56,189,248,0.08);
    --sb-active-bg:     rgba(56,189,248,0.14);
    --sb-active-bar:    #38bdf8;
    --sb-logo-title:    #f0f6ff;
    --sb-logo-sub:      #7286a6;
    --sb-section-label: #5c7096;
    --sb-divider:       rgba(120,160,220,0.12);
    --sb-info-text:     #7286a6;

    /* === 섹션 박스 === */
    --section-box-bg:        #101a30;
    --section-box-border:    rgba(120,160,220,0.14);
    --section-box-accent:    #38bdf8;
    --section-title-color:   #e9f0fb;

    /* === 그림자 (Elevation) — 어두운 섀도 + 은은한 글로우 === */
    --shadow-xs:        0 1px 2px rgba(0,0,0,0.35);
    --shadow-sm:        0 1px 2px rgba(0,0,0,0.4), 0 1px 3px rgba(0,0,0,0.35);
    --shadow-md:        0 2px 6px rgba(0,0,0,0.45), 0 8px 24px rgba(0,0,0,0.35);
    --shadow-lg:        0 4px 12px rgba(0,0,0,0.5), 0 18px 40px rgba(0,0,0,0.45);
"""

CORE_CSS = f"""
<style>
/* 본문/사이드바 로고용 폰트 — Outfit (UI 디스플레이) + Inter (본문 가독) */
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');

:root {{ {DARK_VARS} }}

/* ===== GLOBAL FONTS & BACKGROUND ===== */
/* EMS 관제 배경 — 딥 네이비 + 상단 좌/우 은은한 시안·블루 방사형 글로우로 입체감. */
.stApp {{
    font-family: 'Inter', 'Pretendard', 'Segoe UI', 'Noto Sans KR', sans-serif;
    background-color: var(--bg-app) !important;
    background-image:
        radial-gradient(1100px 620px at 12% -8%, rgba(56,189,248,0.10), transparent 60%),
        radial-gradient(1000px 560px at 100% 0%, rgba(37,99,235,0.12), transparent 55%),
        radial-gradient(1200px 800px at 50% 120%, rgba(14,165,233,0.06), transparent 60%) !important;
    background-attachment: fixed !important;
    color: var(--text-primary) !important;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}}

/* ===== HEADER ===== */
header[data-testid="stHeader"] {{
    background-color: transparent !important;
}}

/* ===== SIDEBAR ===== */
section[data-testid="stSidebar"] {{
    background-color: var(--sb-bg) !important;
    border-right: 1px solid var(--sb-border) !important;
}}
section[data-testid="stSidebar"] * {{
    color: var(--sb-text);
}}
[data-testid="stSidebarNav"] {{ display: none; }}

/* 사이드바 vertical block의 기본 gap 제거 → 메뉴 항목들이 빽빽하게 붙음 */
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
    gap: 0 !important;
}}
section[data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"] {{
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
    margin: 0 !important;
    box-shadow: none !important;
}}
/* 사이드바 내부 element 컨테이너 자체의 margin도 0으로 */
section[data-testid="stSidebar"] [data-testid="stElementContainer"] {{
    margin: 0 !important;
}}

/* 핵심: <button> 자체를 flex 컨테이너로 강제하고 좌측 정렬 (Streamlit 기본 center 오버라이드) */
section[data-testid="stSidebar"] .stButton > button {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: var(--sb-text) !important;
    font-size: 0.86rem !important;
    font-weight: 500 !important;
    padding: 7px 14px !important;
    border-radius: 8px !important;
    width: 100% !important;
    margin: 0 !important;
    transition: background 0.15s, color 0.15s !important;
    line-height: 1.35 !important;
    /* ↓ 부모탭이 가운데 정렬로 보이는 문제 해결 — 버튼 본체에 left-align 강제 */
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    text-align: left !important;
}}
section[data-testid="stSidebar"] .stButton > button:hover {{
    background: var(--sb-hover-bg) !important;
    color: var(--sb-text-active) !important;
    transform: none !important;
    border: none !important;
    box-shadow: none !important;
}}

/* 버튼 내부 wrapper들도 동일하게 좌측 정렬 + 폭 100% 강제 */
section[data-testid="stSidebar"] .stButton > button > div,
section[data-testid="stSidebar"] .stButton > button > div > p {{
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    text-align: left !important;
    width: 100% !important;
    gap: 10px !important;
    margin: 0 !important;
}}

/* Material Symbols 아이콘: 고정 폭 + line-height:1로 행간 베이스라인 100% 일치 */
section[data-testid="stSidebar"] .stButton > button [data-testid="stIconMaterial"],
section[data-testid="stSidebar"] .stButton > button span[class*="material-symbols"] {{
    flex-shrink: 0 !important;
    width: 1.25em !important;
    min-width: 1.25em !important;
    height: 1.25em !important;
    font-size: 1.05rem !important;
    font-weight: 400 !important;
    line-height: 1 !important;             /* ← 아이콘별 ascender/descender 차이 제거 */
    vertical-align: middle !important;     /* ← 텍스트 베이스라인과 정확히 정렬 */
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    text-align: center !important;
    color: var(--sb-text-muted) !important;
    transition: color 0.15s !important;
}}
section[data-testid="stSidebar"] .stButton > button:hover [data-testid="stIconMaterial"],
section[data-testid="stSidebar"] .stButton > button:hover span[class*="material-symbols"] {{
    color: var(--sb-text-active) !important;
}}
/* 마커 숨김 처리하여 불필요한 간격(Spacing) 제거 */
div[data-testid="stElementContainer"]:has(.sb-sub),
div[data-testid="stElementContainer"]:has(.sb-sub-active),
div[data-testid="stElementContainer"]:has(.sb-active-btn),
div[data-testid="stElementContainer"]:has(.sb-nav-btn) {{
    display: none !important;
    margin: 0 !important; padding: 0 !important; height: 0 !important;
}}

/* 활성 메뉴 (부모) — 우측 액센트 바 */
div[data-testid="stElementContainer"]:has(.sb-active-btn) + div[data-testid="stElementContainer"] button {{
    background: var(--sb-active-bg) !important;
    color: var(--sb-text-active) !important;
    border-right: 3px solid var(--sb-active-bar) !important;
    font-weight: 600 !important;
    padding-right: 11px !important;
}}
/* 활성 상태 아이콘 — 강조 컬러로 함께 전환 */
div[data-testid="stElementContainer"]:has(.sb-active-btn) + div[data-testid="stElementContainer"] button [data-testid="stIconMaterial"],
div[data-testid="stElementContainer"]:has(.sb-active-btn) + div[data-testid="stElementContainer"] button span[class*="material-symbols"] {{
    color: var(--sb-text-active) !important;
}}

/* 서브메뉴 컨테이너 — Streamlit 기본 elementContainer 갭만 제거(오버랩 방지) */
div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"],
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] {{
    margin-top: 0 !important;
    margin-bottom: 0 !important;
}}

/* 서브메뉴: 좌측 정렬 + 좌측 인덴트(28px)로 부모-자식 시각적 계층 표현.
   아이콘은 작고 흐릿하게 표시되어 서브임을 직관적으로 알림. */
div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"] button,
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] button {{
    font-size: 0.81rem !important;
    padding: 5px 14px 5px 28px !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    text-align: left !important;
    justify-content: flex-start !important;
    min-height: 30px !important;
    line-height: 1.3 !important;
    font-weight: 400 !important;
    color: var(--sb-text-muted) !important;
}}

/* 서브메뉴 내부 라벨 — 좌측 정렬 + 아이콘 ↔ 텍스트 간격 살짝 좁게 */
div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"] button > div,
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] button > div,
div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"] button > div > p,
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] button > div > p {{
    display: flex !important;
    align-items: center !important;
    gap: 8px !important;
    text-align: left !important;
    justify-content: flex-start !important;
    width: 100% !important;
    margin: 0 !important;
}}

/* 서브메뉴 아이콘 — 작고 흐릿하게(opacity 0.65) → 부모 아이콘과 시각적 위계 차이 부여 */
div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"] button [data-testid="stIconMaterial"],
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] button [data-testid="stIconMaterial"],
div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"] button span[class*="material-symbols"],
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] button span[class*="material-symbols"] {{
    font-size: 0.95rem !important;
    width: 1.15em !important;
    min-width: 1.15em !important;
    opacity: 0.7 !important;
}}

div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"] button:hover {{
    color: var(--sb-text-active) !important;
    background: var(--sb-hover-bg) !important;
}}
div[data-testid="stElementContainer"]:has(.sb-sub) + div[data-testid="stElementContainer"] button:hover [data-testid="stIconMaterial"] {{
    opacity: 1 !important;
    color: var(--sb-text-active) !important;
}}

/* 활성 자식 탭: 부모와 동일하게 우측 3px 액센트 바 + 좌측 정렬 유지 */
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] button {{
    color: var(--sb-text-active) !important;
    font-weight: 600 !important;
    background: var(--sb-active-bg) !important;
    border-right: 3px solid var(--sb-active-bar) !important;
    padding-right: 11px !important;
    border-radius: 0 !important;
}}
div[data-testid="stElementContainer"]:has(.sb-sub-active) + div[data-testid="stElementContainer"] button [data-testid="stIconMaterial"] {{
    opacity: 1 !important;
    color: var(--sb-text-active) !important;
}}

.sb-logo-title {{
    font-family: 'Outfit', 'Inter', sans-serif;
    font-size: 1.95rem;
    font-weight: 700;
    color: var(--sb-logo-title) !important;
    letter-spacing: -0.01em;
    line-height: 1.05;
}}
.sb-logo-sub {{
    font-family: 'Outfit', 'Inter', sans-serif;
    font-size: 0.66rem;
    font-weight: 500;
    color: var(--sb-logo-sub) !important;
    letter-spacing: 0.4px;
    margin-top: 4px;
}}
.sb-section-label {{
    font-size:0.62rem; font-weight:700; color:var(--sb-section-label) !important;
    text-transform:uppercase; letter-spacing:1.4px;
    padding:14px 0 22px 6px; margin:0;
}}
.sb-divider {{ border:none; border-top:1px solid var(--sb-divider); margin:8px 0; }}
.sb-info-text {{ color:var(--sb-info-text) !important; font-size:0.72rem; line-height:1.8; padding:2px 4px; }}

/* ===== CUSTOM HTML CLASSES ===== */
.card-container {{
    background: var(--metric-bg) !important;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    padding: 18px !important;
    margin-bottom: 12px !important;
    box-shadow: var(--shadow-xs) !important;
}}
.kpi-card {{
    background: var(--metric-bg);
    border: 1px solid var(--border);
    border-radius: 14px; padding: 22px;
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    cursor: pointer; height: 100%;
    box-shadow: var(--shadow-xs);
    backdrop-filter: blur(6px);
}}
.kpi-card:hover {{
    border-color: var(--accent);
    box-shadow: var(--shadow-md), var(--accent-glow);
    transform: translateY(-1px);
}}
.kpi-card-title  {{ font-size:0.74rem; color:var(--text-secondary); font-weight:600; text-transform:uppercase; letter-spacing:0.6px; }}
.kpi-card-value  {{ font-size:1.95rem; font-weight:700; color:var(--text-primary); margin:6px 0 2px; line-height:1.1; letter-spacing:-0.02em; }}
.kpi-card-unit   {{ font-size:0.85rem; color:var(--text-muted); font-weight:500; }}
.kpi-card-change {{ font-size:0.78rem; font-weight:600; margin-top:10px; }}
.kpi-change-up      {{ color:#f87171; }}
.kpi-change-down    {{ color:#38bdf8; }}
.kpi-change-neutral {{ color:var(--text-secondary); }}

.chart-container {{
    background: var(--metric-bg); border:1px solid var(--border);
    border-radius:14px; padding:22px; margin-bottom:16px;
    box-shadow: var(--shadow-xs);
    backdrop-filter: blur(6px);
}}
.chart-title {{
    font-size:1rem; font-weight:600; color:var(--text-primary) !important;
    margin-bottom:16px; display:flex; align-items:center; gap:8px;
    letter-spacing:-0.01em;
}}
/* 섹션 마커 — 둥근 도트(파랑) */
.chart-title-dot {{ width:8px; height:8px; border-radius:50%; background:var(--accent); flex-shrink:0; }}
/* 서브섹션 마커 — 세로 바(연한 슬레이트) — 섹션과 시각적 위계 차별화 */
.chart-subtitle-bar {{
    width:3px; height:16px; border-radius:2px;
    background:var(--text-muted); flex-shrink:0;
}}
/* 서브섹션 타이틀 — 본문보다 약간 작고 부드러운 톤 */
.chart-subtitle {{
    font-size:0.95rem; font-weight:600; color:var(--text-secondary) !important;
    margin: 8px 0 12px 0; display:flex; align-items:center; gap:8px;
    letter-spacing:-0.01em;
}}

.section-header {{
    font-size:1.15rem; font-weight:700; color:var(--text-primary) !important;
    margin:22px 0 14px 0; padding-bottom:10px;
    border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:10px;
    letter-spacing:-0.01em;
}}
.sub-page-header {{
    background:var(--metric-bg); border:1px solid var(--border);
    border-radius:14px; padding:16px 22px; margin-bottom:20px;
    display:flex; align-items:center; gap:14px;
    box-shadow: var(--shadow-xs);
}}
.sub-page-title {{ font-size:1.22rem; font-weight:700; color:var(--text-primary) !important; letter-spacing:-0.01em; }}
.sub-page-breadcrumb {{ font-size:0.76rem; color:var(--text-muted) !important; font-weight:500; }}

/* ===== 사이드바 BEMS 로고 영역 — <a> 링크로 클릭 시 대시보드(홈) 이동 ===== */
section[data-testid="stSidebar"] a.bems-logo-home {{
    display: block;
    padding: 20px 12px 14px 12px;
    border-bottom: 1px solid var(--sb-divider);
    margin-bottom: 4px;
    text-decoration: none !important;
    color: inherit !important;
    cursor: pointer;
    transition: background 0.15s;
}}
section[data-testid="stSidebar"] a.bems-logo-home:hover {{
    background: rgba(255,255,255,0.05);
}}
section[data-testid="stSidebar"] a.bems-logo-home,
section[data-testid="stSidebar"] a.bems-logo-home * {{
    text-decoration: none !important;
}}

/* ===== 사이드바 접힘 상태에서 상단에 표시되는 BEMS 미니 헤더 (<a> 링크) ===== */
a.bems-mini-header {{
    display: none;
    position: fixed;
    top: 8px;
    left: 60px;                /* 좌측 ">>" 토글 버튼 공간 확보 */
    z-index: 999990;
    align-items: center;
    gap: 10px;
    padding: 6px 14px;
    background: var(--metric-bg);
    border: 1px solid var(--border);
    border-radius: 999px;
    box-shadow: var(--shadow-sm);
    text-decoration: none !important;
    color: inherit !important;
    user-select: none;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
}}
a.bems-mini-header,
a.bems-mini-header * {{
    text-decoration: none !important;
}}
a.bems-mini-header:hover {{
    background: var(--accent-hover);
    border-color: var(--accent);
    box-shadow: var(--shadow-md);
}}
a.bems-mini-header .bems-mh-logo {{
    width: 22px; height: 22px; flex-shrink: 0;
    display: inline-flex; align-items: center; justify-content: center;
}}
a.bems-mini-header .bems-mh-title {{
    font-size: 0.92rem; font-weight: 700; color: var(--text-primary) !important;
    letter-spacing: -0.01em;
}}
a.bems-mini-header .bems-mh-sep {{
    color: var(--text-muted) !important; font-size: 0.78rem;
}}
a.bems-mini-header .bems-mh-page {{
    font-size: 0.84rem; color: var(--text-secondary) !important; font-weight: 500;
}}
/* 사이드바가 접힌 상태에서만 표시 (Streamlit이 stSidebar에 aria-expanded="false" 부여) */
html:has(section[data-testid="stSidebar"][aria-expanded="false"]) a.bems-mini-header {{
    display: inline-flex;
}}

/* ===== FOOTER ===== */
.fems-footer-v2 {{ margin-top:4rem; padding:2rem 1rem; text-align:center; border-top:1px solid var(--border-light); }}
.footer-status-bar {{
    display:inline-flex; align-items:center; gap:15px;
    background:var(--footer-pill); border:1px solid var(--border);
    padding:9px 26px; border-radius:50px; margin-bottom:20px;
    box-shadow: var(--shadow-sm);
}}
.status-dot {{ width:8px; height:8px; background:#22c55e; border-radius:50%; box-shadow:0 0 8px rgba(34,197,94,0.4); }}
.status-text {{ font-size:0.85rem; color:var(--text-primary) !important; font-weight:500; }}
.sync-time {{ font-size:0.8rem; color:var(--text-secondary) !important; border-left:1px solid var(--border); padding-left:15px; }}
.footer-attribution {{ color:var(--text-secondary) !important; font-size:0.85rem; margin-bottom:12px; }}
.footer-attribution b {{ color:var(--accent); }}

/* ===== SCROLLBAR — 미니멀 ===== */
::-webkit-scrollbar {{ width:8px; height:8px; }}
::-webkit-scrollbar-track {{ background:var(--scrollbar-bg); }}
::-webkit-scrollbar-thumb {{ background:var(--scrollbar-thumb); border-radius:6px; border:2px solid transparent; background-clip: padding-box; }}
::-webkit-scrollbar-thumb:hover {{ background:#94a3b8; background-clip: padding-box; }}

/* ===== SECTION BOX =====
   st.container(border=True) 섹션을 네이비 카드 프레임으로 변환.

   [Streamlit 1.49+ DOM 변경 대응]
   기존 data-testid="stVerticalBlockBorderWrapper"가 제거되고 모든 레이아웃 블록이
   data-testid="stLayoutWrapper"로 감싸짐(보더 여부 무관). stLayoutWrapper를 그대로
   스타일하면 컬럼/일반 블록까지 전부 카드가 되므로, 섹션 컨테이너 첫 줄에 넣는
   .sec-tone 마커(page_common.section_tone)를 "직계 체인"으로 매칭해 섹션만 선별:
     stLayoutWrapper > stVerticalBlock > stElementContainer(마커 보유)
   직계 체인이므로 조상 블록·중첩 컨테이너는 매칭되지 않음.
   → 섹션 카드로 보이려면 반드시 section_tone() 호출 필요. */
[data-testid="stVerticalBlockBorderWrapper"],
[data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .sec-tone) {{
    background: var(--section-box-bg) !important;
    border: 1px solid var(--section-box-border) !important;
    border-left: 3px solid var(--section-box-accent) !important;
    border-radius: 14px !important;
    padding: 20px 24px !important;
    margin-bottom: 22px !important;   /* 섹션 사이 검정 배경이 드러나도록 간격 확대 */
    /* 상단 인셋 하이라이트로 유리판이 떠 있는 느낌 + 은은한 딥 섀도 */
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 10px 28px rgba(0,0,0,0.55) !important;
}}
/* Streamlit 기본 컨테이너 보더/패딩은 내부 stVerticalBlock에 그려짐 → 이중 보더 방지 */
[data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .sec-tone) > [data-testid="stVerticalBlock"] {{
    border: none !important;
    padding: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
}}

/* ===== 섹션 색조 (SECTION TONES) =====
   page_common.section_tone("cyan"|"emerald"|"violet"|"amber"|"rose") 를
   st.container(border=True) 첫 줄에서 호출하면 해당 섹션에 색조 틴트 적용.
   섹션마다 다른 색조를 부여 → 스크롤 중에도 어느 섹션인지 즉시 구분됨. */
div[data-testid="stElementContainer"]:has(.sec-tone) {{
    display: none !important;
    margin: 0 !important; padding: 0 !important; height: 0 !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.sec-tone-cyan),
[data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .sec-tone-cyan) {{
    background: linear-gradient(165deg, rgba(56,189,248,0.16) 0%, rgba(56,189,248,0.05) 45%, rgba(56,189,248,0.02) 100%), var(--section-box-bg) !important;
    border-color: rgba(56,189,248,0.30) !important;
    border-left-color: #38bdf8 !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.sec-tone-emerald),
[data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .sec-tone-emerald) {{
    background: linear-gradient(165deg, rgba(52,211,153,0.15) 0%, rgba(52,211,153,0.05) 45%, rgba(52,211,153,0.02) 100%), var(--section-box-bg) !important;
    border-color: rgba(52,211,153,0.30) !important;
    border-left-color: #34d399 !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.sec-tone-violet),
[data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .sec-tone-violet) {{
    background: linear-gradient(165deg, rgba(167,139,250,0.16) 0%, rgba(167,139,250,0.05) 45%, rgba(167,139,250,0.02) 100%), var(--section-box-bg) !important;
    border-color: rgba(167,139,250,0.30) !important;
    border-left-color: #a78bfa !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.sec-tone-amber),
[data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .sec-tone-amber) {{
    background: linear-gradient(165deg, rgba(251,191,36,0.13) 0%, rgba(251,191,36,0.04) 45%, rgba(251,191,36,0.02) 100%), var(--section-box-bg) !important;
    border-color: rgba(251,191,36,0.28) !important;
    border-left-color: #fbbf24 !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.sec-tone-rose),
[data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] .sec-tone-rose) {{
    background: linear-gradient(165deg, rgba(251,113,133,0.15) 0%, rgba(251,113,133,0.05) 45%, rgba(251,113,133,0.02) 100%), var(--section-box-bg) !important;
    border-color: rgba(251,113,133,0.30) !important;
    border-left-color: #fb7185 !important;
}}

.section-title {{
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--section-title-color) !important;
    margin: 0 0 14px 0;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
    letter-spacing: -0.01em;
}}
.section-title-icon {{
    font-size: 1.15rem;
    line-height: 1;
}}
.section-title-sub {{
    font-size: 0.78rem;
    font-weight: 400;
    color: var(--text-secondary);
    margin-left: auto;
}}

/* ===== BUTTONS — 깔끔한 인디고 그라데이션 + 부드러운 섀도 ===== */
/* 사이드바 버튼은 위쪽 section[data-testid="stSidebar"] .stButton > button 규칙이 우선시됩니다. */
.stApp .stButton > button {{
    background: var(--btn-bg) !important;
    border: 1px solid var(--btn-border) !important;
    color: #ffffff !important;
    border-radius: 9px !important;
    font-weight: 600 !important;
    letter-spacing: 0.01em !important;
    box-shadow: var(--shadow-xs) !important;
    transition: transform 0.15s ease, box-shadow 0.15s ease, filter 0.15s ease !important;
}}
.stApp .stButton > button:hover {{
    filter: brightness(1.05);
    box-shadow: var(--shadow-md) !important;
    transform: translateY(-1px);
}}
.stApp .stButton > button:active {{
    transform: translateY(0);
    box-shadow: var(--shadow-xs) !important;
}}
.stApp .stButton > button[kind="primary"] {{
    background: var(--btn-primary) !important;
    border-color: transparent !important;
}}
.stApp .stDownloadButton > button {{
    background: var(--btn-bg) !important;
    border: 1px solid var(--btn-border) !important;
    color: #ffffff !important;
    border-radius: 9px !important;
    font-weight: 600 !important;
    box-shadow: var(--shadow-xs) !important;
}}

/* ===== METRICS — 깨끗한 카드 + 미세 그림자 ===== */
[data-testid="stMetric"] {{
    background: var(--metric-bg) !important;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    padding: 14px 18px !important;
    box-shadow: var(--shadow-xs) !important;
    overflow: visible !important;
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
}}
[data-testid="stMetric"]:hover {{
    box-shadow: var(--shadow-sm), var(--accent-glow) !important;
    border-color: var(--accent) !important;
}}
[data-testid="stMetricLabel"] {{
    color: var(--text-secondary) !important;
    font-size: 0.78rem !important;
    white-space: normal !important; overflow: visible !important;
}}
[data-testid="stMetricLabel"] p, [data-testid="stMetricLabel"] div,
[data-testid="stMetricLabel"] span {{
    color: var(--text-secondary) !important;
    overflow: visible !important; white-space: normal !important;
}}
[data-testid="stMetricValue"] {{
    color: var(--text-primary) !important;
    font-size: 1.3rem !important;
    overflow: visible !important; white-space: normal !important;
}}
[data-testid="stMetricValue"] > div {{
    color: var(--text-primary) !important;
    overflow: visible !important;
    white-space: normal !important;
    word-break: break-all !important;
    line-height: 1.2 !important;
    text-overflow: clip !important;
    max-width: 100% !important;
}}
[data-testid="stMetricDelta"] {{ font-size:0.78rem !important; }}

/* ===== MULTISELECT 태그 (양쪽 테마 공통) =====
   첫 태그(또는 wrap된 행의 첫 태그)만 좌측 경계로 밀려 클리핑되는 문제.
   원인 후보: BaseWeb의 negative margin / transform / text-indent / 음수 position.
   대응: 모든 변형 차단 + listbox에 pseudo-spacer로 무조건 좌측 여백 확보. */

/* === 태그 색상/스타일 (라이트 모드 컴팩트) === */
[data-baseweb="tag"] {{
    background-color: var(--accent) !important;
    border-color: var(--accent) !important;
    color: #ffffff !important;
    flex-shrink: 0 !important;
    flex-grow: 0 !important;
    margin: 0 !important;
    padding: 1px 6px !important;       /* 컴팩트하게 — 좁은 폭에서 한 줄 유지 */
    font-size: 0.78rem !important;
    line-height: 1.4 !important;
    border-radius: 6px !important;
    /* 모든 위치 변형 차단 */
    transform: none !important;
    translate: 0 !important;
    text-indent: 0 !important;
    position: static !important;
    left: auto !important;
    inset-inline-start: auto !important;
    overflow: visible !important;
}}

[data-baseweb="tag"] > span,
[data-baseweb="tag"] > div {{
    color: #ffffff !important;
    background: transparent !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: visible !important;
    text-indent: 0 !important;
    transform: none !important;
}}
[data-baseweb="tag"] svg {{
    fill: #ffffff !important;
    color: #ffffff !important;
}}
[data-baseweb="tag"] [role="button"]:hover {{
    background: rgba(255,255,255,0.20) !important;
    border-radius: 4px !important;
}}
[data-baseweb="tag"] [role="button"]:hover svg {{
    fill: #ffffff !important;
}}

/* === MultiSelect 전체 트리: overflow / transform / text-indent 차단 === */
[data-testid="stMultiSelect"],
[data-testid="stMultiSelect"] *,
.stMultiSelect,
.stMultiSelect * {{
    overflow: visible !important;
    overflow-x: visible !important;
    overflow-y: visible !important;
}}
.stMultiSelect *,
[data-testid="stMultiSelect"] * {{
    transform: none !important;
    translate: 0 !important;
    text-indent: 0 !important;
}}

/* === 핵심 발견 ===
   BaseWeb 구조:
     [data-baseweb="select"]
       └── div (실제 flex 컨테이너 — 태그·input·indicator를 형제로 담음)
            ├── span[data-baseweb="tag"]  × N
            ├── input[role="combobox"]   ← role=combobox는 input!
            └── div (×, ▼ indicator)
   - [role="listbox"] = 드롭다운 팝업 (Select all/전사/남양주1 등 목록), 태그 컨테이너 아님
   - [role="combobox"] = 검색 input, 컨테이너 아님
   → CSS :has(> [data-baseweb="tag"])로 태그의 직계 부모 div를 정확히 타게팅. */

/* 모든 select 내부 div + 태그 직계 부모에 padding/margin 명시 */
.stMultiSelect [data-baseweb="select"] > div,
.stMultiSelect [data-baseweb="select"] > div > div,
.stMultiSelect [data-baseweb="select"] > div > div > div,
.stMultiSelect div:has(> [data-baseweb="tag"]),
.stMultiSelect div:has([data-baseweb="tag"]) {{
    margin: 0 !important;
    margin-left: 0 !important;
    margin-inline-start: 0 !important;
    padding-left: 6px !important;             /* 12px → 6px (좌측 여백 축소) */
    padding-right: 6px !important;
    padding-inline-start: 6px !important;
    padding-inline-end: 6px !important;
    /* 태그가 너무 많아 행이 깨지는 대신 가로 스크롤 */
    flex-wrap: nowrap !important;
    overflow-x: auto !important;
    overflow-y: hidden !important;
    scrollbar-width: thin;
    gap: 4px !important;
    column-gap: 4px !important;
    row-gap: 4px !important;
    box-sizing: border-box !important;
    align-items: center !important;
    align-content: center !important;
    position: relative !important;
    left: 0 !important;
    inset-inline-start: 0 !important;
}}

/* === 첫 태그 추가 margin 제거 (이미 컨테이너 padding이 좌측 여백 확보) === */
.stMultiSelect [data-baseweb="tag"]:first-child,
.stMultiSelect [data-baseweb="tag"]:first-of-type,
.stMultiSelect [data-baseweb="tag"]:nth-child(1),
.stMultiSelect [data-baseweb="tag"]:nth-of-type(1) {{
    margin-left: 0 !important;
    margin-inline-start: 0 !important;
}}

/* 두 번째 태그부터도 margin-left 0 (gap에 의존) */
.stMultiSelect [data-baseweb="tag"]:not(:first-child) {{
    margin-left: 0 !important;
    margin-inline-start: 0 !important;
}}

/* nowrap 모드에서는 ::before 스페이서가 가로 스크롤만 늘리므로 제거 */
.stMultiSelect div:has(> [data-baseweb="tag"])::before {{
    display: none !important;
    content: none !important;
}}

/* 태그 영역 가로 스크롤바를 얇고 차분하게 */
.stMultiSelect div:has(> [data-baseweb="tag"])::-webkit-scrollbar {{
    height: 4px;
}}
.stMultiSelect div:has(> [data-baseweb="tag"])::-webkit-scrollbar-thumb {{
    background: rgba(100,116,139,0.35);
    border-radius: 4px;
}}
</style>
"""

LIGHT_REFINED_CSS = f"""
<style>
.stApp p, .stApp span, .stApp label, .stApp div {{
    color: inherit;
}}
.stMarkdown p {{ color: var(--text-primary) !important; line-height: 1.65; }}
.stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {{
    color: var(--text-primary) !important;
    letter-spacing: -0.02em;
    font-weight: 700;
}}
.stMarkdown h1 {{ font-size: 1.75rem; }}
.stMarkdown h2 {{ font-size: 1.35rem; }}
.stMarkdown h3 {{ font-size: 1.10rem; }}

/* ===== TABS — 깔끔한 언더라인 스타일 ===== */
.stTabs [data-baseweb="tab-list"] {{
    background-color: transparent;
    border-bottom:1px solid var(--border);
    gap:4px;
    padding-bottom: 0;
}}
.stTabs [data-baseweb="tab"] {{
    color: var(--text-secondary);
    background:transparent;
    border-bottom:2px solid transparent;
    padding:10px 22px;
    font-size: 0.88rem;
    font-weight: 500;
    border-radius: 8px 8px 0 0;
    transition: color 0.15s, background 0.15s;
}}
.stTabs [data-baseweb="tab"]:hover {{
    color: var(--text-primary);
    background: var(--accent-hover);
}}
.stTabs [aria-selected="true"] {{
    color: var(--accent) !important;
    border-bottom-color: var(--accent) !important;
    background: var(--tab-active-bg) !important;
    font-weight: 600 !important;
}}

/* ===== INPUTS — 깔끔한 라운드 + 미세 그림자 ===== */
.stSelectbox > div > div,
.stMultiSelect > div > div,
.stDateInput > div > div,
.stNumberInput > div > div {{
    background-color: var(--input-bg) !important;
    border-color: var(--input-border) !important;
    border-radius: 9px !important;
    color: var(--text-primary) !important;
    transition: border-color 0.15s, box-shadow 0.15s;
}}
/* 멀티셀렉트는 태그가 들어가 시각적으로 더 명확한 프레임 필요
   → BaseWeb 실제 보더 컨테이너([data-baseweb="select"] > div)를 직접 타게팅. */
[data-testid="stMultiSelect"] [data-baseweb="select"] > div,
.stMultiSelect [data-baseweb="select"] > div {{
    border: 1px solid var(--input-border-strong) !important;   /* 콜드 블루 강조 보더 */
    border-radius: 10px !important;
    background-color: var(--input-bg) !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.35), inset 0 0 0 1px rgba(120,160,220,0.04) !important;
    transition: border-color 0.15s, box-shadow 0.15s !important;
}}
[data-testid="stMultiSelect"] [data-baseweb="select"] > div:hover,
.stMultiSelect [data-baseweb="select"] > div:hover {{
    border-color: var(--accent) !important;
    box-shadow: 0 2px 6px rgba(0,0,0,0.4) !important;
}}
[data-testid="stMultiSelect"] [data-baseweb="select"] > div:focus-within,
.stMultiSelect [data-baseweb="select"] > div:focus-within {{
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-hover) !important;
}}

/* 사이드바 열림 등으로 가용 폭이 줄어들 때 필터 컬럼 행이 2줄로 깨지는 현상 방지 */
[data-testid="stHorizontalBlock"]:has(.stMultiSelect),
[data-testid="stHorizontalBlock"]:has(.stSelectbox),
[data-testid="stHorizontalBlock"]:has(.stNumberInput),
[data-testid="stHorizontalBlock"]:has(.stDateInput) {{
    flex-wrap: nowrap !important;
}}
.stSelectbox > div > div:focus-within,
.stMultiSelect > div > div:focus-within,
.stDateInput > div > div:focus-within,
.stNumberInput > div > div:focus-within {{
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-hover) !important;
}}
[data-baseweb="select"] > div,
[data-baseweb="select"] input,
[data-baseweb="input"] > div,
[data-baseweb="input"] input {{
    background-color: var(--input-bg) !important;
    color: var(--text-primary) !important;
}}
[data-baseweb="popover"] [role="listbox"],
[data-baseweb="menu"] {{
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border-radius: 10px !important;
    box-shadow: var(--shadow-md) !important;
    border: 1px solid var(--border) !important;
}}
[data-baseweb="menu"] li,
[data-baseweb="menu"] [role="option"] {{
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
}}
[data-baseweb="menu"] li:hover,
[data-baseweb="menu"] [role="option"]:hover {{
    background-color: var(--accent-hover) !important;
    color: var(--accent-strong) !important;
}}
[data-testid="stNumberInputContainer"] {{
    background-color: var(--input-bg) !important;
    border-color: var(--input-border) !important;
    border-radius: 9px !important;
}}
[data-testid="stNumberInputContainer"] input,
[data-testid="stNumberInputContainer"] button {{
    background-color: var(--input-bg) !important;
    color: var(--text-primary) !important;
    border-color: var(--input-border) !important;
}}
[data-testid="stDateInput"] input {{
    background-color: var(--input-bg) !important;
    color: var(--text-primary) !important;
}}
/* select 내부 span에 텍스트 색 적용하되, 태그(tag) 안의 span은 제외 — 태그는 흰색 유지 */
[data-baseweb="select"] span:not([data-baseweb="tag"] span) {{
    color: var(--text-primary) !important;
}}
/* 태그 내부 텍스트/아이콘 — 파란 배경에 흰 글씨 보장 */
[data-baseweb="tag"] span,
[data-baseweb="tag"] div,
[data-baseweb="tag"] [role="presentation"] {{
    color: #ffffff !important;
}}

/* ===== MISC ===== */
hr {{ border-color: var(--border) !important; }}
.streamlit-expanderHeader {{
    background-color: var(--expander-bg) !important;
    color: var(--text-primary) !important;
    border-radius: 10px !important;
}}
[data-testid="stExpander"] {{
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    box-shadow: var(--shadow-xs) !important;
    overflow: hidden;
}}
[data-testid="stExpander"] summary {{
    color: var(--text-primary) !important;
    padding: 12px 16px !important;
}}
[data-testid="stExpander"] summary span {{
    color: var(--text-primary) !important;
    font-weight: 500;
}}
[data-testid="stExpander"] summary:hover {{
    background: var(--bg-card2) !important;
}}
.stFileUploader > div {{
    background-color: var(--input-bg) !important;
    border-color: var(--input-border) !important;
    border-radius: 10px !important;
    border-style: dashed !important;
}}
.stAlert {{
    background-color: var(--alert-bg) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    color: var(--text-primary) !important;
    box-shadow: var(--shadow-xs) !important;
}}
.stAlert p, .stAlert span {{
    color: var(--text-primary) !important;
}}

/* ===== DATAFRAME TABLE — 깔끔한 둥근 모서리 ===== */
.stDataFrame [data-testid="stDataFrameResizable"] {{
    background-color: var(--bg-card) !important;
}}
.stDataFrame th, .stDataFrame thead th {{
    background-color: var(--bg-card2) !important;
    color: var(--text-secondary) !important;
    border-color: var(--border) !important;
    font-weight: 600 !important;
}}
.stDataFrame td, .stDataFrame tbody td {{
    background-color: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border-color: var(--border-light) !important;
}}
[data-testid="stDataFrame"] > div {{
    background-color: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    box-shadow: var(--shadow-xs) !important;
    overflow: hidden;
}}

/* Plotly 차트 배경을 카드와 자연스럽게 연결 */
.js-plotly-plot .plotly .main-svg {{ background:transparent !important; }}
</style>
"""

st.markdown(CORE_CSS, unsafe_allow_html=True)
st.markdown(LIGHT_REFINED_CSS, unsafe_allow_html=True)


# ────────────────────────────────────────────────────────────────
# 날짜 위젯 한글화 (Streamlit date_input 캘린더 팝업)
# ────────────────────────────────────────────────────────────────
# Streamlit 의 date_input 은 Base Web 캘린더를 사용하는데 locale prop 을 노출하지 않아
# 영어 월/요일 이름이 그대로 노출됨. window.parent.document 에 MutationObserver 를 걸어
# 캘린더 팝업이 열릴 때마다 텍스트 노드를 한글로 치환.
KOREAN_DATE_LOCALE_JS = """
<script>
(function() {
  const win = window.parent;
  if (!win || win._koreanDateLocaleInstalled) return;
  win._koreanDateLocaleInstalled = true;

  const doc = win.document;

  const MONTHS = {
    'January': '1월', 'February': '2월', 'March': '3월', 'April': '4월',
    'May': '5월', 'June': '6월', 'July': '7월', 'August': '8월',
    'September': '9월', 'October': '10월', 'November': '11월', 'December': '12월',
    'Jan': '1월', 'Feb': '2월', 'Mar': '3월', 'Apr': '4월',
    'Jun': '6월', 'Jul': '7월', 'Aug': '8월',
    'Sep': '9월', 'Sept': '9월', 'Oct': '10월', 'Nov': '11월', 'Dec': '12월'
  };

  const DAY_SHORT = {
    'Sunday': '일', 'Monday': '월', 'Tuesday': '화', 'Wednesday': '수',
    'Thursday': '목', 'Friday': '금', 'Saturday': '토',
    'Sun': '일', 'Mon': '월', 'Tue': '화', 'Wed': '수',
    'Thu': '목', 'Fri': '금', 'Sat': '토',
    'Su': '일', 'Mo': '월', 'Tu': '화', 'We': '수',
    'Th': '목', 'Fr': '금', 'Sa': '토'
  };

  // 긴 키워드부터 매칭하기 위해 정렬된 리스트
  const MONTH_KEYS = Object.keys(MONTHS).sort((a, b) => b.length - a.length);

  function localizeText(text) {
    if (!text) return text;
    let out = text;
    // 월 이름 — 단어 경계 기준 치환
    for (const en of MONTH_KEYS) {
      const re = new RegExp('\\\\b' + en + '\\\\b', 'g');
      out = out.replace(re, MONTHS[en]);
    }
    return out;
  }

  function localizeDayLabel(text) {
    const t = text.trim();
    if (DAY_SHORT.hasOwnProperty(t)) return DAY_SHORT[t];
    return null;
  }

  function processCalendar(root) {
    if (!root) return;
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      const original = node.nodeValue;
      if (!original) continue;
      const dayKo = localizeDayLabel(original);
      if (dayKo !== null) {
        node.nodeValue = dayKo;
        continue;
      }
      const replaced = localizeText(original);
      if (replaced !== original) {
        node.nodeValue = replaced;
      }
    }
  }

  // 캘린더 팝업 단위 옵저버 — 캘린더가 열린 동안만 그 서브트리만 관찰.
  // 기존에는 doc.body 전체를 subtree:true, characterData:true로 감시해서
  // 모든 위젯 변화마다 TreeWalker가 돌았음(체감 버퍼링 원인).
  const localObservers = new WeakMap();

  function attachLocalObserver(root) {
    if (!root || localObservers.has(root)) return;
    processCalendar(root);  // 최초 1회 즉시 치환
    const localObs = new MutationObserver(() => {
      // Base Web 내부 렌더가 끝난 뒤 처리하도록 다음 tick으로 미룸
      win.requestAnimationFrame(() => processCalendar(root));
    });
    localObs.observe(root, { childList: true, subtree: true, characterData: true });
    localObservers.set(root, localObs);
  }

  // body는 childList만 감시 — 캘린더 팝업이 새로 mount될 때만 트리거됨.
  // characterData/subtree 미감시로 일반 위젯 변화에는 반응 안 함.
  const rootObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (!(node instanceof Element)) continue;
        // 추가된 노드 자체 또는 그 자손에서 캘린더 컨테이너 찾기
        if (node.matches && node.matches('[data-baseweb="calendar"], [data-baseweb="datepicker"]')) {
          attachLocalObserver(node);
        }
        if (node.querySelectorAll) {
          node.querySelectorAll('[data-baseweb="calendar"], [data-baseweb="datepicker"]').forEach(attachLocalObserver);
        }
      }
    }
  });
  rootObserver.observe(doc.body, { childList: true, subtree: true });

  // 초기 마운트 시 이미 떠 있는 캘린더가 있으면 처리
  doc.querySelectorAll('[data-baseweb="calendar"], [data-baseweb="datepicker"]').forEach(attachLocalObserver);
})();
</script>
"""

components.html(KOREAN_DATE_LOCALE_JS, height=0)

# ────────────────────────────────────────────────────────────────
# SIDEBAR (fragment로 격리 — 메뉴 그룹 토글 시 메인 본문 깜박임 방지)
# ────────────────────────────────────────────────────────────────
def _toggle_energy_submenu():
    """에너지 메뉴 그룹 토글 콜백.

    on_click 콜백은 widget click 처리 직후 자동 rerun이 일어나기 *전* 에 실행되므로,
    여기서 session_state 를 변경하면 그 변경이 다음 rerun 에서 즉시 반영됨.
    덕분에 명시적 ``st.rerun()`` 호출 없이도 1-click 으로 chevron(▾/▸) 과
    submenu 표시가 동기화됨.
    """
    st.session_state.energy_submenu_open = not st.session_state.energy_submenu_open


def _toggle_ai_submenu():
    """AI 메뉴 그룹 토글 콜백 — 동일 패턴."""
    st.session_state.ai_submenu_open = not st.session_state.ai_submenu_open


def _nav(label: str, icon: str, page: str, submenu=None, is_sub: bool = False):
    """사이드바 네비게이션 버튼을 렌더링합니다.

    icon: ``:material/<name>:`` 형식. 모든 아이콘이 동일 폭이라 텍스트 정렬이 일관됨.
    submenu: 부모-자식 메뉴 트리에서 자식 식별자.

    fragment 안에서 호출되므로 페이지 전환 시에는 ``scope="app"`` 으로
    full rerun을 명시해 메인 콘텐츠가 갱신되도록 합니다.
    """
    is_active = (
        st.session_state.current_page == page
        and st.session_state.current_submenu == submenu
    )
    if is_sub:
        wrapper_cls = "sb-sub-active" if is_active else "sb-sub"
        btn_label = f"{icon}  {label}"
    else:
        wrapper_cls = "sb-active-btn" if is_active else "sb-nav-btn"
        btn_label = f"{icon}  {label}"

    key = f"nav_{page}_{submenu or 'root'}"
    st.markdown(f'<span class="{wrapper_cls}"></span>', unsafe_allow_html=True)
    if st.button(btn_label, key=key, use_container_width=True):
        navigate(page, submenu)
        # 페이지 변경됐으므로 메인 콘텐츠도 재렌더 필요 → app 전체 rerun
        st.rerun(scope="app")


@st.fragment
def render_sidebar_body():
    """사이드바 본문(fragment) — 호출하는 쪽에서 ``with st.sidebar:`` 컨텍스트로 감싸야 함.

    Streamlit 제약: fragment 함수 안에서 ``st.sidebar``를 직접 사용하면
    ``StreamlitAPIException`` 발생. 따라서 fragment 자체는 ``st.sidebar``를
    호출하지 않고, 부모 스코프가 ``with st.sidebar:`` 컨텍스트를 제공함.

    효과:
    - 메뉴 그룹 토글(▾/▸) 같이 사이드바 내부 상태만 바뀌는 인터랙션은
      fragment 자동 rerun으로 처리되어 메인 콘텐츠가 깜박이지 않음.
    - 페이지 nav 버튼은 ``_nav`` 안에서 ``st.rerun(scope='app')`` 으로 전체 rerun.
    """
    logo_b64    = get_logo_base64()
    now         = datetime.now()
    user        = _cached_current_user()
    total_records = _cached_record_count()

    # 로고/타이틀 — 클릭 시 대시보드(홈)로 이동 (?nav=home 쿼리 파라미터)
    logo_html = (
        f'<img src="data:image/png;base64,{logo_b64}" '
        f'style="width:60px;height:60px;object-fit:contain;vertical-align:middle;flex-shrink:0;">'
        if logo_b64 else '<span style="font-size:2.4rem;flex-shrink:0;">⚡</span>'
    )
    st.markdown(
        f"""
        <a href="?nav=home" target="_self" class="bems-logo-home" title="대시보드로 이동">
            <div style="display:flex;align-items:center;gap:18px;">
                {logo_html}
                <div style="min-width:0;">
                    <div class="sb-logo-title">BEMS</div>
                    <div class="sb-logo-sub">Binggrae Energy Management System</div>
                </div>
            </div>
        </a>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<hr class="sb-divider">', unsafe_allow_html=True)

    # ── 메인 메뉴 ──
    st.markdown('<p class="sb-section-label">메뉴</p>', unsafe_allow_html=True)

    # Material Symbols 아이콘 — 모든 아이콘이 동일 폭으로 렌더되어 텍스트가 가지런히 정렬됨
    _nav("대시보드(홈)",       ":material/space_dashboard:", "dashboard")

    # 에너지 모니터링 그룹 — on_click 콜백으로 1-click 토글
    energy_open = st.session_state.energy_submenu_open or (st.session_state.current_page == "energy")
    chevron = "▾" if energy_open else "▸"
    energy_active = st.session_state.current_page == "energy"
    parent_cls = "sb-active-btn" if energy_active else "sb-nav-btn"
    st.markdown(f'<span class="{parent_cls}"></span>', unsafe_allow_html=True)
    # on_click 콜백은 click 처리 직후 자동 rerun 전에 실행되어 같은 사이클에서
    # 새 state 가 반영됨. 명시적 st.rerun() 호출 없이도 1-click 으로 chevron + submenu 동기화.
    st.button(
        f":material/analytics:  에너지 모니터링  {chevron}",
        key="nav_energy_group",
        use_container_width=True,
        on_click=_toggle_energy_submenu,
    )

    if energy_open:
        _nav("사용량 통합",   ":material/monitoring:",            "energy", "usage",      is_sub=True)
        _nav("원단위",       ":material/straighten:",            "energy", "intensity",  is_sub=True)

    _nav("생산실적 분석",      ":material/factory:",         "production")

    # AI 에너지 분석 그룹 (서브메뉴: 에너지 사용 예측 / 에너지 실적 보고서)
    ai_open = st.session_state.ai_submenu_open or (st.session_state.current_page == "ai")
    ai_chevron = "▾" if ai_open else "▸"
    ai_active = st.session_state.current_page == "ai"
    ai_parent_cls = "sb-active-btn" if ai_active else "sb-nav-btn"
    st.markdown(f'<span class="{ai_parent_cls}"></span>', unsafe_allow_html=True)
    # on_click 콜백 패턴 — 1-click 즉시 반응
    st.button(
        f":material/smart_toy:  AI 에너지 분석  {ai_chevron}",
        key="nav_ai_group",
        use_container_width=True,
        on_click=_toggle_ai_submenu,
    )

    if ai_open:
        _nav("에너지 사용 예측",         ":material/insights:", "ai", "prediction", is_sub=True)
        _nav("에너지 실적 보고서",       ":material/description:", "ai", "report",     is_sub=True)

    admin_mode = is_admin()

    if admin_mode:
        st.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
        st.markdown('<p class="sb-section-label">관리자</p>', unsafe_allow_html=True)
        _nav("데이터 업로드", ":material/upload_file:", "upload")
        _nav("변경이력 / 메모", ":material/history:",     "history")

    # 하단 정보 — 권한 배지 + 시각/사용자/레코드
    role_badge = (
        '<span style="display:inline-flex;align-items:center;gap:4px;'
        'padding:2px 8px;border-radius:10px;font-size:0.68rem;font-weight:600;'
        'background:rgba(56,189,248,0.16);color:#6fd2fb;'
        'border:1px solid rgba(56,189,248,0.35);">🔓 root</span>'
        if admin_mode else
        '<span style="display:inline-flex;align-items:center;gap:4px;'
        'padding:2px 8px;border-radius:10px;font-size:0.68rem;font-weight:600;'
        'background:rgba(140,175,225,0.12);color:#9db1cf;'
        'border:1px solid rgba(140,175,225,0.25);">👁️ viewer</span>'
    )
    st.markdown(f"""
    <div style="margin-top:12px; padding:10px 4px;
                border-top:1px solid var(--sb-divider);">
        <div style="margin-bottom:6px;">{role_badge}</div>
        <div class="sb-info-text">
            🕐 {now.strftime('%Y.%m.%d %H:%M')}<br>
            👤 {user}<br>
            🗄️ {total_records:,} records
        </div>
    </div>
    """, unsafe_allow_html=True)


# 사이드바 컨텍스트 안에서 fragment를 호출 (Streamlit 제약 우회)
with st.sidebar:
    render_sidebar_body()


# ────────────────────────────────────────────────────────────────
# 메인 콘텐츠 라우팅
# ────────────────────────────────────────────────────────────────
page = st.session_state.current_page
sub  = st.session_state.current_submenu

# 사이드바 접힘 상태에서 보이는 BEMS 미니 헤더(현재 페이지명 표시)
_PAGE_DISPLAY_NAMES: dict[tuple[str, str | None], str] = {
    ("dashboard", None):       "대시보드",
    ("production", None):      "생산 실적",
    ("energy", "usage"):       "에너지 모니터링 / 사용량 통합",
    ("energy", "power"):       "에너지 모니터링 / 사용량 통합",
    ("energy", "fuel_water"):  "에너지 모니터링 / 사용량 통합",
    ("energy", "intensity"):   "에너지 모니터링 / 원단위",
    ("energy", None):          "에너지 모니터링",
    ("savings", None):         "에너지 절감관리",
    ("ai", "prediction"):      "AI 에너지 분석 / 에너지 사용 예측",
    ("ai", "report"):          "AI 에너지 분석 / 에너지 실적 보고서",
    ("ai", None):              "AI 에너지 분석",
    ("upload", None):          "데이터 업로드",
    ("history", None):         "변경이력 / 이벤트 메모",
}
_current_page_label = (
    _PAGE_DISPLAY_NAMES.get((page, sub))
    or _PAGE_DISPLAY_NAMES.get((page, None))
    or "대시보드"
)
# 미니 헤더 / footer 용 module-level 변수 — 사이드바 fragment와 별도로 필요
# (logo_b64는 캐시되어 있어 비용 0; now는 footer 동기화 시각 표시용)
logo_b64 = get_logo_base64()
now = datetime.now()
_mini_logo_html = (
    f'<img src="data:image/png;base64,{logo_b64}" '
    f'style="width:22px;height:22px;object-fit:contain;">'
    if logo_b64 else '<span style="font-size:1.1rem;">⚡</span>'
)
st.markdown(f"""
<a href="?nav=home" target="_self" class="bems-mini-header" title="대시보드로 이동">
    <span class="bems-mh-logo">{_mini_logo_html}</span>
    <span class="bems-mh-title">BEMS</span>
    <span class="bems-mh-sep">/</span>
    <span class="bems-mh-page">{_current_page_label}</span>
</a>
""", unsafe_allow_html=True)

if page == "dashboard":
    from app.pages.dashboard_main import render_main_dashboard
    render_main_dashboard()

elif page == "production":
    from app.pages.production_performance import render_production_performance
    render_production_performance()

elif page == "energy":
    if sub == "intensity":
        from app.pages.energy_intensity import render_energy_intensity
        render_energy_intensity()
    else:
        if sub == "power":
            st.session_state["eu_source"] = "전력"
        elif sub == "fuel_water":
            st.session_state["eu_source"] = "연료"
        from app.pages.energy_usage import render_energy_usage
        render_energy_usage()

elif page == "savings":
    t1, t2 = st.tabs(["절감 계획 관리", "절감 실적 현황"])
    with t1:
        from app.pages.savings_plan import render_savings_plan
        render_savings_plan()
    with t2:
        from app.pages.savings_results import render_savings_results
        render_savings_results()

elif page == "ai":
    if sub == "report":
        from app.pages.ai_report import render_ai_report
        render_ai_report()
    else:
        # 기본 또는 sub == "prediction"
        from app.pages.ai_prediction import render_ai_prediction
        render_ai_prediction()

elif page == "upload" and is_admin():
    from app.pages.data_upload import render_upload_page
    render_upload_page()

elif page == "history" and is_admin():
    from app.pages.audit_log import render_audit_page
    render_audit_page()

else:
    from app.pages.dashboard_main import render_main_dashboard
    render_main_dashboard()


# ────────────────────────────────────────────────────────────────
# FOOTER
# ────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="fems-footer-v2">
  <div class="footer-status-bar">
    <div class="status-dot"></div>
    <div class="status-text">데이터 수집 엔진 정상 작동 중</div>
    <div class="sync-time">최근 동기화: {now.strftime('%H:%M:%S')}</div>
  </div>
  <div class="footer-attribution">
    BEMS — 데이터 출처: <b>종합정보시스템 / 기상청 API Hub</b>
  </div>
  <div style="margin-top:14px; font-size:0.72rem; color:var(--text-muted);">
    © 2026 Binggrae Energy Management System. All rights reserved.
  </div>
</div>
""", unsafe_allow_html=True)
