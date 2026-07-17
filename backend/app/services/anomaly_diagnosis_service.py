"""
Anomaly Diagnosis Service
=========================
AI 예측 모델의 일별 예측-실측 오차가 임계값을 초과한 이상 이벤트에 대해
LLM(OpenAI)에게 원인 가설 + 점검 우선순위를 진단받는 서비스.

호출 흐름:
    dashboard_main → get_or_create_diagnosis(factory, pred_date, target)
        → 캐시 확인 (anomaly_analysis 테이블)
        → 미스 시: 컨텍스트 수집 → LLM 호출 → 캐시에 저장
        → 진단 마크다운 텍스트 반환

캐시 정책:
    동일 (factory, pred_date, target) 조합은 한 번만 LLM 호출.
    실측값/예측값이 (역채움 등으로) 변경되면 force_refresh=True 로 재생성.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
import pandas as pd
from dotenv import load_dotenv

from app.database.db_connection import get_connection, execute_query, execute_write
from app.domain.factories import PRODUCTION_DAILY_FACTORY_MAP
from app.services.production_actual_service import overlay_actual_production
from app.services.v5_common import (
    BAND_STATUS_LABELS_KO,
    TARGET_SPECS, classify_band,
)
from app.utils.tls import httpx_verify
try:
    from app.services.variable_anomaly_service import (
        detect_anomalous_inputs,
        VariableAnomaly,
    )
    _HAS_VARIABLE_ANOMALY = True
except Exception:
    _HAS_VARIABLE_ANOMALY = False
    VariableAnomaly = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]

try:
    from app.services.item_energy_impact_service import (
        get_intensity_table,
        lookup_status,
        LookupNotAvailable,
    )
    _HAS_IMPACT = True
except Exception:
    _HAS_IMPACT = False
    LookupNotAvailable = Exception  # type: ignore[misc,assignment]

load_dotenv()


# ──────────────────────────────────────
# 상수 / 설정
# ──────────────────────────────────────

BASELINE_DAYS = 14            # 기저 비교: 이상일 직전 14영업일
PROD_LOOKBACK_DAYS = 14       # production_daily 카테고리 비중 비교 창
LLM_MODEL = "gpt-5.4"         # ai_report_service 와 동일

# prediction_log 의 factory(실공장) → production_daily 의 factory(통합) 매핑
# 남양주1·남양주2 는 production_daily 에서 "남양주" 로 통합 저장됨
PROD_FACTORY_MAP = dict(PRODUCTION_DAILY_FACTORY_MAP)


@dataclass
class AnomalyContext:
    """LLM 컨텍스트 빌더의 구조화 산출물 (디버깅·로깅 용).

    v5.2 도입에 따른 확장 필드:
      - pred_p05/pred_p95: 정상범주 밴드
      - band_status/band_position: 'over'|'under', 정규화 거리
      - anomalous_inputs: 같은 공장 최근 60영업일 분포에서 벗어난 입력 변수 리스트
    """
    factory: str
    pred_date: str
    target: str
    pred_value: float                  # = P50 (호환)
    actual_value: float
    mape: float                        # 참고 지표 (이상 판정은 더 이상 사용 안 함)
    residual_signed: float
    residual_pct_signed: float
    pred_p05: float | None = None
    pred_p95: float | None = None
    band_status: str | None = None     # 'over'|'under'|'inside'|None
    band_position: float | None = None
    baseline: dict[str, Any] = field(default_factory=dict)
    prod_breakdown: list[dict[str, Any]] = field(default_factory=list)
    energy_raw: dict[str, Any] = field(default_factory=dict)
    intensity_lines: list[str] = field(default_factory=list)
    anomalous_inputs: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_prompt_text(self) -> str:
        """LLM input 으로 보낼 사람-친화 텍스트로 직렬화."""
        unit = TARGET_SPECS.get(self.target, {}).get("unit", "")
        sign_word = "과소예측 (실측↑)" if self.residual_signed > 0 else "과대예측 (실측↓)"
        lines: list[str] = []

        lines.append("[이상 이벤트]")
        lines.append(f"- 공장: {self.factory}")
        lines.append(f"- 일자: {self.pred_date}")
        lines.append(f"- 항목: {self.target} ({unit})")

        # v5.2 밴드 정보가 있으면 정상범주를 우선 노출
        if self.pred_p05 is not None and self.pred_p95 is not None:
            band_label = BAND_STATUS_LABELS_KO.get(self.band_status or "", "—")
            lines.append(
                f"- 모델 정상범주: {self.pred_p05:,.1f} ~ {self.pred_p95:,.1f} {unit} "
                f"(중앙값 {self.pred_value:,.1f})"
            )
            lines.append(f"- 실측값: {self.actual_value:,.1f} {unit} → 판정: {band_label}")
            if self.band_position is not None:
                lines.append(
                    f"- 정규화 위치: {self.band_position:+.2f}  "
                    f"(±1 = 밴드 경계, |위치|≥2면 매우 비정상)"
                )
        else:
            lines.append(f"- 예측값: {self.pred_value:,.1f} {unit}")
            lines.append(f"- 실측값: {self.actual_value:,.1f} {unit}")

        lines.append(f"- 참고 절대 오차율(MAPE): {self.mape:.2f}%")
        lines.append(f"- 잔차(실측-예측): {self.residual_signed:+,.1f} {unit}  ({self.residual_pct_signed:+.2f}%)")
        lines.append(f"- 잔차 방향: {sign_word}")

        if self.anomalous_inputs:
            lines.append("")
            lines.append("[이날 평소와 다른 입력 변수 (최근 60영업일 분포 대비)]")
            for f in self.anomalous_inputs:
                arrow = "↑↑" if f.get("direction") == "high" else "↓↓"
                label = f.get("label") or f.get("feature")
                v = f.get("value")
                mu = f.get("baseline_mean")
                z = f.get("z_score")
                rule = f.get("rule")
                if rule == "zscore" and z is not None and mu is not None:
                    sigma = f.get("baseline_std") or 0
                    lines.append(
                        f"- {label}: 오늘 {v:,.2f} (평소 {mu:,.2f}±{sigma:,.2f}, z={z:+.2f}) {arrow}"
                    )
                elif rule == "percentile":
                    lines.append(f"- {label}: 오늘 {v:,.2f} (평소 분포 {arrow}측 극단)")
                elif rule == "flag":
                    lines.append(f"- {label}: 오늘 활성화됨 (평소엔 거의 없음)")
                else:
                    lines.append(f"- {label}: 오늘 {v}")

        if self.baseline:
            lines.append("")
            lines.append(f"[기저 비교 — 동 공장·동 항목 최근 {BASELINE_DAYS}영업일]")
            for k, v in self.baseline.items():
                lines.append(f"- {k}: {v}")

        if self.energy_raw:
            lines.append("")
            lines.append("[당일 energy_daily — 사용량 raw]")
            for k, v in self.energy_raw.items():
                lines.append(f"- {k}: {v}")

        if self.prod_breakdown:
            lines.append("")
            lines.append(f"[당일 production_daily — category2 비중 (직전 {PROD_LOOKBACK_DAYS}일 평균 대비)]")
            for row in self.prod_breakdown:
                cat = row.get("category2") or "(미분류)"
                today_kg = row.get("today_kg", 0)
                base_kg = row.get("baseline_kg", 0)
                today_share = row.get("today_share_pct", 0)
                base_share = row.get("baseline_share_pct", 0)
                delta_pct = row.get("delta_pct")
                share_delta = today_share - base_share
                delta_str = f"{delta_pct:+.1f}%" if delta_pct is not None else "n/a"
                lines.append(
                    f"- {cat}: 당일 {today_kg:,.0f} kg (비중 {today_share:.1f}%) "
                    f"vs 기저평균 {base_kg:,.0f} kg (비중 {base_share:.1f}%) "
                    f"→ 실적 {delta_str}, 비중 Δ{share_delta:+.1f}pp"
                )

        if self.intensity_lines:
            lines.append("")
            lines.append("[회귀 계수 룩업 — What-if 정량 추정용]")
            for s in self.intensity_lines:
                lines.append(f"- {s}")

        if self.notes:
            lines.append("")
            lines.append("[보조 메모]")
            for s in self.notes:
                lines.append(f"- {s}")

        return "\n".join(lines)


# ──────────────────────────────────────
# 캐시 테이블 보장 (idempotent)
# ──────────────────────────────────────

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS anomaly_analysis (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    factory         VARCHAR(50)  NOT NULL,
    pred_date       DATE         NOT NULL,
    target          VARCHAR(20)  NOT NULL,
    pred_value      DOUBLE       NOT NULL,
    pred_p05        DOUBLE       DEFAULT NULL,
    pred_p95        DOUBLE       DEFAULT NULL,
    actual_value    DOUBLE       NOT NULL,
    mape            DOUBLE       NOT NULL,
    band_status     VARCHAR(16)  DEFAULT NULL,
    band_position   DOUBLE       DEFAULT NULL,
    diagnosis       MEDIUMTEXT   NOT NULL,
    context_snapshot MEDIUMTEXT  DEFAULT NULL,
    model_used      VARCHAR(64)  DEFAULT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_anom (factory, pred_date, target)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_TABLE_READY = False


def _ensure_table() -> None:
    """schema.sql 적용 안 된 기존 DB 환경에서도 동작하도록 1회 보장."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(_TABLE_DDL)
            conn.commit()
            cur.close()
        finally:
            conn.close()
        _TABLE_READY = True
    except Exception as exc:
        # viewer 권한이면 CREATE 실패할 수 있음 — host 에서 init_db 했다고 가정하고 진행
        print(f"[anomaly_diagnosis] _ensure_table skipped: {exc}")
        _TABLE_READY = True


