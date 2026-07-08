# 이 파일은 정상범주(P05~P95) 이탈에 대한 "판정 규칙" 엔진입니다.
#
# 배경: P05~P95 는 90% 구간이므로, 모델이 완벽히 보정돼 있어도 정상 상태에서
# 하루 단위 판정의 10%는 밴드를 벗어납니다. 5공장 × 3타겟 × 7일 = 105건 판정이면
# 기대 이탈만 ~10건 — 단일점 이탈을 그대로 경보로 쓰면 배너가 항상 빨간불이 되어
# 알람 피로로 아무도 안 보게 됩니다. 그래서 SPC run rule 의 취지를 빌려 2단계로
# 나눕니다:
#
#   주의(watch): 단일일 밴드 이탈 — 통계적 우연일 수 있음. 차트 마커/상세표에만 표시.
#   경보(alert): 우연으로 보기 어려운 패턴 —
#     R1 연속이탈: 같은 방향 이탈이 연속 2영업일 이상
#     R2 빈발이탈: 최근 7일(조회창) 안에 같은 방향 이탈이 3회 이상
#   지속편향(drift): 밴드 안이라도 표준화 잔차(band_position)가 한쪽으로 치우친
#     상태가 누적되면 CUSUM 으로 감지 — 김해 연료·남양주2 전력처럼 "매일 조금씩
#     계속 높은" 유형은 단일점 판정으로는 못 잡거나 늦게 잡는 이상입니다.
#
# CUSUM 파라미터 근거:
#   band_position = (실측 − P50) / (밴드반폭),  밴드반폭 = (P95−P05)/2 ≈ 1.645σ
#   → band_position 1.0 ≈ 1.645σ, 즉 σ ≈ 0.61 (band_position 단위)
#   표준 CUSUM 권장값 K=0.5σ, H=4~5σ 를 이 단위로 환산:
#   K ≈ 0.30, H ≈ 2.5 (≈4σ). 최소 표본 5점 미만이면 판정 보류.
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

# ── 판정 규칙 상수 ──
SEVERITY_ALERT = "alert"    # 경보 — run rule 확인된 이탈
SEVERITY_WATCH = "watch"    # 주의 — 단일일 이탈(우연 가능)

CONSECUTIVE_RUN_MIN = 2     # R1: 같은 방향 연속 이탈 최소 길이 (영업일 기준)
FREQUENT_COUNT_MIN = 3      # R2: 조회창 내 같은 방향 이탈 최소 횟수

CUSUM_K = 0.30              # 허용 슬랙(≈0.5σ, band_position 단위)
CUSUM_H = 2.5               # 경보 임계(≈4σ, band_position 단위)
CUSUM_MIN_OBS = 5           # CUSUM 판정 최소 표본 수
DRIFT_MIN_BIAS_PCT = 2.0    # 실질성 하한 — 평균 편향이 이 % 미만인 drift 는 보고하지 않음
                            # (통계적으로는 유의해도 에너지 관리 관점에서 조치 불가능한 수준)

RULE_CONSECUTIVE = "연속이탈"
RULE_FREQUENT = "빈발이탈"

_OUT_STATUSES = ("over", "under")


def _series_sorted(group: pd.DataFrame) -> pd.DataFrame:
    g = group.copy()
    g["pred_date"] = pd.to_datetime(g["pred_date"], errors="coerce")
    return g.dropna(subset=["pred_date"]).sort_values("pred_date").reset_index(drop=True)


# ──────────────────────────────────────
# CUSUM drift 감지 (단일 시계열)
# ──────────────────────────────────────

