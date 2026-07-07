from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.prediction_monitoring_service import (
    MonitoringThresholds,
    build_prediction_monitoring_summary,
    get_monitoring_overall_status,
)


def _history_frame(actuals: list[float], preds: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=len(actuals), freq="D")
    return pd.DataFrame(
        {
            "factory": ["F1"] * len(actuals),
            "pred_date": dates,
            "target": ["power"] * len(actuals),
            "actual_value": actuals,
            "pred_value": preds,
        }
    )


def test_detects_recent_offset_when_pattern_still_matches() -> None:
    actuals = [100 + i * 5 for i in range(14)]
    preds = actuals[:7] + [value + 20 for value in actuals[7:]]
    thresholds = MonitoringThresholds(window_size=5, min_points=4)

    summary = build_prediction_monitoring_summary(
        _history_frame(actuals, preds),
        thresholds=thresholds,
    )

    row = summary.iloc[0]
    assert row["status"] == "offset_alert"
    assert row["direction_accuracy"] == 100.0
    assert row["one_sided_rate"] == 100.0
    assert row["latest_bias"] == 20.0
    assert row["estimated_started_at"] is not None

    overall = get_monitoring_overall_status(summary)
    assert overall["alert_count"] == 1


def test_normal_when_recent_predictions_match_actuals() -> None:
    actuals = [100 + i * 3 for i in range(12)]
    preds = actuals.copy()
    thresholds = MonitoringThresholds(window_size=5, min_points=4)

    summary = build_prediction_monitoring_summary(
        _history_frame(actuals, preds),
        thresholds=thresholds,
    )

    assert summary.iloc[0]["status"] == "normal"
    assert get_monitoring_overall_status(summary)["normal_count"] == 1


def test_insufficient_when_actual_history_is_too_short() -> None:
    actuals = [100, 105, 110]
    preds = [101, 106, 111]
    thresholds = MonitoringThresholds(window_size=5, min_points=4)

    summary = build_prediction_monitoring_summary(
        _history_frame(actuals, preds),
        thresholds=thresholds,
    )

    assert summary.iloc[0]["status"] == "insufficient"

def main() -> int:
    test_detects_recent_offset_when_pattern_still_matches()
    test_normal_when_recent_predictions_match_actuals()
    test_insufficient_when_actual_history_is_too_short()
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