# ──────────────────────────────────────
# 프롬프트 로더
# ──────────────────────────────────────

def _resolve_prompt_path() -> Path:
    app_dir = Path(__file__).resolve().parent.parent
    return app_dir / "prompts" / "anomaly_diagnosis_prompt.md"


# ──────────────────────────────────────
# 캐시 조회/저장
# ──────────────────────────────────────

def get_cached_diagnosis(
    factory: str, pred_date: date | str, target: str
) -> Optional[dict]:
    """캐시된 진단 1건 조회. 없으면 None."""
    _ensure_table()
    pd_str = pred_date.isoformat() if isinstance(pred_date, date) else str(pred_date)
    rows = execute_query(
        "SELECT * FROM anomaly_analysis "
        "WHERE factory=%s AND pred_date=%s AND target=%s LIMIT 1",
        (factory, pd_str, target),
    )
    return rows[0] if rows else None


def _save_diagnosis(
    factory: str,
    pred_date: str,
    target: str,
    pred_value: float,
    actual_value: float,
    mape: float,
    diagnosis: str,
    context_snapshot: str,
    model_used: str,
    pred_p05: float | None = None,
    pred_p95: float | None = None,
    band_status: str | None = None,
    band_position: float | None = None,
) -> None:
    _ensure_table()
    execute_write(
        """
        INSERT INTO anomaly_analysis
            (factory, pred_date, target,
             pred_value, pred_p05, pred_p95,
             actual_value, mape,
             band_status, band_position,
             diagnosis, context_snapshot, model_used, created_at, updated_at)
        VALUES (%s,%s,%s,
                %s,%s,%s,
                %s,%s,
                %s,%s,
                %s,%s,%s,NOW(),NOW())
        ON DUPLICATE KEY UPDATE
            pred_value=VALUES(pred_value),
            pred_p05=VALUES(pred_p05),
            pred_p95=VALUES(pred_p95),
            actual_value=VALUES(actual_value),
            mape=VALUES(mape),
            band_status=VALUES(band_status),
            band_position=VALUES(band_position),
            diagnosis=VALUES(diagnosis),
            context_snapshot=VALUES(context_snapshot),
            model_used=VALUES(model_used),
            updated_at=NOW()
        """,
        (factory, pred_date, target,
         pred_value, pred_p05, pred_p95,
         actual_value, mape,
         band_status, band_position,
         diagnosis, context_snapshot, model_used),
    )


