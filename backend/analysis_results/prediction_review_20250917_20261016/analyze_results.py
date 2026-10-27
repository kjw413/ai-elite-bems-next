"""Offline diagnostics for the completed web-model replay export.

Reads only the saved evidence, never the operational DB or model. Recomputes
calendar-window MAPE in daily_predictions.csv and writes companion diagnostics.
Run from any directory with the repository .venv Python after export completion.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent
FACTORIES = ["남양주1", "남양주2", "김해", "광주", "논산"]
TARGETS = ["전력", "연료", "용수"]
KEYS = ["factory", "date", "target"]
COLORS = {"남양주1": "#1F64B5", "남양주2": "#B38600", "김해": "#CF6827", "광주": "#687B2B", "논산": "#B35482"}
STYLES = {"남양주1": "-", "남양주2": "--", "김해": "-.", "광주": ":", "논산": (0, (5, 1, 1, 1))}


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def csv(name, frame):
    frame.to_csv(OUT / name, index=False, encoding="utf-8-sig")


def masks(daily):
    regular = daily.is_workday & ~daily.is_special_event
    return {
        "all_calendar_days": pd.Series(True, index=daily.index),
        "workdays": daily.is_workday,
        "regular_workdays": regular,
        "nonworkdays": ~daily.is_workday,
        "special_events": daily.is_special_event,
        "post_source_test_regular_workdays": regular & daily.date.gt(pd.Timestamp("2026-05-15")),
    }


def metrics(group, prediction="predicted"):
    pair = group.loc[group.actual.notna() & group[prediction].notna()]
    error = pair[prediction] - pair.actual
    valid = pair.actual.ne(0)
    ape = error.loc[valid].abs() / pair.loc[valid, "actual"].abs() * 100
    denominator = pair.actual.abs().sum()
    return {
        "rows": len(group), "valid_pairs": len(pair), "valid_ape": int(valid.sum()),
        "zero_actual": int(pair.actual.eq(0).sum()),
        "missing_actual": int(group.actual.isna().sum()),
        "mape_pct": ape.mean(), "median_ape_pct": ape.median(),
        "p90_ape_pct": ape.quantile(0.9),
        "wape_pct": error.abs().sum() / denominator * 100 if denominator else np.nan,
        "bias_pct": error.sum() / denominator * 100 if denominator else np.nan,
        "mean_signed_error_pct": (error.loc[valid] / pair.loc[valid, "actual"].abs() * 100).mean(),
        "mae": error.abs().mean(), "rmse": np.sqrt((error ** 2).mean()),
    }


def grouped_metrics(frame, columns, prediction="predicted"):
    rows = []
    for key, group in frame.groupby(columns, sort=False, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        rows.append(dict(zip(columns, key)) | metrics(group, prediction))
    return pd.DataFrame(rows)


def repair_rolling(daily):
    """Window is (date-N days, date]; average only defined, nonzero-actual APE."""
    checked = 0
    for _, group in daily.groupby(["factory", "target"], sort=False):
        group = group.sort_values("date")
        series = pd.Series(group.ape_pct.to_numpy(), index=pd.DatetimeIndex(group.date))
        for window in (7, 30):
            roll = series.rolling(f"{window}D", min_periods=1, closed="right")
            means, counts = roll.mean().to_numpy(), roll.count().to_numpy()
            daily.loc[group.index, f"mape_{window}d_pct"] = means
            daily.loc[group.index, f"valid_ape_{window}d"] = counts.astype(int)
            # Independent bounded slice computation, including initial partial windows.
            for position, day in enumerate(group.date):
                included = group.loc[group.date.gt(day - pd.Timedelta(days=window)) & group.date.le(day), "ape_pct"].dropna()
                expected = included.mean()
                assert np.isclose(means[position], expected, equal_nan=True, rtol=1e-12, atol=1e-10)
                assert counts[position] == len(included)
                checked += 1
    return checked


def make_figures(daily):
    available = {f.name for f in font_manager.fontManager.ttflist}
    plt.rcParams.update({"font.family": "Malgun Gothic" if "Malgun Gothic" in available else "DejaVu Sans",
        "font.size": 11, "axes.titleweight": "semibold", "axes.spines.top": False,
        "axes.spines.right": False, "axes.edgecolor": "#777777", "axes.unicode_minus": False,
        "grid.color": "#E5E7EB", "grid.linewidth": 0.7, "figure.facecolor": "white"})
    figures = []
    for scope in ["all_calendar_days", "regular_workdays"]:
        fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True)
        for target, ax in zip(TARGETS, axes):
            for factory in FACTORIES:
                series = daily.loc[daily.factory.eq(factory) & daily.target.eq(target)].sort_values("date")
                include = pd.Series(True, index=series.index) if scope == "all_calendar_days" else series.is_workday & ~series.is_special_event
                ape = pd.Series(series.ape_pct.where(include).to_numpy(), index=pd.DatetimeIndex(series.date))
                rolling = ape.rolling("30D", min_periods=1).mean()
                ax.plot(rolling.index, rolling.to_numpy(), color=COLORS[factory], linestyle=STYLES[factory], linewidth=1.8, label=factory)
            ax.set_title(target, loc="left", fontsize=13)
            ax.set_ylabel("30일 MAPE (%)")
            ax.set_ylim(bottom=0)
            ax.grid(axis="y")
            ax.axvline(pd.Timestamp("2026-01-01"), color="#929292", linewidth=0.9, linestyle="--")
            ax.axvline(pd.Timestamp("2026-05-16"), color="#929292", linewidth=0.9, linestyle=":")
        ticks = [daily.date.min(), pd.Timestamp("2025-12-01"), pd.Timestamp("2026-02-01"), pd.Timestamp("2026-04-01"), pd.Timestamp("2026-06-01"), pd.Timestamp("2026-08-01"), daily.date.max()]
        axes[-1].set_xticks(ticks)
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        axes[-1].set_xlim(daily.date.min(), daily.date.max())
        axes[0].legend(ncol=5, loc="upper center", bbox_to_anchor=(0.5, 1.18), frameon=False)
        label = "전체 달력일" if scope == "all_calendar_days" else "일반 근무일만"
        fig.suptitle(f"현재 웹모델의 과거 재예측 · {label}", x=0.07, y=0.98, ha="left", fontsize=19)
        fig.text(0.07, 0.946, "2025-09-17~2026-10-16 | 30달력일 창 내 유효 APE 평균 · 실측 0 제외 · 자원별 세로축 범위 다름", fontsize=11)
        fig.text(0.07, 0.025, "세로선은 소스 기본설정의 학습/테스트 종료 기준일 다음날. 아티팩트의 실제 학습종료일은 확인되지 않음.\n당시 미래예측 성능이 아니며 초기 29일은 부분 창. 일반 근무일 그림의 평균·유효분모 상세는 CSV 참조.", fontsize=10, color="#555555")
        fig.subplots_adjust(left=0.075, right=0.975, top=0.88, bottom=0.10, hspace=0.29)
        filename = f"mape_30d_{scope}.png"
        fig.savefig(OUT / filename, dpi=150, facecolor="white")
        plt.close(fig)
        figures.append(filename)
    return figures


def main():
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("completed_at"):
        raise RuntimeError("Inference export is incomplete; wait for completed_at in manifest.json")
    daily = pd.read_csv(OUT / "daily_predictions.csv", parse_dates=["date"])
    inputs = pd.read_csv(OUT / "daily_model_inputs.csv", parse_dates=["date"])
    variables = pd.read_csv(OUT / "model_variables.csv")
    assert len(daily) == manifest["expected_rows"] == 5925
    assert not daily.duplicated(KEYS).any() and not inputs.duplicated(KEYS).any()
    assert daily.groupby(["factory", "target"]).size().eq(395).all()
    assert not daily.predicted.isna().any()
    recomputed = (daily.predicted - daily.actual).abs() / daily.actual.abs().where(daily.actual.ne(0)) * 100
    assert np.allclose(daily.ape_pct, recomputed, equal_nan=True, rtol=1e-12, atol=1e-10)
    rolling_checks = repair_rolling(daily)
    csv("daily_predictions.csv", daily)

    scoped = []
    for scope, mask in masks(daily).items():
        scoped.append(grouped_metrics(daily.loc[mask], ["factory", "target"]).assign(scope=scope))
    scoped = pd.concat(scoped, ignore_index=True)
    csv("diagnostic_scope_metrics.csv", scoped)

    end = daily.date.max()
    recent_start, previous_start = end - pd.Timedelta(days=59), end - pd.Timedelta(days=119)
    compare = []
    for scope, mask in masks(daily).items():
        for label, lower, upper in [("recent60", recent_start, end), ("previous60", previous_start, recent_start - pd.Timedelta(days=1))]:
            table = grouped_metrics(daily.loc[mask & daily.date.between(lower, upper)], ["factory", "target"])
            table = table.assign(scope=scope, period=label, date_from=lower, date_to=upper)
            compare.append(table)
    periods = pd.concat(compare, ignore_index=True)
    csv("recent_previous_60d_metrics.csv", periods)
    recent = periods.loc[periods.period.eq("recent60")].drop(columns="period")
    previous = periods.loc[periods.period.eq("previous60")].drop(columns="period")
    change = recent.merge(previous, on=["factory", "target", "scope"], suffixes=("_recent60", "_previous60"), validate="one_to_one")
    for name in ["mape_pct", "wape_pct", "bias_pct"]:
        change[f"{name}_change_pp"] = change[f"{name}_recent60"] - change[f"{name}_previous60"]
    change["mape_change_relative_pct"] = (change.mape_pct_recent60 / change.mape_pct_previous60.where(change.mape_pct_previous60.ne(0)) - 1) * 100
    csv("recent_previous_60d_changes.csv", change)

    monthly = daily.copy()
    monthly["calendar_month"] = monthly.date.dt.strftime("%Y-%m")
    month_rows = []
    for scope, mask in masks(monthly).items():
        m = grouped_metrics(monthly.loc[mask], ["factory", "target", "calendar_month"])
        month_rows.append(m.assign(scope=scope))
    csv("monthly_metrics_by_scope.csv", pd.concat(month_rows, ignore_index=True))

    # lag1 is RAW target usage, the previous workday's observed value; no expm1.
    joined = daily.merge(inputs[KEYS + ["lag1"]].rename(columns={"lag1": "lag1_baseline_prediction"}), on=KEYS, validate="one_to_one")
    assert joined.lag1_baseline_prediction.notna().all()
    baseline_checks = 0
    for _, series in joined.groupby(["factory", "target"], sort=False):
        series = series.sort_values("date")
        for row in series.itertuples():
            before = series.loc[series.is_workday & series.date.lt(row.date)]
            if not before.empty:
                assert np.isclose(row.lag1_baseline_prediction, before.iloc[-1].actual, rtol=1e-12, atol=1e-8)
                baseline_checks += 1
    joined["lag1_baseline_ape_pct"] = (joined.lag1_baseline_prediction - joined.actual).abs() / joined.actual.abs().where(joined.actual.ne(0)) * 100
    joined["model_minus_baseline_ape_pp"] = joined.ape_pct - joined.lag1_baseline_ape_pct
    csv("daily_lag1_baseline_comparison.csv", joined[KEYS + ["actual", "predicted", "lag1_baseline_prediction", "ape_pct", "lag1_baseline_ape_pct", "model_minus_baseline_ape_pp", "is_workday", "is_special_event", "evaluation_split"]])
    baseline_rows = []
    for scope, mask in masks(joined).items():
        for key, g in joined.loc[mask].groupby(["factory", "target"], sort=False):
            model_m, base_m = metrics(g), metrics(g, "lag1_baseline_prediction")
            row = dict(factory=key[0], target=key[1], scope=scope,
                **{f"model_{k}": v for k,v in model_m.items()}, **{f"lag1_{k}": v for k,v in base_m.items()})
            row["model_mape_improvement_pp"] = base_m["mape_pct"] - model_m["mape_pct"]
            row["model_mape_improvement_relative_pct"] = (1-model_m["mape_pct"]/base_m["mape_pct"]) * 100 if base_m["mape_pct"] else np.nan
            baseline_rows.append(row)
    baseline = pd.DataFrame(baseline_rows)
    csv("lag1_baseline_metrics.csv", baseline)

    # Exploratory univariate associations on variables actually used by each series.
    features = inputs.rename(columns={name: "input__" + name for name in inputs.columns if name not in KEYS})
    joined_inputs = daily.merge(features, on=KEYS, validate="one_to_one")
    correlations = []
    for scope in ["regular_workdays", "post_source_test_regular_workdays"]:
        mask = masks(joined_inputs)[scope]
        for (factory, target), group in joined_inputs.loc[mask].groupby(["factory", "target"], sort=False):
            used = variables.loc[variables.factory.eq(factory) & variables.target.eq(target), "feature"].unique()
            for feature in used:
                x = pd.to_numeric(group["input__" + feature], errors="coerce")
                for error_name in ["error", "signed_error_pct"]:
                    pair = pd.DataFrame({"x": x, "y": group[error_name]}).replace([np.inf,-np.inf], np.nan).dropna()
                    if len(pair) < 30 or pair.x.nunique() < 2 or pair.y.nunique() < 2:
                        continue
                    correlations.append(dict(factory=factory, target=target, scope=scope, feature=feature,
                        residual_measure=error_name, valid_pairs=len(pair), feature_unique=int(pair.x.nunique()),
                        pearson_r=pair.x.corr(pair.y), spearman_r=pair.x.rank().corr(pair.y.rank())))
    correlations = pd.DataFrame(correlations)
    correlations["abs_spearman_r"] = correlations.spearman_r.abs()
    correlations = correlations.sort_values(["factory", "target", "scope", "residual_measure", "abs_spearman_r"], ascending=[True,True,True,True,False])
    csv("feature_error_associations.csv", correlations)

    candidates = pd.read_csv(OUT / "per_model_predictions.csv", parse_dates=["date"])
    candidates = candidates.loc[np.isclose(candidates["quantile"], .5)].copy()
    cand = candidates.merge(daily, on=KEYS, validate="many_to_one")
    algorithm_rows = []
    for scope, mask in masks(cand).items():
        for key, group in cand.loc[mask].groupby(["factory", "target", "idx", "mt", "algo"], sort=False):
            m = metrics(group, "pred")
            ensemble = metrics(group)
            algorithm_rows.append(dict(zip(["factory", "target", "estimator_index", "model_group", "algorithm"], key)) |
                dict(scope=scope, weight=group.weight.iloc[0], comparison="raw_component_vs_postprocessed_operational_ensemble",
                    **m, ensemble_mape_pct=ensemble["mape_pct"], ensemble_wape_pct=ensemble["wape_pct"],
                    raw_component_minus_ensemble_mape_pp=m["mape_pct"]-ensemble["mape_pct"]))
    algorithm_metrics = pd.DataFrame(algorithm_rows)
    csv("algorithm_metrics.csv", algorithm_metrics)

    figures = make_figures(daily)
    regular_change = change.loc[change.scope.eq("regular_workdays")].sort_values("mape_pct_change_pp", ascending=False)
    latest_regular = scoped.loc[scoped.scope.eq("post_source_test_regular_workdays")].sort_values("mape_pct", ascending=False)
    strongest = correlations.loc[correlations.scope.eq("post_source_test_regular_workdays") & correlations.residual_measure.eq("signed_error_pct")].groupby(["factory", "target"], sort=False).head(3)
    findings = dict(
        created_at=datetime.now().astimezone().isoformat(),
        evaluation="Current-model retrospective replay using realized production/weather/WIP; not an as-of backtest.",
        rows=len(daily), calendar_days=395, zero_actual=int(daily.zero_actual.sum()),
        missing_actual=int(daily.missing_actual.sum()),
        rolling_definition="Mean of defined APE in (date-N calendar days,date]; actual=0 excluded; min_periods=1; valid_ape_Nd stores denominator; initial windows may be shorter.",
        rolling_independent_checks=rolling_checks,
        baseline_raw_units_independent_checks=baseline_checks,
        baseline_definition="RAW lag1 energy-usage feature, not log1p; previous observed WORKDAY because web feature history filters nonworkdays. Nonworkday predictions use that same previous-workday baseline.",
        regular_workday_definition="is_workday and not is_special_event. Special-event flag is production below 1 ton or below 10% of the 30-row rolling production median (min 7 rows), as captured from the web service; it is not a holiday flag.",
        comparison_periods=dict(recent60=[str(recent_start.date()),str(end.date())],previous60=[str(previous_start.date()),str((recent_start-pd.Timedelta(days=1)).date())]),
        recent_previous_regular_workday_changes=regular_change.to_dict("records"),
        post_source_test_regular_workday_metrics=latest_regular.to_dict("records"),
        post_source_test_regular_workday_baseline=baseline.loc[baseline.scope.eq("post_source_test_regular_workdays")].to_dict("records"),
        exploratory_top_feature_associations=strongest.to_dict("records"),
        limitations=["Date split labels and chart reference lines use source-code default train/validation/test dates; actual artifact training cutoff is unverified. This is a retrospective diagnostic, not historical live forecasting performance.",
            "Recent60 vs previous60 comparison is descriptive; seasonality, operating-day composition and low actuals may change MAPE.",
            "Nonworkday predictions extend the model beyond its workday training population; inspect regular_workdays separately.",
            "Feature correlations are exploratory associations, not causes or proof that a variable should be added. Many variables are correlated and no multiple-testing inference is claimed.",
            "Algorithm components are raw P50 candidates; the operational ensemble has quantile ordering/calibration/drift/holiday postprocessing. This is candidate exploration on the same data, not a fair algorithm-selection experiment.",
            "No algorithm was retrained or selected; a future-date holdout with frozen covariates and common postprocessing is needed before switching."],
        next_variable_hypotheses="남양주2 연료는 생산량 변수와 부호오차의 음의 연관이 관측되므로 가동시간·보일러 운전상태·CIP/살균 횟수를 후보로, 논산 용수는 최근 사용량·기온 변수와의 잔차 연관이 남으므로 세척/CIP 횟수·냉각 보충수·설비 정비 이력을 후보로 점검한다. 이 추가변수들은 현재 데이터에서 직접 검증되지 않은 운영 가설이다. 같은 시간순 검증구간에서 변수군을 하나씩 추가해 개선을 검증하고, 알고리즘 변경은 동일 후처리 조건의 미래 홀드아웃에서 비교한다.",
        figures=figures,
        validation=dict(keys_unique=True,expected_rows=True,ape_independently_recomputed=True,rolling_independently_recomputed=True,
            chart_rendered=True,visual_inspection="Pending human/agent pixel inspection"),
    )
    (OUT / "diagnostics_findings.json").write_text(json.dumps(clean(findings),ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["현재 웹모델 재예측: 보조 진단", "기간: 2025-09-17~2026-10-16 (395일), 5공장×3자원=5,925행", "",
        "MAPE: 일별 |예측-실측|/|실측|×100의 유효값 평균. 실측0은 제외(14건).",
        "7/30일 이동 MAPE: 달력일 창에서 정의된 APE만 평균하며 valid_ape_*d가 유효분모. 첫 창은 부분기간.",
        "일반 근무일은 웹 근무일 중 특이일 제외. 특이일은 생산량 1톤 미만 또는 30행 이동 생산량 중앙값의 10% 미만(최소7행).",
        f"최근60일 {recent_start.date()}~{end.date()} / 이전60일 {previous_start.date()}~{(recent_start-pd.Timedelta(days=1)).date()}", "",
        "일반 근무일: 최근60일 대비 이전60일 MAPE 변화(percentage point)"]
    for r in regular_change.itertuples():
        lines.append(f"{r.factory} {r.target}: {r.mape_pct_previous60:.2f}% → {r.mape_pct_recent60:.2f}% ({r.mape_pct_change_pp:+.2f}pp), 유효일 {int(r.valid_ape_previous60)}/{int(r.valid_ape_recent60)}")
    lines += ["", "해석 한계"] + ["- " + text for text in findings["limitations"]]
    lines += ["", "추가 변수와 알고리즘 검토(가설)", findings["next_variable_hypotheses"]]
    lines += ["", "상세: diagnostic_scope_metrics.csv, recent_previous_60d_changes.csv, lag1_baseline_metrics.csv, feature_error_associations.csv, algorithm_metrics.csv"]
    (OUT / "diagnostics_findings.txt").write_text("\n".join(lines)+"\n",encoding="utf-8-sig")
    print(json.dumps(clean(dict(rows=len(daily),zero_actual=int(daily.zero_actual.sum()),rolling_checks=rolling_checks,
        scope_rows=len(scoped),algorithm_rows=len(algorithm_metrics),association_rows=len(correlations),figures=figures)),ensure_ascii=False))


if __name__ == "__main__":
    main()
