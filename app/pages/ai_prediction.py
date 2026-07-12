"""
AI Energy Prediction Page
===========================
에너지 사용 예측 (v5 앙상블 모델 기반).
- 탭 1: 다기간 일괄 예측 실행
- 탭 2: 예측 이력 조회
"""
# 이 파일은 AI 에너지 예측 화면을 보여줍니다.

import streamlit as st
from datetime import date, datetime, timedelta
import pandas as pd
import plotly.graph_objects as go

from app.database.db_connection import is_admin
from app.services.v5_common import (
    BATCH_ALL_FACTORIES_LABEL,
    FACTORY_DISPLAY_ORDER,
    FACTORY_PHYSICAL_DISPLAY_ORDER,
    PREDICTION_FACTORY_OPTIONS,
    TARGET_SPECS,
)
from app.services.usage_prediction_v5_service import (
    get_active_model_path,
    get_model_registry,
    get_training_status,
    predict_v5_batch,
    get_prefill_data_batch,
    get_prediction_history,
    generate_missing_prediction_history,
    is_manual_editor_supported,
    backfill_actuals,
)
from app.services.prediction_monitoring_service import (
    build_prediction_monitoring_summary,
    get_monitoring_overall_status,
)
from app.services.v5_retrain_service import trigger_v5_retrain
from app.components.anomaly_monitor import render_anomaly_monitor
from app.utils.df_format import numeric_column_config
from app.utils.page_common import section_tone
from app.utils.page_state import persist_many
try:
    from app.services.weather_sync_service import sync_all_stations, get_weather_sync_status
    _WEATHER_SYNC_AVAILABLE = True
except ImportError:
    _WEATHER_SYNC_AVAILABLE = False