# ──────────────────────────────────────
# 컨텍스트 빌더
# ──────────────────────────────────────

def _fetch_anomaly_event(
    factory: str, pred_date: str, target: str
) -> Optional[dict]:
    rows = execute_query(
        """
        SELECT factory, pred_date, target,
               pred_value, pred_p05, pred_p95,
               actual_value, mape,
               band_status, band_position,
               mix_prod_kg
        FROM prediction_log
        WHERE factory=%s AND pred_date=%s AND target=%s
        LIMIT 1
        """,
        (factory, pred_date, target),
    )
    return rows[0] if rows else None


def _fetch_baseline(
    factory: str, pred_date: str, target: str
) -> dict[str, Any]:
    """동 공장·동 항목 직전 BASELINE_DAYS영업일 통계."""
    base_from = (
        datetime.strptime(pred_date, "%Y-%m-%d").date() - timedelta(days=BASELINE_DAYS * 2)
    ).isoformat()
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            """
            SELECT pred_date, pred_value, actual_value, mape
            FROM prediction_log
            WHERE factory=%s AND target=%s
              AND pred_date >= %s AND pred_date < %s
              AND actual_value IS NOT NULL
            ORDER BY pred_date DESC
            LIMIT %s
            """,
            conn,
            params=(factory, target, base_from, pred_date, BASELINE_DAYS),
        )
    finally:
        conn.close()

    if df.empty:
        return {"기저 데이터": "없음 (직전 14영업일 실측·예측 매칭 데이터 부재)"}

    df["residual"] = df["actual_value"] - df["pred_value"]
    return {
        "샘플 수": f"{len(df)} 건",
        "평균 MAPE": f"{df['mape'].mean():.2f}%",
        "MAPE σ": f"{df['mape'].std(ddof=0):.2f}%" if len(df) >= 2 else "n/a",
        "평균 잔차(실측-예측)": f"{df['residual'].mean():+,.1f}",
        "평균 실측값": f"{df['actual_value'].mean():,.1f}",
        "평균 예측값": f"{df['pred_value'].mean():,.1f}",
    }


