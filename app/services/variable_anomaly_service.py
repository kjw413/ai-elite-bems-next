"""
Variable Anomaly Service
=========================
v5.2 분위수 회귀 모델의 보완 기능 — "어떤 입력 변수가 비정상인가" 탐지.

목적:
    실측이 정상범주(P05~P95)를 벗어난 이상일에 대해, 그 날의 입력 변수 중
    같은 공장 직전 영업일 분포에서 통계적으로 벗어난 항목을 자동 식별.
    SHAP 같은 인스턴스 단위 기여도 분해 없이도, 변수 단위의 "오늘 평소와 달랐던 것"을
    실무자/LLM에게 직관적으로 알려준다.

판정 정책 (변수 유형별):
    - 연속 정규: z-score 절댓값 ≥ Z_THRESHOLD (기본 2.0)
    - 우측 꼬리(강수량/HDD/CDD): 분위수(P95 초과 / 0이 아닌 P5 미만)
    - 이산 플래그(is_holiday 등): 평소 0이었는데 1이면 표시
    - 계절성/식별자(dow/month): 비교 의미 없음 → 제외

사용처:
    - dashboard_main.py 이상감지 상세 카드
    - anomaly_diagnosis_service.py LLM 컨텍스트 강화
    - ai_prediction.py 단일 예측 카드 (옵션)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.services.v5_common import (
    STATION_MAP,
    WEATHER_COLS,
    calendar_flags,
    fill_weather_gaps,
    is_workday,
    load_holidays_excel,
    load_weather_station_excel,
)
from app.services.v5_explainability import humanize_feature_name
from app.database.db_connection import get_connection


# 기본 임계값
Z_THRESHOLD: float = 2.0          # 연속 변수 |z| 임계
PCT_HIGH: float = 95.0            # 우측 꼬리 변수 상위 임계 백분위수
PCT_LOW: float = 5.0              # 우측 꼬리 변수 하위 임계 백분위수
BASELINE_BDAYS: int = 60          # 비교 기준 영업일 수
MIN_BASELINE_OBS: int = 15        # 최소 비교 표본 수

# 변수 분류
NUMERIC_FEATS: set[str] = {
    "log_mix_ton",
    "log_mix_ton_lag1",
    "log_mix_ton_r7mean",
    "lag1",
    "r7mean",
    "intensity_lag1",
    "평균기온",
    "평균기온_lag1",
    "평균기온_r7mean",
    "상대습도",
    "일사량",
    "일조시간",
    "THI",
    "dist_to_h",
    "dist_from_h",
    "consecutive_nonwork_before",
}
SKEWED_FEATS: set[str] = {"일강수량", "HDD", "CDD"}
FLAG_FEATS: set[str] = {
    "is_holiday", "is_pre_holiday", "is_post_holiday", "is_post_long_weekend"
}
SKIP_FEATS: set[str] = {"dow", "month"}


@dataclass
class VariableAnomaly:
    feature: str
    label: str
    value: float
    baseline_mean: float | None
    baseline_std: float | None
    z_score: float | None
    direction: str               # 'high' | 'low'
    severity: float              # |z| (연속) 또는 정규화 거리(꼬리) — 정렬용
    rule: str                    # 'zscore' | 'percentile' | 'flag'

    def to_summary_text(self) -> str:
        """LLM/사람용 한 줄 요약."""
        arrow = "↑↑" if self.direction == "high" else "↓↓"
        if self.rule == "zscore" and self.z_score is not None:
            return (f"{self.label}: 오늘 {self.value:,.1f} (평소 {self.baseline_mean:,.1f}±"
                    f"{self.baseline_std:,.1f}, z={self.z_score:+.1f}) {arrow}")
        if self.rule == "percentile":
            return f"{self.label}: 오늘 {self.value:,.1f} (평소 분포 {arrow}측 극단)"
        if self.rule == "flag":
            return f"{self.label}: 오늘 활성화됨 (평소엔 아님)"
        return f"{self.label}: 오늘 {self.value:,.1f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "label": self.label,
            "value": self.value,
            "baseline_mean": self.baseline_mean,
            "baseline_std": self.baseline_std,
            "z_score": self.z_score,
            "direction": self.direction,
            "severity": self.severity,
            "rule": self.rule,
        }


def _fetch_baseline_inputs(
    factory: str,
    target_date: date,
    bdays: int = BASELINE_BDAYS,
) -> pd.DataFrame:
    """target_date 직전 N영업일의 입력 변수 분포를 DB·기상·캘린더에서 합성.

    SHAP 모델 X와 똑같이 만들 필요는 없고, 사람이 해석 가능한 핵심 변수만 모음.
    """
    holiday_set = load_holidays_excel()

    # 충분한 영업일 확보를 위해 ~2배 윈도우 lookback
    hist_to = target_date - timedelta(days=1)
    hist_from = target_date - timedelta(days=int(bdays * 2.5) + 14)

    query = """
        SELECT date, mix_prod_kg, total_power_kwh, fuel_nm3, water_ton
        FROM energy_daily
        WHERE factory=%s AND date>=%s AND date<=%s
        ORDER BY date
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            query, conn,
            params=(factory, hist_from.strftime("%Y-%m-%d"), hist_to.strftime("%Y-%m-%d")),
        )
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    # 영업일 필터
    df = df[df["date"].apply(lambda ts: is_workday(pd.Timestamp(ts), holiday_set))].copy()
    if df.empty:
        return df

    # 최근 bdays개만 사용
    df = df.tail(bdays).reset_index(drop=True)

    # 기상 병합
    station = STATION_MAP.get(factory)
    if station:
        df_w = load_weather_station_excel(station)
        if not df_w.empty:
            df_w = df_w.copy()
            df_w["date"] = pd.to_datetime(df_w["날짜"], errors="coerce")
            df = df.merge(
                df_w.drop(columns=["날짜"]),
                on="date", how="left",
            )
    for c in WEATHER_COLS:
        if c not in df.columns:
            df[c] = np.nan
    # 보간(예측 경로와 동일 정책)
    df_renamed = df.rename(columns={"date": "날짜"})
    df_renamed = fill_weather_gaps(df_renamed, WEATHER_COLS)
    df = df_renamed.rename(columns={"날짜": "date"})

    # 파생
    df["mix_ton"] = pd.to_numeric(df["mix_prod_kg"], errors="coerce").fillna(0.0) / 1000.0
    df["log_mix_ton"] = np.log1p(df["mix_ton"])
    df["HDD"] = np.maximum(18.0 - df["평균기온"], 0.0)
    df["CDD"] = np.maximum(df["평균기온"] - 22.0, 0.0)
    df["THI"] = 0.72 * (df["평균기온"] + df["상대습도"]) + 40.6

    # 캘린더 플래그
    for col in ["is_holiday", "is_pre_holiday", "is_post_holiday",
                "is_post_long_weekend", "consecutive_nonwork_before",
                "dist_to_h", "dist_from_h"]:
        df[col] = 0
    h_set = holiday_set or set()
    sorted_h = sorted(h_set)
    for idx, ts in enumerate(df["date"]):
        flags = calendar_flags(ts, h_set)
        for k, v in flags.items():
            if k in df.columns:
                df.at[idx, k] = int(v)
        # dist_to_h / dist_from_h
        d0 = pd.Timestamp(ts).date()
        future_h = [h for h in sorted_h if h > d0]
        past_h = [h for h in sorted_h if h < d0]
        df.at[idx, "dist_to_h"] = (future_h[0] - d0).days if future_h else 30
        df.at[idx, "dist_from_h"] = (d0 - past_h[-1]).days if past_h else 30

    return df