def _render_model_status_body():
    """모델 상태 본문(외부 컨테이너 없이 컨텐츠만 렌더)."""
    registry = get_model_registry()
    st.markdown(
        '<div class="section-title">'
        '<span class="section-title-icon">📦</span>모델 정보'
        '<span class="section-title-sub">v5 앙상블 학습 결과</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.markdown("**현재 활성 모델**")
    active_key = registry.get("active_model_key", "-")
    active_path = registry.get("active_model_path")
    active_artifact = registry.get("active_artifact") if isinstance(registry.get("active_artifact"), dict) else {}
    st.write(f"- active_key: `{active_key}`")
    st.code(str(active_path), language="text")
    active_abs_path = get_active_model_path()
    if not active_abs_path.exists():
        st.warning(
            "모델 파일이 없습니다. `v5_model_registry.json`의 현재 설정에 맞는 모델 파일을 준비한 뒤 다시 시도하세요."
        )
    if active_artifact:
        sha = str(active_artifact.get("sha256") or "")
        size_bytes = active_artifact.get("size_bytes")
        size_mb = None
        try:
            size_mb = float(size_bytes) / 1024 / 1024 if size_bytes is not None else None
        except (TypeError, ValueError):
            size_mb = None
        st.write(f"- sha256: `{sha[:12]}...`" if sha else "- sha256: `-`")
        if size_mb is not None:
            st.write(f"- size: `{size_mb:,.1f} MB`")
        st.write(f"- git: `{active_artifact.get('git_commit', '-')}`")
    st.markdown("**업데이트 시각**")
    st.write(f"- weights: `{registry.get('weights_updated_at', '-')}`")
    st.write(f"- full: `{registry.get('full_trained_at', '-')}`")
    st.write(f"- data_end: `{registry.get('data_end_date_global', '-')}`")


# 모델 상태 화면을 구성합니다.
def _render_model_status(*, height="content"):
    """모델 상태 패널 (자체 bordered 컨테이너 포함). height='stretch' 시 부모 행에 맞춰 늘어남."""
    with st.container(border=True, height=height):
        _render_model_status_body()


def _render_retrain_section_body():
    """모델 재학습 본문(외부 컨테이너 없이 컨텐츠만 렌더). 관리자 전용."""
    if not is_admin():
        return

    status = get_training_status()
    st_status = status.get("status", "unknown")
    progress_current = int(status.get("progress_current") or 0)
    progress_total = int(status.get("progress_total") or 0)
    progress_pct = float(status.get("progress_pct") or 0.0)
    progress_ratio = min(max(progress_pct / 100.0, 0.0), 1.0)
    if st_status == "success" and progress_pct <= 0:
        progress_ratio = 1.0
        progress_pct = 100.0

    st.markdown(
        '<div class="section-title">'
        '<span class="section-title-icon">🧠</span>모델 재학습'
        '<span class="section-title-sub">관리자 전용 · 수동 트리거</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    col_a, col_b = st.columns([3, 1])

    with col_a:
        # 성공 표시는 "실제 모델 활성화"가 완료된 경우에만. noop/skip/시작실패는 성공 아님.
        mode = status.get("mode")
        new_model_path = status.get("new_model_path")
        activated = (
            st_status == "success"
            and mode in {"full_quantile", "full", "weights"}
            and bool(new_model_path)
        )
        if st_status == "running":
            st.warning("학습 진행 중")
        elif st_status == "interrupted":
            # 전원 차단/크래시로 워커가 죽었으나 상태가 'running'으로 남은 경우.
            # 모델·레지스트리는 무손상이므로 그대로 다시 요청하면 된다.
            st.warning("⚠️ 이전 학습이 중단되었습니다 (전원 차단/프로세스 종료). 모델은 그대로이며, 다시 요청하면 처음부터 재학습합니다.")
        elif st_status == "fail":
            st.error("학습 실패")
        elif activated:
            st.success("재학습 완료 — 새 모델 활성화됨")
        elif st_status == "success":
            st.info("대기/완료 — 모델 변경 없음")
        else:
            st.info("대기")

        st.progress(progress_ratio)
        if progress_total > 0:
            st.caption(f"{progress_pct:.1f}% ({progress_current}/{progress_total})")
        else:
            st.caption(f"{progress_pct:.1f}%")

        current_step = status.get("current_step") or status.get("message") or "-"
        st.write(f"- 진행상태: `{current_step}`")

        # "재학습일"과 "데이터 마지막 날짜"는 활성 모델의 실제 상태를 반영해야 하므로
        # registry(=학습이 성공해야만 갱신되는 단일 소스)에서 읽는다.
        # 학습이 실패/중단되면 registry는 그대로이므로 두 필드도 그대로 유지된다.
        try:
            registry = get_model_registry()
        except Exception:
            registry = {}
        weights_iso = registry.get("weights_updated_at")
        retrain_date = (
            weights_iso[:10] if isinstance(weights_iso, str) and len(weights_iso) >= 10 else "-"
        )
        st.write(f"- 재학습일: `{retrain_date}`")
        st.write(f"- 데이터 마지막 날짜: `{registry.get('data_end_date_global') or '-'}`")
        st.write(f"- 학습 데이터 끝일: `{registry.get('train_end_date_global') or '-'}`")

        current_factory = status.get("current_factory")
        current_target = status.get("current_target")
        if current_factory or current_target:
            st.write(f"- 대상: `{current_factory or '-'} / {current_target or '-'}`")

        # 분위수 전체 재학습 진행 세부 — 단계/현재 학습 모델 유형·분위수/롤링 분할 경계
        phase = status.get("phase")
        if phase:
            st.write(f"- 단계: `{phase}`")

        cur_mtype = status.get("current_model_type")
        cur_q = status.get("current_quantile")
        if cur_mtype or cur_q is not None:
            try:
                q_label = f"P{int(round(float(cur_q) * 100)):02d}" if cur_q is not None else "-"
            except (TypeError, ValueError):
                q_label = "-"
            st.write(f"- 현재 학습: `{cur_mtype or '-'} / {q_label}`")

        split = status.get("split") or {}
        if split:
            st.write(
                f"- 분할: 학습≤`{split.get('train_end', '-')}` · "
                f"검증`{split.get('valid_start', '-')}~{split.get('valid_end', '-')}` · "
                f"테스트`{split.get('test_start', '-')}~{split.get('test_end', '-')}`"
            )

        if status.get("error"):
            st.caption(f"error: {status.get('error')}")

        run_id = status.get("run_id")
        if run_id:
            st.caption(f"run_id: {run_id}")

    with col_b:
        is_running = st_status == "running"
        if st.button(
            "🔄 재학습 요청",
            use_container_width=True,
            key="btn_retrain",
            disabled=is_running,
        ):
            info = trigger_v5_retrain(changed_factories=None, trigger_mode="manual")
            if info.get("started"):
                st.success(info.get("message"))
                st.rerun()
            else:
                st.warning(info.get("message"))

        if st.button("↻ 상태 새로고침", use_container_width=True, key="btn_retrain_refresh"):
            st.rerun()

        st.caption("관리자 계정에서만 수동 재학습을 실행할 수 있습니다.")


def _render_retrain_section(*, height="content"):
    """모델 재학습 패널 (자체 bordered 컨테이너 포함). height='stretch' 시 부모 행에 맞춰 늘어남."""
    with st.container(border=True, height=height):
        _render_retrain_section_body()


# 날씨 데이터 동기화 패널 (관리자 여부 무관 - DB 접근 없음)
def _render_weather_sync_panel():
    """날씨 데이터 동기화 패널 - 기상청 ASOS API → Excel 파일 갱신. 관리자 불필요."""
    if not _WEATHER_SYNC_AVAILABLE:
        return

    with st.container(border=True):
        section_tone("cyan")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🌤️</span>날씨 데이터 동기화'
            '<span class="section-title-sub">기상청 ASOS API · 파일만 갱신</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.caption("누락된 날씨 데이터를 자동 수집합니다. DB 접근 없이 파일만 갱신하므로 누구나 실행할 수 있습니다.")

        # 현재 날씨 데이터 상태 표시
        try:
            weather_status = get_weather_sync_status()
            cols = st.columns(len(weather_status))
            for i, (station, info) in enumerate(weather_status.items()):
                with cols[i]:
                    icon = "✅" if info["is_up_to_date"] else "⚠️"
                    missing = info["missing_days"]
                    missing_str = f"({missing}일 누락)" if missing and missing > 0 else ""
                    st.metric(
                        label=f"{icon} {station}",
                        value=info["last_date"],
                        delta=missing_str if missing_str else "최신",
                        delta_color="inverse" if missing and missing > 0 else "normal",
                    )
        except Exception:
            st.info("날씨 상태를 불러올 수 없습니다.")

        if st.button("🌤️ 날씨 데이터 지금 동기화", use_container_width=True, key="btn_weather_sync", type="primary"):
            with st.spinner("기상청 API에서 날씨 데이터를 수집 중입니다..."):
                try:
                    results = sync_all_stations()
                    total_added = sum(r.get("added_days", 0) for r in results)
                    errors = [r for r in results if r.get("error")]
                    if errors:
                        for e in errors:
                            st.warning(f"{e['station']}: {e['error']}")
                    if total_added > 0:
                        st.success(f"✅ 총 {total_added}일치 날씨 데이터가 추가되었습니다.")
                        for r in results:
                            if r.get("added_days", 0) > 0:
                                st.caption(f"  - {r['station']}: +{r['added_days']}일 (최종일: {r['last_date']})")
                    elif not errors:
                        st.info("모든 관측소가 이미 최신 상태입니다.")
                except Exception as e:
                    st.error(f"동기화 중 오류가 발생했습니다: {e}")


def _batch_results_to_rows(batch_results: list[dict]) -> list[dict]:
    """predict_v5_batch 결과를 화면 표시용 행 목록으로 변환.

    v5.2 결과에 pred_p05/pred_p95/band_status가 있으면 같이 표시.
    """
    from app.services.v5_common import BAND_STATUS_LABELS_KO  # 지연 import (순환 방지)
    rows: list[dict] = []
    for day_r in batch_results:
        for tgt, rr in day_r.get("results", {}).items():
            if "error" in rr:
                continue
            pred_val = rr.get("pred", 0.0)
            actual_val = rr.get("actual")
            err_pct = None
            if actual_val is not None and actual_val != 0:
                err_pct = abs(pred_val - actual_val) / max(abs(actual_val), 1.0) * 100.0

            p05 = rr.get("pred_p05")
            p95 = rr.get("pred_p95")
            band_status = rr.get("band_status")
            band_position = rr.get("band_position")

            row = {
                "날짜": day_r["date"],
                "항목": tgt,
                "예측 P50": round(pred_val, 2),
                "정상하한 P05": round(p05, 2) if p05 is not None else None,
                "정상상한 P95": round(p95, 2) if p95 is not None else None,
                "실측값": round(actual_val, 2) if actual_val is not None else None,
                "상태": BAND_STATUS_LABELS_KO.get(band_status, "—") if band_status else "—",
                "위치(±)": round(band_position, 2) if band_position is not None else None,
                "오차(%)": round(err_pct, 2) if err_pct is not None else None,
                "생산량(kg)": round(day_r.get("mix_prod_kg", 0.0), 1),
            }
            rows.append(row)
    return rows


def _render_single_factory_result(
    factory_label: str,
    batch_results: list[dict],
    selected_targets: list[str],
):
    """단일 공장 예측 결과(표 + 차트)를 한 컨테이너에 렌더링.

    레이아웃:
      ┌ 🏭 {공장명} (좌측 상단 칩)
      │ 📊 예측 결과   {N}건 (우측)
      │ ▼ 📄 데이터 테이블  (접었다 폈다)
      │ [차트]
      └
    """
    rows = _batch_results_to_rows(batch_results)
    if not rows:
        return
    with st.container(border=True):
        section_tone("emerald")
        # 좌측 상단 — 어떤 공장 결과인지 한눈에 보이는 칩 배지
        st.markdown(
            f'<div style="display:inline-flex; align-items:center; gap:8px;'
            f' padding:4px 12px; margin:0 0 6px 0;'
            f' background:var(--accent-soft);'
            f' border-left:3px solid var(--accent);'
            f' border-radius:6px;">'
            f'<span style="font-size:0.95rem;">🏭</span>'
            f'<span style="font-size:0.9rem; font-weight:700; color:var(--text-primary);">{factory_label}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        # 섹션 타이틀 — 우측 보조에는 건수만 (공장명은 위 칩으로 이동)
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📊</span>예측 결과'
            f'<span class="section-title-sub">{len(rows)}건</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        df_out = pd.DataFrame(rows)
        # 데이터 테이블 — 접었다 폈다 가능 (전체 공장 모드에서 5개 표가 누적되는 경우 가독성↑)
        with st.expander(
            "📄 데이터 테이블 (날짜·항목·정상범위·실측·상태·생산량)", expanded=True
        ):
            st.dataframe(df_out, use_container_width=True, hide_index=True,
                         column_config=numeric_column_config(df_out))
        _render_batch_chart(df_out, selected_targets)

        # ── 모델 변수 영향도 (실무자가 "어떤 변수 때문에 이 예측이 나왔는지" 이해하도록) ──
        _render_factory_feature_importance(factory_label, selected_targets)


def _render_factory_feature_importance(factory_label: str, selected_targets: list[str]) -> None:
    """예측 결과 아래에 항목별 모델 변수 영향도 Top 5 노출."""
    try:
        from app.services.v5_explainability import (
            explain_top_features_korean,
            get_v5_feature_importance,
        )
    except Exception:
        return

    # 집계 공장(전사/남양주)은 자체 모델 없음 → 스킵
    if factory_label in ("전사", "남양주"):
        return

    target_label_map = {"power": "⚡ 전력", "fuel": "🔥 연료", "water": "💧 용수"}
    blocks: list[tuple[str, list[dict]]] = []
    for tgt in selected_targets:
        items = get_v5_feature_importance(factory=factory_label, target=tgt, top_n=5)
        if items:
            blocks.append((target_label_map.get(tgt, tgt), items))
    if not blocks:
        return

    with st.expander("📊 모델 변수 영향도 — 어떤 변수가 이 예측을 좌우하는가?", expanded=False):
        st.caption(
            "실무자가 데이터 비전공자라도 \"왜 이 값이 나왔는가\"를 이해할 수 있도록, "
            "이 모델이 학습 과정에서 가장 자주·강하게 활용한 변수 Top 5 를 항목별로 보여줍니다."
        )
        cols = st.columns(len(blocks))
        for col, (label, items) in zip(cols, blocks):
            with col:
                st.markdown(
                    f"<div style='font-weight:700;font-size:0.92rem;color:var(--text-primary);"
                    f"margin-bottom:4px;'>{label}</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"<div style='color:var(--text-primary);font-size:0.82rem;line-height:1.5;"
                    f"padding:6px 8px;background:var(--bg-card2);border-left:3px solid #818cf8;"
                    f"border-radius:5px;margin-bottom:8px;'>"
                    f"💬 {explain_top_features_korean(items)}</div>",
                    unsafe_allow_html=True,
                )
                # 막대 시각화 — 단순 HTML 사용 (plotly 다중 차트 방지로 가벼움 유지)
                bar_html = ""
                for it in items:
                    pct = it["importance"] * 100
                    bar_html += (
                        f"<div style='margin:5px 0;'>"
                        f"<div style='display:flex;justify-content:space-between;font-size:0.8rem;'>"
                        f"<span style='color:var(--text-primary);'>#{it['rank']}. {it['label']}</span>"
                        f"<span style='color:#818cf8;font-weight:600;'>{pct:.1f}%</span>"
                        f"</div>"
                        f"<div style='background:var(--border);border-radius:4px;height:6px;margin-top:2px;'>"
                        f"<div style='background:#818cf8;width:{min(pct*2, 100)}%;height:100%;border-radius:4px;'></div>"
                        f"</div></div>"
                    )
                st.markdown(bar_html, unsafe_allow_html=True)


# 직전 일괄 예측 결과를 보관하는 session_state 키. 예측은 수십 초가 걸리므로
# 버튼 콜백 안에서만 렌더하면 이후 위젯 touch로 rerun 될 때 결과가 통째로 사라진다.
# 결과를 여기에 저장해 두고 매 rerun 마다 다시 그린다.
BATCH_RESULT_KEY = "batch_prediction_result"


def _render_persisted_batch_result() -> None:
    """session_state에 저장된 직전 일괄 예측 결과를 렌더링.

    버튼 클릭 여부와 무관하게 매 rerun 마다 호출되어, 예측 후 다른 위젯을
    조작해 rerun이 발생해도 예측 결과(표·차트)가 유지되도록 한다.
    """
    payload = st.session_state.get(BATCH_RESULT_KEY)
    if not payload:
        return

    selected_targets = payload["selected_targets"]
    save_to_db = payload["save_to_db"]
    sig = payload.get("input_sig")
    if sig:
        st.caption(f"📌 표시 중인 예측 결과: {sig[0]} · {sig[1]}~{sig[2]} (다시 실행하기 전까지 유지됩니다)")

    if payload["mode"] == "all":
        total_days = payload["total_days"]
        if save_to_db:
            st.success(f"✅ 5개 공장 · 총 {total_days}건 예측 완료 (자동 저장됨)")
        else:
            st.success(f"✅ 5개 공장 · 총 {total_days}건 예측 완료")
            st.info("ℹ️ viewer 모드에서는 예측 이력이 저장되지 않습니다. 관리자 모드(host PC)에서 실행하세요.")
        for m in payload["fail_messages"]:
            st.warning(f"⚠️ 일부 실패: {m}")

        results_by_factory = payload["results_by_factory"]
        for fac in FACTORY_PHYSICAL_DISPLAY_ORDER:
            fac_results = results_by_factory.get(fac, [])
            if not fac_results:
                continue
            _render_single_factory_result(fac, fac_results, selected_targets)
        return

    # 단일/집계 공장 모드
    selected_factory = payload["selected_factory"]
    batch_results = payload["batch_results"]
    manual_editor_supported = payload["manual_editor_supported"]
    if save_to_db and not manual_editor_supported:
        st.success(f"✅ {len(batch_results)}일 집계 예측 완료 (구성 공장 예측값 자동 저장/재사용)")
    elif save_to_db:
        st.success(f"✅ {len(batch_results)}일 예측 완료 (자동 저장됨)")
    else:
        st.success(f"✅ {len(batch_results)}일 예측 완료")
        st.info("ℹ️ viewer 모드에서는 예측 이력이 저장되지 않습니다. 관리자 모드로 로그인하세요.")
    _render_single_factory_result(selected_factory, batch_results, selected_targets)


# 일괄 예측 tab 화면을 구성합니다.
def _render_batch_prediction_tab():
    """탭 1: 다기간 일괄 예측.

    공장 선택 시 동작:
      - 단일 실공장(남양주1·남양주2·김해·광주·논산): 수동 입력 에디터 + 단일 결과
      - 집계 공장(전사·남양주): 입력 에디터 없이 합산 결과 1건
      - 전체: 5개 실공장을 순차 예측한 뒤 공장별로 결과 섹션을 분리해 표시
    """
    factories = list(PREDICTION_FACTORY_OPTIONS)

    with st.container(border=True):
        section_tone("violet")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">⚙️</span>예측 조건'
            '<span class="section-title-sub">공장 · 기간 선택</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        # 예측 이력 탭과 동일한 3컬럼 레이아웃(공장·시작·종료)으로 통일.
        # 예측 항목은 항상 전력/연료/용수 세 가지 모두 예측하므로 별도 선택을 두지 않음.
        c1, c2, c3 = st.columns([1, 1, 1])

        with c1:
            selected_factory = st.selectbox("공장", options=factories, index=0, key="batch_factory")
        with c2:
            default_from = (datetime.today() - timedelta(days=7)).date()
            date_from = st.date_input("시작 날짜", value=default_from, key="batch_date_from")
        with c3:
            default_to = datetime.today().date()
            date_to = st.date_input("종료 날짜", value=default_to, key="batch_date_to")

    # 예측 대상은 고정: 전력 / 연료 / 용수 (TARGET_SPECS 의 모든 키)
    selected_targets = list(TARGET_SPECS.keys())

    # Validate
    if date_from > date_to:
        st.error("시작 날짜가 종료 날짜보다 이후입니다.")
        return

    day_count = (date_to - date_from).days + 1
    st.caption(f"📅 선택 기간: {day_count}일 (영업일만 예측)")

    is_batch_all = (selected_factory == BATCH_ALL_FACTORIES_LABEL)

    # 입력 데이터 편집 영역 — 전체/집계 공장은 미지원
    edited_df = None
    if is_batch_all:
        with st.container(border=True):
            st.markdown(
                '<div class="section-title">'
                '<span class="section-title-icon">🗂️</span>전체 안내'
                '</div>',
                unsafe_allow_html=True,
            )
            st.info(
                "`전체` 모드에서는 5개 실공장(남양주1·남양주2·김해·광주·논산)을 "
                "순차로 자동 예측한 뒤 공장별로 결과 섹션을 분리해 보여 줍니다. "
                "이 모드에서는 수동 입력 편집을 지원하지 않습니다 — 공장별 사전 채움 데이터를 그대로 사용합니다."
            )
        manual_editor_supported = False
    else:
        manual_editor_supported = is_manual_editor_supported(selected_factory)
        if manual_editor_supported:
            df_prefill = get_prefill_data_batch(
                selected_factory,
                date_from,
                date_to,
                selected_targets,
            )

            if df_prefill.empty:
                st.warning("선택된 기간에 영업일이 없습니다.")
                return

            with st.container(border=True):
                st.markdown(
                    '<div class="section-title">'
                    '<span class="section-title-icon">📝</span>입력 데이터 확인·수정'
                    '<span class="section-title-sub">영업일 기준 · 셀 직접 편집 가능</span>'
                    '</div>',
                    unsafe_allow_html=True,
                )

                wip_cols = [c for c in df_prefill.columns if str(c).startswith("wip_")]
                if wip_cols:
                    required_item_codes = ", ".join(col.replace("wip_", "") for col in wip_cols)
                    st.info(f"현재 활성 모델은 재공품 입력을 사용합니다. 입력 대상 품목: `{required_item_codes}`")
                    if date_from < date(2023, 1, 1):
                        st.warning("재공품 피처 모델은 2023-01-01 이후 날짜만 지원합니다.")

                st.caption(
                    "아래 표의 데이터를 직접 수정하여 예측에 반영할 수 있습니다. "
                    "💡 엑셀에서 셀 범위를 복사한 뒤 표의 시작 셀을 클릭하고 Ctrl+V 로 한 번에 붙여넣을 수 있습니다."
                )
                edited_df = st.data_editor(
                    df_prefill,
                    use_container_width=True,
                    hide_index=True,
                    disabled=["날짜"],
                    key=f"batch_data_editor_{selected_factory}_{date_from}_{date_to}_{'_'.join(selected_targets)}"
                )
        else:
            with st.container(border=True):
                st.markdown(
                    '<div class="section-title">'
                    '<span class="section-title-icon">🧮</span>집계 공장 안내'
                    '</div>',
                    unsafe_allow_html=True,
                )
                st.info(
                    f"`{selected_factory}` 는 집계 공장입니다. "
                    "구성 공장의 저장된 예측값을 우선 재사용하고, 없는 값만 각 공장별로 자동 예측한 뒤 합산합니다. "
                    "집계 모드에서는 수동 입력 편집을 지원하지 않습니다."
                )

    btn_label = "🤖 전체 예측 실행" if is_batch_all else "🤖 일괄 예측 실행"
    if st.button(btn_label, type="primary", use_container_width=True, key="btn_batch"):
        save_to_db = is_admin()
        input_sig = (
            selected_factory,
            date_from.strftime("%Y-%m-%d"),
            date_to.strftime("%Y-%m-%d"),
        )
        try:
            if is_batch_all:
                # 5개 실공장 순차 예측 — 진행률은 progress 바로 표시.
                target_factories = list(FACTORY_PHYSICAL_DISPLAY_ORDER)
                progress = st.progress(0.0, text="전체 예측 시작...")
                results_by_factory: dict[str, list[dict]] = {}
                fail_messages: list[str] = []
                for idx, fac in enumerate(target_factories):
                    progress.progress(
                        idx / len(target_factories),
                        text=f"({idx+1}/{len(target_factories)}) {fac} 예측 중...",
                    )
                    try:
                        fac_results = predict_v5_batch(
                            factory=fac,
                            date_from=date_from,
                            date_to=date_to,
                            targets=selected_targets,
                            override_df=None,
                            save_to_db=save_to_db,
                        )
                        results_by_factory[fac] = fac_results
                    except Exception as fac_exc:  # 일부 공장 실패해도 나머지 진행
                        fail_messages.append(f"{fac}: {fac_exc}")
                        results_by_factory[fac] = []
                progress.progress(1.0, text="전체 예측 완료")
                progress.empty()  # 완료된 진행바 제거 (결과는 아래에서 영속 렌더)

                total_days = sum(len(v) for v in results_by_factory.values())
                if total_days == 0:
                    st.session_state.pop(BATCH_RESULT_KEY, None)
                    st.warning("선택된 기간에 영업일이 없거나 모든 공장 예측이 실패했습니다.")
                    for m in fail_messages:
                        st.error(m)
                    return

                # 결과를 session_state에 저장 — 렌더는 함수 끝의 영속 렌더가 담당.
                st.session_state[BATCH_RESULT_KEY] = {
                    "mode": "all",
                    "input_sig": input_sig,
                    "selected_targets": selected_targets,
                    "save_to_db": save_to_db,
                    "results_by_factory": results_by_factory,
                    "fail_messages": fail_messages,
                    "total_days": total_days,
                }
            else:
                # ── 단일/집계 공장 모드 ──
                with st.spinner(f"{selected_factory} {date_from}~{date_to} 예측 중..."):
                    batch_results = predict_v5_batch(
                        factory=selected_factory,
                        date_from=date_from,
                        date_to=date_to,
                        targets=selected_targets,
                        override_df=edited_df if manual_editor_supported else None,
                        save_to_db=save_to_db,
                    )

                if not batch_results:
                    st.session_state.pop(BATCH_RESULT_KEY, None)
                    st.warning("선택된 기간에 영업일이 없습니다.")
                    return

                st.session_state[BATCH_RESULT_KEY] = {
                    "mode": "single",
                    "input_sig": input_sig,
                    "selected_factory": selected_factory,
                    "selected_targets": selected_targets,
                    "save_to_db": save_to_db,
                    "batch_results": batch_results,
                    "manual_editor_supported": manual_editor_supported,
                }

        except FileNotFoundError as e:
            st.error(f"예측 실패: {e}")
            st.info("`RUN_GUIDE_KR.md`의 'v5 모델 생성'을 먼저 수행한 뒤 다시 시도하세요.")
            return
        except Exception as e:
            st.error(f"예측 실패: {e}")
            return

    # 버튼 클릭 여부와 무관하게, 저장된 결과가 있으면 매 rerun 마다 재렌더.
    # (예측 후 다른 위젯 touch로 rerun 되어도 수십 초짜리 결과가 사라지지 않도록)
    _render_persisted_batch_result()


# 일괄 chart 화면을 구성합니다.
def _render_batch_chart(df: pd.DataFrame, targets: list[str]):
    """예측 vs 실측 비교 차트.

    v5.2: P05~P95 정상범주 음영 + P50 라인 + 실측 점(밴드 밖은 빨강/주황 강조).
    v5.1 폴백: 기존대로 P50 라인만 표시.
    """
    for tgt in targets:
        df_t = df[df["항목"] == tgt].copy()
        if df_t.empty:
            continue

        unit = TARGET_SPECS.get(tgt, {}).get("unit", "")
        fig = go.Figure()

        # 정상범주 음영 (P05~P95) — v5.2일 때만
        has_band = ("정상하한 P05" in df_t.columns and "정상상한 P95" in df_t.columns
                    and df_t["정상하한 P05"].notna().any()
                    and df_t["정상상한 P95"].notna().any())
        if has_band:
            df_band = df_t.dropna(subset=["정상하한 P05", "정상상한 P95"]).sort_values("날짜")
            # 음영을 위한 fill='tonexty' 트릭 — 상한을 먼저 그리고 하한을 그 위에 동일 색 fill
            fig.add_trace(go.Scatter(
                x=df_band["날짜"], y=df_band["정상상한 P95"],
                mode="lines",
                line=dict(width=0),
                showlegend=False,
                hoverinfo="skip",
                name="P95",
            ))
            fig.add_trace(go.Scatter(
                x=df_band["날짜"], y=df_band["정상하한 P05"],
                mode="lines",
                line=dict(width=0),
                fill="tonexty",
                fillcolor="rgba(79,195,247,0.18)",
                name="정상범주 P05~P95",
                hovertemplate="P05~P95: %{y:,.1f}<extra></extra>",
            ))

        # P50(중앙값) 라인
        fig.add_trace(go.Scatter(
            x=df_t["날짜"], y=df_t["예측 P50"],
            mode="lines+markers",
            name="예측 중앙값 P50",
            line=dict(color="#4FC3F7", width=2),
            marker=dict(size=6),
        ))

        # 실측 — 밴드 밖 마커는 빨강(over)/주황(under)
        if df_t["실측값"].notna().any():
            if has_band:
                df_actual = df_t.dropna(subset=["실측값"]).copy()
                colors = []
                for _, r in df_actual.iterrows():
                    status = str(r.get("상태", ""))
                    if "과사용" in status:
                        colors.append("#dc2626")
                    elif "저사용" in status:
                        colors.append("#f59e0b")
                    else:
                        colors.append("#FF8A65")
                fig.add_trace(go.Scatter(
                    x=df_actual["날짜"], y=df_actual["실측값"],
                    mode="lines+markers",
                    name="실측값",
                    line=dict(color="#FF8A65", width=2),
                    marker=dict(size=8, color=colors,
                                line=dict(color="white", width=1)),
                ))
            else:
                fig.add_trace(go.Scatter(
                    x=df_t["날짜"], y=df_t["실측값"],
                    mode="lines+markers",
                    name="실측값",
                    line=dict(color="#FF8A65", width=2),
                    marker=dict(size=6),
                ))

        xaxis_cfg = _build_date_xaxis_config(df_t["날짜"])

        # 다크 테마 정합 — 앱 전체가 다크인데 이 차트만 plotly_white(흰 배경)로
        # 렌더되어 이질적이었음. 투명 배경 + 밝은 폰트/그리드로 통일.
        fig.update_layout(
            title=f"{tgt} 정상범주 + 실측 ({unit})" if has_band else f"{tgt} 예측 vs 실측 ({unit})",
            xaxis_title="날짜",
            yaxis_title=unit,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#e9f0fb", family="Inter, Segoe UI, sans-serif"),
            height=340 if has_band else 320,
            margin=dict(l=40, r=20, t=60, b=40),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            xaxis=xaxis_cfg,
        )
        fig.update_xaxes(gridcolor="rgba(120,160,220,0.14)", tickfont=dict(color="#e9f0fb"))
        fig.update_yaxes(gridcolor="rgba(120,160,220,0.14)", tickfont=dict(color="#e9f0fb"))
        st.plotly_chart(fig, use_container_width=True)


# 날짜 x축의 시작·끝은 YYYY-MM-DD, 중간 눈금은 MM-DD로 표시합니다.
def _build_date_xaxis_config(date_series: pd.Series, max_ticks: int = 8) -> dict:
    dates = pd.to_datetime(pd.Series(date_series), errors="coerce").dropna()
    if dates.empty:
        return dict(tickformat="%m-%d")

    unique_sorted = sorted(set(dates.dt.normalize().tolist()))
    n = len(unique_sorted)
    if n == 1:
        only = unique_sorted[0]
        return dict(
            tickmode="array",
            tickvals=[only],
            ticktext=[only.strftime("%Y-%m-%d")],
        )

    # 균등 간격 인덱스 + 첫/끝 인덱스 보장 (ceil로 라벨 개수 상한 유지)
    import math
    step = max(1, math.ceil((n - 1) / max(1, max_ticks - 1)))
    indices = sorted(set(list(range(0, n, step)) + [n - 1]))

    tickvals = [unique_sorted[i] for i in indices]
    ticktext = []
    for i, ts in zip(indices, tickvals):
        if i == 0 or i == n - 1:
            ticktext.append(ts.strftime("%Y-%m-%d"))
        else:
            ticktext.append(ts.strftime("%m-%d"))

    return dict(tickmode="array", tickvals=tickvals, ticktext=ticktext)


def _render_history_charts(df_hist: pd.DataFrame, factory_filter: str | None):
    """예측 이력 추이 차트."""
    if df_hist.empty:
        return

    st.markdown("### 📈 추이")
    df_chart = df_hist.copy()
    df_chart["pred_date"] = pd.to_datetime(df_chart["pred_date"], errors="coerce")
    df_chart = df_chart.dropna(subset=["pred_date"]).sort_values(["factory", "pred_date", "target"]).reset_index(drop=True)

    if factory_filter:
        grouped: list[tuple[str, pd.DataFrame]] = [("", df_chart)]
    else:
        # '전체' 필터 결과는 표준 순서(전사 → 남양주 → 김해 → 광주 → 논산)로 그룹화.
        # 표준 순서에 없는 공장(예: 신규/구버전 잔재)은 사전순으로 뒤에 따라옴.
        groups_dict = {f: g for f, g in df_chart.groupby("factory", sort=False)}
        ordered: list[tuple[str, pd.DataFrame]] = []
        for f in FACTORY_DISPLAY_ORDER:
            if f in groups_dict:
                ordered.append((f, groups_dict.pop(f)))
        for f in sorted(groups_dict.keys()):
            ordered.append((f, groups_dict[f]))
        grouped = ordered

    for factory_name, df_factory in grouped:
        if not factory_filter and factory_name:
            st.markdown(f"#### {factory_name}")

        from app.services.v5_common import BAND_STATUS_LABELS_KO  # 지연 import
        chart_rows = []
        for _, row in df_factory.iterrows():
            p05_raw = row.get("pred_p05") if "pred_p05" in df_factory.columns else None
            p95_raw = row.get("pred_p95") if "pred_p95" in df_factory.columns else None
            p05 = None if (p05_raw is None or pd.isna(p05_raw)) else float(p05_raw)
            p95 = None if (p95_raw is None or pd.isna(p95_raw)) else float(p95_raw)
            bs = row.get("band_status") if "band_status" in df_factory.columns else None
            if isinstance(bs, float) and pd.isna(bs):
                bs = None
            chart_rows.append(
                {
                    "날짜": row["pred_date"],
                    "항목": row["target"],
                    "예측 P50": row["pred_value"],
                    "정상하한 P05": p05,
                    "정상상한 P95": p95,
                    "실측값": row["actual_value"],
                    "상태": BAND_STATUS_LABELS_KO.get(bs, "—") if bs else "—",
                }
            )

        if not chart_rows:
            continue

        df_factory_chart = pd.DataFrame(chart_rows)
        targets = [t for t in TARGET_SPECS.keys() if t in set(df_factory_chart["항목"].tolist())]
        _render_batch_chart(df_factory_chart, targets)

        # 예측 실행 탭과 동일한 모델 변수 영향도 차트를 이력에도 노출.
        # 집계 공장(전사/남양주)은 자체 모델이 없어 함수 내부에서 스킵됨.
        actual_factory = factory_filter if factory_filter else factory_name
        if actual_factory and targets:
            _render_factory_feature_importance(actual_factory, targets)


def _render_prediction_monitoring_panel(df_hist: pd.DataFrame) -> None:
    """최근 예측 이력 기반 모델 성능/offset 감지 패널."""
    monitoring_df = build_prediction_monitoring_summary(df_hist)
    overall = get_monitoring_overall_status(monitoring_df)

    with st.container(border=True):
        section_tone("rose")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🩺</span>모델 성능 감지'
            '<span class="section-title-sub">최근 bias · 패턴 일치 · offset</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        status = overall.get("status")
        message = str(overall.get("message") or "")
        if status in {"offset_alert", "degraded"}:
            st.error(message)
        elif status in {"watch", "offset_warning"}:
            st.warning(message)
        elif status == "normal":
            st.success(message)
        else:
            st.info(message)

        if monitoring_df.empty:
            st.caption("실측값이 있는 예측 이력이 쌓이면 자동으로 판정됩니다.")
            return

        metric_cols = st.columns(4)
        with metric_cols[0]:
            st.metric("전체 상태", str(overall.get("label", "-")))
        with metric_cols[1]:
            st.metric("감지", f"{int(overall.get('alert_count', 0))}건")
        with metric_cols[2]:
            st.metric("주의", f"{int(overall.get('warning_count', 0))}건")
        with metric_cols[3]:
            st.metric("정상", f"{int(overall.get('normal_count', 0))}건")

        display_cols = [
            "factory",
            "target",
            "status_label",
            "latest_from",
            "latest_to",
            "latest_n",
            "baseline_n",
            "latest_bias",
            "latest_bias_pct",
            "latest_mape",
            "baseline_mape",
            "direction_accuracy",
            "delta_corr",
            "one_sided_rate",
            "estimated_started_at",
            "recommendation",
        ]
        rename_map = {
            "factory": "공장",
            "target": "항목",
            "status_label": "상태",
            "latest_from": "최근 시작",
            "latest_to": "최근 종료",
            "latest_n": "최근 건수",
            "baseline_n": "기준 건수",
            "latest_bias": "평균 Bias",
            "latest_bias_pct": "Bias(%)",
            "latest_mape": "최근 MAPE(%)",
            "baseline_mape": "기준 MAPE(%)",
            "direction_accuracy": "방향 일치율(%)",
            "delta_corr": "증감 상관",
            "one_sided_rate": "한방향 잔차(%)",
            "estimated_started_at": "추정 시작일",
            "recommendation": "권장 조치",
        }
        df_display = monitoring_df[[c for c in display_cols if c in monitoring_df.columns]].rename(columns=rename_map)
        for col in [
            "평균 Bias",
            "Bias(%)",
            "최근 MAPE(%)",
            "기준 MAPE(%)",
            "방향 일치율(%)",
            "증감 상관",
            "한방향 잔차(%)",
        ]:
            if col in df_display.columns:
                df_display[col] = pd.to_numeric(df_display[col], errors="coerce").round(2)

        st.dataframe(
            df_display,
            use_container_width=True,
            hide_index=True,
            column_config=numeric_column_config(df_display),
        )
        st.caption("Offset은 증감 방향이 대체로 맞지만 예측-실측 차이가 한쪽으로 지속 누적될 때 감지됩니다.")

# 이력 tab 화면을 구성합니다.
def _render_history_tab():
    """탭 2: 예측 이력 조회."""
    factories = list(PREDICTION_FACTORY_OPTIONS)

    with st.container(border=True):
        section_tone("cyan")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🔎</span>이력 조회 조건'
            '<span class="section-title-sub">공장 · 기간 필터</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        c1, c2, c3 = st.columns([1, 1, 1])

        with c1:
            hist_factory = st.selectbox(
                "공장",
                options=["전체"] + factories,
                index=0,
                key="hist_factory",
            )
        with c2:
            hist_from = st.date_input(
                "시작 날짜",
                value=(datetime.today() - timedelta(days=30)).date(),
                key="hist_from",
            )
        with c3:
            hist_to = st.date_input(
                "종료 날짜",
                value=datetime.today().date(),
                key="hist_to",
            )

        do_generate_missing = False
        # 관리자에게는 [이력 조회 / 누락 예측 생성 / 실측값 역채움], viewer에게는 [이력 조회] 하나만 노출
        # → 실무자(viewer) 화면에서 운영 전용 버튼을 숨겨 UI 노이즈 제거.
        if is_admin():
            col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
            with col_btn1:
                do_query = st.button("📋 이력 조회", use_container_width=True, key="btn_hist_query")
            with col_btn2:
                do_generate_missing = st.button(
                    "🤖 누락 예측 생성",
                    use_container_width=True,
                    key="btn_generate_missing_history",
                    help="현재 조건에서 prediction_log에 없는 영업일/항목만 명시적으로 예측해 저장합니다.",
                )
            with col_btn3:
                if st.button(
                    "🔄 실측값 역채움",
                    use_container_width=True,
                    key="btn_backfill",
                    help="저장된 예측에 대응되는 실측값을 energy_daily에서 다시 읽어와 prediction_log에 채워 넣습니다.",
                ):
                    try:
                        with st.spinner("실측값 역채움 중..."):
                            cnt = backfill_actuals()
                        st.success(f"✅ {cnt}건 역채움 완료")
                    except Exception as e:
                        st.error(f"역채움 실패: {e}")
        else:
            do_query = st.button("📋 이력 조회", use_container_width=True, key="btn_hist_query")

    current_filters = {
        "factory": hist_factory,
        "date_from": hist_from.isoformat(),
        "date_to": hist_to.isoformat(),
    }

    # 필터 변경 시 자동 재조회 — 이전에는 조회 후 필터를 바꾸면 저장된 결과와
    # 조건 불일치로 화면이 통째로 사라져 버그처럼 보였음. 한 번이라도 조회한
    # 상태에서 조건이 바뀌면 버튼 재클릭 없이 새 조건으로 다시 조회한다.
    if not do_query:
        _stored_prev = st.session_state.get("prediction_history_filters")
        if (st.session_state.get("prediction_history_df") is not None
                and _stored_prev is not None
                and _stored_prev != current_filters):
            do_query = True

    if do_generate_missing:
        factory_filter = hist_factory if hist_factory != "전체" else None
        try:
            with st.spinner("누락 예측 생성 중..."):
                info = generate_missing_prediction_history(
                    factory=factory_filter,
                    date_from=hist_from,
                    date_to=hist_to,
                    targets=None,
                    save_to_db=True,
                )
            generated = int(info.get("generated_rows", 0) or 0)
            if generated > 0:
                st.success(f"✅ 누락 예측 {generated}건 생성/저장 완료")
            else:
                st.info("생성할 누락 예측이 없습니다.")
            do_query = True
        except Exception as e:
            st.error(f"누락 예측 생성 실패: {e}")

    if do_query:
        factory_filter = hist_factory if hist_factory != "전체" else None
        df_hist = get_prediction_history(
            factory=factory_filter,
            date_from=hist_from,
            date_to=hist_to,
        )

        if df_hist.empty:
            st.session_state["prediction_history_df"] = None
            st.session_state["prediction_history_filters"] = None
            st.info("조회된 이력이 없습니다. 관리자라면 같은 조건에서 `누락 예측 생성`을 먼저 실행할 수 있습니다.")
            return

        st.session_state["prediction_history_df"] = df_hist
        st.session_state["prediction_history_filters"] = current_filters

    stored_filters = st.session_state.get("prediction_history_filters")
    df_hist = st.session_state.get("prediction_history_df")
    if df_hist is None or stored_filters != current_filters:
        return

    # Summary cards — v5.2: PICP(밴드 안 비율) + MAPE 보조 표시
    with st.container(border=True):
        section_tone("emerald")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📊</span>이력 요약'
            '<span class="section-title-sub" title="PICP = 실측이 정상범주(P05~P95) 안에 들어온 비율. '
            '90%에 가까울수록 모델 신뢰도가 높음.">'
            '정상범주 적중률 · 항목별 ⓘ</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        df_with_actual = df_hist[df_hist["mape"].notna()]
        has_band = ("band_status" in df_hist.columns
                    and df_hist["band_status"].notna().any())

        if not df_with_actual.empty:
            picp_help = (
                "PICP(Prediction Interval Coverage Probability) = 실측이 정상범주 "
                "[P05, P95] 안에 들어온 비율. 90%가 이상적입니다(목표). "
                "MAPE는 참고 지표로 함께 표시합니다."
            )
            summary_cols = st.columns(len(TARGET_SPECS))
            for i, tgt in enumerate(TARGET_SPECS.keys()):
                df_t = df_with_actual[df_with_actual["target"] == tgt]
                with summary_cols[i]:
                    if df_t.empty:
                        st.metric(label=f"{tgt}", value="데이터 부족", help=picp_help)
                        continue
                    avg_mape = df_t["mape"].mean()
                    if has_band and "band_status" in df_t.columns:
                        bs = df_t["band_status"].dropna()
                        if not bs.empty:
                            picp = float((bs == "inside").sum()) / float(len(bs)) * 100.0
                            st.metric(
                                label=f"{tgt} PICP (정상범주 적중률)",
                                value=f"{picp:.1f}%",
                                help=picp_help,
                            )
                            st.caption(f"MAPE {avg_mape:.2f}% · {len(df_t)}건")
                            continue
                    # 폴백: v5.1 행만 있으면 기존 MAPE 카드
                    st.metric(
                        label=f"{tgt} 평균 MAPE",
                        value=f"{avg_mape:.2f}%",
                        help="이 항목은 v5.1(점추정) 이력만 있어 PICP를 계산할 수 없습니다.",
                    )
                    st.caption(f"({len(df_t)}건 기준)")
        else:
            st.caption("실측값이 있는 이력이 없어 요약을 표시할 수 없습니다.")

    _render_prediction_monitoring_panel(df_hist)

    # 추이 차트
    factory_filter = hist_factory if hist_factory != "전체" else None
    with st.container(border=True):
        section_tone("violet")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📈</span>예측 vs 실측 추이'
            '<span class="section-title-sub">항목별 시계열</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_history_charts(df_hist, factory_filter)

    # 상세 이력 테이블
    with st.container(border=True):
        section_tone("amber")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">📋</span>상세 이력'
            f'<span class="section-title-sub">{len(df_hist)}건</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        from app.services.v5_common import BAND_STATUS_LABELS_KO  # 지연 import
        rename_map = {
            "factory": "공장",
            "pred_date": "예측일",
            "target": "항목",
            "pred_value": "예측 P50",
            "pred_p05": "정상하한 P05",
            "pred_p95": "정상상한 P95",
            "actual_value": "실측값",
            "mape": "오차(%)",
            "band_status": "상태",
            "band_position": "위치(±)",
            "mix_prod_kg": "생산량(kg)",
            "created_at": "생성일시",
            "updated_at": "갱신일시",
        }
        # 존재하는 컬럼만 rename (v5.1 폴백 행 호환)
        rename_keep = {k: v for k, v in rename_map.items() if k in df_hist.columns}
        df_display = df_hist.rename(columns=rename_keep)

        if "상태" in df_display.columns:
            df_display["상태"] = df_display["상태"].map(
                lambda s: BAND_STATUS_LABELS_KO.get(s, "") if isinstance(s, str) else ""
            )

        for col in ["예측 P50", "정상하한 P05", "정상상한 P95",
                    "실측값", "오차(%)", "위치(±)", "생산량(kg)"]:
            if col in df_display.columns:
                df_display[col] = pd.to_numeric(df_display[col], errors="coerce").round(2)

        st.dataframe(df_display, use_container_width=True, hide_index=True,
                     column_config=numeric_column_config(df_display))
        from app.utils.page_common import csv_download  # 지연 import
        csv_download(
            df_display,
            filename=f"prediction_history_{hist_from}_{hist_to}.csv",
            key="dl_pred_history",
        )


# 이상감지 현황 탭 (홈 대시보드에서 이동 — 2026-07)
def _render_anomaly_tab():
    """탭 3: 이상감지 현황 — 알림 배너 + 공장×에너지원 그리드 + 관리도 + LLM 진단."""
    with st.container(border=True):
        section_tone("rose")
        st.markdown(
            '<div class="section-title">'
            '<span class="section-title-icon">🚨</span>이상감지 기준일'
            '<span class="section-title-sub">기준일 포함 최근 7일 이탈 · 최근 30일 지속편향 판정</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns([1, 2.2])
        with c1:
            anomaly_base = st.date_input(
                "기준일",
                value=date.today() - timedelta(days=1),
                key="anomaly_base_date",
                label_visibility="collapsed",
            )
        with c2:
            st.caption(
                "예측은 매일 자동 실행됩니다. 실측이 AI 정상범주(P05~P95)를 벗어나면 "
                "이상으로 판정하며, 반복 이탈·지속편향만 '경보'로 승격해 알람 피로를 줄입니다."
            )
    render_anomaly_monitor(anomaly_base)


# AI 예측 화면을 구성합니다.
def render_ai_prediction():
    """AI 에너지 사용 예측 페이지.

    실무 사용자(viewer)에게는 "예측 실행", "예측 이력", "이상감지 현황" 탭을 노출합니다.
    모델 메타데이터/재학습/날씨 동기화 같은 시스템 운영 기능은 관리자(host PC)
    전용 ⚙️ 모델 관리 탭으로 분리해 일반 사용자 화면을 단순하게 유지합니다.
    """
    # 페이지 이동 후 재방문에도 필터 값을 유지
    persist_many({
        "batch_factory":   None,
        "batch_date_from": None,
        "batch_date_to":   None,
        "hist_factory":    None,
        "hist_from":       None,
        "hist_to":         None,
        "anomaly_base_date": None,
    })

    admin_mode = is_admin()

    st.markdown("""
    <div class="sub-page-header">
        <span style="font-size:1.5rem;">🤖</span>
        <div>
            <div class="sub-page-title">에너지 사용 예측</div>
            <div class="sub-page-breadcrumb">AI 에너지 분석 > 에너지 사용 예측</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    if admin_mode:
        tab1, tab2, tab_anom, tab3 = st.tabs(
            ["📈 예측 실행", "📋 예측 이력", "🚨 이상감지 현황", "⚙️ 모델 관리"])
    else:
        tab1, tab2, tab_anom = st.tabs(["📈 예측 실행", "📋 예측 이력", "🚨 이상감지 현황"])
        tab3 = None

    with tab1:
        _render_batch_prediction_tab()

    with tab2:
        _render_history_tab()

    with tab_anom:
        _render_anomaly_tab()

    if tab3 is not None:
        with tab3:
            # 모델 정보 + 모델 재학습 좌우 같은 높이 카드 — Streamlit 1.43+ 의 height='stretch' 사용.
            col_info, col_retrain = st.columns(2, gap="medium")
            with col_info:
                _render_model_status(height="stretch")
            with col_retrain:
                _render_retrain_section(height="stretch")
            _render_weather_sync_panel()
