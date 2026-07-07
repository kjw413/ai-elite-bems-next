"""
Dashboard Main Page
===================
에너지 대시보드:
  0. AI 예측 기반 이상 알림 배너 (prediction_log 비교)
  1. 7일간 생산량·원단위 추이 차트 (사업장/원단위 선택)
  2. 유틸리티 원단위 현황 (vs 전년 비교)
  3. 생산량 및 사용량 현황 (vs 전년 비교)
"""
# 이 파일은 메인 대시보드 요약 화면을 보여줍니다.

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, date, timedelta
import sys
import calendar
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger(__name__)

from app.domain.factories import (
    DASHBOARD_FACTORY_COLORS,
    DASHBOARD_FACTORY_ORDER,
    FACTORY_DISPLAY_ORDER,
    FACTORY_QUERY_ORDER,
)
from app.services.query_service import (
    get_record_count,
    get_date_range,
    get_7day_trend,
    get_unit_rate_comparison,
    get_production_usage_comparison,
    get_monthly_yoy_summary,
)
from app.database.db_connection import get_connection, is_admin
from app.services.anomaly_diagnosis_service import (
    get_or_create_diagnosis,
    get_cached_diagnosis,
    delete_cached_diagnosis,
)
from app.utils.page_state import persist

# ──────────────────────────────────────
# AI 이상 알림 설정값
# ──────────────────────────────────────
# v5.2 도입: 이상 판정 = "실측이 정상범주(P05~P95) 밖" (정성적)
# v5.1 호환: 밴드가 없는 행은 MAPE 임계로 폴백 (점진적 마이그레이션)
ANOMALY_LOOKBACK_DAYS = 7      # 이상 감지 상세 테이블 조회 기간 (기준일 포함 최근 N일)
SPC_LOOKBACK_DAYS = 30         # SPC(통계적 공정 관리) 차트 조회 기간 — 패턴 인지 위해 더 길게
LEGACY_MAPE_THRESHOLD_PCT = 20.0   # v5.1 행 폴백 임계 (밴드 없으면 이 기준 적용)
BAND_POSITION_MIN = 0.0            # |band_position| ≥ 이 값 이상만 표시 (0이면 모든 over/under 표시)

# ──────────────────────────────────────
# 공통 헬퍼
# ──────────────────────────────────────

def _theme_colors():
    """다크 모드 전용 색상 팔레트 (main.py DARK_VARS와 동기화)."""
    return dict(
        FONT_COLOR     = "#e9f0fb",
        TEXT_PRIMARY   = "#e9f0fb",
        TEXT_SECONDARY = "#9db1cf",
        TEXT_MUTED     = "#647695",
        ACCENT_COLOR   = "#38bdf8",
        GRID_COLOR     = "rgba(120,160,220,0.14)",
        ZERO_COLOR     = "#2a3a58",
        BG_CARD        = "#101a30",
        BG_CARD2       = "#16223c",
        BORDER_COLOR   = "rgba(120,160,220,0.14)",
    )

_T = _theme_colors()
_FONT_COLOR     = _T["FONT_COLOR"]
_TEXT_PRIMARY   = _T["TEXT_PRIMARY"]
_TEXT_SECONDARY = _T["TEXT_SECONDARY"]
_TEXT_MUTED     = _T["TEXT_MUTED"]
_ACCENT_COLOR   = _T["ACCENT_COLOR"]
_GRID_COLOR     = _T["GRID_COLOR"]
_BG_CARD        = _T["BG_CARD"]
_BG_CARD2       = _T["BG_CARD2"]
_BORDER_COLOR   = _T["BORDER_COLOR"]

# Plotly 차트 공통 레이아웃 (라이트 모드 — 투명 배경, 본문과 자연스럽게 연결)
DARK_CHART = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=_FONT_COLOR, family="Inter, Segoe UI, sans-serif", size=16),
)




# ratio html 관련 처리를 담당합니다.
def _ratio_html(val, invert: bool = False) -> str:
    """비율(비)을 퍼센트 문자열 + 색상 HTML로 반환.
    invert=True  : 높을수록 좋음 (생산량) → ≥100% 파란, <100% 빨강
    invert=False : 낙을수록 좋음 (원단위/사용량) → ≤100% 파란, >100% 빨강
    """
    if val is None:
        return f'<span style="color:{_TEXT_SECONDARY}">-</span>'
    pct = val * 100
    if invert:
        color = "#3b82f6" if pct >= 100 else "#ef4444"
    else:
        color = "#3b82f6" if pct <= 100 else "#ef4444"
    return f'<span style="color:{color};font-weight:600">{pct:.1f}%</span>'


# progress rate 관련 처리를 담당합니다.
def _progress_rate(base_date: date) -> float:
    """해당 월의 진척률(%) 계산."""
    total_days = calendar.monthrange(base_date.year, base_date.month)[1]
    return (base_date.day / total_days) * 100


# ──────────────────────────────────────
# 섹션 1: 7일간 추이 차트
# ──────────────────────────────────────

UNIT_OPTIONS = {
    "전력 원단위 [kWh/mix-ton]": "power_per_ton",
    "연료 원단위 [Nm³/mix-ton]": "fuel_per_ton",
    "용수 원단위 [ton/mix-ton]": "water_per_ton",
    "폐수/용수": "wastewater_ratio",
}

UNIT_COLORS = {
    "power_per_ton":    "#F6C90E",   # 노랑 (yellow) - 전력/번개 심볼
    "fuel_per_ton":     "#E8450A",   # 주황빨강 (red-orange) - 화염 심볼
    "water_per_ton":    "#0EA5E9",   # 하늘파랑 (sky-blue) - 물 심볼
    "wastewater_ratio": "#6B7280",   # 검회색 (dark-gray) - 폐수 심볼
}

# 7일 추이 차트 4분면 각각의 헤더용 — (아이콘, 전체 제목)
# 차트 위 좌측에 작은 라벨로 표시되어 어느 지표의 차트인지 즉시 인지 가능.
UNIT_HEADERS = {
    "power_per_ton":    ("⚡", "전력 원단위"),
    "fuel_per_ton":     ("🔥", "연료 원단위"),
    "water_per_ton":    ("💧", "용수 원단위"),
    "wastewater_ratio": ("🚿", "폐수/용수"),
}

# 추이 지표 컬럼 → (사용량 컬럼, 사용량 단위, 지표 단위). 비는 사용량 컬럼 없음.
UNIT_USAGE_INFO = {
    "power_per_ton":    ("total_power_kwh", "전력 사용량 (kWh)",  "전력 원단위 (kWh/ton)"),
    "fuel_per_ton":     ("fuel_nm3",        "연료 사용량 (Nm³)",  "연료 원단위 (Nm³/ton)"),
    "water_per_ton":    ("water_ton",       "용수 사용량 (ton)",  "용수 원단위 (ton/ton)"),
    "wastewater_ratio": (None,              None,                "폐수/용수"),
}

PROD_COLOR = "#A4D65E"   # 연녹색 — 생산량 상징색 (메일 리포트와 동기)

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

    fig = go.Figure()

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
        template="plotly_white",
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
    return True


def _render_spc_section(base_date: date) -> None:
    """대시보드 홈의 SPC(통계적 공정 관리) 섹션.

    UI:
      - 공장 선택 selectbox (전체 5개 실공장)
      - 타겟별(전력/연료/용수) 차트를 세로 3행으로 한 번에 노출
      - 각 차트는 정상범주 음영 + UCL/LCL 점선 + 중심선 + 실측 마커
    """
    st.markdown(
        f"<div style='font-size:0.92rem; font-weight:600; color:{_TEXT_PRIMARY}; margin:8px 0 4px;'>"
        f"📈 SPC 관리도 — 정상범주 vs 실측 추이"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"통계적 공정 관리(SPC) 방식: 정상범주(P05~P95)를 UCL/LCL로 보고, "
        f"실측이 통제선 밖이면 ↑(과사용) / ↓(저사용)으로 자동 표시합니다. "
        f"조회 기간: 최근 {SPC_LOOKBACK_DAYS}일."
    )

    # 공장 선택 — 전사(집계) + 실공장 5개.
    # 집계 공장의 SPC는 _fetch_spc_data 에서 실공장 행을 합산해 동일 스키마로 생성.
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
            "표시할 SPC 데이터가 없습니다. AI 예측 페이지에서 배치 예측을 먼저 실행하면 "
            "여기에 정상범주와 실측 추이가 시각화됩니다."
        )


def _render_anomaly_alert(base_date: date):
    """AI 예측 이상 알림 배너를 대시보드 상단에 렌더링합니다.

    v5.2 정성적 이상: 실측이 정상범주(P05~P95) 밖이면 이상으로 분류.
    'over'(과사용 ↑) / 'under'(저사용 ↓) 를 구분해 노출.
    """
    anomaly_df = _fetch_anomaly_data(base_date)
    n_anomalies = len(anomaly_df)

    # over/under 카운트 — v5.2 행만 정확. 폴백된 v5.1 행은 'over'로 본다(SQL이 actual>pred만 통과시킴).
    if not anomaly_df.empty and "band_status" in anomaly_df.columns:
        bs_series = anomaly_df["band_status"].fillna("over_legacy")
        n_over = int((bs_series == "over").sum() + (bs_series == "over_legacy").sum())
        n_under = int((bs_series == "under").sum())
    else:
        n_over = n_anomalies
        n_under = 0

    if n_anomalies == 0:
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
                    최근 {ANOMALY_LOOKBACK_DAYS}일간 실측값이 모델 정상범주(P05~P95) 안에 머물렀습니다.
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        # 이상 감지 경고 배너 — over/under 구분
        breakdown_parts = []
        if n_over > 0:
            breakdown_parts.append(f"<span style='color:#ef4444;font-weight:700;'>과사용 ↑ {n_over}건</span>")
        if n_under > 0:
            breakdown_parts.append(f"<span style='color:#f59e0b;font-weight:700;'>저사용 ↓ {n_under}건</span>")
        breakdown_html = " · ".join(breakdown_parts) if breakdown_parts else f"{n_anomalies}건"

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
                    AI 이상감지: 정상범주 이탈 {n_anomalies}건 ({breakdown_html})
                </div>
                <div style="color: {_TEXT_SECONDARY}; font-size: 0.78rem;">
                    최근 {ANOMALY_LOOKBACK_DAYS}일간 실측이 모델이 학습한 정상범주(P05~P95) 밖으로 벗어난 항목입니다.
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # 상세 항목을 expander로 표시 — 행마다 [🔍 AI 진단] 버튼
        with st.expander(f"🔍 이상 감지 상세 ({n_anomalies}건)", expanded=False):
            _render_anomaly_detail_table(anomaly_df)
            st.caption(
                "※ 판정 기준: 실측이 정상범주 [P05, P95] 밖이면 이상 | "
                f"조회 기간: 최근 {ANOMALY_LOOKBACK_DAYS}일 | "
                "위치(±) = (실측−P50)/(밴드폭/2), |위치|≥1 = 밴드 경계 초과"
            )