def _build_today_inputs(
    factory: str,
    target_date: date,
    mix_prod_kg: float | None = None,
) -> dict[str, float]:
    """target_date 당일의 입력 변수 벡터를 만든다. mix_prod_kg가 주어지지 않으면 DB 조회."""
    out: dict[str, float] = {}

    # 생산량
    if mix_prod_kg is None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT mix_prod_kg FROM energy_daily WHERE factory=%s AND date=%s LIMIT 1",
                (factory, target_date.strftime("%Y-%m-%d")),
            )
            r = cur.fetchone()
            cur.close()
            mix_prod_kg = float(r[0]) if r and r[0] is not None else 0.0
        finally:
            conn.close()

    mix_ton = float(mix_prod_kg or 0.0) / 1000.0
    out["log_mix_ton"] = float(np.log1p(mix_ton))

    # 날씨
    station = STATION_MAP.get(factory)
    if station:
        df_w = load_weather_station_excel(station)
        if not df_w.empty:
            row = df_w[df_w["날짜"] == pd.Timestamp(target_date)]
            if not row.empty:
                r0 = row.iloc[0]
                for c in WEATHER_COLS:
                    v = r0.get(c)
                    out[c] = float(v) if v is not None and not pd.isna(v) else np.nan

    if "평균기온" in out:
        t = out["평균기온"]
        if not np.isnan(t):
            out["HDD"] = float(max(18.0 - t, 0.0))
            out["CDD"] = float(max(t - 22.0, 0.0))
            if "상대습도" in out and not np.isnan(out["상대습도"]):
                out["THI"] = float(0.72 * (t + out["상대습도"]) + 40.6)

    # 캘린더 플래그
    holiday_set = load_holidays_excel() or set()
    flags = calendar_flags(target_date, holiday_set)
    for k, v in flags.items():
        out[k] = float(v)
    sorted_h = sorted(holiday_set)
    d0 = target_date
    future_h = [h for h in sorted_h if h > d0]
    past_h = [h for h in sorted_h if h < d0]
    out["dist_to_h"] = float((future_h[0] - d0).days) if future_h else 30.0
    out["dist_from_h"] = float((d0 - past_h[-1]).days) if past_h else 30.0

    return out


