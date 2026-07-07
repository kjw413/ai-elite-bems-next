# 이 파일은 개선안 §10.5(공장-대상별 calibration layer)를 구현합니다.
#
# 목적: 모델 재학습 없이, 최근 운영 이력(prediction_log)의 P95 초과율을 기준으로
# 공장-대상별 정상범위 밴드 상·하한을 사후 보정한다. CQR(§10.4)이 학습 검증구간
# 잔차로 보정하는 반면, 이 레이어는 "현재 운영분포"에서 상단 coverage 가 목표보다
# 나쁜 항목(김해 연료 16.8% 등)만 선택적으로 넓혀 과사용 오탐/미탐을 줄인다.
#
# 저장 위치: TRAINED_MODEL_DIR / "v5_band_calibration.json"
# 적용 지점: usage_prediction_v5_service._infer_target (CQR 적용 직후, 단조성/밴드분류 전)
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.services.v5_common import (
    TARGET_SPECS,
    TRAINED_MODEL_DIR,
    _json_safe,
    _write_json_atomic,
    read_json,
)

logger = logging.getLogger(__name__)

CALIBRATION_PATH: Path = TRAINED_MODEL_DIR / "v5_band_calibration.json"

# 목표 P95 초과율(중심 90% 밴드의 상단 tail). 개선안 §10.6 게이트(≤8%)보다 보수적인 5%.
DEFAULT_TARGET_P95_EXCEED_RATE = 0.05
# 보정 산출 기준 최근 창(일). 개선안 §10.5 권장 60~90일.
DEFAULT_WINDOW_DAYS = 90
# 보정을 산출할 최소 관측 표본 수(너무 적으면 잡음 → 보정 보류).
DEFAULT_MIN_SAMPLES = 20

# calibration 항목 기본값 = 항등(원 밴드 그대로).
_IDENTITY_ENTRY: dict[str, float] = {
    "upper_factor": 1.0,
    "upper_margin": 0.0,
    "lower_factor": 1.0,
    "lower_margin": 0.0,
}


# ---------------------------------------------------------------------------
# 로드 / 조회
# ---------------------------------------------------------------------------
def load_band_calibration(path: Path | None = None) -> dict[str, Any]:
    """calibration JSON 을 읽어 반환한다(없으면 빈 구조).

    구조:
        {
          "schema_version": 1,
          "updated_at": "...ISO...",
          "target_p95_exceed_rate": 0.05,
          "window_days": 90,
          "calibration": {"<공장>": {"<대상>": {upper_factor, upper_margin, ...}}}
        }
    """
    p = path or CALIBRATION_PATH
    data = read_json(p, {})
    if not isinstance(data, dict):
        return {"calibration": {}}
    data.setdefault("calibration", {})
    return data


# mtime 기반 로드 캐시 — 배치 예측(다수 날짜×대상) 루프에서 JSON 재읽기 회피.
_CALIB_CACHE: dict[str, Any] = {"mtime_ns": None, "data": None}


def load_band_calibration_cached(path: Path | None = None) -> dict[str, Any]:
    """파일 mtime 이 바뀌지 않았으면 캐시된 calibration 을 반환한다."""
    p = path or CALIBRATION_PATH
    try:
        mtime_ns = p.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    if _CALIB_CACHE["data"] is not None and _CALIB_CACHE["mtime_ns"] == mtime_ns:
        return _CALIB_CACHE["data"]
    data = load_band_calibration(p)
    _CALIB_CACHE["mtime_ns"] = mtime_ns
    _CALIB_CACHE["data"] = data
    return data


def get_calibration_entry(
    factory: str,
    target: str,
    *,
    calibration: dict[str, Any] | None = None,
) -> dict[str, float] | None:
    """(factory, target) 보정 항목을 반환. 없으면 None(=항등)."""
    table = (calibration if calibration is not None else load_band_calibration_cached()).get("calibration") or {}
    entry = (table.get(str(factory)) or {}).get(str(target))
    if not isinstance(entry, dict):
        return None
    merged = dict(_IDENTITY_ENTRY)
    for k in _IDENTITY_ENTRY:
        if k in entry:
            try:
                merged[k] = float(entry[k])
            except (TypeError, ValueError):
                pass
    return merged


