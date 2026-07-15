"""
Anomaly Monitor Component
=========================
AI 예측 기반 이상감지 현황 — 알림 배너 + 공장×에너지원 상태 그리드 + SPC 관리도 + LLM 진단.

원래 메인 대시보드(홈) 상단 섹션이었으나 2026-07 결정으로 홈에서 분리,
AI 에너지 분석 > 에너지 사용 예측 페이지의 "이상감지 현황" 탭에서 렌더링한다.
(dashboard_main.py 의 임원 요약 보고서는 _fetch_anomaly_data 를 계속 공유)

진입점: render_anomaly_monitor(base_date)
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.database.db_connection import get_connection, is_admin
from app.domain.factories import FACTORY_DISPLAY_ORDER
from app.services.anomaly_diagnosis_service import (
    get_or_create_diagnosis,
    get_cached_diagnosis,
    delete_cached_diagnosis,
)
from app.services.anomaly_rules_service import (
    CONSECUTIVE_RUN_MIN,
    FREQUENT_COUNT_MIN,
    SEVERITY_ALERT,
    SEVERITY_WATCH,
    detect_drift,
    evaluate_band_rules,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────
# AI 이상 알림 설정값
# ──────────────────────────────────────
# v5.2 도입: 이상 판정 = "실측이 정상범주(P05~P95) 밖" (정성적)
# v5.1 호환: 밴드가 없는 행은 MAPE 임계로 폴백 (점진적 마이그레이션)
ANOMALY_LOOKBACK_DAYS = 7      # 이상 감지 상세 테이블 조회 기간 (기준일 포함 최근 N일)
SPC_LOOKBACK_DAYS = 30         # SPC(통계적 공정 관리) 차트 조회 기간 — 패턴 인지 위해 더 길게
LEGACY_MAPE_THRESHOLD_PCT = 20.0   # v5.1 행 폴백 임계 (밴드 없으면 이 기준 적용)
BAND_POSITION_MIN = 0.0            # |band_position| ≥ 이 값 이상만 표시 (0이면 모든 over/under 표시)

# ── 테마 색상 (main.py DARK_VARS와 동기화) ──
_TEXT_PRIMARY   = "#e9f0fb"
_TEXT_SECONDARY = "#9db1cf"
_TEXT_MUTED     = "#647695"
_ACCENT_COLOR   = "#38bdf8"
_GRID_COLOR     = "rgba(120,160,220,0.14)"
_BG_CARD        = "#1a2a52"
_BG_CARD2       = "#22345f"
_BORDER_COLOR   = "rgba(122,164,224,0.22)"

# Plotly 차트 공통 레이아웃 (투명 배경 — 다크 카드와 자연스럽게 연결)
DARK_CHART = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=_TEXT_PRIMARY, family="Inter, Segoe UI, sans-serif", size=16),
)

FACTORY_OPTIONS = list(FACTORY_DISPLAY_ORDER)


# ──────────────────────────────────────
# 섹션 0: AI 예측 기반 이상 알림
# ──────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def _fetch_anomaly_data(base_date: date) -> pd.DataFrame:
    """prediction_log에서 최근 ANOMALY_LOOKBACK_DAYS 일간 이상 항목을 조회합니다.

    판정 기준 (v5.2 정성적 이상):
      - 1순위: band_status IN ('over','under') — 실측이 P05~P95 밴드 밖
      - 2순위(v5.1 폴백): 밴드 컬럼이 NULL이면 MAPE >= LEGACY_MAPE_THRESHOLD_PCT 또한 실측>예측(과사용)

    반환 컬럼:
      factory, pred_date, target, pred_value, pred_p05, pred_p95,
      actual_value, mape, band_status, band_position
    """
    date_from = base_date - timedelta(days=ANOMALY_LOOKBACK_DAYS - 1)
    query = """
        SELECT
            p.factory,
            p.pred_date,
            p.target,
            p.pred_value,
            p.pred_p05,
            p.pred_p95,
            p.actual_value,
            p.mape,
            p.band_status,
            p.band_position
        FROM prediction_log p
        WHERE p.pred_date BETWEEN %s AND %s
          AND p.actual_value IS NOT NULL
          AND p.pred_value IS NOT NULL
          AND p.pred_value > 0
          AND (
            -- v5.2: 밴드 밖 (정성적 이상)
            (p.band_status IN ('over','under')
             AND (p.band_position IS NULL OR ABS(p.band_position) >= %s))
            OR
            -- v5.1 폴백: 밴드 없는 행은 MAPE>=임계 + 실측>예측(과사용)
            (p.band_status IS NULL
             AND p.mape IS NOT NULL
             AND p.mape >= %s
             AND p.actual_value > p.pred_value)
          )
        ORDER BY
          ABS(COALESCE(p.band_position, 0)) DESC,
          COALESCE(p.mape, 0) DESC,
          p.pred_date DESC
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            query, conn,
            params=(
                date_from.strftime("%Y-%m-%d"),
                base_date.strftime("%Y-%m-%d"),
                BAND_POSITION_MIN,
                LEGACY_MAPE_THRESHOLD_PCT,
            ),
        )
        return df
    except Exception as exc:
        logger.exception("Failed to fetch anomaly data for base_date=%s: %s", base_date, exc)
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=120, show_spinner=False)
def _fetch_band_rule_series(base_date: date,
                            lookback_days: int = SPC_LOOKBACK_DAYS) -> pd.DataFrame:
    """run rule/CUSUM 판정용 시계열 — 전 실공장×타겟, 실측 있는 행 전체.

    이상 행만 뽑는 _fetch_anomaly_data 와 달리 정상(inside) 행도 포함해야
    연속/빈발/지속편향 패턴을 판정할 수 있습니다. 조회창은 배너 노출창(7일)보다
    긴 SPC_LOOKBACK_DAYS(30일) — 창 경계에 걸친 연속 run 과 CUSUM 안정성 확보.
    """
    date_from = base_date - timedelta(days=lookback_days - 1)
    query = """
        SELECT factory, pred_date, target, pred_value, actual_value,
               band_status, band_position
        FROM prediction_log
        WHERE pred_date BETWEEN %s AND %s
          AND actual_value IS NOT NULL
          AND pred_value IS NOT NULL
        ORDER BY factory, target, pred_date
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            query, conn,
            params=(date_from.strftime("%Y-%m-%d"), base_date.strftime("%Y-%m-%d")),
        )
    except Exception as exc:
        logger.exception("Failed to fetch band rule series for base_date=%s: %s", base_date, exc)
        return pd.DataFrame()
    finally:
        conn.close()
    if not df.empty:
        for c in ["pred_value", "actual_value", "band_position"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ──────────────────────────────────────
# SPC (Statistical Process Control) 차트
# ──────────────────────────────────────
# v5.2의 정상범주 [P05, P95]를 SPC의 LCL/UCL로 직접 매핑:
#   UCL (Upper Control Limit) = P95 → 상한 통제선
#   LCL (Lower Control Limit) = P05 → 하한 통제선
#   Centerline (CL)           = P50 → 중심선
#   실측값 = 실제 관측치(데이터 포인트)
# "관측치가 통제선 밖" = "정상범주 이탈" = 이상 (Out of control)
#
# 일반 SPC 차트와 다른 점:
#   - UCL/LCL이 고정상수가 아니라 매일 모델이 재산출하는 동적 값
#   - 운영 조건(생산량, 외기, 휴일 등)에 따라 매일 폭이 달라짐
#   - 따라서 라인이 아니라 매일의 점/구간으로 그림 (Plotly의 step + fill)


def _spc_target_unit(target: str) -> str:
    """SPC 차트 Y축 단위 텍스트."""
    from app.services.v5_common import TARGET_SPECS
    return TARGET_SPECS.get(target, {}).get("unit", "")


@st.cache_data(ttl=120, show_spinner=False)
def _fetch_spc_data(factory: str, target: str, base_date: date,
                    lookback_days: int = SPC_LOOKBACK_DAYS) -> pd.DataFrame:
    """SPC 차트용 시계열 데이터: prediction_log에서 (P05/P50/P95/실측/상태) 조회.

    집계 공장(전사/남양주)은 prediction_log에 자체 행이 없으므로,
    구성 실공장들의 행을 pred_date별로 합산해 동일 스키마로 반환합니다.
    합산 규칙은 usage_prediction_v5_service._aggregate_prediction_history_rows 와 동일:
      - pred_value/actual_value: 모든 멤버 값이 있을 때만 합산
      - pred_p05/pred_p95: 모든 멤버가 v5.2 밴드를 가질 때만 합산 (보수적 가법)
      - band_status/band_position: 합산 후 classify_band 로 재산출
    """
    from app.services.v5_common import AGGREGATE_FACTORY_MEMBERS, classify_band

    date_from = base_date - timedelta(days=lookback_days - 1)
    members = AGGREGATE_FACTORY_MEMBERS.get(factory)
    if members:
        placeholders = ",".join(["%s"] * len(members))
        query = f"""
            SELECT factory, pred_date, pred_value, pred_p05, pred_p95,
                   actual_value, mape
            FROM prediction_log
            WHERE factory IN ({placeholders}) AND target=%s
              AND pred_date BETWEEN %s AND %s
            ORDER BY pred_date
        """
        params = (
            *members, target,
            date_from.strftime("%Y-%m-%d"),
            base_date.strftime("%Y-%m-%d"),
        )
    else:
        query = """
            SELECT pred_date, pred_value, pred_p05, pred_p95,
                   actual_value, band_status, band_position, mape
            FROM prediction_log
            WHERE factory=%s AND target=%s
              AND pred_date BETWEEN %s AND %s
            ORDER BY pred_date
        """
        params = (
            factory, target,
            date_from.strftime("%Y-%m-%d"),
            base_date.strftime("%Y-%m-%d"),
        )

    conn = get_connection()
    try:
        df = pd.read_sql_query(query, conn, params=params)
    except Exception as exc:
        logger.exception(
            "Failed to fetch SPC data for factory=%s target=%s base_date=%s: %s",
            factory,
            target,
            base_date,
            exc,
        )
        return pd.DataFrame()
    finally:
        conn.close()
    if df.empty:
        return df
    df["pred_date"] = pd.to_datetime(df["pred_date"], errors="coerce")
    df = df.dropna(subset=["pred_date"]).sort_values("pred_date").reset_index(drop=True)
    for c in ["pred_value", "pred_p05", "pred_p95",
              "actual_value", "mape"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if members:
        expected = len(members)
        rows: list[dict] = []
        for pred_date, group in df.groupby("pred_date", sort=True):
            pred_complete = int(group["pred_value"].notna().sum()) >= expected
            pred_total = float(group["pred_value"].sum()) if pred_complete else None

            p05_total = float(group["pred_p05"].sum()) if int(group["pred_p05"].notna().sum()) >= expected else None
            p95_total = float(group["pred_p95"].sum()) if int(group["pred_p95"].notna().sum()) >= expected else None

            actual_complete = int(group["actual_value"].notna().sum()) >= expected
            actual_total = float(group["actual_value"].sum()) if actual_complete else None

            mape_val = None
            if actual_complete and pred_total is not None and abs(actual_total) > 0:
                mape_val = abs(pred_total - actual_total) / abs(actual_total) * 100.0

            band_status, band_pos = (None, None)
            if actual_total is not None and p05_total is not None and p95_total is not None and pred_total is not None:
                band_status, band_pos = classify_band(actual_total, p05_total, pred_total, p95_total)

            rows.append({
                "pred_date": pred_date,
                "pred_value": pred_total,
                "pred_p05": p05_total,
                "pred_p95": p95_total,
                "actual_value": actual_total,
                "band_status": band_status,
                "band_position": band_pos,
                "mape": mape_val,
            })
        df = pd.DataFrame(rows)
        for c in ["pred_value", "pred_p05", "pred_p95",
                  "actual_value", "band_position", "mape"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    else:
        df["band_position"] = pd.to_numeric(df["band_position"], errors="coerce")
    return df


def _render_spc_chart(factory: str, target: str, base_date: date) -> bool:
    """단일 (공장, 타겟) SPC 차트.

    Returns: True if chart was rendered, False if no data.
    """
    df = _fetch_spc_data(factory, target, base_date)
    if df.empty:
        return False

    unit = _spc_target_unit(target)

    # 지속편향(CUSUM) 감지 — 단일점 이탈로는 안 잡히는 "매일 조금씩 계속 높은/낮은"
    # drift 유형(냉방 기저부하 증가 등)을 잔차 누적합으로 포착.
    drift = detect_drift(df)

    fig = go.Figure()

    if drift is not None and df["pred_date"].notna().any():
        drift_fill = (
            "rgba(220,38,38,0.07)" if drift["direction"] == "over"
            else "rgba(245,158,11,0.07)"
        )
        fig.add_vrect(
            x0=pd.Timestamp(drift["start_date"]) - pd.Timedelta(hours=12),
            x1=df["pred_date"].max() + pd.Timedelta(hours=12),
            fillcolor=drift_fill, line_width=0, layer="below",
        )

    # 1) 정상범주 음영 (P05 ~ P95)
    has_band = df["pred_p05"].notna().any() and df["pred_p95"].notna().any()
    if has_band:
        df_band = df.dropna(subset=["pred_p05", "pred_p95"])
        fig.add_trace(go.Scatter(
            x=df_band["pred_date"], y=df_band["pred_p95"],
            mode="lines",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=df_band["pred_date"], y=df_band["pred_p05"],
            mode="lines",
            line=dict(width=0),
            fill="tonexty",
            fillcolor="rgba(37,99,235,0.10)",
            name="정상범주 (P05~P95)",
            hovertemplate="P05~P95<extra></extra>",
        ))

        # 2) UCL / LCL 점선
        fig.add_trace(go.Scatter(
            x=df_band["pred_date"], y=df_band["pred_p95"],
            mode="lines",
            line=dict(color="#dc2626", width=1.5, dash="dash"),
            name="UCL (P95, 상한)",
            hovertemplate="UCL: %{y:,.1f}<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=df_band["pred_date"], y=df_band["pred_p05"],
            mode="lines",
            line=dict(color="#f59e0b", width=1.5, dash="dash"),
            name="LCL (P05, 하한)",
            hovertemplate="LCL: %{y:,.1f}<extra></extra>",
        ))

    # 3) 중심선 (P50)
    # SPC 의미 일관성: 통제선 3개(UCL/CL/LCL)는 모두 동일한 날짜 집합(밴드 있는 날) 위에서만 그림.
    # 그래야 v5.1 legacy 행과 v5.2 행이 섞여 있을 때, CL이 UCL/LCL 인터폴레이션선을 가로지르며
    # "중심이 하한 아래로 내려간 것처럼" 보이는 시각 착시가 생기지 않음.
    # legacy 행의 P50(pred_value)은 actual 마커와 함께 별도 트레이스로 처리.
    if has_band:
        fig.add_trace(go.Scatter(
            x=df_band["pred_date"], y=df_band["pred_value"],
            mode="lines",
            line=dict(color="#6b7280", width=1, dash="dot"),
            name="CL (P50, 중심선)",
            hovertemplate="CL: %{y:,.1f}<extra></extra>",
        ))

    # 4) 실측 — 상태별 색상 마커
    df_actual = df.dropna(subset=["actual_value"]).copy()
    if not df_actual.empty:
        # 색상 결정: band_status 우선, 없으면 actual vs band로 즉석 판정.
        # 밴드 자체가 없으면 'legacy'(v5.1 시절 예측) — 별도 마커로 구분.
        def _classify_row(r):
            bs = r.get("band_status")
            if isinstance(bs, str) and bs in ("over", "under", "inside"):
                return bs
            actual = r.get("actual_value")
            p05 = r.get("pred_p05"); p95 = r.get("pred_p95")
            if pd.notna(actual) and pd.notna(p05) and pd.notna(p95):
                if actual > p95: return "over"
                if actual < p05: return "under"
                return "inside"
            # 여기 도달하면 P05/P95가 NULL → v5.1 시절 예측 행
            return "legacy"

        df_actual["_status"] = df_actual.apply(_classify_row, axis=1)
        # band_position 표시용 — 없으면 사람 친화 텍스트로
        df_actual["_pos_str"] = df_actual["band_position"].apply(
            lambda v: f"{float(v):+.2f}" if pd.notna(v) else "—"
        )
        # 상태 한국어 라벨 (툴팁용)
        STATUS_KO = {
            "inside": "✓ 정상범주",
            "over":   "↑ 과사용",
            "under":  "↓ 저사용",
            "legacy": "ⓘ v5.1 예측 (정상범주 없음)",
        }
        df_actual["_status_ko"] = df_actual["_status"].map(STATUS_KO).fillna("—")

        color_map = {
            "inside":  "#10b981",
            "over":    "#dc2626",
            "under":   "#f59e0b",
            "legacy":  "#cbd5e1",   # 연한 회색
        }
        marker_colors = [color_map.get(s, "#cbd5e1") for s in df_actual["_status"]]
        # legacy 행은 빈 원으로(테두리만) — 한눈에 "밴드 없는 옛 예측"임을 구분
        marker_line_widths = [2.0 if s == "legacy" else 1.5 for s in df_actual["_status"]]
        marker_line_colors = [
            "#94a3b8" if s == "legacy" else "white"
            for s in df_actual["_status"]
        ]
        # 빈 마커 효과: legacy는 fill을 흰색으로 → 테두리만 회색
        marker_fill = [
            "#ffffff" if s == "legacy" else c
            for s, c in zip(df_actual["_status"], marker_colors)
        ]

        # 라인은 회색으로 깔고, 마커만 상태별 색 → 점 위치/색 모두 한눈에 보이게.
        # legacy 행은 연결선에서 끊기게 처리해 "v5.1 시점이 v5.2 추세선처럼" 보이지 않게 함.
        actual_line_y = df_actual.apply(
            lambda r: float("nan") if r["_status"] == "legacy" else float(r["actual_value"]),
            axis=1,
        )
        fig.add_trace(go.Scatter(
            x=df_actual["pred_date"], y=actual_line_y,
            mode="lines",
            line=dict(color="rgba(15,23,42,0.35)", width=1.5),
            connectgaps=False,
            showlegend=False,
            hoverinfo="skip",
        ))
        # 마커는 legacy 포함 전체 행에 표시 (legacy는 빈 원)
        fig.add_trace(go.Scatter(
            x=df_actual["pred_date"], y=df_actual["actual_value"],
            mode="markers",
            marker=dict(
                size=10,
                color=marker_fill,
                line=dict(color=marker_line_colors, width=marker_line_widths),
            ),
            name="실측값",
            customdata=df_actual[["_status_ko", "_pos_str"]].values,
            hovertemplate=(
                "<b>%{x|%Y-%m-%d}</b><br>"
                "실측: %{y:,.1f}<br>"
                "상태: %{customdata[0]}<br>"
                "위치: %{customdata[1]}<extra></extra>"
            ),
        ))

        # 5) 이상 점에는 "↑" / "↓" 주석 (가독성)
        for _, r in df_actual.iterrows():
            s = r["_status"]
            if s == "over":
                fig.add_annotation(
                    x=r["pred_date"], y=r["actual_value"],
                    text="↑", showarrow=False,
                    font=dict(color="#dc2626", size=16, family="Inter, sans-serif"),
                    yshift=14,
                )
            elif s == "under":
                fig.add_annotation(
                    x=r["pred_date"], y=r["actual_value"],
                    text="↓", showarrow=False,
                    font=dict(color="#f59e0b", size=16, family="Inter, sans-serif"),
                    yshift=-14,
                )

        # 6) legacy(밴드 없는 v5.1) 행이 차트에 포함된 경우 안내 캡션
        n_legacy = int((df_actual["_status"] == "legacy").sum())
        if n_legacy > 0:
            st.caption(
                f"⚠ 이 차트에 v5.1 시절 저장된 예측 {n_legacy}건(빈 원 ○)은 "
                f"정상범주가 없어 inside/over/under 판정이 불가합니다. "
                f"AI 예측 페이지에서 같은 기간을 다시 예측하면 v5.2 밴드가 채워집니다."
            )

    fig.update_layout(
        title=dict(
            text=f"<b>{factory} · {target}</b>",
            x=0.02, xanchor="left",
            font=dict(size=14, color=_TEXT_PRIMARY),
        ),
        xaxis_title="",
        yaxis_title=unit,
        height=260,
        margin=dict(l=50, r=20, t=40, b=30),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
            font=dict(size=10),
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridcolor=_GRID_COLOR, tickformat="%m-%d")
    fig.update_yaxes(showgrid=True, gridcolor=_GRID_COLOR)
    st.plotly_chart(fig, use_container_width=True)

    if drift is not None:
        is_over = drift["direction"] == "over"
        dir_ko = "높게" if is_over else "낮게"
        icon = "📈" if is_over else "📉"
        bias_txt = (
            f" 평균 {drift['mean_bias_pct']:+.1f}%"
            if drift.get("mean_bias_pct") is not None else ""
        )
        st.caption(
            f"{icon} 음영 구간: {drift['start_date'].strftime('%m/%d')}부터 "
            f"{drift['days']}영업일째 실측이 AI 예측(중심선)보다{bias_txt} 계속 {dir_ko} "
            f"유지되고 있습니다. 하루하루는 정상범위 안이라도 이런 흐름이 이어지면 "
            f"설비 상태 변화나 기저부하 증감을 점검하세요."
        )
    return True


# 상태 그리드 셀 스펙 — 상태색은 항상 아이콘+라벨과 함께 사용 (색상 단독 의존 금지).
# 심각도 순서: 경보(빨강) > 지속(주황) > 주의(황색) > 정상(초록).
_GRID_STATES = {
    "alert":  dict(color="#ef4444", bg="rgba(239,68,68,0.13)",  border="rgba(239,68,68,0.45)"),
    "drift":  dict(color="#fb923c", bg="rgba(251,146,60,0.11)", border="rgba(251,146,60,0.40)"),
    "watch":  dict(color="#f59e0b", bg="rgba(245,158,11,0.09)", border="rgba(245,158,11,0.35)"),
    "normal": dict(color="#10b981", bg="rgba(16,185,129,0.07)", border="rgba(16,185,129,0.30)"),
    "nodata": dict(color="#647695", bg="transparent",           border="rgba(122,164,224,0.15)"),
}

_GRID_TARGET_HEADERS = [("전력", "⚡"), ("연료", "🔥"), ("용수", "💧")]


def _grid_cell_html(state: str, label: str, sub: str) -> str:
    """상태 그리드 셀 1칸 HTML."""
    spec = _GRID_STATES[state]
    return (
        f"<td style='background:{spec['bg']}; border:1px solid {spec['border']}; "
        f"border-radius:10px; padding:9px 6px; text-align:center; width:28%;'>"
        f"<div style='font-size:0.85rem; font-weight:700; color:{spec['color']}; line-height:1.3;'>{label}</div>"
        f"<div style='font-size:0.69rem; color:{_TEXT_MUTED}; margin-top:2px;'>{sub}</div>"
        f"</td>"
    )


def _render_anomaly_status_grid(base_date: date) -> None:
    """5개 실공장 × 3개 에너지원 = 15칸 상태 그리드.

    셀 판정 우선순위: 경보(반복 이탈) > 계속 높음/낮음(지속편향) > 주의(단발 이탈) > 정상.
    통계 용어(CUSUM·잔차 등)는 노출하지 않고 일상어로만 표기.
    """
    from app.services.v5_common import FACTORY_PHYSICAL_DISPLAY_ORDER

    series_df = _fetch_band_rule_series(base_date)
    rules = evaluate_band_rules(series_df, base_date, recent_days=ANOMALY_LOOKBACK_DAYS)
    flags = rules["row_flags"]
    drifts = {(s["factory"], s["target"]): s for s in rules["drift_signals"]}

    if series_df.empty:
        st.info(
            "표시할 판정 데이터가 없습니다. 예측은 매일 자동 실행되므로 "
            "잠시 후 새로고침하거나 logs/automation/auto_prediction.log 를 확인하세요."
        )
        return

    have_data = set(zip(series_df["factory"], series_df["target"]))
    recent_from = base_date - timedelta(days=ANOMALY_LOOKBACK_DAYS - 1)
    recent = series_df[pd.to_datetime(series_df["pred_date"]).dt.date >= recent_from]

    head_style = (
        f"font-size:0.78rem; color:{_TEXT_SECONDARY}; font-weight:600; "
        f"padding:2px 4px; text-align:center;"
    )
    fac_style = (
        f"font-size:0.84rem; color:{_TEXT_PRIMARY}; font-weight:600; "
        f"text-align:left; padding:2px 8px 2px 2px; white-space:nowrap; width:12%;"
    )

    rows_html: list[str] = []
    header_cells = "".join(
        f"<th style='{head_style}'>{icon} {t}</th>" for t, icon in _GRID_TARGET_HEADERS
    )
    rows_html.append(f"<tr><th></th>{header_cells}</tr>")

    for fac in FACTORY_PHYSICAL_DISPLAY_ORDER:
        cells: list[str] = []
        for tgt, _icon in _GRID_TARGET_HEADERS:
            if (fac, tgt) not in have_data:
                cells.append(_grid_cell_html("nodata", "—", "예측 없음"))
                continue

            cell_recent = recent[(recent["factory"] == fac) & (recent["target"] == tgt)]
            n_out = int(cell_recent["band_status"].isin(["over", "under"]).sum())

            cell_flags = (
                flags[(flags["factory"] == fac) & (flags["target"] == tgt)]
                if not flags.empty else flags
            )
            n_alert = (
                int((cell_flags["severity"] == SEVERITY_ALERT).sum())
                if not cell_flags.empty else 0
            )
            drift = drifts.get((fac, tgt))

            if n_alert > 0:
                cells.append(_grid_cell_html(
                    "alert", "🚨 경보", f"7일 내 이탈 {n_out}건 반복"
                ))
            elif drift is not None:
                is_over = drift["direction"] == "over"
                label = "📈 계속 높음" if is_over else "📉 계속 낮음"
                bias = drift.get("mean_bias_pct")
                sub = (
                    f"예측 대비 평균 {bias:+.1f}% · {drift['days']}일째"
                    if bias is not None else f"{drift['days']}일째 지속"
                )
                cells.append(_grid_cell_html("drift", label, sub))
            elif n_out > 0:
                cells.append(_grid_cell_html(
                    "watch", "⚠️ 주의", f"하루 이탈 {n_out}건 (우연 가능)"
                ))
            else:
                cells.append(_grid_cell_html("normal", "✓ 정상", "범위 내"))

        rows_html.append(f"<tr><th style='{fac_style}'>{fac}</th>{''.join(cells)}</tr>")

    st.markdown(
        "<table style='width:100%; border-collapse:separate; border-spacing:5px; "
        "table-layout:fixed; margin:2px 0 4px;'>" + "".join(rows_html) + "</table>",
        unsafe_allow_html=True,
    )
    st.caption(
        "🚨 경보 = 같은 방향 이탈 반복(연속 2일 또는 7일 내 3회 이상) · "
        "📈📉 계속 높음/낮음 = 실측이 AI 예측보다 한쪽으로 계속 치우침 · "
        "⚠️ 주의 = 하루 단위 이탈(우연 가능) · ✓ 정상 = 정상범위 유지 | 최근 7일 기준"
    )


def _render_spc_section(base_date: date) -> None:
    """이상감지 현황 — 15칸 상태 그리드 + 접힌 상세 차트.

    그리드 하나로 전 공장×에너지원 판정을 한눈에 보여주고,
    정상범주 관리도 차트는 expander 안으로 이동해 필요할 때만 펼쳐 봅니다.
    """
    st.markdown(
        f"<div style='font-size:0.92rem; font-weight:600; color:{_TEXT_PRIMARY}; margin:8px 0 4px;'>"
        f"📊 공장 × 에너지원 이상감지 현황"
        f"</div>",
        unsafe_allow_html=True,
    )

    _render_anomaly_status_grid(base_date)

    # 상세 추이 차트 — 기본 접힘. 그리드에서 이상 칸을 본 뒤 원인 확인용.
    with st.expander("📈 공장별 상세 추이 차트 (정상범위 vs 실측)", expanded=False):
        # 공장 선택 — 전사(집계) + 실공장 5개.
        # 집계 공장의 차트는 _fetch_spc_data 에서 실공장 행을 합산해 동일 스키마로 생성.
        SPC_FACTORY_OPTIONS = [f for f in FACTORY_OPTIONS if f != "남양주"]
        sel_col1, sel_col2 = st.columns([0.55, 0.45])
        with sel_col1:
            sel_factory = st.selectbox(
                "공장",
                options=SPC_FACTORY_OPTIONS,
                index=0,
                key="spc_factory_select",
                label_visibility="collapsed",
            )
        with sel_col2:
            st.markdown(
                "<div style='font-size:0.75rem; color:#94a3b8; padding-top:6px;'>"
                "● <span style='color:#10b981;'>정상</span>  "
                "● <span style='color:#dc2626;'>↑과사용</span>  "
                "● <span style='color:#f59e0b;'>↓저사용</span>  "
                "○ <span style='color:#94a3b8;'>v5.1 (밴드없음)</span>"
                "</div>",
                unsafe_allow_html=True,
            )

        targets = ["전력", "연료", "용수"]
        any_chart = False
        for tgt in targets:
            rendered = _render_spc_chart(sel_factory, tgt, base_date)
            if rendered:
                any_chart = True
            else:
                st.caption(f"_{sel_factory} · {tgt}: 표시할 예측 이력이 없습니다._")

        if not any_chart:
            st.info(
                "표시할 차트 데이터가 없습니다. 예측은 매일 자동 실행되므로 "
                "잠시 후 새로고침해 보세요."
            )


def _render_anomaly_alert(base_date: date):
    """AI 예측 이상 알림 배너 — 2단계 판정(경보/주의) + 지속편향(CUSUM).

    P05~P95 는 90% 구간이라 완벽한 모델도 단일일 판정의 ~10%는 밴드를 벗어납니다.
    그래서 run rule 을 통과한 이탈(연속·빈발)과 CUSUM 지속편향만 '경보'로 올리고,
    단일일 이탈은 '주의'로 낮춰 알람 피로를 방지합니다 (anomaly_rules_service 참고).
    """
    anomaly_df = _fetch_anomaly_data(base_date)
    series_df = _fetch_band_rule_series(base_date)
    rules = evaluate_band_rules(series_df, base_date, recent_days=ANOMALY_LOOKBACK_DAYS)
    drift_signals = rules["drift_signals"]

    # run rule 판정(severity/rules)을 이상 행에 병합 — 키: (공장, 타겟, 날짜)
    if not anomaly_df.empty:
        anomaly_df = anomaly_df.copy()
        anomaly_df["_date_key"] = pd.to_datetime(anomaly_df["pred_date"]).dt.strftime("%Y-%m-%d")
        row_flags = rules["row_flags"]
        if not row_flags.empty:
            rf = row_flags.copy()
            rf["_date_key"] = pd.to_datetime(rf["pred_date"]).dt.strftime("%Y-%m-%d")
            anomaly_df = anomaly_df.merge(
                rf[["factory", "target", "_date_key", "severity", "rules"]],
                on=["factory", "target", "_date_key"],
                how="left",
            )
        if "severity" not in anomaly_df.columns:
            anomaly_df["severity"] = None
        if "rules" not in anomaly_df.columns:
            anomaly_df["rules"] = None
        # 밴드 없는 v5.1 폴백 행 등 규칙 판정 불가 행은 '주의'로 분류
        anomaly_df["severity"] = anomaly_df["severity"].fillna(SEVERITY_WATCH)
        anomaly_df["rules"] = anomaly_df["rules"].fillna("")
        # 경보 먼저, 그 안에서는 기존 정렬(|위치| 내림차순) 유지
        anomaly_df["_sev_rank"] = (anomaly_df["severity"] != SEVERITY_ALERT).astype(int)
        anomaly_df = (
            anomaly_df.sort_values("_sev_rank", kind="stable")
            .drop(columns=["_sev_rank", "_date_key"])
            .reset_index(drop=True)
        )

    n_alert = int((anomaly_df["severity"] == SEVERITY_ALERT).sum()) if not anomaly_df.empty else 0
    n_watch = int(len(anomaly_df)) - n_alert if not anomaly_df.empty else 0
    n_drift = len(drift_signals)

    if n_alert == 0 and n_drift == 0 and n_watch == 0:
        # 정상 상태 배너
        st.markdown(f"""
        <div style="
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.10) 0%, rgba(16, 185, 129, 0.05) 100%);
            border: 1px solid rgba(16, 185, 129, 0.35);
            border-radius: 10px;
            padding: 12px 20px;
            margin-bottom: 14px;
            display: flex;
            align-items: center;
            gap: 12px;
        ">
            <div style="
                width: 36px; height: 36px;
                background: rgba(16, 185, 129, 0.15);
                border-radius: 50%;
                display: flex; align-items: center; justify-content: center;
                font-size: 1.1rem;
            ">✅</div>
            <div>
                <div style="color: #10b981; font-weight: 700; font-size: 0.92rem;">AI 이상감지: 정상</div>
                <div style="color: {_TEXT_SECONDARY}; font-size: 0.78rem;">
                    최근 {ANOMALY_LOOKBACK_DAYS}일간 실측값이 모델 정상범주(P05~P95) 안에 머물렀고,
                    지속편향(CUSUM) 신호도 없습니다.
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        return

    if n_alert > 0 or n_drift > 0:
        # ── 경보 배너 (빨강): run rule 확인 이탈 또는 지속편향 ──
        title_parts = []
        if n_alert > 0:
            title_parts.append(f"반복 이탈 {n_alert}건")
        if n_drift > 0:
            title_parts.append(f"계속 높음/낮음 {n_drift}건")
        title_text = " · ".join(title_parts)
        watch_suffix = (
            f" <span style='color:#f59e0b;font-weight:600;'>(그 외 주의 {n_watch}건)</span>"
            if n_watch > 0 else ""
        )
        st.markdown(f"""
        <div style="
            background: linear-gradient(135deg, rgba(239, 68, 68, 0.12) 0%, rgba(239, 68, 68, 0.05) 100%);
            border: 1px solid rgba(239, 68, 68, 0.45);
            border-radius: 10px;
            padding: 12px 20px;
            margin-bottom: 14px;
            display: flex;
            align-items: center;
            gap: 12px;
        ">
            <div style="
                width: 36px; height: 36px;
                background: rgba(239, 68, 68, 0.18);
                border-radius: 50%;
                display: flex; align-items: center; justify-content: center;
                font-size: 1.2rem;
                animation: pulse 2s infinite;
            ">🚨</div>
            <div style="flex: 1;">
                <div style="color: #ef4444; font-weight: 700; font-size: 0.92rem;">
                    AI 이상감지: 경보 — {title_text}{watch_suffix}
                </div>
                <div style="color: {_TEXT_SECONDARY}; font-size: 0.78rem;">
                    우연으로 보기 어려운 패턴입니다 — 같은 방향 이탈이
                    연속 {CONSECUTIVE_RUN_MIN}영업일 이상 / {ANOMALY_LOOKBACK_DAYS}일 내
                    {FREQUENT_COUNT_MIN}회 이상 반복되거나, 실측이 AI 예측보다 계속 높거나 낮게 유지되고 있습니다.
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        # ── 주의 배너 (주황): 단일일 이탈만 — 통계적 우연 가능 ──
        st.markdown(f"""
        <div style="
            background: linear-gradient(135deg, rgba(245, 158, 11, 0.10) 0%, rgba(245, 158, 11, 0.04) 100%);
            border: 1px solid rgba(245, 158, 11, 0.40);
            border-radius: 10px;
            padding: 12px 20px;
            margin-bottom: 14px;
            display: flex;
            align-items: center;
            gap: 12px;
        ">
            <div style="
                width: 36px; height: 36px;
                background: rgba(245, 158, 11, 0.15);
                border-radius: 50%;
                display: flex; align-items: center; justify-content: center;
                font-size: 1.1rem;
            ">⚠️</div>
            <div style="flex: 1;">
                <div style="color: #f59e0b; font-weight: 700; font-size: 0.92rem;">
                    AI 이상감지: 주의 {n_watch}건 (경보 없음)
                </div>
                <div style="color: {_TEXT_SECONDARY}; font-size: 0.78rem;">
                    하루 단위 이탈입니다. 정상범위는 통계 구간이라 하루쯤은 우연히 벗어날 수
                    있으며, 같은 방향 이탈이 반복되면 경보로 올라갑니다.
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    # 상세 항목을 expander로 표시 — 지속편향 신호 + 행별 [🔍 AI 진단] 버튼
    expander_label = f"🔍 이상 감지 상세 (경보 {n_alert} · 주의 {n_watch} · 계속 높음/낮음 {n_drift})"
    with st.expander(expander_label, expanded=(n_alert + n_drift > 0)):
        if drift_signals:
            for sig in drift_signals:
                is_over = sig["direction"] == "over"
                dir_ko = "계속 높음 📈" if is_over else "계속 낮음 📉"
                sig_color = "#ef4444" if is_over else "#f59e0b"
                bias_txt = (
                    f" · AI 예측 대비 평균 {sig['mean_bias_pct']:+.1f}%"
                    if sig.get("mean_bias_pct") is not None else ""
                )
                st.markdown(
                    f"<div style='font-size:0.85rem; padding:4px 0; color:{_TEXT_PRIMARY};'>"
                    f"<b>{sig['factory']} · {sig['target']}</b> "
                    f"<span style='color:{sig_color}; font-weight:700;'>{dir_ko}</span>"
                    f" — {sig['start_date'].strftime('%m/%d')}부터 {sig['days']}영업일째"
                    f"{bias_txt}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.markdown(
                f"<div style='height:1px; background:{_BORDER_COLOR}; margin:6px 0 10px;'></div>",
                unsafe_allow_html=True,
            )
        if not anomaly_df.empty:
            _render_anomaly_detail_table(anomaly_df)
        st.caption(
            f"※ 경보 = 같은 방향 이탈이 연속 {CONSECUTIVE_RUN_MIN}영업일 이상 또는 "
            f"{ANOMALY_LOOKBACK_DAYS}일 내 {FREQUENT_COUNT_MIN}회 이상 반복 · "
            "계속 높음/낮음 = 실측이 AI 예측보다 한쪽으로 계속 치우침 | "
            "주의 = 하루 단위 정상범위 이탈(우연 가능) | "
            f"조회 기간: 최근 {ANOMALY_LOOKBACK_DAYS}일 | "
            "위치 = 정상범위 중심에서 벗어난 정도 (±1 = 범위 가장자리)"
        )


# ──────────────────────────────────────
# AI 이상 원인 진단 (LLM)
# ──────────────────────────────────────

def _diag_session_key(factory: str, pred_date_str: str, target: str) -> str:
    """진단 결과 노출 여부를 추적하는 session_state 키."""
    return f"show_anomaly_diag::{factory}::{pred_date_str}::{target}"


def _render_anomaly_detail_table(anomaly_df: pd.DataFrame) -> None:
    """이상감지 상세 — 헤더 + 행별 컴포넌트(분석 버튼 포함).

    v5.2 컬럼: 날짜 / 공장 / 항목 / 정상범위 [P05~P95] / 실측 / 상태(↑/↓) / 위치 / 판정 / 진단
    판정: run rule 통과 여부 — 경보(연속·빈발 이탈) vs 주의(단일일 이탈).
    v5.1 폴백: 정상범위는 '—'로 표시.
    """
    # ── 헤더 행 ──
    col_widths = [0.6, 0.7, 0.6, 1.3, 1.0, 0.8, 0.6, 1.2, 0.8]
    header_cols = st.columns(col_widths)
    header_style = (
        f"color:{_TEXT_SECONDARY}; font-size:0.78rem; font-weight:600; "
        f"letter-spacing:0.02em; padding:6px 0; text-align:center; "
        f"border-bottom:1px solid {_BORDER_COLOR};"
    )
    for i, label in enumerate(
        ["날짜", "공장", "항목", "정상범위 [P05~P95]", "실측값", "상태", "위치", "판정", "AI 진단"]
    ):
        with header_cols[i]:
            st.markdown(
                f"<div style='{header_style}'>{label}</div>",
                unsafe_allow_html=True,
            )

    # ── 데이터 행 ──
    for i, (_, row) in enumerate(anomaly_df.iterrows()):
        factory = str(row["factory"])
        pred_date_str = pd.to_datetime(row["pred_date"]).strftime("%Y-%m-%d")
        pred_d_short = pd.to_datetime(row["pred_date"]).strftime("%m/%d")
        target = str(row["target"])
        actual_v = float(row["actual_value"])

        # v5.2 컬럼 (없으면 None)
        p05_raw = row.get("pred_p05") if "pred_p05" in anomaly_df.columns else None
        p95_raw = row.get("pred_p95") if "pred_p95" in anomaly_df.columns else None
        p05 = None if (p05_raw is None or pd.isna(p05_raw)) else float(p05_raw)
        p95 = None if (p95_raw is None or pd.isna(p95_raw)) else float(p95_raw)
        bs = row.get("band_status") if "band_status" in anomaly_df.columns else None
        if isinstance(bs, float) and pd.isna(bs):
            bs = None
        bp = row.get("band_position") if "band_position" in anomaly_df.columns else None
        bp_val = None if (bp is None or (isinstance(bp, float) and pd.isna(bp))) else float(bp)

        # 상태 라벨/색
        if bs == "over":
            status_color = "#dc2626"; status_text = "↑ 과사용"
        elif bs == "under":
            status_color = "#f59e0b"; status_text = "↓ 저사용"
        else:
            status_color = "#ea580c"; status_text = "이상(legacy)"

        # 위치 색상 (심각도)
        if bp_val is None:
            pos_color = _TEXT_SECONDARY; pos_text = "—"
        else:
            mag = abs(bp_val)
            if mag >= 2.0:
                pos_color = "#dc2626"
            elif mag >= 1.0:
                pos_color = "#ea580c"
            else:
                pos_color = "#ca8a04"
            pos_text = f"{bp_val:+.2f}"

        # 정상범위 텍스트
        if p05 is not None and p95 is not None:
            band_text = f"{p05:,.0f} ~ {p95:,.0f}"
        else:
            band_text = "—"

        # 판정 (경보/주의) — _render_anomaly_alert 에서 병합된 severity/rules
        severity = row.get("severity") if "severity" in anomaly_df.columns else None
        rules_text = str(row.get("rules") or "") if "rules" in anomaly_df.columns else ""
        if severity == SEVERITY_ALERT:
            sev_html = (
                "<span style='color:#ef4444; font-weight:700;'>🚨 경보</span>"
                + (
                    f"<br><span style='color:{_TEXT_MUTED}; font-size:0.72rem;'>{rules_text}</span>"
                    if rules_text else ""
                )
            )
        else:
            sev_html = "<span style='color:#f59e0b; font-weight:600;'>⚠ 주의</span>"

        cell_style = (
            f"font-size:0.85rem; padding:8px 0; text-align:center; "
            f"color:{_TEXT_PRIMARY}; border-bottom:1px solid {_BORDER_COLOR};"
        )
        num_style = cell_style + " font-variant-numeric:tabular-nums;"

        cols = st.columns(col_widths)
        with cols[0]:
            st.markdown(f"<div style='{cell_style}'>{pred_d_short}</div>", unsafe_allow_html=True)
        with cols[1]:
            st.markdown(f"<div style='{cell_style}'>{factory}</div>", unsafe_allow_html=True)
        with cols[2]:
            st.markdown(f"<div style='{cell_style}'>{target}</div>", unsafe_allow_html=True)
        with cols[3]:
            st.markdown(f"<div style='{num_style}'>{band_text}</div>", unsafe_allow_html=True)
        with cols[4]:
            st.markdown(f"<div style='{num_style}'>{actual_v:,.0f}</div>", unsafe_allow_html=True)
        with cols[5]:
            st.markdown(
                f"<div style='{cell_style}'><span style='color:{status_color}; font-weight:700;'>{status_text}</span></div>",
                unsafe_allow_html=True,
            )
        with cols[6]:
            st.markdown(
                f"<div style='{num_style}'><span style='color:{pos_color}; font-weight:700;'>{pos_text}</span></div>",
                unsafe_allow_html=True,
            )
        with cols[7]:
            st.markdown(f"<div style='{cell_style}'>{sev_html}</div>", unsafe_allow_html=True)
        sess_key = _diag_session_key(factory, pred_date_str, target)
        with cols[8]:
            # 캐시 존재 시에는 다른 라벨 노출 — 무료(캐시) vs LLM 호출 명시
            cached = get_cached_diagnosis(factory, pred_date_str, target)
            btn_label = "📂 보기" if cached else "🔍 분석"
            btn_help = (
                "이미 진단된 결과를 캐시에서 로드합니다."
                if cached
                else "LLM(OpenAI)을 호출해 이상 원인 가설과 점검 우선순위를 진단합니다. 10~30초 소요."
            )
            if st.button(
                btn_label,
                key=f"btn_diag_{factory}_{pred_date_str}_{target}_{i}",
                use_container_width=True,
                help=btn_help,
            ):
                st.session_state[sess_key] = True

        # ── 클릭(또는 이전에 열어둔)된 행은 바로 아래에 진단 카드 인라인 렌더 ──
        if st.session_state.get(sess_key):
            _render_diagnosis_card(factory, pred_date_str, target, sess_key)


def _render_diagnosis_card(
    factory: str, pred_date_str: str, target: str, sess_key: str
) -> None:
    """단일 이상 이벤트의 LLM 진단 카드 — 캐시 hit 또는 새로 호출."""
    with st.container(border=True):
        title_col, refresh_col, close_col = st.columns([8, 1, 1])
        with title_col:
            st.markdown(
                f"<div style='font-weight:700; font-size:0.95rem; color:{_TEXT_PRIMARY};'>"
                f"🤖 AI 이상 원인 진단 — <span style='color:#2563eb'>{factory}</span> · "
                f"{pred_date_str} · <span style='color:#2563eb'>{target}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with refresh_col:
            do_refresh = False
            if is_admin():
                if st.button(
                    "↻ 재생성",
                    key=f"refresh_diag_{factory}_{pred_date_str}_{target}",
                    help="캐시 무시하고 LLM에 다시 진단 요청 (관리자 전용, 비용 발생)",
                    use_container_width=True,
                ):
                    do_refresh = True
                    delete_cached_diagnosis(factory, pred_date_str, target)
        with close_col:
            if st.button(
                "✕ 닫기",
                key=f"close_diag_{factory}_{pred_date_str}_{target}",
                use_container_width=True,
            ):
                st.session_state[sess_key] = False
                st.rerun()

        # 진단 호출 (캐시 우선)
        cached_first = get_cached_diagnosis(factory, pred_date_str, target) if not do_refresh else None
        if cached_first:
            result = {
                "diagnosis": cached_first["diagnosis"],
                "from_cache": True,
                "model_used": cached_first.get("model_used"),
                "created_at": cached_first.get("created_at"),
                "updated_at": cached_first.get("updated_at"),
                "error": None,
            }
        else:
            with st.spinner("LLM이 컨텍스트를 분석하는 중... (10~30초 소요)"):
                result = get_or_create_diagnosis(
                    factory, pred_date_str, target, force_refresh=do_refresh
                )

        if result.get("error"):
            st.error(f"진단 실패: {result['error']}")
            return

        # 메타 캡션
        meta_bits = []
        if result.get("from_cache"):
            ts = result.get("updated_at") or result.get("created_at")
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
            meta_bits.append(f"캐시: {ts_str}")
        else:
            meta_bits.append("새로 생성")
        if result.get("model_used"):
            meta_bits.append(f"모델: {result['model_used']}")
        st.caption(" · ".join(meta_bits))

        # 진단 본문 (마크다운 + 인라인 HTML 허용)
        st.markdown(result["diagnosis"], unsafe_allow_html=True)

        # ── 모델 변수 영향도 (실무자가 "어떤 변수 때문인지" 즉시 이해할 수 있도록) ──
        _render_feature_importance_panel(factory, target)

        # ── 이벤트 메모 섹션 (실무자 원인·조치 기록) ──
        _render_event_memo_section(factory, pred_date_str, target)


def _render_feature_importance_panel(factory: str, target: str) -> None:
    """v5 앙상블 모델의 가중평균 feature importance 를 Top 5 로 요약 표시.

    실무자가 데이터 비전공자라도 "어떤 변수가 예측에 가장 큰 영향을 줬는지"
    한국어 라벨 + 막대 그래프 + 한 줄 자연어 요약으로 이해할 수 있도록 구성.
    """
    try:
        from app.services.v5_explainability import (
            explain_top_features_korean,
            get_v5_feature_importance,
        )
    except Exception:
        return

    # prediction_log.target 코드를 v5 모델 키로 변환 (대부분 동일)
    items = get_v5_feature_importance(factory=factory, target=target, top_n=5)
    if not items:
        return  # 모델 없거나 추출 실패 시 패널 숨김

    with st.expander("📊 모델 변수 영향도 — 이 예측은 어떤 변수에 가장 크게 좌우되는가?", expanded=False):
        # 한 줄 자연어 요약 (실무자 친화)
        st.markdown(
            f"<div style='color:{_TEXT_PRIMARY};font-size:0.9rem;line-height:1.55;"
            f"padding:8px 10px;background:{_BG_CARD2};border-left:3px solid #6366f1;"
            f"border-radius:6px;margin-bottom:10px;'>"
            f"💬 {explain_top_features_korean(items)}"
            f"</div>",
            unsafe_allow_html=True,
        )

        # 가로 막대 차트로 시각화
        labels = [it["label"] for it in items][::-1]      # plotly 위→아래 표시 위해 역순
        values = [it["importance"] * 100 for it in items][::-1]
        fig = go.Figure(go.Bar(
            x=values, y=labels, orientation="h",
            marker_color="#6366f1",
            text=[f"{v:.1f}%" for v in values],
            textposition="outside",
            textfont=dict(size=14, color=_TEXT_PRIMARY),
            hovertemplate="%{y}: %{x:.1f}%<extra></extra>",
        ))
        fig.update_layout(
            **DARK_CHART,
            height=max(180, 32 * len(labels) + 60),
            margin=dict(l=10, r=60, t=10, b=10),
            xaxis=dict(
                title="", ticksuffix="%",
                range=[0, max(values) * 1.25 if values else 100],
                tickfont=dict(color=_TEXT_PRIMARY, size=13),
                gridcolor=_GRID_COLOR,
            ),
            yaxis=dict(
                tickfont=dict(color=_TEXT_PRIMARY, size=14),
                automargin=True,
            ),
        )
        st.plotly_chart(fig, use_container_width=True,
                        key=f"feat_imp_{factory}_{target}")
        st.caption(
            "※ 모델이 학습 과정에서 변수를 얼마나 자주·강하게 사용했는지의 거시적 지표입니다. "
            "단일 일자 예측치에 대한 인스턴스 단위 기여도(SHAP 등)는 아닙니다."
        )


def _render_event_memo_section(factory: str, pred_date_str: str, target: str) -> None:
    """이상감지 카드 하단에 이벤트 메모 입력/조회 영역을 렌더링."""
    from app.services.event_annotation_service import (
        EVENT_TAGS,
        PRED_TARGET_TO_EVENT,
        TARGET_LABELS,
        add_event,
        delete_event,
        list_events_for_chart,
    )

    event_target = PRED_TARGET_TO_EVENT.get(target, "overall")

    st.markdown(
        f"<div style='margin-top:12px;padding-top:12px;border-top:1px solid {_BORDER_COLOR};'>"
        f"<b style='color:{_TEXT_PRIMARY};font-size:0.9rem;'>📝 이벤트 메모</b>"
        f"<span style='font-size:0.78rem;color:{_TEXT_MUTED};margin-left:8px;'>"
        f"이 항목과 같은 일자·공장의 원인/조치 기록을 남기면 추후 \"왜 그랬어?\" 질문에 즉시 답할 수 있습니다.</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # 같은 (factory, date) 의 기존 메모 노출
    existing = list_events_for_chart(factory, pred_date_str, pred_date_str, [event_target])
    if not existing.empty:
        for _, row in existing.iterrows():
            badge_color = {
                "critical": "#ef4444", "warn": "#f59e0b", "info": "#3b82f6",
            }.get(str(row.get("severity", "info")), "#3b82f6")
            tgt_label = TARGET_LABELS.get(str(row["target"]), str(row["target"]))
            row_id = int(row["id"])
            mc1, mc2 = st.columns([10, 1])
            with mc1:
                st.markdown(
                    f"<div style='background:{_BG_CARD2};border:1px solid {_BORDER_COLOR};"
                    f"border-radius:8px;padding:8px 12px;margin:6px 0;'>"
                    f"<span style='display:inline-block;padding:1px 8px;border-radius:10px;"
                    f"background:{badge_color}22;color:{badge_color};font-size:0.72rem;font-weight:600;margin-right:8px;'>"
                    f"{row['tag']} · {tgt_label}</span>"
                    f"<span style='color:{_TEXT_PRIMARY};font-size:0.88rem;'>{row['note']}</span>"
                    f"<div style='color:{_TEXT_MUTED};font-size:0.72rem;margin-top:3px;'>"
                    f"by {row.get('created_by') or '-'}"
                    f"</div></div>",
                    unsafe_allow_html=True,
                )
            with mc2:
                if st.button("🗑", key=f"del_evt_{row_id}",
                             help="이 메모 삭제", use_container_width=True):
                    try:
                        delete_event(row_id)
                        st.success("삭제됨")
                        st.rerun()
                    except Exception as e:
                        st.error(f"삭제 실패: {e}")

    # 새 메모 추가 폼
    with st.form(key=f"evt_form_{factory}_{pred_date_str}_{target}", clear_on_submit=True):
        f_c1, f_c2 = st.columns([1, 1])
        with f_c1:
            tag = st.selectbox(
                "태그", EVENT_TAGS, index=EVENT_TAGS.index("센서고장") if "센서고장" in EVENT_TAGS else 0,
                key=f"evt_tag_{factory}_{pred_date_str}_{target}",
                help="원인 분류 — 추후 검색·통계 시 활용됩니다.",
            )
        with f_c2:
            severity = st.selectbox(
                "중요도", ["info", "warn", "critical"], index=1,
                key=f"evt_sev_{factory}_{pred_date_str}_{target}",
                help="info=정보, warn=주의, critical=치명적",
            )
        note = st.text_area(
            "내용", placeholder="예) 냉동기 인버터 고장으로 24시간 임시운전 → 13:00 부품 교체 후 정상화",
            key=f"evt_note_{factory}_{pred_date_str}_{target}",
            height=80,
        )
        submitted = st.form_submit_button("💾 메모 저장", type="primary", use_container_width=False)
        if submitted:
            if not note or not note.strip():
                st.warning("내용을 입력해 주세요.")
            else:
                try:
                    add_event(
                        factory=factory,
                        event_date=pred_date_str,
                        note=note,
                        target=event_target,
                        tag=tag,
                        severity=severity,
                    )
                    st.success("메모가 저장되었습니다.")
                    st.rerun()
                except Exception as e:
                    st.error(f"저장 실패: {e}")


# ──────────────────────────────────────
# 진입점
# ──────────────────────────────────────

def render_anomaly_monitor(base_date: date) -> None:
    """이상감지 현황 통합 렌더.

    구성: ① 알림 배너(경보/주의/정상) + 상세 테이블(LLM 진단·이벤트 메모 포함)
          ② 공장 × 에너지원 상태 그리드 + 상세 관리도(expander)
    """
    _render_anomaly_alert(base_date)
    _render_spc_section(base_date)