def detect_anomalous_inputs(
    factory: str,
    target_date: date,
    mix_prod_kg: float | None = None,
    z_threshold: float = Z_THRESHOLD,
    bdays: int = BASELINE_BDAYS,
    top_n: int | None = 5,
) -> list[VariableAnomaly]:
    """target_date의 입력 변수 중 같은 공장 최근 N영업일 분포와 다른 항목을 반환.

    Parameters
    ----------
    factory : str
        실공장명(남양주1/남양주2/김해/광주/논산). 집계 공장은 별도 처리 필요.
    target_date : date
        이상일.
    mix_prod_kg : float | None
        당일 생산량. None이면 DB 조회.
    z_threshold : float
        연속 변수 |z| 임계 (기본 2.0).
    bdays : int
        비교 기준 영업일 수 (기본 60).
    top_n : int | None
        상위 N개만 반환 (None이면 전체).

    Returns
    -------
    list[VariableAnomaly]
        severity 내림차순 정렬. 없으면 빈 리스트.
    """
    baseline = _fetch_baseline_inputs(factory, target_date, bdays=bdays)
    if baseline.empty or len(baseline) < MIN_BASELINE_OBS:
        return []
    today = _build_today_inputs(factory, target_date, mix_prod_kg=mix_prod_kg)
    if not today:
        return []

    findings: list[VariableAnomaly] = []

    for feat, val in today.items():
        if feat in SKIP_FEATS:
            continue
        if feat not in baseline.columns:
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if np.isnan(v):
            continue

        series = pd.to_numeric(baseline[feat], errors="coerce").dropna()
        if len(series) < MIN_BASELINE_OBS:
            continue

        if feat in FLAG_FEATS:
            base_rate = float(series.mean())
            # 평소 거의 0(<5%)이었는데 오늘 1이면 표시
            if v >= 0.5 and base_rate < 0.05:
                findings.append(VariableAnomaly(
                    feature=feat,
                    label=humanize_feature_name(feat),
                    value=v,
                    baseline_mean=base_rate,
                    baseline_std=None,
                    z_score=None,
                    direction="high",
                    severity=1.0 - base_rate,  # 드물수록 큼
                    rule="flag",
                ))
            continue

        if feat in SKEWED_FEATS:
            p_high = float(np.percentile(series, PCT_HIGH))
            p_low = float(np.percentile(series, PCT_LOW))
            # 우측 꼬리: 상위 P95 초과가 핵심. 0 빈도가 높은 분포는 하위 무시.
            if v > p_high:
                findings.append(VariableAnomaly(
                    feature=feat,
                    label=humanize_feature_name(feat),
                    value=v,
                    baseline_mean=float(series.mean()),
                    baseline_std=float(series.std()),
                    z_score=None,
                    direction="high",
                    severity=(v - p_high) / max(abs(p_high), 1.0),
                    rule="percentile",
                ))
            elif p_low > 0 and v < p_low:
                findings.append(VariableAnomaly(
                    feature=feat,
                    label=humanize_feature_name(feat),
                    value=v,
                    baseline_mean=float(series.mean()),
                    baseline_std=float(series.std()),
                    z_score=None,
                    direction="low",
                    severity=(p_low - v) / max(abs(p_low), 1.0),
                    rule="percentile",
                ))
            continue

        # 기본: NUMERIC_FEATS — z-score
        if feat not in NUMERIC_FEATS:
            continue
        mu = float(series.mean())
        sigma = float(series.std())
        if sigma < 1e-6:
            continue
        z = (v - mu) / sigma
        if abs(z) >= z_threshold:
            findings.append(VariableAnomaly(
                feature=feat,
                label=humanize_feature_name(feat),
                value=v,
                baseline_mean=mu,
                baseline_std=sigma,
                z_score=float(z),
                direction="high" if z > 0 else "low",
                severity=float(abs(z)),
                rule="zscore",
            ))

    findings.sort(key=lambda x: x.severity, reverse=True)
    if top_n is not None and top_n > 0:
        findings = findings[:top_n]
    return findings


def detect_anomalous_inputs_for_aggregate(
    aggregate_factory: str,
    target_date: date,
    z_threshold: float = Z_THRESHOLD,
    bdays: int = BASELINE_BDAYS,
    top_n: int | None = 5,
) -> dict[str, list[VariableAnomaly]]:
    """집계 공장(남양주/전사)의 경우 구성 공장별로 따로 탐지하여 dict로 반환."""
    from app.services.v5_common import AGGREGATE_FACTORY_MEMBERS
    members = AGGREGATE_FACTORY_MEMBERS.get(aggregate_factory, ())
    out: dict[str, list[VariableAnomaly]] = {}
    for m in members:
        out[m] = detect_anomalous_inputs(
            factory=m,
            target_date=target_date,
            z_threshold=z_threshold,
            bdays=bdays,
            top_n=top_n,
        )
    return out


def summarize_anomalies_korean(findings: list[VariableAnomaly]) -> str:
    """Top findings를 한 줄 자연어 문장으로 요약 (LLM/배너용)."""
    if not findings:
        return "이날 입력 변수 중 평소와 크게 다른 항목은 감지되지 않았습니다."
    pieces = []
    for f in findings[:3]:
        arrow = "↑" if f.direction == "high" else "↓"
        pieces.append(f"{f.label}({arrow})")
    return "오늘 평소와 달랐던 입력 변수: " + ", ".join(pieces)