def _fetch_energy_raw(
    factory: str, pred_date: str, target: str
) -> dict[str, Any]:
    """당일 energy_daily 의 전체 사용량/원단위 + 전일 대비 변화."""
    spec = TARGET_SPECS.get(target, {})
    db_col = spec.get("db_col")
    if not db_col:
        return {}

    # 단일 공장 기준 (남양주1/2 도 그대로)
    rows = execute_query(
        """
        SELECT date, mix_prod_kg, total_power_kwh, fuel_nm3, water_ton,
               power_per_ton_kwh, fuel_per_ton_nm3, water_per_ton_ton
        FROM energy_daily
        WHERE factory=%s AND date BETWEEN
            DATE_SUB(%s, INTERVAL 1 DAY) AND %s
        ORDER BY date
        """,
        (factory, pred_date, pred_date),
    )
    if not rows:
        return {"raw": "energy_daily 데이터 없음"}

    rows = overlay_actual_production(pd.DataFrame(rows)).to_dict("records")

    today = next((r for r in rows if str(r["date"]).split(" ")[0] == pred_date), None)
    yest = next((r for r in rows if str(r["date"]).split(" ")[0] != pred_date), None)
    if today is None:
        return {"raw": f"{pred_date} energy_daily 행 없음"}

    out: dict[str, Any] = {}
    out["당일 mix_prod_kg"] = f"{float(today.get('mix_prod_kg', 0) or 0):,.0f} kg"
    out[f"당일 {db_col}"] = f"{float(today.get(db_col, 0) or 0):,.1f}"

    intensity_col_map = {
        "total_power_kwh": "power_per_ton_kwh",
        "fuel_nm3": "fuel_per_ton_nm3",
        "water_ton": "water_per_ton_ton",
    }
    intensity_col = intensity_col_map.get(db_col)
    if intensity_col and today.get(intensity_col) is not None:
        out[f"당일 {intensity_col}"] = f"{float(today[intensity_col]):,.2f}"

    if yest is not None:
        prev_val = float(yest.get(db_col, 0) or 0)
        curr_val = float(today.get(db_col, 0) or 0)
        if prev_val > 0:
            dod = (curr_val - prev_val) / prev_val * 100
            out[f"전일 대비 {db_col}"] = f"{dod:+.1f}% (전일 {prev_val:,.1f} → 당일 {curr_val:,.1f})"
        prev_prod = float(yest.get("mix_prod_kg", 0) or 0)
        curr_prod = float(today.get("mix_prod_kg", 0) or 0)
        if prev_prod > 0:
            out["전일 대비 mix_prod_kg"] = (
                f"{(curr_prod - prev_prod) / prev_prod * 100:+.1f}% "
                f"(전일 {prev_prod:,.0f} → 당일 {curr_prod:,.0f})"
            )

    return out