# ──────────────────────────────────────
# AI 이상 원인 진단 (LLM)
# ──────────────────────────────────────

def _diag_session_key(factory: str, pred_date_str: str, target: str) -> str:
    """진단 결과 노출 여부를 추적하는 session_state 키."""
    return f"show_anomaly_diag::{factory}::{pred_date_str}::{target}"


def _render_anomaly_detail_table(anomaly_df: pd.DataFrame) -> None:
    """이상감지 상세 — 헤더 + 행별 컴포넌트(분석 버튼 포함).

    v5.2 컬럼: 날짜 / 공장 / 항목 / 정상범위 [P05~P95] / 실측 / 상태(↑/↓) / 위치 / 진단
    v5.1 폴백: 정상범위는 '—'로 표시.
    """
    # ── 헤더 행 ──
    col_widths = [0.6, 0.8, 0.6, 1.4, 1.0, 0.9, 0.7, 0.8]
    header_cols = st.columns(col_widths)
    header_style = (
        f"color:{_TEXT_SECONDARY}; font-size:0.78rem; font-weight:600; "
        f"letter-spacing:0.02em; padding:6px 0; text-align:center; "
        f"border-bottom:1px solid {_BORDER_COLOR};"
    )
    for i, label in enumerate(
        ["날짜", "공장", "항목", "정상범위 [P05~P95]", "실측값", "상태", "위치", "AI 진단"]
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
        sess_key = _diag_session_key(factory, pred_date_str, target)
        with cols[7]:
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


# 날짜 label 관련 처리를 담당합니다.
def _date_label(date_str: str) -> str:
    d_str = str(date_str).split(" ")[0]
    parts = d_str.split("-")
    if len(parts) >= 3:
        return f"{int(parts[1])}/{int(parts[2])}"
    return str(date_str)


# trend chart 화면을 구성합니다.
def render_trend_chart(base_date: str):
    # mix-ton / 원단위는 사내 표준 용어지만 외부·신규 사용자에게는 낯설 수 있어 hover 도움말 제공.
    intensity_help = (
        "원단위(Intensity) = 사용량 / 생산량. "
        "예: 전력 원단위 [kWh/mix-ton] = 전력 사용량(kWh) ÷ 믹스 생산량(ton). "
        "낮을수록 같은 양을 만들 때 에너지를 적게 쓰는 효율적 운영을 의미합니다. "
    )
    st.markdown(f"""
    <div class="section-header" title="{intensity_help}">
        <span class="section-icon">📈</span>
        7일간 생산량 · 원단위 추이 ⓘ
    </div>
    """, unsafe_allow_html=True)

    # ── 컨트롤 행: 사업장 선택 + 데이터 값 표시 토글 ──
    ctrl_factory_label, ctrl_factory, ctrl_show_val = st.columns([1, 3.2, 0.8])
    with ctrl_factory_label:
        st.markdown(
            "<div style='padding-top:6px; font-size:0.85rem; color:#374151; font-weight:600;'>사업장 필터</div>",
            unsafe_allow_html=True,
        )
    with ctrl_factory:
        persist("trend_factory_radio")
        selected_factory = st.radio(
            "사업장 선택",
            options=FACTORY_OPTIONS,
            index=0,
            horizontal=True,
            key="trend_factory_radio",
            label_visibility="collapsed",
        )
    with ctrl_show_val:
        persist("trend_show_values")
        show_trend_values = st.checkbox(
            "데이터 값 표시", value=False, key="trend_show_values"
        )

    trend_df = get_7day_trend(base_date, factory=selected_factory)

    if trend_df.empty:
        st.info("해당 기간 데이터가 없습니다.")
        return

    # ── 현재 화면 필터 그대로 CSV 다운로드 ──
    # 실무진 보고서(엑셀/PPT) 작성 시 화면 캡처 + 수치 옮겨 적기 부담을 줄이기 위해
    # 현재 사업장/기준일 필터가 적용된 7일치 원시 데이터를 그대로 내보냅니다.
    _csv_df = trend_df.copy()
    if "date" in _csv_df.columns:
        _csv_df["date"] = pd.to_datetime(_csv_df["date"]).dt.strftime("%Y-%m-%d")
    _csv_bytes = _csv_df.to_csv(index=False).encode("utf-8-sig")  # 엑셀 한글 깨짐 방지 BOM
    _dl_col1, _dl_col2 = st.columns([5, 1.2])
    with _dl_col2:
        st.download_button(
            label="⬇️ CSV",
            data=_csv_bytes,
            file_name=f"7day_trend_{selected_factory}_{base_date}.csv",
            mime="text/csv",
            use_container_width=True,
            key=f"dl_trend_{selected_factory}_{base_date}",
            help="현재 화면 필터(사업장·기준일)가 적용된 7일치 원시 데이터를 CSV로 내려받습니다.",
        )

    x_labels = [_date_label(d) for d in trend_df["date"]]
    prod_vals = trend_df["mix_prod_kg"].tolist()
    

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    unit_keys = list(UNIT_OPTIONS.keys())
    
    for row_idx in range(2):
        cols = st.columns(2)
        for col_idx in range(2):
            idx = row_idx * 2 + col_idx
            unit_label = unit_keys[idx]
            selected_col = UNIT_OPTIONS[unit_label]
            unit_color = UNIT_COLORS[selected_col]

            with cols[col_idx]:
                # ── 차트 헤더: 에너지원 라벨 ──
                # 빨간 박스 위치(차트 좌상단)에 어느 에너지원의 추이인지 한눈에 보이도록
                # 아이콘 + 한글 제목 + (단위) 를 표시. 색상은 차트 라인 색과 일치시켜
                # 4개 차트가 시각적으로도 구분되도록 함.
                _h_icon, _h_name = UNIT_HEADERS[selected_col]
                _h_unit = unit_label.split("[", 1)[1].rstrip("]") if "[" in unit_label else ""
                # 단위가 없는 지표(폐수/용수 비)는 빈 대괄호([])를 표시하지 않는다.
                _h_unit_html = (
                    f'<span style="font-size:0.75rem; color:{_TEXT_SECONDARY}; '
                    f'font-weight:500;">[{_h_unit}]</span>' if _h_unit else ""
                )
                st.markdown(
                    f"""
                    <div style="display:inline-flex; align-items:center; gap:8px;
                                padding:6px 14px; margin:0 0 6px 4px;
                                background: {unit_color}1A;
                                border-left: 3px solid {unit_color};
                                border-radius: 6px;">
                        <span style="font-size:1.0rem;">{_h_icon}</span>
                        <span style="font-size:0.95rem; font-weight:700;
                                     color:{_TEXT_PRIMARY};">{_h_name}</span>
                        {_h_unit_html}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                fig = go.Figure()

                # 좌축: 생산량
                prod_mode = "lines+markers+text" if show_trend_values else "lines+markers"
                fig.add_trace(go.Scatter(
                    x=x_labels,
                    y=prod_vals,
                    name="믹스량[kg]",
                    mode=prod_mode,
                    line=dict(color=PROD_COLOR, width=2),
                    marker=dict(size=7, color=PROD_COLOR),
                    text=[f"{v:,.0f}" if v is not None and v > 0 else "" for v in prod_vals] if show_trend_values else None,
                    textposition="bottom center",
                    textfont=dict(size=15, color=PROD_COLOR),
                    connectgaps=True,
                    yaxis="y1",
                ))

                # 우축: 원단위 (폐수/용수 비는 소수점 2자리로 표시)
                short_legend = unit_label.replace(" 원단위 [", "원단위[")
                unit_vals = trend_df[selected_col].tolist()
                _val_dec = 2 if selected_col == "wastewater_ratio" else 1
                unit_mode = "lines+markers+text" if show_trend_values else "lines+markers"
                fig.add_trace(go.Scatter(
                    x=x_labels,
                    y=unit_vals,
                    name=short_legend,
                    mode=unit_mode,
                    line=dict(color=unit_color, width=2.5),
                    marker=dict(size=7, color=unit_color),
                    text=[f"{v:.{_val_dec}f}" if v is not None else "" for v in unit_vals] if show_trend_values else None,
                    textposition="top center",
                    textfont=dict(size=15, color=unit_color),
                    connectgaps=True,
                    yaxis="y2",
                ))

                # ── 이벤트 메모 마커 (해당 에너지원 + 같은 기간/공장에 등록된 메모) ──
                # 실무자가 차트 스파이크 발생 시 남긴 원인/조치 기록을 시각적으로 표시.
                from app.services.event_annotation_service import list_events_for_chart as _list_evt
                # 폐수/용수 비 차트의 이벤트 메모는 폐수(wastewater) 타깃과 연결.
                energy_target = "wastewater" if selected_col == "wastewater_ratio" else selected_col.replace("_per_ton", "")
                _date_strs = pd.to_datetime(trend_df["date"]).dt.strftime("%Y-%m-%d").tolist()
                if _date_strs:
                    _ev_df = _list_evt(
                        factory=selected_factory,
                        date_from=_date_strs[0],
                        date_to=_date_strs[-1],
                        targets=[energy_target],
                    )
                    if not _ev_df.empty:
                        _date_to_label = {d: x_labels[i] for i, d in enumerate(_date_strs)}
                        _date_to_unit = {d: unit_vals[i] for i, d in enumerate(_date_strs)}
                        ev_x: list = []; ev_y: list = []; ev_text: list = []
                        for _, _r in _ev_df.iterrows():
                            _d = pd.to_datetime(_r["event_date"]).strftime("%Y-%m-%d")
                            if _d not in _date_to_label:
                                continue
                            _event_y = _date_to_unit.get(_d)
                            if _event_y is None or pd.isna(_event_y):
                                continue
                            ev_x.append(_date_to_label[_d])
                            ev_y.append(_event_y)
                            ev_text.append(
                                f"📝 {_r['tag']} | {_r['note']}<br>"
                                f"<span style='color:#94a3b8'>by {_r.get('created_by') or '-'}</span>"
                            )
                        if ev_x:
                            fig.add_trace(go.Scatter(
                                x=ev_x, y=ev_y,
                                mode="markers",
                                marker=dict(symbol="triangle-up", size=15, color="#9333ea",
                                            line=dict(color="white", width=1.5)),
                                name="이벤트 메모",
                                hovertemplate="%{text}<extra></extra>",
                                text=ev_text,
                                yaxis="y2",
                                showlegend=True,
                            ))


                fig.update_layout(
                    **DARK_CHART,
                    height=260, # 공간 최적화를 위해 높이 축소
                    margin=dict(l=40, r=40, t=20, b=40), # 불필요한 여백 최소화
                    legend=dict(
                        orientation="h", y=-0.25, x=0.5, xanchor="center",
                        font=dict(size=16),
                    ),
                    xaxis=dict(
                        gridcolor=_GRID_COLOR,
                        tickfont=dict(size=15, color=_FONT_COLOR),
                    ),
                    yaxis=dict(
                        tickfont=dict(color=_FONT_COLOR, size=15),
                        gridcolor=_GRID_COLOR,
                        tickformat="~s",
                    ),
                    yaxis2=dict(
                        tickfont=dict(color=unit_color, size=15),
                        overlaying="y",
                        side="right",
                        gridcolor="rgba(0,0,0,0)",
                        tickformat=f".{_val_dec}f",
                    ),
                )

                st.plotly_chart(fig, use_container_width=True, key=f"trend_{selected_col}")

                # ── 데이터 테이블 상세보기 토글 ──
                usage_col, usage_label, unit_long = UNIT_USAGE_INFO.get(
                    selected_col, (None, None, unit_label)
                )
                with st.expander("📄 데이터 테이블 상세보기", expanded=False):
                    tbl_cols = ["date", "mix_prod_kg"]
                    rename_map = {"date": "날짜", "mix_prod_kg": "믹스 생산량 (kg)"}
                    if usage_col and usage_col in trend_df.columns:
                        tbl_cols.append(usage_col)
                        rename_map[usage_col] = usage_label
                    tbl_cols.append(selected_col)
                    rename_map[selected_col] = unit_long
                    tbl_df = trend_df[tbl_cols].copy().rename(columns=rename_map)
                    tbl_df["날짜"] = pd.to_datetime(tbl_df["날짜"]).dt.strftime("%Y-%m-%d")
                    st.dataframe(
                        tbl_df,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "믹스 생산량 (kg)": st.column_config.NumberColumn(format="%,.0f"),
                            **(
                                {usage_label: st.column_config.NumberColumn(format="%,.0f")}
                                if usage_col else {}
                            ),
                            unit_long: st.column_config.NumberColumn(format="%,.2f"),
                        },
                    )



# ──────────────────────────────────────
# 섹션 2: 월간 원단위 전년비 절감 현황 차트
# ──────────────────────────────────────

_YOY_CHART_DEFS = [
    ("전력 원단위 [kWh/mix-ton]", "power"),
    ("연료 원단위 [Nm³/mix-ton]", "fuel"),
    ("용수 원단위 [ton/mix-ton]", "water"),
    ("폐수/용수", "wwratio"),
]

_YOY_USAGE_DEFS = [
    ("전력 사용량 [kWh]", "power"),
    ("연료 사용량 [Nm³]", "fuel"),
    ("용수 사용량 [ton]", "water"),
    ("폐수 사용량 [ton]", "waste"),
]

_YOY_COLORS = [
    ("#fde68a", "#d97706"),   # 전력: 밝은노랑 → 진한노랑
    ("#fca5a5", "#b91c1c"),   # 연료: 연빨강 → 진빨강
    ("#7dd3fc", "#1d4ed8"),   # 용수: 하늘파랑 → 진파랑
    ("#d1d5db", "#6b7280"),   # 폐수: 밝은회 → 진회색
]

_ALL_YOY_FACTORIES = list(FACTORY_DISPLAY_ORDER)


# 전년 대비 chart 화면을 구성합니다.
def render_yoy_chart(base_date: date):
    """월간 실적 전년비 현황 막대 차트."""
    st.markdown("""
    <div class='section-header'>
        <span class='section-icon'>📊</span>
        월간 원단위/사용량/생산량 전년비
    </div>
    """, unsafe_allow_html=True)

    ctrl_col1, ctrl_col2, ctrl_col3, ctrl_col4, _ = st.columns([0.7, 0.7, 1.0, 1.4, 2.2])
    with ctrl_col1:
        persist("yoy_year_sel")
        sel_year = st.number_input(
            "연도",
            min_value=2020,
            max_value=base_date.year,
            value=base_date.year,
            step=1,
            key="yoy_year_sel",
        )
    with ctrl_col2:
        persist("yoy_month_sel")
        sel_month = st.selectbox(
            "월",
            options=list(range(1, 13)),
            index=base_date.month - 1,
            format_func=lambda m: f"{m}월",
            key="yoy_month_sel",
        )
    with ctrl_col3:
        persist("yoy_data_type")
        ui_data_type = st.selectbox(
            "지표 구분",
            options=["원단위", "사용량", "생산량"],
            index=0,
            key="yoy_data_type",
        )
    with ctrl_col4:
        persist("yoy_factory_sel")
        sel_factories = st.multiselect(
            "공장 필터",
            options=_ALL_YOY_FACTORIES,
            default=["남양주", "김해", "광주", "논산"],
            key="yoy_factory_sel",
            placeholder="공장 선택...",
        )
        if not sel_factories:
            sel_factories = _ALL_YOY_FACTORIES

    prev_year = int(sel_year) - 1
    sel_year  = int(sel_year)

    sub_text = (
        f"{sel_year}년 {sel_month}월 vs "
        f"{prev_year}년 {sel_month}월 공장별 {ui_data_type} 비교"
    )
    st.markdown(
        f'<div style="font-size:0.80rem; color:{_TEXT_SECONDARY}; margin-bottom:8px;">{sub_text}</div>',
        unsafe_allow_html=True,
    )

    with st.spinner(f"{ui_data_type} 데이터 집계 중..."):
        yoy_df = get_monthly_yoy_summary(sel_year, sel_month, ui_data_type)

    if yoy_df.empty:
        st.info("해당 기간 데이터가 없습니다.")
        return

    plot_df = yoy_df[yoy_df["factory"].isin(sel_factories)].copy()
    if plot_df.empty:
        st.info("선택된 공장의 데이터가 없습니다.")
        return

    # ── 현재 화면 필터(연·월·지표·공장) 그대로 CSV 다운로드 ──
    # 보고서/회의 자료 작성 시 화면 수치를 그대로 옮길 수 있도록 raw 데이터 export.
    _yoy_csv = plot_df.copy()
    _yoy_csv_bytes = _yoy_csv.to_csv(index=False).encode("utf-8-sig")
    _yoy_dl_col1, _yoy_dl_col2 = st.columns([5, 1.2])
    with _yoy_dl_col2:
        st.download_button(
            label="⬇️ CSV",
            data=_yoy_csv_bytes,
            file_name=f"yoy_{ui_data_type}_{sel_year}{sel_month:02d}.csv",
            mime="text/csv",
            use_container_width=True,
            key=f"dl_yoy_{ui_data_type}_{sel_year}_{sel_month}",
            help="현재 화면 필터(연·월·지표·공장)가 적용된 전년비 비교 데이터를 CSV로 내려받습니다.",
        )

    if ui_data_type == "생산량":
        chart_defs = [("믹스 생산량 [ton]", "prod")]
    elif ui_data_type == "사용량":
        chart_defs = _YOY_USAGE_DEFS
    else:
        chart_defs = _YOY_CHART_DEFS

    # Draw logic
    n_charts = len(chart_defs)
    if n_charts == 1:
        cols2 = st.columns([1])
    else:
        cols2 = None

    for i in range(n_charts):
        if n_charts > 1:
            if i % 2 == 0:
                cols2 = st.columns(2)
            active_col = cols2[i % 2]
        else:
            active_col = cols2[0]

        title, key = chart_defs[i]
        curr_col = f"curr_{key}"
        prev_col = f"prev_{key}"
        # Fallback colors if production
        p_color, c_color = _YOY_COLORS[i] if i < len(_YOY_COLORS) else ("#9ca3af", "#4b5563")

        with active_col:
            fig = go.Figure()
            x_labels = plot_df["factory"].tolist()
            curr_vals = plot_df[curr_col].tolist()
            prev_vals = plot_df[prev_col].tolist()

            max_val_in_chart = max([v for v in curr_vals + prev_vals if v is not None] or [0])
            text_size = 13 if n_charts == 1 else 12
            
            # format values depending on magnitude
            # fmt val 관련 처리를 담당합니다.
            def fmt_val(v):
                if v is None: return ""
                if v >= 1000: return f"{v:,.0f}"
                return f"{v:,.2f}"

            # 전년 막대(연한 색) — 텍스트는 어두운 슬레이트로 대비 확보
            fig.add_trace(go.Bar(
                name=f"{prev_year}년",
                x=x_labels, y=prev_vals,
                marker_color=p_color, marker_opacity=0.85, width=0.35,
                text=[fmt_val(v) for v in prev_vals],
                textposition="inside",
                textfont=dict(size=text_size, color="#0f172a"),
                insidetextanchor="middle",
            ))
            # 당해 막대(진한 색) — 텍스트는 흰색
            fig.add_trace(go.Bar(
                name=f"{sel_year}년",
                x=x_labels, y=curr_vals,
                marker_color=c_color, marker_opacity=1.0, width=0.35,
                text=[fmt_val(v) for v in curr_vals],
                textposition="inside",
                textfont=dict(size=text_size, color="#ffffff"),
                insidetextanchor="middle",
            ))

            annotations = []
            for j, (cv, pv) in enumerate(zip(curr_vals, prev_vals)):
                if cv is not None and pv is not None and pv > 0:
                    dp = (cv - pv) / pv * 100
                    sg = "+" if dp > 0 else ""
                    
                    # Inverse color logic for production (Good is Blue if +, Bad is Red if -)
                    # Normal: Negative is Good (Blue), Positive is Bad (Red)
                    if ui_data_type == "생산량":
                        cl = "#3b82f6" if dp > 0 else "#ef4444"
                    else:
                        cl = "#ef4444" if dp > 0 else "#3b82f6"
                        
                    mv = max(cv, pv)
                    annotations.append(dict(
                        x=j, y=mv,
                        text=f'<b><span style="color:{cl}">{sg}{dp:.1f}%</span></b>',
                        showarrow=False, xanchor="center", yanchor="bottom",
                        yshift=4, font=dict(size=18),
                    ))

            fig.update_layout(
                **DARK_CHART,
                title=dict(text=title, font=dict(size=21, color=_FONT_COLOR),
                           x=0.5, xanchor="center"),
                barmode="group", bargap=0.25, bargroupgap=0.05, height=350 if n_charts == 1 else 300,
                margin=dict(l=40, r=20, t=50, b=60),
                legend=dict(orientation="h", y=-0.22, x=0.5, xanchor="center",
                            font=dict(size=18)),
                xaxis=dict(gridcolor=_GRID_COLOR, tickfont=dict(size=18, color=_FONT_COLOR)),
                yaxis=dict(gridcolor=_GRID_COLOR,
                           tickfont=dict(size=16, color=_FONT_COLOR),
                           rangemode="tozero", tickformat="~s", 
                           range=[0, max_val_in_chart * 1.15] if max_val_in_chart > 0 else None),
                annotations=annotations,
            )
            st.plotly_chart(fig, use_container_width=True,
                            key=f"yoy_{key}_{sel_year}_{sel_month}")




# ──────────────────────────────────────
# 섹션 3/4: 비교 현황 테이블
# ──────────────────────────────────────

# comparison table 화면을 구성합니다.
def _render_comparison_table(
    df: pd.DataFrame,
    section_title: str,
    unit_labels: list,
    base_date_str: str,
    invert_labels: set = None,
):
    """공장 계층 비교 테이블 렌더링.
    invert_labels: 비 컬럼 색상을 반전할 unit_label 세트 (생산량 등)
    """

    if invert_labels is None:
        invert_labels = set()

    # 섹션 헤더 (간략: 테이블이 나란히 배치될 때 진척률 레이블 중복 제거)
    st.markdown(f"""
    <div class="section-header">
        <span class="section-icon">📋</span>
        {section_title}
    </div>
    """, unsafe_allow_html=True)

    if df.empty:
        st.info("데이터가 없습니다.")
        return

    # 테이블 HTML
    th_style = (
        f"background:{_BG_CARD2}; color:{_TEXT_SECONDARY}; font-size:0.82rem; "
        f"padding:5px 8px; border:1px solid {_BORDER_COLOR}; text-align:center;"
    )
    td_base = f"font-size:0.82rem; padding:4px 8px; border:1px solid {_BORDER_COLOR};"

    col_headers = [
        ("구분",        100, "center"),
        ("공장",         58, "center"),
        ("전년 MTD",     82, "right"),
        ("당해 MTD",     82, "right"),
        ("증감비",       60, "center"),
        ("전년 YTD",     82, "right"),
        ("당해 YTD",     82, "right"),
        ("증감비",       60, "center"),
    ]

    html = '<div style="overflow-x:auto;"><table style="width:100%; border-collapse:collapse;">'
    html += '<thead><tr>'
    for name, width, align in col_headers:
        html += f'<th style="{th_style} width:{width}px; text-align:{align};">{name}</th>'
    html += '</tr></thead><tbody>'

    factory_order = [label for label, _ in FACTORY_QUERY_ORDER]

    for unit_label in unit_labels:
        unit_df = df[df["구분"] == unit_label]
        if unit_df.empty:
            continue

        n_rows = sum(1 for f in factory_order if not unit_df[unit_df["공장"] == f].empty)
        first = True

        for factory in factory_order:
            row = unit_df[unit_df["공장"] == factory]
            if row.empty:
                continue
            r = row.iloc[0]

            is_total = (factory == "ALL")
            row_bg = _BG_CARD2 if is_total else _BG_CARD
            fw = "font-weight:600;" if is_total else ""

            is_unit_rate = "원단위" in unit_label or "mix-ton" in unit_label
            is_ratio = "폐수/용수" in unit_label   # 폐수/용수 비 — 소수점 2자리

            # fmt val 관련 처리를 담당합니다.
            def fmt_val(v):
                if v is None:
                    return "-"
                if is_ratio:
                    return f"{v:.2f}"
                if is_unit_rate:
                    return f"{v:.1f}"
                if abs(v) >= 1_000_000:
                    return f"{v:,.0f}"
                if abs(v) >= 1_000:
                    return f"{v:,.1f}"
                return f"{v:,.1f}"

            if first:
                label_display = unit_label.replace("\n", "<br>")
                html += (
                    f'<tr><td rowspan="{n_rows}" style="{td_base} background:{_BG_CARD}; '
                    f'text-align:center; vertical-align:middle; font-size:0.80rem; '
                    f'font-weight:600; color:{_TEXT_PRIMARY};">{label_display}</td>'
                )
                first = False
            else:
                html += '<tr>'

            is_inv = unit_label in invert_labels
            html += f'<td style="{td_base} background:{row_bg}; text-align:center; {fw} color:{_TEXT_PRIMARY};">{factory}</td>'
            html += f'<td style="{td_base} background:{row_bg}; text-align:right; {fw} color:{_TEXT_SECONDARY};">{fmt_val(r.get("전년동월_MTD"))}</td>'
            html += f'<td style="{td_base} background:{row_bg}; text-align:right; {fw}">{fmt_val(r.get("당해월"))}</td>'
            html += f'<td style="{td_base} background:{row_bg}; text-align:center;">{_ratio_html(r.get("전년동월_MTD비"), invert=is_inv)}</td>'
            html += f'<td style="{td_base} background:{row_bg}; text-align:right; {fw} color:{_TEXT_SECONDARY};">{fmt_val(r.get("전년동기"))}</td>'
            html += f'<td style="{td_base} background:{row_bg}; text-align:right; {fw}">{fmt_val(r.get("당해누계"))}</td>'
            html += f'<td style="{td_base} background:{row_bg}; text-align:center;">{_ratio_html(r.get("전년동기비"), invert=is_inv)}</td>'
            html += '</tr>'

    html += '</tbody></table></div>'
    st.markdown(html, unsafe_allow_html=True)






# ──────────────────────────────────────
# 섹션 3/4: 대시보드 KPI + 분석 영역
# ──────────────────────────────────────

_DASH_FACTORIES = list(DASHBOARD_FACTORY_ORDER)
_DASH_COLORS = dict(DASHBOARD_FACTORY_COLORS)

# (db_label, icon, title, unit_text, is_prod, metric_key)
# metric_key 는 savings_target.metric 컬럼과 매칭되어 KPI 카드 진척률 산출에 사용.
_KPI_DEFS = [
    ("전력 원단위\n[kWh/mix-ton]", "⚡", "전력 원단위", "(kWh/mix-ton)", False, "power_per_ton"),
    ("연료 원단위\n[Nm³/mix-ton]", "🔥", "연료 원단위", "(Nm³/mix-ton)", False, "fuel_per_ton"),
    ("용수 원단위\n[ton/mix-ton]", "💧", "용수 원단위", "(ton/mix-ton)", False, "water_per_ton"),
    ("폐수/용수", "🚿", "폐수/용수", "(폐수량/용수량)", False, "wastewater_ratio"),
    ("믹스 생산량\n[ton]", "🍦", "생산량", "(ton)", True, "mix_prod"),
]

_DONUT_DEFS = [
    ("전력 사용량", "전력 사용량\n[kWh]", "kWh"),
    ("연료 사용량", "연료 사용량\n[Nm³]", "Nm³"),
    ("용수 사용량", "용수 사용량\n[ton]", "ton"),
    ("폐수 사용량", "폐수 사용량\n[ton]", "ton"),
]


_COMPARE_MODE_OPTIONS = ["월 동기 누계(MTD)", "연 동기 누계 (YTD)"]


def _resolve_compare_mode(compare_mode: str):
    """비교 기준 라디오 값을 KPI/분석 영역에서 쓸 컬럼 키 4종으로 변환."""
    if compare_mode == "월 동기 누계(MTD)":
        return ("당해월", "전년동월_MTD비", "전년동월_MTD", "전년 동월 대비")
    return ("당해누계", "전년동기비", "전년동기", "전년 동기 대비")


def _render_kpi_summary_top(
    unit_df: pd.DataFrame,
    usage_df: pd.DataFrame,
    base_date_str: str,
):
    """KPI 요약 (목표 대비 달성 현황판) — 대시보드 최상단 렌더링.

    비교 기준 라디오는 이 영역이 단일 소유자이며, 하단 분석 영역은
    같은 session_state 키('dash_compare_mode')를 읽어 동일 기준을 공유합니다.
    """
    # ── 비교 기준 필터 ──
    # MTD/YTD 같은 약어는 신입 사원·타 부서 사용자에게 낯설 수 있어 도움말을 함께 노출.
    persist("dash_compare_mode")
    compare_mode = st.radio(
        "비교 기준",
        options=_COMPARE_MODE_OPTIONS,
        index=0, horizontal=True, key="dash_compare_mode",
        help=(
            "MTD(Month-To-Date) — 이번 달 1일부터 기준일까지의 누계.\n"
            "YTD(Year-To-Date) — 이번 해 1월 1일부터 기준일까지의 누계.\n"
            "전년 동기간(전년 같은 기간)과 비교해 증감률을 산출합니다."
        ),
    )
    cmp_curr, cmp_ratio, cmp_prev, cmp_label = _resolve_compare_mode(compare_mode)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    # KPI 요약 카드 (전체 너비) — 목표 대비 달성 현황을 한눈에
    _render_kpi_cards(unit_df, usage_df, cmp_curr, cmp_ratio, cmp_prev, cmp_label, base_date_str)

    # 컨테이너 하단 보더가 카드 위로 올라오는 시각적 컷오프 방지용 spacer.
    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)


def _render_dashboard_analysis_sections(
    unit_df: pd.DataFrame,
    usage_df: pd.DataFrame,
    base_date_str: str,
):
    """대시보드 하단 분석 영역 (알람/이슈 + 사용량 구성 + 상세 테이블)."""
    # 상단 KPI 영역에서 설정된 비교 기준을 동일 키로 재사용 (단일 widget 정책)
    compare_mode = st.session_state.get("dash_compare_mode", _COMPARE_MODE_OPTIONS[0])
    cmp_curr, cmp_ratio, cmp_prev, cmp_label = _resolve_compare_mode(compare_mode)

    # Bottom Row: 알람/이슈(1/3) + 사용량 구성(2/3)
    bot_left, bot_right = st.columns([1, 2], gap="medium")
    with bot_left:
        _render_issue_alerts(unit_df, usage_df, cmp_ratio, cmp_prev, cmp_label)
    with bot_right:
        _render_energy_composition(usage_df, base_date_str)

    # 상세 테이블 (접기)
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
    with st.expander("📄 상세 비교 테이블 보기 (원단위 / 사용량)", expanded=False):
        u_labels = [d[0] for d in _KPI_DEFS if not d[4]]
        s_labels = ["믹스 생산량\n[ton]", "전력 사용량\n[kWh]",
                    "연료 사용량\n[Nm³]", "용수 사용량\n[ton]", "폐수 사용량\n[ton]"]

        # ── 현재 기준일·비교 기준 그대로 CSV 다운로드 ──
        # 화면의 HTML 표는 캡처용이므로, 보고서·엑셀 작업을 위해 raw DataFrame 도 함께 제공.
        def _flatten_label(s: str) -> str:
            """'전력 원단위\n[kWh/mix-ton]' 같은 줄바꿈 라벨을 한 줄로 정리."""
            return str(s).replace("\n", " ").strip()

        unit_csv_df = unit_df[unit_df["구분"].isin(u_labels)].copy()
        usage_csv_df = usage_df[usage_df["구분"].isin(s_labels)].copy()
        unit_csv_df["구분"] = unit_csv_df["구분"].map(_flatten_label)
        usage_csv_df["구분"] = usage_csv_df["구분"].map(_flatten_label)

        dl_c1, dl_c2 = st.columns(2)
        with dl_c1:
            st.download_button(
                label="⬇️ 원단위 비교 CSV",
                data=unit_csv_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"unit_rate_{base_date_str}.csv",
                mime="text/csv",
                use_container_width=True,
                key=f"dl_unit_rate_{base_date_str}",
                help="현재 기준일에 맞춘 원단위 MTD/YTD 비교 데이터를 CSV로 내려받습니다.",
            )
        with dl_c2:
            st.download_button(
                label="⬇️ 사용량 비교 CSV",
                data=usage_csv_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"usage_{base_date_str}.csv",
                mime="text/csv",
                use_container_width=True,
                key=f"dl_usage_{base_date_str}",
                help="현재 기준일에 맞춘 생산량·사용량 MTD/YTD 비교 데이터를 CSV로 내려받습니다.",
            )

        c1, c2 = st.columns(2)
        with c1:
            _render_comparison_table(unit_df, "유틸리티 원단위 현황", u_labels,
                                    base_date_str)
        with c2:
            _render_comparison_table(usage_df, "생산량 및 사용량 현황", s_labels,
                                    base_date_str,
                                    invert_labels={"믹스 생산량\n[ton]"})


# ─── 1. KPI 카드 ───

@st.dialog("📊 KPI 목표 설정 (관리자 전용)", width="large")
def _target_modal_dialog(year: int):
    """공장 × 지표 매트릭스로 전년 대비 절감/증가 목표(%)를 일괄 설정."""
    from app.services.target_service import (
        FACTORY_LABELS,
        INCREASE_METRICS,
        METRIC_LABELS,
        TARGET_FACTORIES,
        TARGET_METRICS,
        get_targets,
        upsert_targets,
    )

    st.caption(
        f"**{year}년 목표** — 원단위 4종은 \"전년 대비 절감률(%)\", 생산량은 \"전년 대비 증가율(%)\"로 입력합니다. "
        "값을 비워 두면 해당 칸의 목표가 삭제되어 KPI 카드의 진척률 막대가 비표시됩니다."
    )

    existing = get_targets(year=year, factories=TARGET_FACTORIES)

    # 표 형태로 입력 — 행: 공장, 열: 지표
    rows = []
    for fac in TARGET_FACTORIES:
        row = {"공장": FACTORY_LABELS.get(fac, fac)}
        for m in TARGET_METRICS:
            v = existing.get((fac, m))
            row[METRIC_LABELS[m]] = float(v) if v is not None else None
        rows.append(row)
    df_targets = pd.DataFrame(rows)

    edited = st.data_editor(
        df_targets,
        hide_index=True,
        use_container_width=True,
        disabled=["공장"],
        column_config={
            METRIC_LABELS[m]: st.column_config.NumberColumn(
                METRIC_LABELS[m],
                help=(
                    f"전년 대비 {'증가' if m in INCREASE_METRICS else '절감'}률(%) — "
                    "양수로 입력하세요. 비우면 목표 미설정."
                ),
                format="%.1f",
                step=0.1,
                min_value=-100.0,
                max_value=100.0,
            )
            for m in TARGET_METRICS
        },
        key=f"target_editor_{year}",
    )

    st.markdown(
        "<div style='font-size:0.8rem;color:#64748b;margin-top:6px;'>"
        "💡 예) <b>전력 원단위 5</b> 입력 = 전년 동기 대비 5% 절감 목표 → "
        "현재값 ÷ (전년동기값 × 0.95) × 100 이 KPI 카드의 \"목표 대비\" 값이 됩니다."
        "</div>",
        unsafe_allow_html=True,
    )

    btn_save, btn_cancel, _spacer = st.columns([1, 1, 3])
    with btn_save:
        if st.button("💾 저장", type="primary", use_container_width=True, key=f"target_save_{year}"):
            items: list[dict] = []
            for r_idx, fac in enumerate(TARGET_FACTORIES):
                for m in TARGET_METRICS:
                    val = edited.iloc[r_idx][METRIC_LABELS[m]]
                    items.append({"factory": fac, "metric": m, "target_pct": val})
            try:
                upsert_targets(year=year, items=items)
                st.success(f"{year}년 목표가 저장되었습니다.")
                st.rerun()
            except Exception as e:
                st.error(f"저장 실패: {e}")
    with btn_cancel:
        if st.button("취소", use_container_width=True, key=f"target_cancel_{year}"):
            st.rerun()


def _render_target_modal(year: int):
    """버튼 클릭 시 1회만 모달이 열리도록 session flag 를 즉시 소비하고 dialog 호출."""
    if st.session_state.pop("_target_modal_open", False):
        _target_modal_dialog(year)


def _kpi_card_html(icon, title, unit_text, val_str, change_pct, target_pct, cmp_label, is_prod):
    """단일 KPI 카드 HTML.

    레이아웃:
      [아이콘 + 타이틀]
      (단위)
      [현재값]
      목표 대비 X%   + 진척률 막대 (목표 미설정 시 생략)
      {cmp_label} ↑/↓ X%   ← 변화율과 라벨을 한 줄로 결합
    """
    # 목표 대비 진척률 (옵션) — 퍼센트 값에 좋음(파랑)/나쁨(빨강) 색상 + 두꺼운 글씨
    if target_pct is not None:
        bw = min(target_pct, 100)
        bc = "#3b82f6" if (is_prod and target_pct >= 100) or (not is_prod and target_pct <= 100) else "#ef4444"
        
        target_diff = target_pct - 100
        target_arrow = "↑" if target_diff >= 0 else "↓"
        
        prog = (f'<div style="font-size:1.02rem;color:{_TEXT_SECONDARY};margin-top:8px;">'
                f'목표 대비 <span style="color:{bc};font-weight:700;">{target_arrow} {abs(target_diff):.1f}%</span></div>'
                f'<div style="background:{_BORDER_COLOR};border-radius:4px;height:7px;margin-top:4px;overflow:hidden;">'
                f'<div style="background:{bc};width:{bw}%;height:100%;border-radius:4px;"></div></div>')
    else:
        prog = ""

    # 비교 라벨 + 변화율 (한 줄 결합 — "전년 동월 대비 ↓ 2.7%")
    if change_pct is not None:
        chg_color = "#3b82f6" if (is_prod and change_pct >= 0) or (not is_prod and change_pct < 0) else "#ef4444"
        arrow = "↑" if change_pct >= 0 else "↓"
        cmp_line = (
            f'<div style="font-size:1.02rem;color:{_TEXT_SECONDARY};margin-top:10px;">'
            f'{cmp_label} '
            f'<span style="color:{chg_color};font-weight:700;">{arrow} {abs(change_pct):.1f}%</span>'
            f'</div>'
        )
    else:
        cmp_line = (
            f'<div style="font-size:1.02rem;color:{_TEXT_MUTED};margin-top:10px;">'
            f'{cmp_label} <span style="color:{_TEXT_MUTED};">-</span></div>'
        )

    # 주의: 들여쓰기·줄바꿈이 있는 트리플쿼트 f-string으로 두면, prog가 빈 문자열이
    # 되는 경우(목표 미설정 연도) `{prog}` 자리가 공백만 남은 "blank line"이 되어
    # 위쪽 HTML 블록을 종료시킨다. 다음 줄의 4-space 들여쓰기 때문에 cmp_line이
    # Markdown 코드블록으로 해석돼 `<div style="font-size:` 같은 HTML이 그대로
    # 노출된다 (저장된 KPI 목표가 없는 과거 연도로 기준일을 옮기면 재현됨).
    # 단일 라인으로 합쳐 들여쓰기/공백줄 자체를 없애 근본 차단한다.
    return (
        f'<div style="background:{_BG_CARD};border:1px solid {_BORDER_COLOR};'
        f'border-radius:12px;padding:16px 18px;text-align:center;">'
        f'<div style="font-size:1.10rem;color:{_TEXT_SECONDARY};font-weight:600;">{icon} {title}</div>'
        f'<div style="font-size:0.98rem;color:{_TEXT_SECONDARY};margin-top:2px;">{unit_text}</div>'
        f'<div style="font-size:1.55rem;font-weight:700;color:{_TEXT_PRIMARY};margin:8px 0 4px;">{val_str}</div>'
        f'{prog}{cmp_line}'
        f'</div>'
    )



def _render_kpi_cards(unit_df, usage_df, cmp_curr, cmp_ratio, cmp_prev, cmp_label, base_date_str):
    """전사 기준 5개 KPI 카드 — 목표 대비 달성 현황 포함.

    "목표 대비 X%" 는 savings_target 테이블의 사용자 입력 목표(전년 대비 절감/증가율)를
    기반으로 산출됩니다. 목표 미입력 시 진척률 막대는 비표시.
    """
    from app.services.target_service import (
        compute_progress_pct,
        get_targets,
        INCREASE_METRICS,
    )

    # 헤더 + 설정 버튼 (admin 만 노출)
    base_dt = pd.to_datetime(base_date_str)
    cur_year = int(base_dt.year)
    is_admin_user = is_admin()

    h_left, h_right = st.columns([4, 1])
    with h_left:
        st.markdown(f"""
        <div style="display:flex;justify-content:flex-start;align-items:center;gap:10px;margin-bottom:8px;">
            <div class="section-header" style="margin:0;"><span class="section-icon">📊</span>
                KPI 요약 <span style="font-size:0.78rem;color:{_TEXT_SECONDARY};font-weight:400;">({cmp_label} · {cur_year}년 목표 기준)</span></div>
            <span style="font-size:0.78rem;color:{_TEXT_MUTED};">기준: {base_date_str}</span>
        </div>""", unsafe_allow_html=True)
    with h_right:
        if is_admin_user:
            if st.button("⚙️ 목표 설정", key=f"open_target_modal_{cur_year}",
                         help="전년 대비 절감/증가 목표(%)를 공장·지표별로 설정합니다. (관리자 전용)",
                         use_container_width=True):
                st.session_state["_target_modal_open"] = True

    # 목표 모달 (admin 만)
    if is_admin_user and st.session_state.get("_target_modal_open"):
        _render_target_modal(cur_year)

    # 전사 목표 fetch (KPI 카드는 전사 ALL 만 노출)
    targets = get_targets(year=cur_year, factories=["ALL"])

    cols = st.columns(5)
    for idx, (db_label, icon, title, unit_text, is_prod, metric_key) in enumerate(_KPI_DEFS):
        src = usage_df if is_prod else unit_df
        row = src[(src["구분"] == db_label) & (src["공장"] == "ALL")]
        if row.empty:
            with cols[idx]:
                st.markdown(_kpi_card_html(icon, title, unit_text, "-", None, None, cmp_label, is_prod),
                            unsafe_allow_html=True)
            continue
        r = row.iloc[0]
        curr, prev_val = r.get(cmp_curr), r.get(cmp_prev)
        chg = (curr / prev_val - 1) * 100 if curr is not None and prev_val and prev_val != 0 else None

        target_pct_raw = targets.get(("ALL", metric_key))
        progress_pct = compute_progress_pct(
            current_value=curr,
            prev_value=prev_val,
            target_pct=target_pct_raw,
            is_increase_metric=(metric_key in INCREASE_METRICS),
        )

        if curr is None:
            vs = "-"
        elif metric_key == "wastewater_ratio":   # 폐수/용수 비는 소수점 2자리
            vs = f"{curr:,.2f}"
        elif abs(curr) < 1000:
            vs = f"{curr:,.1f}"
        else:
            vs = f"{curr:,.0f}"
        with cols[idx]:
            st.markdown(_kpi_card_html(icon, title, unit_text, vs, chg, progress_pct, cmp_label, is_prod),
                        unsafe_allow_html=True)


# ─── 2. 알람/이슈 ───

def _render_issue_alerts(unit_df, usage_df, cmp_ratio, cmp_prev, cmp_label):
    """데이터 기반 주요 알람 카드."""
    st.markdown("""<div class="section-header"><span class="section-icon">🔔</span> 월간 주요 이슈</div>""",
        unsafe_allow_html=True)
    alerts = []
    for db_label, _, short, _, _, _ in _KPI_DEFS:
        if "생산량" in short: continue
        rows = unit_df[unit_df["구분"] == db_label]
        for f in _DASH_FACTORIES:
            r = rows[rows["공장"] == f]
            if r.empty: continue
            ratio = r.iloc[0][cmp_ratio]
            if ratio is not None and ratio > 1.0:
                c = (ratio - 1) * 100
                alerts.append({"icon": "🔥" if c >= 5 else "⚠️",
                    "title": f"{f} {short} 악화", "desc": f"{cmp_label} +{c:.1f}% 증가", "sev": c})
    prod_label = "믹스 생산량\n[ton]"
    if not usage_df.empty:
        pr = usage_df[usage_df["구분"] == prod_label]
        for f in _DASH_FACTORIES:
            r = pr[pr["공장"] == f]
            if r.empty: continue
            ratio = r.iloc[0][cmp_ratio]
            if ratio is not None and ratio < 0.9:
                c = (1 - ratio) * 100
                alerts.append({"icon": "⚠️", "title": f"{f} 생산량 감소",
                    "desc": f"{cmp_label} -{c:.1f}% 감소", "sev": c})
    alerts.sort(key=lambda a: a["sev"], reverse=True)
    alerts = alerts[:5]
    if not alerts:
        st.markdown(f"""<div style="background:{_BG_CARD};border:1px solid {_BORDER_COLOR};border-radius:10px;
            padding:16px 20px;color:#10b981;text-align:center;">✅ 주요 이상 항목이 없습니다.</div>""",
            unsafe_allow_html=True); return
    for a in alerts:
        sc = "#ef4444" if a["sev"] >= 5 else "#f59e0b"
        st.markdown(f"""<div style="background:{_BG_CARD};border:1px solid {_BORDER_COLOR};border-radius:10px;
            padding:10px 16px;margin-bottom:6px;display:flex;align-items:center;gap:12px;">
            <div style="font-size:1.4rem;">{a['icon']}</div>
            <div style="flex:1;"><div style="color:{_TEXT_PRIMARY};font-weight:600;font-size:0.88rem;">{a['title']}</div>
            <div style="color:{_TEXT_SECONDARY};font-size:0.76rem;">{a['desc']}</div></div>
            <div style="width:6px;height:36px;border-radius:3px;background:{sc};"></div></div>""",
            unsafe_allow_html=True)


# ─── 5. 사용량 구성 도넛 ───

def _render_energy_composition(usage_df, base_date_str):
    """공장별 에너지 사용량 비중 도넛 차트 4개."""
    # 테마별 색상 — 매 호출마다 최신 테마 반영 (import 캐시 우회)
    _t = _theme_colors()
    center_text_color = _t["TEXT_PRIMARY"]   # 중앙 합계: 다크=#e0e0e0, 라이트=#000
    slice_text_color = "#ffffff"             # 컬러 슬라이스 위 퍼센트는 양쪽 모두 흰색이 가독성 좋음

    bd = pd.to_datetime(base_date_str)
    st.markdown(f"""<div class="section-header"><span class="section-icon">🍩</span>
        에너지 사용량 구성 <span style="font-size:0.82rem;color:{_TEXT_SECONDARY};">({bd.year}-{bd.month:02d})</span></div>""",
        unsafe_allow_html=True)
    if usage_df.empty:
        st.info("사용량 데이터가 없습니다."); return
    cols = st.columns(4)
    dc = [_DASH_COLORS[f] for f in _DASH_FACTORIES]
    for col, (ct, dl, unit) in zip(cols, _DONUT_DEFS):
        rows = usage_df[usage_df["구분"] == dl]
        vals, labels = [], []
        for f in _DASH_FACTORIES:
            r = rows[rows["공장"] == f]
            vals.append(r.iloc[0]["당해누계"] if not r.empty and r.iloc[0]["당해누계"] is not None else 0)
            labels.append(f)
        total = sum(vals)
        ts = f"{total:,.0f}" if total >= 1000 else f"{total:,.1f}"
        fig = go.Figure(go.Pie(labels=labels, values=vals, hole=0.55,
            marker=dict(colors=dc),
            textinfo="percent",
            textposition="inside",
            textfont=dict(size=14, color=slice_text_color, weight="bold"),
            hovertemplate="%{label}: %{value:,.0f} " + unit + " (%{percent})<extra></extra>"))
        fig.update_layout(**DARK_CHART, height=220, margin=dict(l=5, r=5, t=30, b=5),
            title=dict(text=ct, font=dict(size=16, color=_FONT_COLOR), x=0.5, xanchor="center"),
            showlegend=False,
            annotations=[dict(text=f"<b>{ts}</b><br><span style='font-size:9px'>{unit}</span>",
                x=0.5, y=0.5, font=dict(size=15, color=center_text_color), showarrow=False)])
        with col:
            st.plotly_chart(fig, use_container_width=True, key=f"donut_{dl}")
            lp = []
            for f in labels:
                lp.append(f'<span style="color:{_DASH_COLORS[f]};font-size:0.98rem;">■</span> '
                          f'<span style="font-size:0.98rem;color:{_TEXT_PRIMARY};">{f}</span>')
            # 도넛 차트(가운데 정렬)와 시각적으로 맞추기 위해 범례도 center 정렬.
            st.markdown(
                f"<div style='text-align:center;'>{'&nbsp;&nbsp;'.join(lp)}</div>",
                unsafe_allow_html=True,
            )







# ──────────────────────────────────────
# One-Page 임원 요약 보고서 (브라우저 인쇄 친화)
# ──────────────────────────────────────

# 인쇄 시 Streamlit 기본 chrome(사이드바·툴바·푸터)을 숨기고
# 임원 요약만 깔끔하게 출력되도록 @media print 룰을 한 번만 주입.
_EXEC_PRINT_CSS = """
<style>
/* 임원 요약 보고서는 인쇄/PDF 문서 성격이므로, 다크 앱 위에서도
   흰 "종이 시트" 프리뷰로 렌더링 → 내부의 진한 본문색이 그대로 유효. */
.exec-summary-page {
  background: #ffffff !important;
  color: #0f172a !important;
  border-radius: 14px;
  padding: 28px 32px;
  box-shadow: 0 18px 48px rgba(0,0,0,0.55);
  border: 1px solid rgba(120,160,220,0.14);
}
.exec-summary-page * { color: inherit; }
/* 흰 시트 안에 재사용되는 다크 KPI 카드를 밝은 문서용으로 오버라이드 */
.exec-summary-page .kpi-card,
.exec-summary-page [data-testid="stMetric"] {
  background: #f8fafc !important;
  border: 1px solid #e2e8f0 !important;
  box-shadow: none !important;
  backdrop-filter: none !important;
}
.exec-summary-page .kpi-card-title { color: #475569 !important; }
.exec-summary-page .kpi-card-value { color: #0f172a !important; }
.exec-summary-page .kpi-card-unit  { color: #94a3b8 !important; }
@media print {
  [data-testid="stSidebar"], [data-testid="stToolbar"], [data-testid="stHeader"],
  [data-testid="stStatusWidget"], header, footer, .no-print { display: none !important; }
  [data-testid="stAppViewContainer"] { padding: 0 !important; margin: 0 !important; }
  [data-testid="stMain"], .main, .block-container {
    padding: 0 !important; margin: 0 !important; max-width: 100% !important;
  }
  .exec-summary-page { box-shadow: none !important; border: none !important;
    page-break-inside: avoid; }
  body { background: #ffffff !important; }
}
</style>
"""


def _exec_summary_top_n_factories(unit_df: pd.DataFrame, cmp_ratio: str, n: int = 1):
    """전력 원단위 기준 최우수/개선 시급 공장 1개씩."""
    rows = unit_df[unit_df["구분"] == "전력 원단위\n[kWh/mix-ton]"]
    candidates = []
    for _, r in rows.iterrows():
        if r["공장"] == "ALL":
            continue
        v = r.get(cmp_ratio)
        if v is None:
            continue
        candidates.append({
            "factory": r["공장"],
            "ratio": float(v),
        })
    candidates.sort(key=lambda x: x["ratio"])
    best = candidates[:n]
    worst = candidates[-n:][::-1] if candidates else []
    return best, worst


def _render_exec_summary_view(base_date: date, unit_df: pd.DataFrame, usage_df: pd.DataFrame):
    """One-Page 임원 요약 보고서. 브라우저의 Ctrl+P / 🖨 버튼으로 PDF 저장 가능."""
    from app.services.event_annotation_service import (
        TARGET_LABELS as EVT_TARGET_LABELS,
        list_events,
    )
    from app.services.target_service import (
        INCREASE_METRICS,
        compute_progress_pct,
        get_targets,
    )

    # 인쇄용 CSS 주입
    st.markdown(_EXEC_PRINT_CSS, unsafe_allow_html=True)

    base_date_str = base_date.strftime("%Y-%m-%d")
    cur_year = base_date.year

    # ── 액션 바 (인쇄 / 닫기) — 인쇄 시 숨김 ──
    st.markdown("<div class='no-print'>", unsafe_allow_html=True)
    btn_print, btn_close, _spacer = st.columns([1, 1, 5])
    with btn_print:
        if st.button("🖨️ 인쇄 / PDF 저장", type="primary", use_container_width=True,
                     key="exec_print_btn",
                     help="브라우저 인쇄 다이얼로그를 띄웁니다. PDF 로 저장 옵션 선택 가능."):
            # 클릭 시 window.print() 호출 — components.html 로 즉시 실행
            from streamlit.components.v1 import html as _html
            _html("<script>window.parent.print();</script>", height=0)
    with btn_close:
        if st.button("✕ 돌아가기", use_container_width=True, key="exec_close_btn"):
            st.session_state["_exec_summary_open"] = False
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
    st.caption("💡 또는 Ctrl+P 로 인쇄 다이얼로그를 직접 열 수도 있습니다.")

    # ── 임원 요약 보고서 본문 (.exec-summary-page) ──
    st.markdown("<div class='exec-summary-page'>", unsafe_allow_html=True)

    # 헤더
    st.markdown(f"""
    <div style="border-bottom:2px solid #0f172a;padding-bottom:10px;margin-bottom:16px;">
        <div style="font-size:1.6rem;font-weight:700;color:#0f172a;">
            🏭 에너지 요약 보고서
        </div>
        <div style="font-size:0.95rem;color:#475569;margin-top:4px;">
            보고 기준일: <b>{base_date_str}</b> &nbsp;·&nbsp;
            비교 기준: 전년 동월 누계(MTD) &nbsp;·&nbsp;
            출력 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── 1. KPI 요약 5장 ──
    cmp_curr = "당해월"; cmp_ratio = "전년동월_MTD비"; cmp_prev = "전년동월_MTD"
    targets = get_targets(year=cur_year, factories=["ALL"])

    st.markdown(
        "<div style='font-size:1.1rem;font-weight:700;color:#0f172a;margin:8px 0 8px;'>"
        "📊 주요 KPI (전사 · 전년 동월 대비)</div>",
        unsafe_allow_html=True,
    )
    kc = st.columns(5)
    for idx, (db_label, icon, title, unit_text, is_prod, metric_key) in enumerate(_KPI_DEFS):
        src = usage_df if is_prod else unit_df
        row = src[(src["구분"] == db_label) & (src["공장"] == "ALL")]
        if row.empty:
            with kc[idx]:
                st.markdown(_kpi_card_html(icon, title, unit_text, "-", None, None,
                                           "전년 동월 대비", is_prod), unsafe_allow_html=True)
            continue
        r = row.iloc[0]
        curr, prev_val = r.get(cmp_curr), r.get(cmp_prev)
        chg = (curr / prev_val - 1) * 100 if curr is not None and prev_val and prev_val != 0 else None
        prog = compute_progress_pct(
            current_value=curr, prev_value=prev_val,
            target_pct=targets.get(("ALL", metric_key)),
            is_increase_metric=(metric_key in INCREASE_METRICS),
        )
        if curr is None:
            vs = "-"
        elif metric_key == "wastewater_ratio":   # 폐수/용수 비는 소수점 2자리
            vs = f"{curr:,.2f}"
        elif abs(curr) < 1000:
            vs = f"{curr:,.1f}"
        else:
            vs = f"{curr:,.0f}"
        with kc[idx]:
            st.markdown(
                _kpi_card_html(icon, title, unit_text, vs, chg, prog, "전년 동월 대비", is_prod),
                unsafe_allow_html=True,
            )

    # ── 2. 최우수 / 개선 시급 공장 ──
    best, worst = _exec_summary_top_n_factories(unit_df, cmp_ratio, n=1)
    st.markdown(
        "<div style='font-size:1.1rem;font-weight:700;color:#0f172a;margin:24px 0 8px;'>"
        "🏆 최우수 · ⚠ 개선 시급 공장 (전력 원단위 기준)</div>",
        unsafe_allow_html=True,
    )
    bw_l, bw_r = st.columns(2)
    with bw_l:
        if best:
            b = best[0]
            chg = (b['ratio'] - 1) * 100
            st.markdown(
                f"<div style='background:#eff6ff;border:1px solid #3b82f6;border-radius:10px;padding:12px 16px;'>"
                f"<div style='color:#1e40af;font-weight:700;'>🏆 최우수: {b['factory']}</div>"
                f"<div style='color:#0f172a;font-size:0.9rem;margin-top:4px;'>"
                f"전력 원단위 전년 동월 대비 <b style='color:#3b82f6;'>{chg:+.1f}%</b>"
                f"</div></div>",
                unsafe_allow_html=True,
            )
        else:
            st.caption("데이터 부족 — 공장별 비교가 불가합니다.")
    with bw_r:
        if worst:
            w = worst[0]
            chg = (w['ratio'] - 1) * 100
            st.markdown(
                f"<div style='background:#fef2f2;border:1px solid #ef4444;border-radius:10px;padding:12px 16px;'>"
                f"<div style='color:#991b1b;font-weight:700;'>⚠ 개선 시급: {w['factory']}</div>"
                f"<div style='color:#0f172a;font-size:0.9rem;margin-top:4px;'>"
                f"전력 원단위 전년 동월 대비 <b style='color:#ef4444;'>{chg:+.1f}%</b>"
                f"</div></div>",
                unsafe_allow_html=True,
            )

    # ── 3. AI 이상감지 요약 ──
    st.markdown(
        "<div style='font-size:1.1rem;font-weight:700;color:#0f172a;margin:24px 0 8px;'>"
        "🚨 AI 이상감지 (최근 7일)</div>",
        unsafe_allow_html=True,
    )
    anomaly_df = _fetch_anomaly_data(base_date)
    if anomaly_df.empty:
        st.markdown(
            "<div style='background:#ecfdf5;border:1px solid #10b981;border-radius:10px;padding:10px 16px;color:#065f46;'>"
            "✅ 최근 7일간 실측값이 모델 정상범주(P05~P95) 안에 머물렀습니다.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='background:#fef2f2;border:1px solid #ef4444;border-radius:10px;padding:10px 16px;color:#991b1b;font-weight:700;'>"
            f"🚨 {len(anomaly_df)}건의 정상범주 이탈 감지 — 심각도 상위 5건:</div>",
            unsafe_allow_html=True,
        )
        top5 = anomaly_df.head(5)
        for _, ar in top5.iterrows():
            d_str = pd.to_datetime(ar["pred_date"]).strftime("%Y-%m-%d")

            # v5.2 밴드 정보
            p05_raw = ar.get("pred_p05") if "pred_p05" in anomaly_df.columns else None
            p95_raw = ar.get("pred_p95") if "pred_p95" in anomaly_df.columns else None
            p05 = None if (p05_raw is None or pd.isna(p05_raw)) else float(p05_raw)
            p95 = None if (p95_raw is None or pd.isna(p95_raw)) else float(p95_raw)
            bs = ar.get("band_status") if "band_status" in anomaly_df.columns else None
            if isinstance(bs, float) and pd.isna(bs):
                bs = None
            bp = ar.get("band_position") if "band_position" in anomaly_df.columns else None
            bp_val = None if (bp is None or (isinstance(bp, float) and pd.isna(bp))) else float(bp)

            if bs == "over":
                arrow = "↑ 과사용"; color = "#dc2626"
            elif bs == "under":
                arrow = "↓ 저사용"; color = "#f59e0b"
            else:
                arrow = "이상"; color = "#ea580c"

            if p05 is not None and p95 is not None:
                band_part = f"정상범위 {p05:,.0f}~{p95:,.0f} vs 실측 {ar['actual_value']:,.0f}"
            else:
                band_part = f"예측 {ar['pred_value']:,.0f} vs 실측 {ar['actual_value']:,.0f}"

            pos_part = f" (위치 {bp_val:+.2f})" if bp_val is not None else ""

            st.markdown(
                f"<div style='background:#fff;border:1px solid #fecaca;border-radius:6px;padding:6px 12px;margin:4px 0;font-size:0.88rem;'>"
                f"<b>{ar['factory']}</b> · {d_str} · {ar['target']} — "
                f"{band_part} "
                f"<span style='color:{color};font-weight:700;'>{arrow}</span>{pos_part}"
                f"</div>",
                unsafe_allow_html=True,
            )

    # ── 4. 이번 달 이벤트 메모 (최근 5건) ──
    st.markdown(
        "<div style='font-size:1.1rem;font-weight:700;color:#0f172a;margin:24px 0 8px;'>"
        "📝 이벤트 메모 (이번 달 최근 5건)</div>",
        unsafe_allow_html=True,
    )
    month_start = base_date.replace(day=1).strftime("%Y-%m-%d")
    events_df = list_events(date_from=month_start, date_to=base_date_str, limit=5)
    if events_df.empty:
        st.caption("이번 달 등록된 이벤트 메모가 없습니다.")
    else:
        for _, er in events_df.iterrows():
            d_str = pd.to_datetime(er["event_date"]).strftime("%Y-%m-%d")
            tgt_lbl = EVT_TARGET_LABELS.get(str(er["target"]), str(er["target"]))
            st.markdown(
                f"<div style='background:#fff;border:1px solid #e6eaf2;border-radius:6px;padding:6px 12px;margin:4px 0;font-size:0.88rem;'>"
                f"<b>{er['factory']}</b> · {d_str} · "
                f"<span style='color:#6366f1;'>{er['tag']} / {tgt_lbl}</span> — "
                f"{er['note']}"
                f"<span style='color:#94a3b8;font-size:0.78rem;'> (by {er.get('created_by') or '-'})</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

    # ── 푸터 ──
    st.markdown(f"""
    <div style="border-top:1px solid #cbd5e1;margin-top:24px;padding-top:8px;
                font-size:0.78rem;color:#64748b;">
        ※ 본 보고서는 빙그레 생산본부 에너지 대시보드(BEMS)에서 자동 생성된 요약입니다.
        세부 수치는 시스템에서 직접 확인하시기 바랍니다.
    </div>
    """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


# ──────────────────────────────────────
# 메일 송부 핸들러 (대시보드 액션바)
# ──────────────────────────────────────

def _send_dashboard_report_mail(ref_date: date) -> None:
    """대시보드의 '📧 메일 송부' 버튼 핸들러.

    기존 CLI(`tools/mail/run_daily_mail.py`)와 동일한 빌더/발송 파이프라인을
    재사용하여, 현재 기준일(`ref_date`)의 일일 에너지 원단위 리포트를 .env의
    `MAIL_RECIPIENTS` 로 즉시 발송한다. 발송 결과는 토스트와 배너로 안내.
    """
    # 무거운 의존성(plotly→PNG, jinja2, smtplib)은 사용 시점에만 로드.
    try:
        from tools.mail.config import get_mail_config
        from tools.mail.daily_report_builder import build_daily_report
        from tools.mail.mail_service import (
            MailMessage,
            MailSendError,
            send_mail,
        )
    except Exception as e:
        st.error(f"메일 모듈 로드 실패: {e}")
        return

    cfg = get_mail_config()
    if not cfg.is_valid:
        st.error(
            "메일 설정이 비어 있습니다. .env의 다음 항목을 확인하세요: "
            + ", ".join(cfg.missing_keys())
        )
        return

    try:
        with st.spinner(f"{ref_date} 기준 리포트 생성 및 발송 중..."):
            report = build_daily_report(ref_date=ref_date)
            msg = MailMessage(
                subject=report.subject,
                html_body=report.html,
                inline_images=report.inline_images,
            )
            result = send_mail(msg, cfg)
    except MailSendError as e:
        st.error(f"메일 발송 실패: {e}")
        return
    except Exception as e:
        st.error(f"메일 발송 중 예상치 못한 오류: {e}")
        return

    to_list = result.get("to") or cfg.recipients
    to_str = ", ".join(to_list) if isinstance(to_list, list) else str(to_list)
    st.toast(f"✅ 메일 발송 완료 — {to_str}", icon="📧")
    st.success(
        f"메일 발송 완료 · 기준일 {report.ref_date} · 공장 {report.record_count}개 · "
        f"수신: {to_str}"
    )


# ──────────────────────────────────────
# 메인 렌더 함수
# ──────────────────────────────────────

# main dashboard 화면을 구성합니다.
def render_main_dashboard():
    """대시보드 전체 렌더링."""

    total_records = get_record_count()
    if total_records == 0:
        st.markdown(f"""
        <div style="text-align:center; padding:80px 20px;">
            <div style="font-size:4rem; margin-bottom:20px;">📂</div>
            <div style="font-size:1.5rem; color:{_TEXT_PRIMARY}; margin-bottom:10px;">데이터가 없습니다</div>
            <div style="color:{_TEXT_SECONDARY};">Data Upload 탭에서 에너지 데이터를 먼저 업로드해 주세요.</div>
        </div>
        """, unsafe_allow_html=True)
        return

    db_min, db_max = get_date_range()
    if not db_max:
        return

    db_max_date = pd.to_datetime(db_max).date()
    db_min_date = pd.to_datetime(db_min).date()

    # ── 헤더 ──
    st.markdown(f"""
    <div style="font-size:1.4rem; font-weight:700; color:{_TEXT_PRIMARY}; padding:4px 0; margin-bottom: 24px;">
        🖥️ 에너지 대시보드
    </div>
    """, unsafe_allow_html=True)

    two_days_ago = date.today() - timedelta(days=2)
    # 이틀 전이 DB 범위 안에 있으면 이틀 전을, 아니면 DB 최대일을 기본값으로 사용
    if db_min_date <= two_days_ago <= db_max_date:
        default_base = two_days_ago
    else:
        default_base = db_max_date

    # 기준일 설정 섹션 (가깝게 배치하기 위해 flex 컨테이너와 합침)
    col_info, col_label, col_date, col_mail = st.columns([2.4, 0.4, 1.1, 1.0])
    with col_date:
        persist("dashboard_base_date")
        base_date = st.date_input(
            "기준일",
            value=default_base,
            min_value=db_min_date,
            max_value=db_max_date,
            key="dashboard_base_date",
            label_visibility="collapsed",
        )
    with col_label:
        st.markdown(f"""
        <div style="text-align:right; font-size:0.95rem; font-weight:600; color:{_TEXT_SECONDARY}; padding-top:8px;">
            기준일
        </div>
        """, unsafe_allow_html=True)
    with col_mail:
        # 기준일 기준 일일 에너지 원단위 HTML 리포트를 .env의 MAIL_RECIPIENTS 에게 즉시 발송.
        # 본문은 tools.mail.daily_report_builder 재사용 (CLI .bat과 동일 산출물).
        if st.button(
            "📧 메일 송부",
            key="dashboard_send_mail_btn",
            use_container_width=True,
            help="현재 기준일의 일일 에너지 원단위 리포트를 .env의 MAIL_RECIPIENTS 에게 발송합니다.",
        ):
            _send_dashboard_report_mail(base_date)

    base_date_str = base_date.strftime("%Y-%m-%d")
    progress = _progress_rate(base_date)

    prev_year = base_date.year - 1
    progress_help = (
        "월 진척률 = 기준일까지의 일수 / 해당 월의 총 일수 × 100. "
        "월 사용량의 \"이번 달 안에서 어디까지 왔는지\" 보여 줍니다."
    )
    
    with col_info:
        st.markdown(f"""
        <div style="display:flex; gap:16px; align-items:center; font-size:0.82rem; flex-wrap:wrap; padding-top:6px; margin-bottom:12px;">
            <span style="border:1px solid {_BORDER_COLOR}; border-radius:4px; padding:2px 12px; color:{_TEXT_SECONDARY};"
                  title="{progress_help}">
                월 진척률 ⓘ <b style="color:{_ACCENT_COLOR}">{progress:.1f}%</b>
                ({base_date.day}/{calendar.monthrange(base_date.year, base_date.month)[1]}일)
            </span>
            <span style="color:{_TEXT_MUTED}; font-size:0.78rem;"
                  title="전년 동월/동기 대비 증감률을 계산할 때 분모가 되는 연도입니다.">
                비교 기준년도: <b style="color:{_TEXT_SECONDARY}">{prev_year}년</b>
            </span>
        </div>
        """, unsafe_allow_html=True)

    # 상단 KPI 요약과 하단 분석 영역이 같은 데이터를 공유하므로 먼저 fetch.
    with st.spinner("원단위/사용량 집계 중..."):
        unit_df  = get_unit_rate_comparison(base_date_str)
        usage_df = get_production_usage_comparison(base_date_str)

    # ── 임원 요약 보고서 모드 (전체 대시보드 대신 1페이지 요약만 노출) ──
    if st.session_state.get("_exec_summary_open"):
        _render_exec_summary_view(base_date, unit_df, usage_df)
        return

    # ── 섹션 0: KPI 요약 (목표 대비 달성 현황판) ──
    with st.container(border=True):
        _render_kpi_summary_top(unit_df, usage_df, base_date_str)

    # ── 섹션 1: AI 예측 이상 알림 + SPC 관리도 ──
    with st.container(border=True):
        _render_anomaly_alert(base_date)
        st.markdown(
            f"<div style='height:1px; background:{_BORDER_COLOR}; margin:8px 0 6px;'></div>",
            unsafe_allow_html=True,
        )
        _render_spc_section(base_date)

    # ── 섹션 2: 7일 추이 차트 ──
    with st.container(border=True):
        render_trend_chart(base_date_str)

    # ── 섹션 3: 월간 실적 전년비 현황 차트 ──
    with st.container(border=True):
        render_yoy_chart(base_date)

    # ── 섹션 4: 분석 영역 (알람/이슈 + 사용량 구성 + 상세 테이블) ──
    with st.container(border=True):
        _render_dashboard_analysis_sections(unit_df, usage_df, base_date_str)


