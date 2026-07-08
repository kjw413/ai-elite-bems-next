"""
Audit Log Page
================
변경 이력 조회 페이지 — 누가 / 언제 / 무엇을 / 어떻게 변경했는지 추적.

표시 정보
  - 요약 KPI: 총 건수, 최근 7일, 변경 유형 분포, 변경자 수
  - 필터: 공장 / 변경 유형 / 기간
  - 상세 테이블: change_type 배지, 컬럼 한글명, 이전→변경 값
"""
# 이 파일은 데이터 변경 이력 조회 화면을 담당합니다.

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.audit_service import get_audit_history
from app.services.query_service import get_factories
from app.utils.excel_parser import COLUMN_DISPLAY_NAMES
from app.utils.page_common import section_tone
from app.utils.page_state import persist_many


# 변경 유형별 색상/라벨 — 라이트 모드 톤에 맞춘 차분한 배지 팔레트
_CHANGE_TYPE_STYLE = {
    "UPLOAD":       {"label": "엑셀 업로드", "fg": "#60a5fa", "bg": "rgba(37,99,235,0.16)"},
    "MANUAL":       {"label": "수동 편집",   "fg": "#fbbf24", "bg": "rgba(245,158,11,0.16)"},
    "WEB_ROW_EDIT": {"label": "웹 셀 편집",  "fg": "#d8b4fe", "bg": "rgba(168,85,247,0.16)"},
    "AUTO_SYNC":    {"label": "자동 동기화", "fg": "#5eead4", "bg": "rgba(20,184,166,0.16)"},
}


def _format_change_type_badge(change_type: str) -> str:
    """변경 유형 → HTML 배지 마크업."""
    style = _CHANGE_TYPE_STYLE.get(change_type, {
        "label": change_type, "fg": "var(--text-secondary)", "bg": "rgba(100,116,139,0.12)"
    })
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;'
        f'font-size:0.74rem;font-weight:600;letter-spacing:0.01em;'
        f'color:{style["fg"]};background:{style["bg"]};">{style["label"]}</span>'
    )


def _kpi_card(title: str, value: str, sub: str = "") -> str:
    """라이트 모드 KPI 카드 마크업."""
    sub_html = (
        f'<div style="font-size:0.74rem;color:#64748b;margin-top:4px;">{sub}</div>'
        if sub else ""
    )
    return f"""
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;
                padding:16px 18px;box-shadow:0 1px 2px rgba(15,23,42,0.04);height:100%;">
        <div style="font-size:0.72rem;color:var(--text-secondary);font-weight:600;
                    text-transform:uppercase;letter-spacing:0.6px;">{title}</div>
        <div style="font-size:1.55rem;font-weight:700;color:var(--text-primary);
                    margin-top:6px;line-height:1.1;letter-spacing:-0.02em;">{value}</div>
        {sub_html}
    </div>
    """


# 변경 이력 화면을 보여줍니다.
def render_audit_page():
    """변경 이력 + 이벤트 메모 통합 페이지."""

    # 페이지 이동 후 재방문에도 필터 값을 유지
    # 동적 key (new_evt_*, edit_*) 는 form 일회성 입력이라 persist 대상 아님.
    persist_many({
        "audit_factory":     None,
        "audit_change_type": None,
        "audit_from":        None,
        "audit_to":          None,
        "evt_filter_factory": None,
        "evt_filter_target":  None,
        "evt_filter_from":    None,
        "evt_filter_to":      None,
    })

    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">📝</span>
        <div>
            <div class="sub-page-title">변경 이력 / 이벤트 메모</div>
            <div class="sub-page-breadcrumb">관리자 > 데이터 변경 추적 + 실무자 원인·조치 기록</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    tab_audit, tab_event = st.tabs(["📜 변경 이력", "📝 이벤트 메모"])
    with tab_audit:
        _render_audit_history_tab()
    with tab_event:
        _render_event_memo_tab()