def _fetch_prod_breakdown(
    factory: str, pred_date: str
) -> list[dict[str, Any]]:
    """당일 vs 직전 PROD_LOOKBACK_DAYS일 평균 — category2 비중·실적 변화."""
    prod_factory = PROD_FACTORY_MAP.get(factory, factory)
    base_from = (
        datetime.strptime(pred_date, "%Y-%m-%d").date() - timedelta(days=PROD_LOOKBACK_DAYS)
    ).isoformat()

    conn = get_connection()
    try:
        df_today = pd.read_sql_query(
            """
            SELECT category2, SUM(actual_qty) AS qty
            FROM production_daily
            WHERE factory=%s AND date=%s
            GROUP BY category2
            """,
            conn,
            params=(prod_factory, pred_date),
        )
        df_base = pd.read_sql_query(
            """
            SELECT category2, AVG(daily_sum) AS avg_qty
            FROM (
              SELECT date, category2, SUM(actual_qty) AS daily_sum
              FROM production_daily
              WHERE factory=%s AND date >= %s AND date < %s
                AND actual_qty > 0
              GROUP BY date, category2
            ) t
            GROUP BY category2
            """,
            conn,
            params=(prod_factory, base_from, pred_date),
        )
    finally:
        conn.close()

    if df_today.empty:
        return []

    today_total = float(df_today["qty"].sum()) or 1.0
    base_lookup = {row["category2"]: float(row["avg_qty"] or 0) for _, row in df_base.iterrows()}
    base_total = sum(base_lookup.values()) or 1.0

    out: list[dict[str, Any]] = []
    for _, r in df_today.iterrows():
        cat = r["category2"]
        today_kg = float(r["qty"] or 0)
        base_kg = base_lookup.get(cat, 0.0)
        delta_pct = ((today_kg - base_kg) / base_kg * 100) if base_kg > 0 else None
        out.append({
            "category2": cat,
            "today_kg": today_kg,
            "baseline_kg": base_kg,
            "today_share_pct": today_kg / today_total * 100,
            "baseline_share_pct": (base_kg / base_total * 100) if base_total > 0 else 0,
            "delta_pct": delta_pct,
        })
    # 비중 큰 순
    out.sort(key=lambda x: x["today_kg"], reverse=True)
    return out


def _fetch_intensity_lines(factory: str, target: str) -> list[str]:
    """item_energy_impact_lookup 에서 해당 공장·항목의 카테고리별 계수 발췌."""
    if not _HAS_IMPACT:
        return []
    try:
        status = lookup_status()
        if not status.get("available"):
            return []
        # 광주 등 외주 비중 큰 공장은 계수 해석에 주의 → 그대로 노출하되 노트에서 환기
        try:
            rows = get_intensity_table(factory)
        except LookupNotAvailable:
            return []
        if not rows:
            return []
        out: list[str] = []
        for r in rows:
            if r.get("target") != target:
                continue
            line = (
                f"{r['category1']}/{r.get('category2') or '-'}: "
                f"{r['intensity_per_kg']:.4f} {r['unit']}/kg "
                f"(CI [{r['ci_lo']:.4f}, {r['ci_hi']:.4f}], "
                f"비중 {r['share_of_kg']*100:.0f}%)"
            )
            out.append(line)
        return out[:8]  # 너무 많으면 잘라냄
    except Exception:
        return []