def detect_drift(
    group: pd.DataFrame,
    k: float = CUSUM_K,
    h: float = CUSUM_H,
    min_obs: int = CUSUM_MIN_OBS,
    min_bias_pct: float = DRIFT_MIN_BIAS_PCT,
) -> dict[str, Any] | None:
    """단일 (공장, 타겟) 시계열의 지속 편향(drift)을 one-sided CUSUM 으로 감지.

    입력: pred_date / band_position (+ 있으면 actual_value / pred_value) 컬럼.
    band_position 이 NaN 인 행(v5.1 legacy)은 건너뜀.

    Returns (drift 활성 시): {
        direction: 'over'|'under', start_date: date, days: int,
        mean_bias_pct: float|None, cusum: float,
    }
    drift 가 아니면 None. "활성" = 마지막 관측 시점에 누적합이 임계 이상.
    평균 편향이 min_bias_pct 미만이면(계산 가능한 경우) 실질성이 없다고 보고 None.
    """
    if group is None or group.empty or "band_position" not in group.columns:
        return None
    g = _series_sorted(group)
    g["band_position"] = pd.to_numeric(g["band_position"], errors="coerce")
    g = g.dropna(subset=["band_position"]).reset_index(drop=True)
    if len(g) < min_obs:
        return None

    s_hi = 0.0  # 과사용 방향 누적
    s_lo = 0.0  # 저사용 방향 누적
    hi_reset = -1  # 누적이 마지막으로 0으로 리셋된 인덱스
    lo_reset = -1
    for i, z in enumerate(g["band_position"]):
        s_hi = max(0.0, s_hi + (z - k))
        s_lo = max(0.0, s_lo + (-z - k))
        if s_hi == 0.0:
            hi_reset = i
        if s_lo == 0.0:
            lo_reset = i

    if s_hi >= h:
        direction, cusum, start_idx = "over", s_hi, hi_reset + 1
    elif s_lo >= h:
        direction, cusum, start_idx = "under", s_lo, lo_reset + 1
    else:
        return None

    start_idx = min(max(start_idx, 0), len(g) - 1)
    drift_rows = g.iloc[start_idx:]

    # 기간 평균 편향(%) — 실측/예측 컬럼이 있으면 계산
    mean_bias_pct: float | None = None
    if {"actual_value", "pred_value"}.issubset(drift_rows.columns):
        valid = drift_rows.dropna(subset=["actual_value", "pred_value"])
        valid = valid[pd.to_numeric(valid["pred_value"], errors="coerce") > 0]
        if not valid.empty:
            bias = (
                (valid["actual_value"].astype(float) - valid["pred_value"].astype(float))
                / valid["pred_value"].astype(float)
            )
            mean_bias_pct = float(bias.mean() * 100.0)

    # 실질성 하한 — 통계적으로만 유의한 미세 편향(예: -0.7%)은 노이즈로 간주
    if mean_bias_pct is not None and abs(mean_bias_pct) < min_bias_pct:
        return None

    return {
        "direction": direction,
        "start_date": drift_rows["pred_date"].iloc[0].date(),
        "days": int(len(drift_rows)),
        "mean_bias_pct": mean_bias_pct,
        "cusum": float(cusum),
    }


# ──────────────────────────────────────
# run rule (R1 연속 / R2 빈발) — 행 단위 경보 판정
# ──────────────────────────────────────