def _render_audit_history_tab():
    """기존 데이터 변경 이력 조회 — 누가 무엇을 어떻게 변경했는지 추적."""
    # ── 필터 ────────────────────────────────────────────────
    db_factories = get_factories()
    today = date.today()
    default_from = today - timedelta(days=30)

    with st.container(border=True):
        section_tone("cyan")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">⚙️</span>조회 조건'
            '<span class="section-title-sub">공장 · 변경 유형 · 기간</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        fcol1, fcol2, fcol3, fcol4 = st.columns([1, 1, 1, 1])
        with fcol1:
            audit_factory = st.selectbox(
                "공장 필터",
                options=["전체"] + db_factories,
                key="audit_factory",
            )
        with fcol2:
            change_type_label = st.selectbox(
                "변경 유형",
                options=["전체", "엑셀 업로드", "수동 편집", "웹 셀 편집", "자동 동기화"],
                key="audit_change_type",
            )
        with fcol3:
            audit_from = st.date_input("시작 날짜", key="audit_from", value=default_from)
        with fcol4:
            audit_to = st.date_input("종료 날짜", key="audit_to", value=today)

    # 라벨 → DB change_type 매핑
    label_to_type = {v["label"]: k for k, v in _CHANGE_TYPE_STYLE.items()}
    selected_type = label_to_type.get(change_type_label) if change_type_label != "전체" else None

    # ── 데이터 로드 ────────────────────────────────────────
    history = get_audit_history(
        factory=audit_factory if audit_factory != "전체" else None,
        date_from=str(audit_from) if audit_from else None,
        date_to=str(audit_to) if audit_to else None,
        limit=2000,
    )

    if not history:
        st.info("선택한 조건에 해당하는 변경 이력이 없습니다.")
        return

    audit_df = pd.DataFrame(history)
    audit_df["changed_at"] = pd.to_datetime(audit_df["changed_at"], errors="coerce")

    # 변경 유형 필터 (UI 단계 — DB 쿼리에는 빠져 있어서 후처리)
    if selected_type:
        audit_df = audit_df[audit_df["change_type"] == selected_type]

    if audit_df.empty:
        st.info("선택한 변경 유형에 해당하는 이력이 없습니다.")
        return

    # ── 요약 KPI ───────────────────────────────────────────
    total_n = len(audit_df)
    seven_days_ago = pd.Timestamp.now() - pd.Timedelta(days=7)
    recent_n = int((audit_df["changed_at"] >= seven_days_ago).sum())
    type_counts = audit_df["change_type"].value_counts()
    top_type = type_counts.index[0] if not type_counts.empty else "-"
    top_type_label = _CHANGE_TYPE_STYLE.get(top_type, {}).get("label", top_type)
    top_type_n = int(type_counts.iloc[0]) if not type_counts.empty else 0
    n_users = audit_df["changed_by"].nunique()

    with st.container(border=True):
        section_tone("emerald")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📊</span>요약 KPI'
            '<span class="section-title-sub">총 건수 · 최근 7일 · 분포</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        k1, k2, k3, k4 = st.columns(4)
        with k1:
            st.markdown(_kpi_card("총 변경", f"{total_n:,}", "선택 기간"), unsafe_allow_html=True)
        with k2:
            st.markdown(
                _kpi_card("최근 7일", f"{recent_n:,}",
                          f"{(recent_n / total_n * 100):.0f}% 비중" if total_n else ""),
                unsafe_allow_html=True,
            )
        with k3:
            st.markdown(
                _kpi_card("최다 유형", top_type_label, f"{top_type_n:,}건"),
                unsafe_allow_html=True,
            )
        with k4:
            st.markdown(_kpi_card("변경자 수", f"{n_users:,}", "고유 사용자"), unsafe_allow_html=True)

    # ── 상세 테이블 ────────────────────────────────────────
    st.markdown(
        f'<div style="font-size:0.95rem;font-weight:600;color:var(--text-primary);margin-bottom:8px;">'
        f'📋 상세 이력 ({total_n:,}건)</div>',
        unsafe_allow_html=True,
    )

    # 표시용 정렬 + 컬럼 변환
    audit_df = audit_df.sort_values("changed_at", ascending=False).reset_index(drop=True)
    audit_df["변경 시간"] = audit_df["changed_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    audit_df["유형"] = audit_df["change_type"].map(_format_change_type_badge)
    audit_df["변경 컬럼"] = audit_df["column_name"].map(
        lambda x: COLUMN_DISPLAY_NAMES.get(x, x)
    )
    audit_df["날짜"] = pd.to_datetime(audit_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    def _fmt_value(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return '<span style="color:#94a3b8;">—</span>'
        return str(v)

    audit_df["이전 값"] = audit_df["old_value"].map(_fmt_value)
    audit_df["변경 값"] = audit_df["new_value"].map(_fmt_value)
    audit_df["공장"] = audit_df["factory"]
    audit_df["변경자"] = audit_df["changed_by"].fillna("-")

    cols = ["변경 시간", "유형", "공장", "날짜", "변경 컬럼", "이전 값", "변경 값", "변경자"]
    view = audit_df[cols]

    # HTML 테이블 — 라이트 모드 일관성, 배지 렌더링 위해 직접 작성
    th_style = (
        "background:var(--bg-card2);color:var(--text-secondary);font-size:0.78rem;font-weight:600;"
        "letter-spacing:0.02em;padding:10px 12px;border-bottom:1px solid var(--border);"
        "text-align:left;white-space:nowrap;"
    )
    td_base = (
        "font-size:0.85rem;padding:9px 12px;border-bottom:1px solid var(--border);"
        "color:var(--text-primary);text-align:left;vertical-align:middle;"
    )

    html = (
        '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;'
        'overflow:hidden;box-shadow:0 1px 2px rgba(15,23,42,0.04);">'
        '<div style="overflow-x:auto;max-height:640px;overflow-y:auto;">'
        '<table style="width:100%;border-collapse:collapse;">'
    )
    html += '<thead style="position:sticky;top:0;z-index:1;"><tr>'
    for c in cols:
        html += f'<th style="{th_style}">{c}</th>'
    html += '</tr></thead><tbody>'

    for i, row in view.iterrows():
        bg = "var(--bg-card)" if i % 2 == 0 else "var(--bg-card2)"
        html += f'<tr style="background:{bg};">'
        for c in cols:
            val = row[c]
            cell_style = td_base
            if c in ("이전 값", "변경 값"):
                cell_style += "font-variant-numeric:tabular-nums;max-width:200px;"
            if c == "변경 시간":
                cell_style += "white-space:nowrap;color:var(--text-secondary);"
            html += f'<td style="{cell_style}">{val}</td>'
        html += '</tr>'

    html += '</tbody></table></div></div>'
    st.markdown(html, unsafe_allow_html=True)

    st.caption(
        f"※ 조회 기간: {audit_from} ~ {audit_to} · "
        f"최대 2,000건 표시 · 정렬: 변경 시간 내림차순"
    )


# ─────────────────────────────────────────────────────────
# 이벤트 메모 탭 — 실무자가 차트 스파이크/이상치에 대해 남긴 원인·조치 기록
# ─────────────────────────────────────────────────────────

def _render_event_memo_tab():
    """이벤트 메모 검색·신규 추가·수정·삭제."""
    from app.services.event_annotation_service import (
        EVENT_SEVERITIES,
        EVENT_TAGS,
        EVENT_TARGETS,
        TARGET_LABELS,
        add_event,
        delete_event,
        list_events,
        update_event,
    )

    db_factories = get_factories()
    today = date.today()

    with st.container(border=True):
        section_tone("violet")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🔎</span>이벤트 메모 조회'
            '<span class="section-title-sub">공장 · 항목 · 기간</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        ec1, ec2, ec3, ec4 = st.columns([1, 1, 1, 1])
        with ec1:
            f_factory = st.selectbox(
                "공장", options=["전체"] + db_factories, key="evt_filter_factory"
            )
        with ec2:
            f_target_label = st.selectbox(
                "항목",
                options=["전체"] + [TARGET_LABELS[t] for t in EVENT_TARGETS],
                key="evt_filter_target",
            )
        with ec3:
            f_from = st.date_input(
                "시작 날짜", key="evt_filter_from", value=today - timedelta(days=90)
            )
        with ec4:
            f_to = st.date_input("종료 날짜", key="evt_filter_to", value=today)

    label_to_target = {TARGET_LABELS[t]: t for t in EVENT_TARGETS}
    sel_target = label_to_target.get(f_target_label) if f_target_label != "전체" else None

    events_df = list_events(
        factory=f_factory,
        date_from=str(f_from) if f_from else None,
        date_to=str(f_to) if f_to else None,
        target=sel_target,
        limit=1000,
    )

    # ── 신규 메모 추가 (페이지 직접 입력) ──
    with st.expander("➕ 메모 새로 추가", expanded=False):
        with st.form("new_event_form", clear_on_submit=True):
            nf1, nf2, nf3 = st.columns([1, 1, 1])
            with nf1:
                n_factory = st.selectbox(
                    "공장", options=db_factories, key="new_evt_factory"
                )
            with nf2:
                n_date = st.date_input("일자", value=today, key="new_evt_date")
            with nf3:
                n_target_label = st.selectbox(
                    "항목",
                    options=[TARGET_LABELS[t] for t in EVENT_TARGETS],
                    key="new_evt_target",
                )
            nf4, nf5 = st.columns([1, 1])
            with nf4:
                n_tag = st.selectbox("태그", options=EVENT_TAGS, key="new_evt_tag")
            with nf5:
                n_severity = st.selectbox(
                    "중요도", options=EVENT_SEVERITIES, index=1, key="new_evt_severity",
                    help="info=정보, warn=주의, critical=치명적",
                )
            n_note = st.text_area(
                "내용", key="new_evt_note",
                placeholder="예) 보일러 #2 튜브 누설 → 점검 후 재가동", height=80,
            )
            submitted = st.form_submit_button("💾 저장", type="primary")
            if submitted:
                if not n_note or not n_note.strip():
                    st.warning("내용을 입력해 주세요.")
                else:
                    try:
                        add_event(
                            factory=n_factory,
                            event_date=str(n_date),
                            note=n_note,
                            target=label_to_target.get(n_target_label, "overall"),
                            tag=n_tag,
                            severity=n_severity,
                        )
                        st.success("메모가 저장되었습니다.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"저장 실패: {e}")

    # ── 결과 목록 + CSV 내보내기 ──
    if events_df.empty:
        st.info("선택한 조건에 해당하는 이벤트 메모가 없습니다.")
        return

    h_left, h_right = st.columns([4, 1])
    with h_left:
        st.markdown(
            f'<div style="font-size:0.95rem;font-weight:600;color:var(--text-primary);margin:8px 0;">'
            f'📋 메모 목록 ({len(events_df):,}건)</div>',
            unsafe_allow_html=True,
        )
    with h_right:
        export_df = events_df.copy()
        if "event_date" in export_df.columns:
            export_df["event_date"] = pd.to_datetime(export_df["event_date"]).dt.strftime("%Y-%m-%d")
        export_df["target"] = export_df["target"].map(lambda t: TARGET_LABELS.get(t, t))
        st.download_button(
            label="⬇️ CSV",
            data=export_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"event_memo_{f_from}_{f_to}.csv",
            mime="text/csv",
            use_container_width=True,
            key="dl_event_memo",
        )

    sev_color = {"critical": "#ef4444", "warn": "#f59e0b", "info": "#3b82f6"}

    for _, row in events_df.iterrows():
        rid = int(row["id"])
        d_str = pd.to_datetime(row["event_date"]).strftime("%Y-%m-%d")
        tgt_label = TARGET_LABELS.get(str(row["target"]), str(row["target"]))
        badge = sev_color.get(str(row.get("severity", "info")), "#3b82f6")

        edit_state_key = f"_evt_edit_{rid}"
        editing = st.session_state.get(edit_state_key, False)

        with st.container(border=True):
            head_l, head_r1, head_r2 = st.columns([8, 1, 1])
            with head_l:
                st.markdown(
                    f"<div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap;'>"
                    f"<span style='font-weight:600;color:var(--text-primary);'>{row['factory']}</span>"
                    f"<span style='color:#64748b;'>{d_str}</span>"
                    f"<span style='display:inline-block;padding:1px 8px;border-radius:10px;"
                    f"background:{badge}22;color:{badge};font-size:0.74rem;font-weight:600;'>"
                    f"{row['tag']} · {tgt_label}</span>"
                    f"<span style='color:#94a3b8;font-size:0.75rem;'>by {row.get('created_by') or '-'}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            with head_r1:
                if st.button("✏️", key=f"edit_evt_{rid}",
                             help="수정", use_container_width=True):
                    st.session_state[edit_state_key] = not editing
                    st.rerun()
            with head_r2:
                if st.button("🗑", key=f"del_evt_pg_{rid}",
                             help="삭제", use_container_width=True):
                    try:
                        delete_event(rid)
                        st.success("삭제됨")
                        st.rerun()
                    except Exception as e:
                        st.error(f"삭제 실패: {e}")

            if editing:
                with st.form(key=f"edit_form_{rid}"):
                    e1, e2 = st.columns([1, 1])
                    with e1:
                        e_tag = st.selectbox(
                            "태그", options=EVENT_TAGS,
                            index=EVENT_TAGS.index(row["tag"]) if row["tag"] in EVENT_TAGS else 0,
                            key=f"edit_tag_{rid}",
                        )
                    with e2:
                        e_sev = st.selectbox(
                            "중요도", options=EVENT_SEVERITIES,
                            index=EVENT_SEVERITIES.index(str(row.get("severity", "info")))
                                if str(row.get("severity", "info")) in EVENT_SEVERITIES else 0,
                            key=f"edit_sev_{rid}",
                        )
                    e_note = st.text_area(
                        "내용", value=str(row["note"]),
                        key=f"edit_note_{rid}", height=80,
                    )
                    saved = st.form_submit_button("💾 수정 저장", type="primary")
                    if saved:
                        if not e_note or not e_note.strip():
                            st.warning("내용을 입력해 주세요.")
                        else:
                            try:
                                update_event(rid, note=e_note, tag=e_tag, severity=e_sev)
                                st.session_state[edit_state_key] = False
                                st.success("수정되었습니다.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"수정 실패: {e}")
            else:
                st.markdown(
                    f"<div style='color:var(--text-primary);font-size:0.9rem;line-height:1.5;"
                    f"padding:6px 2px;'>{row['note']}</div>",
                    unsafe_allow_html=True,
                )