# ---------------------------------------------------------------------------
# 적용
# ---------------------------------------------------------------------------
def apply_band_calibration(
    factory: str,
    target: str,
    p05: float,
    p50: float,
    p95: float,
    *,
    calibration: dict[str, Any] | None = None,
) -> tuple[float, float, dict[str, Any] | None]:
    """공장-대상별 상·하한 보정을 적용해 (p05, p95, info) 를 반환.

    보정식(P50 을 축으로 반폭을 스케일 후 가산):
        p95' = p50 + (p95 - p50) * upper_factor + upper_margin
        p05' = p50 - (p50 - p05) * lower_factor - lower_margin

    - 항목이 없거나 항등이면 원값을 그대로 돌려주고 info=None.
    - 안전장치: 보정 후에도 p05 <= p50 <= p95 를 보장(밴드 뒤집힘 방지).
    """
    entry = get_calibration_entry(factory, target, calibration=calibration)
    if entry is None:
        return p05, p95, None

    uf, um = entry["upper_factor"], entry["upper_margin"]
    lf, lm = entry["lower_factor"], entry["lower_margin"]
    if uf == 1.0 and um == 0.0 and lf == 1.0 and lm == 0.0:
        return p05, p95, None

    half_up = max(p95 - p50, 0.0)
    half_lo = max(p50 - p05, 0.0)
    p95_cal = p50 + half_up * uf + um
    p05_cal = p50 - half_lo * lf - lm

    # 밴드가 P50 을 넘어 뒤집히지 않도록 클램프.
    p95_cal = max(p95_cal, p50)
    p05_cal = min(p05_cal, p50)

    info = {
        "upper_factor": uf,
        "upper_margin": um,
        "lower_factor": lf,
        "lower_margin": lm,
        "p95_before": float(p95),
        "p95_after": float(p95_cal),
        "p05_before": float(p05),
        "p05_after": float(p05_cal),
    }
    return float(p05_cal), float(p95_cal), info


# ---------------------------------------------------------------------------
# 운영 이력 기반 보정폭 산출
# ---------------------------------------------------------------------------
def compute_calibration_from_rows(
    rows: list[dict[str, Any]],
    *,
    target_rate: float = DEFAULT_TARGET_P95_EXCEED_RATE,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    allow_shrink: bool = False,
) -> dict[str, dict[str, dict[str, float]]]:
    """운영 이력 행에서 공장-대상별 상단 가산보정(upper_margin)을 산출.

    각 행 필요 키: factory, target, actual_value, pred_p95.
    (pred_p50 은 없어도 됨 — 상단 가산보정은 P95 잔차만으로 계산.)

    산출 원리(§10.5 = 운영구간 conformal 재보정):
        실측 초과잔차 resid = actual - p95.
        목표 초과율 target_rate 를 맞추려면 margin = resid 의 (1 - target_rate) 분위수.
        margin > 0 이면 상단이 좁아 넓혀야 함(초과율 과다), <= 0 이면 이미 충분.

    allow_shrink=False(기본): margin 을 0 미만으로 내리지 않는다(밴드 축소 → 미탐 위험 회피).
    반환: {factory: {target: {upper_margin, upper_factor=1.0, ...}}} (보정이 필요한 항목만).
    """
    grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for r in rows:
        fac = r.get("factory")
        tgt = r.get("target")
        actual = r.get("actual_value")
        p95 = r.get("pred_p95")
        if fac is None or tgt is None or actual is None or p95 is None:
            continue
        try:
            grouped.setdefault((str(fac), str(tgt)), []).append((float(actual), float(p95)))
        except (TypeError, ValueError):
            continue

    out: dict[str, dict[str, dict[str, float]]] = {}
    for (fac, tgt), pairs in grouped.items():
        if len(pairs) < min_samples:
            continue
        actual_arr = np.array([a for a, _ in pairs], dtype=float)
        p95_arr = np.array([p for _, p in pairs], dtype=float)
        resid = actual_arr - p95_arr
        observed_rate = float(np.mean(resid > 0.0))

        # 목표 초과율에 해당하는 상단 잔차 분위수 = 필요한 상향 margin.
        margin = float(np.quantile(resid, 1.0 - target_rate))
        if not allow_shrink:
            margin = max(margin, 0.0)

        # 항등(±0)에 가까우면 기록 생략.
        if abs(margin) < 1e-9:
            continue

        entry = dict(_IDENTITY_ENTRY)
        entry["upper_margin"] = margin
        entry["_observed_p95_exceed_rate"] = observed_rate  # 진단용(적용엔 무시)
        entry["_n_samples"] = float(len(pairs))
        out.setdefault(fac, {})[tgt] = entry

    return out


