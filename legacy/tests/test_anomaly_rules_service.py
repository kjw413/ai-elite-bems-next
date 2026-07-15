from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.anomaly_rules_service import (
    CUSUM_H,
    RULE_CONSECUTIVE,
    RULE_FREQUENT,
    SEVERITY_ALERT,
    SEVERITY_WATCH,
    detect_drift,
    evaluate_band_rules,
)

BASE_DATE = date(2026, 6, 30)


def _series_frame(
    statuses: list[str],
    positions: list[float] | None = None,
    factory: str = "김해",
    target: str = "전력",
    end_date: date = BASE_DATE,
) -> pd.DataFrame:
    """끝일자를 end_date 로 맞춘 연속 일별 시계열 프레임."""
    n = len(statuses)
    dates = pd.date_range(end=pd.Timestamp(end_date), periods=n, freq="D")
    if positions is None:
        positions = [1.2 if s in ("over",) else -1.2 if s == "under" else 0.0 for s in statuses]
    preds = [1000.0] * n
    actuals = [1000.0 * (1.0 + p * 0.1) for p in positions]
    return pd.DataFrame(
        {
            "factory": [factory] * n,
            "target": [target] * n,
            "pred_date": dates,
            "pred_value": preds,
            "actual_value": actuals,
            "band_status": statuses,
            "band_position": positions,
        }
    )


def test_empty_frame_returns_no_flags() -> None:
    result = evaluate_band_rules(pd.DataFrame(), BASE_DATE)
    assert result["n_alert"] == 0
    assert result["n_watch"] == 0
    assert result["drift_signals"] == []
    assert result["row_flags"].empty


def test_single_isolated_outlier_is_watch_not_alert() -> None:
    statuses = ["inside"] * 6 + ["over"]
    df = _series_frame(statuses)

    result = evaluate_band_rules(df, BASE_DATE)

    assert result["n_alert"] == 0
    assert result["n_watch"] == 1
    row = result["row_flags"].iloc[0]
    assert row["severity"] == SEVERITY_WATCH
    assert row["rules"] == ""


def test_two_consecutive_same_direction_is_alert() -> None:
    statuses = ["inside"] * 5 + ["over", "over"]
    df = _series_frame(statuses)

    result = evaluate_band_rules(df, BASE_DATE)

    assert result["n_alert"] == 2
    assert all(
        RULE_CONSECUTIVE in r for r in result["row_flags"]["rules"]
    )


def test_consecutive_opposite_directions_do_not_form_run() -> None:
    statuses = ["inside"] * 5 + ["over", "under"]
    df = _series_frame(statuses, positions=[0, 0, 0, 0, 0, 1.3, -1.3])

    result = evaluate_band_rules(df, BASE_DATE)

    assert result["n_alert"] == 0
    assert result["n_watch"] == 2


def test_three_scattered_same_direction_in_window_is_alert() -> None:
    # 7일 창 안에서 하루 걸러 3회 over — 연속은 아니지만 빈발
    statuses = ["over", "inside", "over", "inside", "over", "inside", "inside"]
    df = _series_frame(statuses)

    result = evaluate_band_rules(df, BASE_DATE)

    assert result["n_alert"] == 3
    assert all(RULE_FREQUENT in r for r in result["row_flags"]["rules"] if r)


def test_run_spanning_window_boundary_still_alerts_inside_row() -> None:
    # 연속 2일 이탈이 7일 노출창 경계에 걸침 — 창 밖 행은 노출되지 않지만
    # 창 안쪽 행은 run 의 일부로 인지되어 경보여야 함.
    statuses = ["over", "over"] + ["inside"] * 6  # 창(최근 7일) 시작 하루 전 + 첫날
    df = _series_frame(statuses)

    result = evaluate_band_rules(df, BASE_DATE, recent_days=7)

    flags = result["row_flags"]
    assert len(flags) == 1  # 창 밖 행은 노출 제외
    assert flags.iloc[0]["severity"] == SEVERITY_ALERT
    assert RULE_CONSECUTIVE in flags.iloc[0]["rules"]


def test_sustained_inside_band_bias_triggers_cusum_drift() -> None:
    # 매일 밴드 안(0.6)이지만 한쪽으로 계속 쏠림 → 단일점 판정으로는 안 잡히는 drift
    n = 10
    statuses = ["inside"] * n
    positions = [0.6] * n
    df = _series_frame(statuses, positions=positions)

    result = evaluate_band_rules(df, BASE_DATE)

    assert result["n_alert"] == 0  # 밴드 이탈 행 자체가 없음
    assert len(result["drift_signals"]) == 1
    sig = result["drift_signals"][0]
    assert sig["direction"] == "over"
    assert sig["days"] == n
    assert sig["cusum"] >= CUSUM_H
    # actual = pred * 1.06 로 생성 → 평균 편향 약 +6%
    assert sig["mean_bias_pct"] is not None
    assert 5.0 < sig["mean_bias_pct"] < 7.0


def test_under_direction_drift_detected() -> None:
    df = _series_frame(["inside"] * 10, positions=[-0.7] * 10)

    sig = detect_drift(df)

    assert sig is not None
    assert sig["direction"] == "under"


def test_no_drift_on_centered_noise() -> None:
    positions = [0.3, -0.4, 0.2, -0.1, 0.4, -0.3, 0.1, -0.2, 0.3, -0.4]
    df = _series_frame(["inside"] * 10, positions=positions)

    assert detect_drift(df) is None


def test_drift_requires_min_observations() -> None:
    df = _series_frame(["inside"] * 4, positions=[2.0] * 4)

    assert detect_drift(df) is None


def test_immaterial_drift_below_bias_floor_is_suppressed() -> None:
    # CUSUM 은 넘지만(0.6×10일) 평균 편향이 +1% — 실질성 하한(2%) 미만이면 미보고
    df = _series_frame(["inside"] * 10, positions=[0.6] * 10)
    df["actual_value"] = df["pred_value"] * 1.01

    assert detect_drift(df) is None


def test_drift_start_date_estimated_after_last_reset() -> None:
    # 앞 5일은 중심(0.0) → 누적 0 유지, 뒤 6일 0.8 쏠림 → drift 시작은 6일차
    positions = [0.0] * 5 + [0.8] * 6
    df = _series_frame(["inside"] * 11, positions=positions)

    sig = detect_drift(df)

    assert sig is not None
    assert sig["days"] == 6
    expected_start = pd.date_range(end=pd.Timestamp(BASE_DATE), periods=11, freq="D")[5].date()
    assert sig["start_date"] == expected_start


def test_groups_evaluated_independently() -> None:
    df_a = _series_frame(["inside"] * 5 + ["over", "over"], factory="김해", target="전력")
    df_b = _series_frame(["inside"] * 6 + ["under"], factory="광주", target="용수")
    df = pd.concat([df_a, df_b], ignore_index=True)

    result = evaluate_band_rules(df, BASE_DATE)

    flags = result["row_flags"]
    gimhae = flags[flags["factory"] == "김해"]
    gwangju = flags[flags["factory"] == "광주"]
    assert (gimhae["severity"] == SEVERITY_ALERT).all()
    assert (gwangju["severity"] == SEVERITY_WATCH).all()