def build_anomaly_context(
    factory: str, pred_date: str, target: str
) -> Optional[AnomalyContext]:
    """진단에 필요한 모든 컨텍스트를 모아 AnomalyContext 로 반환."""
    event = _fetch_anomaly_event(factory, pred_date, target)
    if not event:
        return None

    pred_v = float(event["pred_value"])
    actual_v = float(event["actual_value"]) if event["actual_value"] is not None else 0.0
    mape_v = float(event["mape"]) if event["mape"] is not None else 0.0
    residual = actual_v - pred_v
    residual_pct = (residual / actual_v * 100) if actual_v else 0.0

    # v5.2 밴드 정보 — 없으면 None (v5.1 이력)
    p05_raw = event.get("pred_p05")
    p95_raw = event.get("pred_p95")
    p05_v = float(p05_raw) if p05_raw is not None else None
    p95_v = float(p95_raw) if p95_raw is not None else None
    bs_raw = event.get("band_status")
    band_status = bs_raw if isinstance(bs_raw, str) else None
    bp_raw = event.get("band_position")
    band_position = float(bp_raw) if bp_raw is not None else None
    # band_status가 비어 있고 밴드가 있으면 즉석 계산 (역채움 안 된 행 대응)
    if band_status is None and p05_v is not None and p95_v is not None and actual_v:
        band_status, band_position = classify_band(actual_v, p05_v, pred_v, p95_v)

    ctx = AnomalyContext(
        factory=factory,
        pred_date=pred_date,
        target=target,
        pred_value=pred_v,
        actual_value=actual_v,
        mape=mape_v,
        residual_signed=residual,
        residual_pct_signed=residual_pct,
        pred_p05=p05_v,
        pred_p95=p95_v,
        band_status=band_status,
        band_position=band_position,
    )
    ctx.baseline = _fetch_baseline(factory, pred_date, target)
    ctx.energy_raw = _fetch_energy_raw(factory, pred_date, target)
    ctx.prod_breakdown = _fetch_prod_breakdown(factory, pred_date)
    ctx.intensity_lines = _fetch_intensity_lines(factory, target)

    # z-score 기반 입력 변수 이상치 (v5.2 보완 — 모델 입력 중 "오늘 평소와 달랐던 항목")
    if _HAS_VARIABLE_ANOMALY:
        try:
            pred_d_obj = datetime.strptime(pred_date, "%Y-%m-%d").date()
            findings = detect_anomalous_inputs(
                factory=factory,
                target_date=pred_d_obj,
                top_n=5,
            )
            ctx.anomalous_inputs = [f.to_dict() for f in findings]
        except Exception as e:
            ctx.notes.append(f"입력 변수 이상치 탐지 실패 (무시): {e}")

    if not ctx.prod_breakdown:
        ctx.notes.append(
            "당일 production_daily 데이터가 없습니다 — 카테고리 비중 가설은 사용 불가, "
            "기저/원단위/전일 대비 변화 위주로 분석하십시오."
        )
    if not ctx.intensity_lines:
        ctx.notes.append("회귀 계수 룩업 미존재 — What-if 정량 추정 가설 신뢰도는 낮습니다.")

    return ctx


# ──────────────────────────────────────
# LLM 호출
# ──────────────────────────────────────

def _call_llm(system_prompt: str, user_input: str) -> tuple[str, str]:
    """LLM 호출. (응답 텍스트, 사용 모델명) 반환. 실패 시 RuntimeError."""
    if OpenAI is None:
        raise RuntimeError("openai 패키지가 설치되지 않았습니다.")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your_openai_api_key_here":
        raise RuntimeError("OPENAI_API_KEY 환경 변수가 설정되지 않았습니다.")

    client = OpenAI(api_key=api_key, http_client=httpx.Client(verify=httpx_verify()))
    response = client.responses.create(
        model=LLM_MODEL,
        instructions=system_prompt,
        input=user_input,
    )
    return response.output_text, LLM_MODEL