def build_calibration_payload(
    calibration: dict[str, dict[str, dict[str, float]]],
    *,
    target_rate: float = DEFAULT_TARGET_P95_EXCEED_RATE,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """calibration 테이블을 저장용 payload(메타 포함)로 감싼다."""
    return _json_safe(
        {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "target_p95_exceed_rate": float(target_rate),
            "window_days": int(window_days),
            "calibration": calibration,
        }
    )


def save_band_calibration(payload: dict[str, Any], path: Path | None = None) -> Path:
    """calibration payload 를 원자적으로 저장하고 경로를 반환."""
    p = path or CALIBRATION_PATH
    _write_json_atomic(p, _json_safe(payload))
    return p


def refresh_calibration_from_history(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    target_rate: float = DEFAULT_TARGET_P95_EXCEED_RATE,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    allow_shrink: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    """prediction_log 최근 window_days 를 읽어 calibration 을 재산출·저장한다.

    DB 접근은 지연 import(순환 회피). 실측/P95 가 채워진 v5.2+ 행만 사용.
    반환: 저장된 payload.
    """
    from app.database.db_connection import get_connection  # 지연 import

    targets = tuple(TARGET_SPECS.keys())
    rows: list[dict[str, Any]] = []
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT factory, target, actual_value, pred_p95
            FROM prediction_log
            WHERE actual_value IS NOT NULL
              AND pred_p95 IS NOT NULL
              AND pred_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            """,
            (int(window_days),),
        )
        for factory, target, actual_value, pred_p95 in cur.fetchall():
            if target not in targets:
                continue
            rows.append(
                {
                    "factory": factory,
                    "target": target,
                    "actual_value": actual_value,
                    "pred_p95": pred_p95,
                }
            )
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

    calibration = compute_calibration_from_rows(
        rows,
        target_rate=target_rate,
        min_samples=min_samples,
        allow_shrink=allow_shrink,
    )
    payload = build_calibration_payload(
        calibration, target_rate=target_rate, window_days=window_days
    )
    save_band_calibration(payload, path=path)
    logger.info(
        "[band_calibration] refreshed: %d factory-target entries from %d rows (window=%dd)",
        sum(len(v) for v in calibration.values()),
        len(rows),
        window_days,
    )
    return payload


# 운영자용 수동 재산출 진입점:
#   python -m app.services.v5_band_calibration [--window 90] [--rate 0.05] [--allow-shrink]
# prediction_log 최근 이력으로 v5_band_calibration.json 을 갱신하고 요약을 출력한다.
if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="공장-대상별 밴드 calibration 재산출 (§10.5)")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS, help="최근 이력 창(일)")
    parser.add_argument("--rate", type=float, default=DEFAULT_TARGET_P95_EXCEED_RATE, help="목표 P95 초과율")
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES, help="항목당 최소 표본")
    parser.add_argument("--allow-shrink", action="store_true", help="초과율이 낮으면 밴드 축소 허용")
    args = parser.parse_args()

    result = refresh_calibration_from_history(
        window_days=args.window,
        target_rate=args.rate,
        min_samples=args.min_samples,
        allow_shrink=args.allow_shrink,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
