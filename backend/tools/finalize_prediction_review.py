"""Independently check the saved result grain, formulas and source reconciliation."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "backend" / "analysis_results" / "prediction_review_20250917_20261016"


def read(name):
    with (OUT / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    daily = read("daily_predictions.csv")
    source = {(r["factory"], r["date"]): r for r in read("energy_history_snapshot.csv")}
    columns = {"전력": "total_power_kwh", "연료": "fuel_nm3", "용수": "water_ton"}
    keys = {(r["factory"], r["date"], r["target"]) for r in daily}
    assert len(keys) == len(daily) == 5925
    groups = {}
    zero = 0
    for r in daily:
        pred, actual, err = (float(r[k]) for k in ["predicted", "actual", "error"])
        assert all(math.isfinite(v) for v in [pred, actual, err])
        assert math.isclose(err, pred - actual, abs_tol=1e-7)
        original = float(source[r["factory"], r["date"]][columns[r["target"]]])
        assert actual == original
        assert float(r["pred_p05"]) <= pred <= float(r["pred_p95"])
        if actual == 0:
            zero += 1
            assert r["ape_pct"] == "" and r["signed_error_pct"] == ""
        else:
            expected = abs(err) / abs(actual) * 100
            assert math.isclose(float(r["ape_pct"]), expected, rel_tol=1e-12, abs_tol=1e-9)
        expected_web = abs(err) / max(abs(actual), 1) * 100
        assert math.isclose(float(r["web_ape_pct"]), expected_web, rel_tol=1e-12, abs_tol=1e-9)
        groups.setdefault((r["factory"], r["target"]), []).append(r)
    assert zero == 14
    assert len(groups) == 15 and all(len(g) == 395 for g in groups.values())
    raw = {}
    with (OUT / "web_results.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            for target, result in item["results"].items():
                raw[item["factory"], item["date"], target] = result
    for r in daily:
        original = raw[r["factory"], r["date"], r["target"]]
        assert math.isclose(float(r["predicted"]), original["pred"], rel_tol=1e-12, abs_tol=1e-7)
        assert math.isclose(float(r["actual"]), original["actual"], rel_tol=1e-12, abs_tol=1e-7)
    used = {}
    for r in read("model_variables.csv"):
        used.setdefault((r["factory"], r["target"]), set()).add(r["feature"])
    for r in read("daily_model_inputs.csv"):
        for feature in used[r["factory"], r["target"]]:
            assert r[feature] != "", (r["factory"], r["date"], r["target"], feature)
            assert math.isfinite(float(r[feature]))
    summary = read("summary.csv")
    for r in summary:
        group = groups[r["factory"], r["target"]]
        if r["scope"] == "workdays":
            group = [x for x in group if x["is_workday"] == "True"]
        elif r["scope"] == "regular_workdays":
            group = [x for x in group if x["is_workday"] == "True" and x["is_special_event"] == "False"]
        elif r["scope"] == "nonworkdays":
            group = [x for x in group if x["is_workday"] == "False"]
        values = [float(x["ape_pct"]) for x in group if x["ape_pct"] != ""]
        assert len(group) == int(float(r["rows"]))
        assert math.isclose(sum(values) / len(values), float(r["mape_pct"]), rel_tol=1e-12)
    web = json.loads((OUT / "web_api_parity.json").read_text(encoding="utf-8"))
    assert web["checked_values"] == 96
    checks = dict(
        verified_at=datetime.now().astimezone().isoformat(), rows=5925, series=15,
        dates_per_series=395, unique_keys=True, source_actual_reconciled_rows=5925,
        raw_web_result_reconciled_rows=5925, standard_ape_zero_exclusions=zero,
        summary_groups_independently_recomputed=len(summary),
        used_feature_values_present=True, finite_predictions=True, ordered_prediction_bands=True,
        running_web_api_cases=len(web["cases"]), running_web_api_values=web["checked_values"],
        running_web_api_max_difference=web["maximum_absolute_difference"],
    )
    (OUT / "validation_checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    manifest["finalized_at"] = checks["verified_at"]
    manifest["zero_actual_mape_excluded"] = zero
    manifest["rolling_definition"] = "Valid APE mean in (t-N calendar days,t], N=7/30; actual zero excluded; initial windows partial; count columns retained"
    manifest["running_web_api_parity_values"] = web["checked_values"]
    manifest["output_files"] = {p.name: dict(bytes=p.stat().st_size, sha256=sha(p)) for p in OUT.iterdir()
        if p.is_file() and p.name != "manifest.json" and p.suffix not in {".zip"}}
    manifest["reproduction_scripts"] = {str(p.relative_to(ROOT)): dict(sha256=sha(p)) for p in [
        ROOT / "backend/tools/export_prediction_review.py", ROOT / "backend/tools/verify_prediction_review_web.py",
        Path(__file__), OUT / "analyze_results.py"]}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if "--bundle" in sys.argv:
        workbook = OUT / "prediction_performance_review.xlsx"
        assert workbook.exists() and workbook.stat().st_size > 0
        allowed = {".csv", ".json", ".jsonl", ".txt", ".png", ".xlsx"}
        files = [p for p in OUT.iterdir() if p.is_file() and p.suffix in allowed
            and p.name != "workbook_data.json" and not p.name.startswith("workbook_preview_")]
        bundle = OUT / "prediction_review_bundle.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(files):
                archive.write(path, path.name)
            for path in [ROOT / "backend/tools/export_prediction_review.py",
                    ROOT / "backend/tools/verify_prediction_review_web.py", Path(__file__), OUT / "analyze_results.py"]:
                archive.write(path, "reproduce/" + path.name)
        with zipfile.ZipFile(bundle) as archive:
            assert archive.testzip() is None
            print(json.dumps(dict(bundle=str(bundle), bytes=bundle.stat().st_size, files=len(archive.infolist()))))
    print(json.dumps(checks, ensure_ascii=True))


if __name__ == "__main__":
    main()
