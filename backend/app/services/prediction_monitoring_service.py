from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MonitoringThresholds:
    """Thresholds for recent prediction quality monitoring."""

    window_size: int = 7
    min_points: int = 5
    bias_pct_warn: float = 5.0
    bias_pct_alert: float = 8.0
    bias_z_warn: float = 1.5
    bias_z_alert: float = 2.0
    pattern_accuracy_min: float = 70.0
    delta_corr_min: float = 0.65
    bias_to_mae_min: float = 0.60
    one_sided_rate_min: float = 80.0
    degradation_ratio_warn: float = 1.25
    degradation_ratio_alert: float = 1.50
    degradation_abs_pct_warn: float = 3.0
    degradation_abs_pct_alert: float = 5.0


DEFAULT_THRESHOLDS = MonitoringThresholds()


STATUS_RANK = {
    "insufficient": 0,
    "normal": 1,
    "watch": 2,
    "offset_warning": 3,
    "offset_alert": 4,
    "degraded": 4,
}


STATUS_LABELS_KO = {
    "insufficient": "데이터 부족",
    "normal": "정상",
    "watch": "주의",
    "offset_warning": "Offset 주의",
    "offset_alert": "Offset 감지",
    "degraded": "성능 저하",
}


def build_prediction_monitoring_summary(
    history_df: pd.DataFrame,
    *,
    thresholds: MonitoringThresholds = DEFAULT_THRESHOLDS,
) -> pd.DataFrame:
    """Build recent prediction monitoring rows from prediction history.

    The detector separates two cases that a plain average MAPE often mixes:
    general error growth, and a sustained positive/negative residual offset while
    the day-to-day movement pattern still matches the actual series.
    """
    required_cols = {"factory", "pred_date", "target", "pred_value", "actual_value"}
    if history_df is None or history_df.empty or not required_cols.issubset(history_df.columns):
        return _empty_monitoring_frame()

    df = history_df.copy()
    df["pred_date"] = pd.to_datetime(df["pred_date"], errors="coerce")
    df["pred_value"] = pd.to_numeric(df["pred_value"], errors="coerce")
    df["actual_value"] = pd.to_numeric(df["actual_value"], errors="coerce")
    df = df.dropna(subset=["factory", "target", "pred_date", "pred_value", "actual_value"])
    if df.empty:
        return _empty_monitoring_frame()

    rows: list[dict[str, Any]] = []
    group_cols = ["factory", "target"]
    for (factory, target), group in df.groupby(group_cols, sort=False):
        rows.append(_summarize_group(str(factory), str(target), group, thresholds))

    if not rows:
        return _empty_monitoring_frame()

    out = pd.DataFrame(rows)
    out["_rank"] = out["status"].map(lambda status: STATUS_RANK.get(str(status), 0))
    out = out.sort_values(
        ["_rank", "latest_to", "factory", "target"],
        ascending=[False, False, True, True],
    )
    return out.drop(columns=["_rank"]).reset_index(drop=True)


def get_monitoring_overall_status(monitoring_df: pd.DataFrame) -> dict[str, Any]:
    """Return a compact overall status for UI cards."""
    if monitoring_df is None or monitoring_df.empty:
        return {
            "status": "insufficient",
            "label": STATUS_LABELS_KO["insufficient"],
            "message": "실측값이 있는 예측 이력이 부족합니다.",
            "alert_count": 0,
            "warning_count": 0,
            "normal_count": 0,
            "total_count": 0,
        }

    statuses = monitoring_df["status"].astype(str)
    alert_statuses = {"offset_alert", "degraded"}
    warning_statuses = {"offset_warning", "watch"}
    alert_count = int(statuses.isin(alert_statuses).sum())
    warning_count = int(statuses.isin(warning_statuses).sum())
    normal_count = int((statuses == "normal").sum())
    total_count = int(len(monitoring_df))

    if alert_count > 0:
        status = "offset_alert"
        message = "최근 예측에서 지속적인 offset 또는 성능 저하가 감지되었습니다."
    elif warning_count > 0:
        status = "watch"
        message = "일부 항목에서 최근 bias 증가가 관찰됩니다."
    elif normal_count > 0:
        status = "normal"
        message = "최근 예측 성능이 안정 범위에 있습니다."
    else:
        status = "insufficient"
        message = "판정 가능한 실측 이력이 충분하지 않습니다."

    return {
        "status": status,
        "label": STATUS_LABELS_KO.get(status, status),
        "message": message,
        "alert_count": alert_count,
        "warning_count": warning_count,
        "normal_count": normal_count,
        "total_count": total_count,
    }


