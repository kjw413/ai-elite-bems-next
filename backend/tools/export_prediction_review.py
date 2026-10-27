"""Replay the active web model without altering prediction_log or the model.

Feature generation and postprocessing use predict_v5. Ensemble inference is
vectorized across independent dates, with a direct-path parity check at the end.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import warnings
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
logging.getLogger("streamlit").setLevel(logging.ERROR)

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")
from app.services import usage_prediction_v5_service as svc
from app.services.production_actual_service import fetch_actual_production
from app.services.v5_band_calibration import CALIBRATION_PATH
from threadpoolctl import threadpool_limits

START, END = date(2025, 9, 17), date(2026, 10, 16)
FACTORIES = ["남양주1", "남양주2", "김해", "광주", "논산"]
TARGETS = ["전력", "연료", "용수"]
OUT = Path(__file__).resolve().parents[1] / "analysis_results" / "prediction_review_20250917_20261016"
OUT.mkdir(parents=True, exist_ok=True)


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, np.ndarray)):
        return [safe(v) for v in value]
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def dump(name, value):
    (OUT / name).write_text(json.dumps(safe(value), ensure_ascii=False, indent=2), encoding="utf-8")


def csv(name, frame):
    frame.to_csv(OUT / name, index=False, encoding="utf-8-sig")


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    print("Loading active web model", flush=True)
    model_path = svc.get_active_model_path()
    model_hash = file_hash(model_path)
    model = svc.get_active_model()
    registry = svc.get_model_registry()
    meta = {}
    variable_rows = []
    for factory in FACTORIES:
        for target in TARGETS:
            spec = model[factory][target]
            excluded = {"models", "models_by_q"}
            meta[f"{factory}/{target}"] = {k: safe(v) for k, v in spec.items() if k not in excluded}
            for mt, names in spec.get("features", {}).items():
                for position, name in enumerate(names):
                    variable_rows.append(dict(factory=factory, target=target, model_group=mt, position=position, feature=name))
    dump("model_metadata.json", meta)
    csv("model_variables.csv", pd.DataFrame(variable_rows))
    dump("registry_snapshot.json", registry)
    if CALIBRATION_PATH.exists():
        dump("band_calibration_snapshot.json", json.loads(CALIBRATION_PATH.read_text(encoding="utf-8")))
    manifest = dict(
        started_at=datetime.now().astimezone().isoformat(), date_from=START, date_to=END,
        calendar_days=(END - START).days + 1, factories=FACTORIES, targets=TARGETS,
        expected_rows=((END - START).days + 1) * len(FACTORIES) * len(TARGETS),
        active_model_path=model_path, active_model_sha256=model_hash,
        active_model_size=model_path.stat().st_size,
        evaluation="current_model_retrospective_replay_with_realized_covariates",
        save_to_operational_db=False,
        split_basis="training source defaults; confirm artifact metadata per series",
        source_default_train_end="2025-12-31", source_default_validation_end="2026-03-31",
        source_default_test_end="2026-05-15",
    )
    dump("manifest.json", manifest)
    print("Model loaded; snapshotting bounded input data", flush=True)

    # Cache immutable source snapshots in this process only. No service source edits.
    history_loader = svc._fetch_energy_history
    history = {f: history_loader(f, START - timedelta(days=450), END) for f in FACTORIES}
    energy_snapshot = pd.concat([v.assign(factory=f) for f, v in history.items()], ignore_index=True)
    csv("energy_history_snapshot.csv", energy_snapshot)
    production = fetch_actual_production(START - timedelta(days=450), END)
    csv("production_snapshot.csv", production)
    production_map = {(str(r.factory), pd.Timestamp(r.date).date()): float(r.actual_prod_kg) for r in production.itertuples()}
    actual_map = {}
    for factory, frame in history.items():
        for row in frame.to_dict("records"):
            for target in TARGETS:
                val = row[svc.TARGET_SPECS[target]["db_col"]]
                actual_map[factory, pd.Timestamp(row["date"]).date(), target] = None if pd.isna(val) else float(val)

    svc._fetch_energy_history = lambda factory, lo, hi: history[factory].loc[
        pd.to_datetime(history[factory]["date"]).dt.date.between(lo, hi)].copy()
    svc.get_mix_prod_prefill = lambda factory, day: production_map.get((factory, day))
    svc._fetch_actual_value = lambda factory, target, day: actual_map.get((factory, day, target))
    svc.load_n_active_skus_series = lru_cache(maxsize=10)(svc.load_n_active_skus_series)
    svc.get_active_model = lambda: model
    holiday_set = svc._cached_holidays()
    weather_tables = {}
    weather_rows = []
    for factory in FACTORIES:
        station = svc.STATION_MAP[factory]
        weather_tables[factory] = svc._cached_weather_station(station).copy()
        weather_rows.append(weather_tables[factory].assign(factory=factory, station=station))
        wip = svc._cached_wip_sheet(str(svc.WIP_SOURCE_PATH), factory)
        csv(f"wip_snapshot_{factory}.csv", wip.loc[wip["날짜"].between(pd.Timestamp(START - timedelta(days=450)), pd.Timestamp(END))])
    csv("weather_snapshot.csv", pd.concat(weather_rows, ignore_index=True))

    # First pass uses the actual web function to produce its exact feature rows.
    original_infer = svc._infer_target
    original_append = svc._append_spec_wip_features
    original_quantile = svc._predict_quantile_ensemble
    original_make = svc.make_features
    original_detect = svc.detect_special_events
    original_source = svc._build_feature_source_frame
    context = {}
    x_cache, d_cache, input_cache = {}, {}, {}

    def infer_capture(model_dict, factory, target, pred_date, df_source):
        context["key"] = (factory, pred_date, target)
        return original_infer(model_dict, factory, target, pred_date, df_source)

    def append_capture(X_all, d_full, spec):
        combined = original_append(X_all, d_full, spec)
        key = context["key"]
        mask = d_full["date"].eq(pd.Timestamp(key[1]))
        x_cache[key] = combined.loc[mask].copy().reset_index(drop=True)
        d_cache[key] = d_full.loc[mask].copy().reset_index(drop=True)
        return combined

    svc._infer_target = infer_capture
    svc._append_spec_wip_features = append_capture
    svc._predict_quantile_ensemble = lambda *args: (0.0, [])
    dates = [ts.date() for ts in pd.date_range(START, END)]
    with threadpool_limits(limits=1):
        for factory in FACTORIES:
            for i, day in enumerate(dates):
                prod = production_map.get((factory, day))
                raw = svc.predict_v5(factory, day, 0.0 if prod is None else prod, targets=TARGETS)
                input_cache[factory, day] = raw
                if i % 50 == 0 or i == len(dates) - 1:
                    print(f"Features {factory} {i + 1}/{len(dates)} {day}", flush=True)

        # Predict every underlying estimator over all dates in one call.
        vector = {}
        for factory in FACTORIES:
            for target in TARGETS:
                spec = model[factory][target]
                X = pd.concat([x_cache[factory, d, target] for d in dates], ignore_index=True)
                m_types = list(spec.get("m_types") or ["M1", "M2", "M3", "M4"])
                for q, estimators in spec["models_by_q"].items():
                    weights = np.array(spec["weights_by_q"].get(q), dtype=float)
                    if weights.shape != (len(estimators),):
                        weights = np.ones(len(estimators)) / len(estimators)
                    parts = []
                    total = np.zeros(len(X))
                    for idx, (estimator, weight) in enumerate(zip(estimators, weights)):
                        mt = m_types[idx // 3]
                        columns = spec["features"].get(mt) or list(X.columns)
                        pred = np.expm1(estimator.predict(X[columns]))
                        total += weight * pred
                        parts.append((idx, mt, ["LGBM", "XGB", "CB"][idx % 3], float(weight), pred))
                    for i, day in enumerate(dates):
                        vector[factory, day, target, float(q)] = (float(total[i]), [
                            dict(idx=idx, mt=mt, algo=algo, weight=weight, pred=float(pred[i]))
                            for idx, mt, algo, weight, pred in parts])
                print(f"Model inference {factory} {target}", flush=True)

        # Replay the original inference and web postprocessing using captured rows.
        svc._append_spec_wip_features = original_append
        svc.make_features = lambda *args: d_cache[context["key"]].copy()
        svc.detect_special_events = lambda df: df
        def infer_replay(model_dict, factory, target, pred_date, df_source):
            context["key"] = (factory, pred_date, target)
            context["q_index"] = 0
            return original_infer(model_dict, factory, target, pred_date, df_source)
        def quantile_replay(*args):
            factory, day, target = context["key"]
            quantiles = sorted(model[factory][target]["quantiles"])
            q = quantiles[context["q_index"]]
            context["q_index"] += 1
            return vector[factory, day, target, float(q)]
        svc._infer_target = infer_replay
        svc._predict_quantile_ensemble = quantile_replay
        svc._build_feature_source_frame = lambda factory, pred_date, **kwargs: (pd.DataFrame(), input_cache[factory, pred_date]["wip_inputs"])
        results, features, algorithms, raw_results = [], [], [], []
        for factory in FACTORIES:
            for day in dates:
                raw = svc.predict_v5(factory, day, input_cache[factory, day]["mix_prod_kg"], targets=TARGETS)
                raw_results.append(raw)
                wt = weather_tables[factory]
                weather_match = wt.loc[wt["날짜"].eq(pd.Timestamp(day))]
                missing_weather = [c for c in svc.WEATHER_COLS if weather_match.empty or pd.isna(weather_match.iloc[0].get(c))]
                for target, r in raw["results"].items():
                    key = factory, day, target
                    actual, pred = r.get("actual"), float(r["pred"])
                    error = None if actual is None else pred - actual
                    split = "train_overlap" if day <= date(2025, 12, 31) else "validation_overlap" if day <= date(2026, 3, 31) else "source_test_window" if day <= date(2026, 5, 15) else "post_source_test"
                    row = dict(factory=factory, date=day, target=target, unit=r["unit"], predicted=pred,
                        actual=actual, error=error, absolute_error=None if error is None else abs(error),
                        ape_pct=None if actual is None or actual == 0 else abs(error) / abs(actual) * 100,
                        signed_error_pct=None if actual is None or actual == 0 else error / abs(actual) * 100,
                        web_ape_pct=None if actual is None else abs(error) / max(abs(actual), 1) * 100,
                        zero_actual=actual == 0, missing_actual=actual is None,
                        pred_p05=r.get("pred_p05"), pred_p95=r.get("pred_p95"),
                        band_status=r.get("band_status"), is_workday=r["is_workday"],
                        is_special_event=r["is_special_event"], evaluation_split=split,
                        production_kg=raw["mix_prod_kg"], missing_production=(factory, day) not in production_map,
                        missing_weather_columns=";".join(missing_weather),
                        drift_adjustment=(r.get("drift_correction") or {}).get("adjustment", 0),
                        holiday_factor=(r.get("holiday_correction") or {}).get("factor", 1),
                        base_model_prediction=r.get("pred_before_drift", r.get("pred_before_holiday_adj", pred)))
                    results.append(row)
                    feat = dict(factory=factory, date=day, target=target)
                    used = set(v for names in model[factory][target]["features"].values() for v in names)
                    feat.update({k: safe(v) for k, v in x_cache[key].iloc[0].items() if k in used})
                    features.append(feat)
                    for q, items in r["per_model_by_q"].items():
                        for item in items:
                            algorithms.append(dict(factory=factory, date=day, target=target, quantile=float(q), **item))
        daily = pd.DataFrame(results).sort_values(["factory", "target", "date"]).reset_index(drop=True)
        for window in (7, 30):
            daily[f"mape_{window}d_pct"] = daily.groupby(["factory", "target"])["ape_pct"].transform(
                lambda s: s.rolling(window, min_periods=1).mean())
            daily[f"valid_ape_{window}d"] = daily.groupby(["factory", "target"])["ape_pct"].transform(lambda s: s.rolling(window, min_periods=1).count())
        daily["month"] = pd.to_datetime(daily["date"]).dt.strftime("%Y-%m")
        csv("daily_predictions.csv", daily)
        csv("daily_model_inputs.csv", pd.DataFrame(features))
        csv("per_model_predictions.csv", pd.DataFrame(algorithms))
        with (OUT / "web_results.jsonl").open("w", encoding="utf-8") as handle:
            for row in raw_results:
                handle.write(json.dumps(safe(row), ensure_ascii=False) + "\n")

        def summarize(group):
            valid = group.dropna(subset=["actual", "predicted"])
            return pd.Series(dict(rows=len(group), valid_ape=int(group.ape_pct.notna().sum()),
                zero_actual=int(group.zero_actual.sum()), mape_pct=group.ape_pct.mean(),
                web_mape_pct=group.web_ape_pct.mean(), mae=valid.absolute_error.mean(),
                wape_pct=valid.absolute_error.sum() / valid.actual.abs().sum() * 100 if valid.actual.abs().sum() else np.nan,
                bias_pct=valid.error.sum() / valid.actual.abs().sum() * 100 if valid.actual.abs().sum() else np.nan,
                rmse=float(np.sqrt(np.mean(valid.error ** 2))),
                band_coverage_pct=float(valid.actual.between(valid.pred_p05, valid.pred_p95).mean() * 100)))

        slices = []
        for name, frame in [("all_calendar_days", daily), ("workdays", daily[daily.is_workday]),
                ("regular_workdays", daily[daily.is_workday & ~daily.is_special_event]),
                ("nonworkdays", daily[~daily.is_workday])]:
            table = frame.groupby(["factory", "target"], sort=False).apply(summarize, include_groups=False).reset_index()
            table.insert(0, "scope", name)
            slices.append(table)
        summary = pd.concat(slices, ignore_index=True)
        csv("summary.csv", summary)
        monthly = daily.groupby(["factory", "target", "month"], sort=False).apply(summarize, include_groups=False).reset_index()
        monthly["mape_change_pp"] = monthly.groupby(["factory", "target"])["mape_pct"].diff()
        monthly["mape_change_ratio_pct"] = monthly.groupby(["factory", "target"])["mape_pct"].pct_change() * 100
        csv("monthly_metrics.csv", monthly)
        split_metrics = daily.groupby(["factory", "target", "evaluation_split"], sort=False).apply(summarize, include_groups=False).reset_index()
        csv("split_metrics.csv", split_metrics)
        print("Outputs saved; checking unmodified single-day path parity", flush=True)

        svc._infer_target = original_infer
        svc._predict_quantile_ensemble = original_quantile
        svc.make_features = original_make
        svc.detect_special_events = original_detect
        svc._build_feature_source_frame = original_source
        parity = []
        for factory in FACTORIES:
            for day in [START, date(2026, 4, 1), date(2026, 5, 1), date(2026, 5, 4), END]:
                direct = svc.predict_v5(factory, day, input_cache[factory, day]["mix_prod_kg"], targets=TARGETS)
                for target, r in direct["results"].items():
                    exported = daily.loc[(daily.factory == factory) & (daily.target == target) & (daily.date == day)].iloc[0]
                    for name, col in [("pred", "predicted"), ("pred_p05", "pred_p05"), ("pred_p95", "pred_p95")]:
                        delta = abs(float(r[name]) - float(exported[col]))
                        assert np.isclose(r[name], exported[col], rtol=1e-10, atol=1e-7), (factory, day, target, name, delta)
                        parity.append(dict(factory=factory, date=day, target=target, field=name, absolute_difference=delta))
        csv("parity_checks.csv", pd.DataFrame(parity))
        assert len(daily) == manifest["expected_rows"]
        assert not daily.duplicated(["factory", "date", "target"]).any()
        assert daily.predicted.notna().all()
        assert not daily.missing_actual.any()
        manifest.update(completed_at=datetime.now().astimezone().isoformat(), actual_rows=len(daily),
            parity_checked_values=len(parity), parity_max_absolute_difference=max(r["absolute_difference"] for r in parity),
            source_code_sha256={name: file_hash(Path.cwd() / name) for name in [
                "app/services/usage_prediction_v5_service.py", "app/services/v5_common.py", "app/services/production_actual_service.py"]},
            output_files={p.name: dict(bytes=p.stat().st_size, sha256=file_hash(p)) for p in OUT.iterdir() if p.is_file() and p.name != "manifest.json"},
        )
        dump("manifest.json", manifest)
        print(summary.loc[summary.scope.eq("all_calendar_days"), ["factory", "target", "rows", "mape_pct", "wape_pct"]].to_string(index=False), flush=True)
        print("COMPLETE", flush=True)


if __name__ == "__main__":
    main()
