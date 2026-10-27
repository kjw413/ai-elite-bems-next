"""Compare the saved replay with the currently running local web API."""
from __future__ import annotations

import csv
import json
import math
import urllib.request
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "analysis_results" / "prediction_review_20250917_20261016"


def main():
    with (OUT / "daily_predictions.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_key = {(r["factory"], r["date"], r["target"]): r for r in rows}
    cases = [(f, "2026-10-16") for f in ["남양주1", "남양주2", "김해", "광주", "논산"]]
    cases += [("남양주2", "2026-05-01"), ("남양주2", "2026-05-04"), ("김해", "2026-04-01")]
    receipt = dict(checked_at=datetime.now().astimezone().isoformat(), endpoint="http://127.0.0.1:8000/api/v1/predictions/run", cases=[])
    for factory, day in cases:
        payload = dict(factory=factory, date=day, mix_prod_kg=float(by_key[factory, day, "전력"]["production_kg"]))
        request = urllib.request.Request(receipt["endpoint"], data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
        checks = []
        for item in result["results"]:
            target = item["target"]
            source = by_key[factory, day, target]
            assert not item.get("error"), item
            for api_field, column in [("predicted", "predicted"), ("lower", "pred_p05"), ("upper", "pred_p95"), ("actual", "actual")]:
                web_native = float(item[api_field]) * 1000
                exported = float(source[column])
                assert math.isclose(web_native, exported, rel_tol=1e-9, abs_tol=1e-6), (factory, day, target, column, web_native, exported)
                checks.append(dict(target=target, field=column, difference_native_units=abs(web_native - exported)))
        receipt["cases"].append(dict(payload=payload, response=result, checks=checks))
        print(f"API parity {factory} {day}: {len(checks)} values", flush=True)
    receipt["checked_values"] = sum(len(c["checks"]) for c in receipt["cases"])
    receipt["maximum_absolute_difference"] = max(check["difference_native_units"] for c in receipt["cases"] for check in c["checks"])
    (OUT / "web_api_parity.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print("WEB_API_PARITY_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