def _flag_rows_for_series(
    g: pd.DataFrame,
    recent_from: date,
) -> dict[pd.Timestamp, list[str]]:
    """단일 시계열에서 이탈 행별로 통과한 run rule 목록을 계산.

    반환: {pred_date(Timestamp): [규칙명,...]} — 이탈 행만 키로 포함(규칙 미통과면 빈 리스트).
    R1 연속 판정은 전체 조회창(30일)에서 계산해 창 경계에 걸친 run 도 인지하고,
    반환 키는 recent_from 이후 행으로 한정합니다(배너/상세표 노출 범위와 일치).
    """
    statuses = g["band_status"].astype(str).tolist()
    out_flags: dict[pd.Timestamp, list[str]] = {}

    # R1: 같은 방향 연속 run 탐색 (행 순서 = 인접 영업일)
    run_start = 0
    n = len(g)
    for i in range(n + 1):
        boundary = (
            i == n
            or statuses[i] not in _OUT_STATUSES
            or (i > run_start and statuses[i] != statuses[run_start])
        )
        if not boundary:
            continue
        run_len = i - run_start
        if run_len >= CONSECUTIVE_RUN_MIN and statuses[run_start] in _OUT_STATUSES:
            for j in range(run_start, i):
                d = g["pred_date"].iloc[j]
                out_flags.setdefault(d, []).append(f"{RULE_CONSECUTIVE} {run_len}일")
        run_start = i

    # R2: 최근 창 내 같은 방향 이탈 빈도
    recent = g[g["pred_date"].dt.date >= recent_from]
    for direction in _OUT_STATUSES:
        hits = recent[recent["band_status"].astype(str) == direction]
        if len(hits) >= FREQUENT_COUNT_MIN:
            for d in hits["pred_date"]:
                out_flags.setdefault(d, []).append(f"{RULE_FREQUENT} {len(hits)}회")

    # 이탈인데 아무 규칙도 통과 못 한 행 → 빈 리스트(주의)
    for i in range(n):
        if statuses[i] in _OUT_STATUSES:
            out_flags.setdefault(g["pred_date"].iloc[i], [])

    # 노출 범위 한정
    return {d: rules for d, rules in out_flags.items() if d.date() >= recent_from}


def evaluate_band_rules(
    df: pd.DataFrame,
    base_date: date,
    recent_days: int = 7,
) -> dict[str, Any]:
    """전 공장×타겟 시계열에 run rule + CUSUM 을 적용해 판정 결과를 반환.

    Parameters
    ----------
    df : prediction_log 시계열 (factory/target/pred_date/band_status/band_position
         [/actual_value/pred_value]). 조회창은 recent_days 보다 길게(예: 30일) 주면
         연속 run 의 창 경계 인식과 CUSUM 안정성이 좋아집니다.
    base_date : 판정 기준일 (조회창 끝일)
    recent_days : 배너/상세표 노출 창 — 이 안의 이탈 행만 severity 를 부여.

    Returns
    -------
    {
      "row_flags": DataFrame[factory,target,pred_date,severity,rules],
      "drift_signals": [ {factory,target,direction,start_date,days,mean_bias_pct,cusum}, ... ],
      "n_alert": int, "n_watch": int,
    }
    """
    empty = {
        "row_flags": pd.DataFrame(
            columns=["factory", "target", "pred_date", "severity", "rules"]
        ),
        "drift_signals": [],
        "n_alert": 0,
        "n_watch": 0,
    }
    required = {"factory", "target", "pred_date", "band_status"}
    if df is None or df.empty or not required.issubset(df.columns):
        return empty

    recent_from = base_date - timedelta(days=recent_days - 1)
    flag_rows: list[dict[str, Any]] = []
    drift_signals: list[dict[str, Any]] = []

    for (factory, target), group in df.groupby(["factory", "target"], sort=True):
        g = _series_sorted(group)
        if g.empty:
            continue

        for d, rules in sorted(_flag_rows_for_series(g, recent_from).items()):
            flag_rows.append(
                {
                    "factory": str(factory),
                    "target": str(target),
                    "pred_date": d,
                    "severity": SEVERITY_ALERT if rules else SEVERITY_WATCH,
                    "rules": " · ".join(rules),
                }
            )

        drift = detect_drift(g)
        if drift is not None:
            drift_signals.append({"factory": str(factory), "target": str(target), **drift})

    row_flags = (
        pd.DataFrame(flag_rows)
        if flag_rows
        else empty["row_flags"]
    )
    n_alert = int((row_flags["severity"] == SEVERITY_ALERT).sum()) if not row_flags.empty else 0
    n_watch = int((row_flags["severity"] == SEVERITY_WATCH).sum()) if not row_flags.empty else 0

    return {
        "row_flags": row_flags,
        "drift_signals": drift_signals,
        "n_alert": n_alert,
        "n_watch": n_watch,
    }