def _summarize_group(
    factory: str,
    target: str,
    group: pd.DataFrame,
    thresholds: MonitoringThresholds,
) -> dict[str, Any]:
    g = group.sort_values("pred_date").reset_index(drop=True)
    actual = g["actual_value"].astype(float)
    pred = g["pred_value"].astype(float)
    denom = actual.abs().clip(lower=1.0)
    g["residual"] = pred - actual
    g["abs_error"] = g["residual"].abs()
    g["abs_pct_error"] = g["abs_error"] / denom * 100.0
    g["signed_pct_error"] = g["residual"] / denom * 100.0

    total_n = int(len(g))
    latest = g.tail(thresholds.window_size)
    latest_n = int(len(latest))
    latest_from = _date_text(latest["pred_date"].min()) if latest_n else None
    latest_to = _date_text(latest["pred_date"].max()) if latest_n else None

    base = _base_row(factory, target, total_n, latest_n, latest_from, latest_to)
    if latest_n < thresholds.min_points:
        return {
            **base,
            "status": "insufficient",
            "status_label": STATUS_LABELS_KO["insufficient"],
            "recommendation": "실측값이 쌓인 뒤 다시 확인",
        }

    baseline = g.iloc[: max(0, total_n - thresholds.window_size)]
    latest_bias = float(latest["residual"].mean())
    latest_bias_pct = float(latest["signed_pct_error"].mean())
    latest_mae = float(latest["abs_error"].mean())
    latest_mape = float(latest["abs_pct_error"].mean())
    avg_actual = float(latest["actual_value"].mean())
    bias_to_mae = _safe_ratio(abs(latest_bias), latest_mae) * 100.0
    one_sided_rate = _one_sided_rate(latest["residual"], latest_bias)
    direction_accuracy, delta_corr = _pattern_metrics(latest)

    baseline_n = int(len(baseline))
    baseline_bias = float(baseline["residual"].mean()) if baseline_n >= thresholds.min_points else np.nan
    baseline_mape = float(baseline["abs_pct_error"].mean()) if baseline_n >= thresholds.min_points else np.nan
    baseline_std = _residual_std(baseline["residual"]) if baseline_n >= thresholds.min_points else np.nan
    bias_z = _bias_z_score(latest_bias, baseline_bias, baseline_std)
    estimated_started_at = _estimate_offset_start(g, latest_bias_pct, thresholds)

    pattern_matches = _pattern_matches(direction_accuracy, delta_corr, thresholds)
    bias_is_one_sided = (
        bias_to_mae >= thresholds.bias_to_mae_min * 100.0
        and one_sided_rate >= thresholds.one_sided_rate_min
    )
    strong_bias = (
        abs(latest_bias_pct) >= thresholds.bias_pct_alert
        or bias_z >= thresholds.bias_z_alert
    )
    moderate_bias = (
        abs(latest_bias_pct) >= thresholds.bias_pct_warn
        or bias_z >= thresholds.bias_z_warn
    )
    mape_degraded = _mape_degraded(latest_mape, baseline_mape, thresholds)

    status = "normal"
    recommendation = "정기 모니터링 유지"
    if pattern_matches and bias_is_one_sided and strong_bias:
        status = "offset_alert"
        direction = "과대예측" if latest_bias > 0 else "과소예측"
        recommendation = f"{direction} offset 지속. 최근 bias 보정 또는 재학습 검토"
    elif mape_degraded == "alert":
        status = "degraded"
        recommendation = "최근 MAPE 악화. 입력 변수/생산 조건 변화 및 재학습 검토"
    elif pattern_matches and bias_is_one_sided and moderate_bias:
        status = "offset_warning"
        recommendation = "최근 bias 추세 확인 및 실측 역채움 상태 점검"
    elif mape_degraded == "warn" or moderate_bias:
        status = "watch"
        recommendation = "최근 오차 증가 추적"

    return {
        **base,
        "baseline_n": baseline_n,
        "status": status,
        "status_label": STATUS_LABELS_KO.get(status, status),
        "avg_actual": avg_actual,
        "latest_bias": latest_bias,
        "latest_bias_pct": latest_bias_pct,
        "latest_mae": latest_mae,
        "latest_mape": latest_mape,
        "baseline_mape": baseline_mape,
        "bias_z": bias_z,
        "bias_to_mae_pct": bias_to_mae,
        "one_sided_rate": one_sided_rate,
        "direction_accuracy": direction_accuracy,
        "delta_corr": delta_corr,
        "estimated_started_at": estimated_started_at,
        "recommendation": recommendation,
    }


def _base_row(
    factory: str,
    target: str,
    total_n: int,
    latest_n: int,
    latest_from: str | None,
    latest_to: str | None,
) -> dict[str, Any]:
    return {
        "factory": factory,
        "target": target,
        "total_n": total_n,
        "latest_n": latest_n,
        "latest_from": latest_from,
        "latest_to": latest_to,
        "baseline_n": 0,
        "avg_actual": np.nan,
        "latest_bias": np.nan,
        "latest_bias_pct": np.nan,
        "latest_mae": np.nan,
        "latest_mape": np.nan,
        "baseline_mape": np.nan,
        "bias_z": np.nan,
        "bias_to_mae_pct": np.nan,
        "one_sided_rate": np.nan,
        "direction_accuracy": np.nan,
        "delta_corr": np.nan,
        "estimated_started_at": None,
    }