# ──────────────────────────────────────
# 공개 API
# ──────────────────────────────────────

def get_or_create_diagnosis(
    factory: str,
    pred_date: date | str,
    target: str,
    force_refresh: bool = False,
) -> dict:
    """이상 이벤트 진단 — 캐시 우선, 미스 시 LLM 호출.

    반환:
        {
            "diagnosis": str (마크다운),
            "from_cache": bool,
            "model_used": str | None,
            "created_at": datetime | None,
            "updated_at": datetime | None,
            "error": str | None,
        }
    """
    pd_str = pred_date.isoformat() if isinstance(pred_date, date) else str(pred_date)

    if not force_refresh:
        cached = get_cached_diagnosis(factory, pd_str, target)
        if cached and cached.get("diagnosis"):
            return {
                "diagnosis": cached["diagnosis"],
                "from_cache": True,
                "model_used": cached.get("model_used"),
                "created_at": cached.get("created_at"),
                "updated_at": cached.get("updated_at"),
                "error": None,
            }

    # 컨텍스트 수집
    ctx = build_anomaly_context(factory, pd_str, target)
    if ctx is None:
        return {
            "diagnosis": "",
            "from_cache": False,
            "model_used": None,
            "created_at": None,
            "updated_at": None,
            "error": (
                f"prediction_log 에 해당 이벤트가 없습니다: "
                f"{factory} / {pd_str} / {target}"
            ),
        }

    # 프롬프트 로드
    prompt_path = _resolve_prompt_path()
    try:
        system_prompt = prompt_path.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "diagnosis": "",
            "from_cache": False,
            "model_used": None,
            "created_at": None,
            "updated_at": None,
            "error": f"프롬프트 로드 실패 ({prompt_path}): {e}",
        }

    user_input = (
        "다음은 이상 이벤트와 관련된 모든 컨텍스트입니다. "
        "지정된 출력 형식에 맞춰 진단해주세요.\n\n"
        + ctx.to_prompt_text()
    )

    # LLM 호출
    try:
        diagnosis_text, model_used = _call_llm(system_prompt, user_input)
    except Exception as e:
        return {
            "diagnosis": "",
            "from_cache": False,
            "model_used": None,
            "created_at": None,
            "updated_at": None,
            "error": f"LLM 호출 실패: {e}",
        }

    if not diagnosis_text or not diagnosis_text.strip():
        return {
            "diagnosis": "",
            "from_cache": False,
            "model_used": model_used,
            "created_at": None,
            "updated_at": None,
            "error": "LLM 응답이 비어있습니다.",
        }

    # 캐시 저장 (실패해도 결과는 반환)
    try:
        _save_diagnosis(
            factory=ctx.factory,
            pred_date=ctx.pred_date,
            target=ctx.target,
            pred_value=ctx.pred_value,
            actual_value=ctx.actual_value,
            mape=ctx.mape,
            diagnosis=diagnosis_text,
            context_snapshot=ctx.to_prompt_text(),
            model_used=model_used,
            pred_p05=ctx.pred_p05,
            pred_p95=ctx.pred_p95,
            band_status=ctx.band_status,
            band_position=ctx.band_position,
        )
    except Exception as e:
        print(f"[anomaly_diagnosis] 캐시 저장 실패 (무시): {e}")

    return {
        "diagnosis": diagnosis_text,
        "from_cache": False,
        "model_used": model_used,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
        "error": None,
    }


def delete_cached_diagnosis(
    factory: str, pred_date: date | str, target: str
) -> bool:
    """관리자 — 잘못된 진단 캐시 삭제."""
    _ensure_table()
    pd_str = pred_date.isoformat() if isinstance(pred_date, date) else str(pred_date)
    try:
        execute_write(
            "DELETE FROM anomaly_analysis "
            "WHERE factory=%s AND pred_date=%s AND target=%s",
            (factory, pd_str, target),
        )
        return True
    except Exception as e:
        print(f"[anomaly_diagnosis] 삭제 실패: {e}")
        return False