def _pattern_metrics(latest: pd.DataFrame) -> tuple[float, float]:
    diff_pred = latest["pred_value"].astype(float).diff()
    diff_actual = latest["actual_value"].astype(float).diff()
    valid = diff_pred.notna() & diff_actual.notna() & ((diff_pred.abs() + diff_actual.abs()) > 0)
    if not bool(valid.any()):
        return np.nan, np.nan

    sign_match = np.sign(diff_pred[valid]) == np.sign(diff_actual[valid])
    direction_accuracy = float(sign_match.mean() * 100.0)

    if int(valid.sum()) < 3:
        delta_corr = np.nan
    else:
        corr = pd.concat([diff_pred[valid], diff_actual[valid]], axis=1).corr().iloc[0, 1]
        delta_corr = float(corr) if pd.notna(corr) else np.nan
    return direction_accuracy, delta_corr


def _pattern_matches(
    direction_accuracy: float,
    delta_corr: float,
    thresholds: MonitoringThresholds,
) -> bool:
    direction_ok = pd.notna(direction_accuracy) and direction_accuracy >= thresholds.pattern_accuracy_min
    corr_ok = pd.notna(delta_corr) and delta_corr >= thresholds.delta_corr_min
    return bool(direction_ok or corr_ok)


def _one_sided_rate(residuals: pd.Series, latest_bias: float) -> float:
    if residuals.empty or latest_bias == 0 or pd.isna(latest_bias):
        return np.nan
    latest_sign = np.sign(latest_bias)
    non_zero = residuals[residuals != 0]
    if non_zero.empty:
        return np.nan
    return float((np.sign(non_zero) == latest_sign).mean() * 100.0)


def _residual_std(residuals: pd.Series) -> float:
    if len(residuals) < 2:
        return np.nan
    std = float(residuals.astype(float).std(ddof=1))
    return std if std > 1e-9 else 0.0


def _bias_z_score(latest_bias: float, baseline_bias: float, baseline_std: float) -> float:
    if pd.isna(latest_bias) or pd.isna(baseline_bias) or pd.isna(baseline_std):
        return np.nan
    diff = abs(float(latest_bias) - float(baseline_bias))
    if baseline_std <= 1e-9:
        return float("inf") if diff > 1e-9 else 0.0
    return float(diff / baseline_std)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator is None or pd.isna(denominator) or abs(float(denominator)) <= 1e-9:
        return np.nan
    return float(numerator) / float(denominator)


def _mape_degraded(
    latest_mape: float,
    baseline_mape: float,
    thresholds: MonitoringThresholds,
) -> str | None:
    if pd.isna(latest_mape) or pd.isna(baseline_mape):
        return None
    diff = float(latest_mape) - float(baseline_mape)
    ratio = _safe_ratio(latest_mape, max(float(baseline_mape), 1e-9))
    if ratio >= thresholds.degradation_ratio_alert and diff >= thresholds.degradation_abs_pct_alert:
        return "alert"
    if ratio >= thresholds.degradation_ratio_warn and diff >= thresholds.degradation_abs_pct_warn:
        return "warn"
    return None


def _estimate_offset_start(
    group: pd.DataFrame,
    latest_bias_pct: float,
    thresholds: MonitoringThresholds,
) -> str | None:
    if pd.isna(latest_bias_pct) or abs(latest_bias_pct) < thresholds.bias_pct_warn:
        return None
    sign = np.sign(latest_bias_pct)
    if sign == 0:
        return None

    rolling_bias = group["signed_pct_error"].rolling(
        thresholds.window_size,
        min_periods=thresholds.min_points,
    ).mean()
    mask = (np.sign(rolling_bias) == sign) & (rolling_bias.abs() >= thresholds.bias_pct_warn)
    if mask.empty or not bool(mask.iloc[-1]):
        return None

    first_idx = len(mask) - 1
    while first_idx > 0 and bool(mask.iloc[first_idx - 1]):
        first_idx -= 1

    onset_idx = max(0, first_idx - thresholds.window_size + 1)
    return _date_text(group.iloc[onset_idx]["pred_date"])


def _date_text(value: Any) -> str | None:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _empty_monitoring_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "factory",
            "target",
            "status",
            "status_label",
            "total_n",
            "latest_n",
            "latest_from",
            "latest_to",
            "baseline_n",
            "avg_actual",
            "latest_bias",
            "latest_bias_pct",
            "latest_mae",
            "latest_mape",
            "baseline_mape",
            "bias_z",
            "bias_to_mae_pct",
            "one_sided_rate",
            "direction_accuracy",
            "delta_corr",
            "estimated_started_at",
            "recommendation",
        ]
    )

