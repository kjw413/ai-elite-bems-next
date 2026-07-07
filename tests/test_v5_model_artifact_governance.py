from __future__ import annotations

import hashlib
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import v5_common


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _expect_value_error(fn, message: str, failures: list[str]) -> None:
    try:
        fn()
    except ValueError:
        return
    failures.append(message)


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        ts = datetime(2026, 6, 17, 12, 0, 0)
        versioned = v5_common.build_versioned_model_path(
            "v5.3",
            base_dir=tmp_dir,
            timestamp=ts,
        )
        _check(
            versioned.name == "v5.3_20260617_120000.pkl",
            "versioned model filename includes model key and timestamp",
            failures,
        )

        registry = v5_common.normalize_model_registry(
            {
                "active_model_key": "v5.3",
                "active_model_path": str(versioned),
            }
        )
        _check(
            Path(registry["active_model_path"]).name == versioned.name,
            "normalize_model_registry preserves versioned active model path",
            failures,
        )
        _check(
            registry["active_model_key"] == "v5.3",
            "normalize_model_registry keeps active model key for versioned path",
            failures,
        )

        payload = b"small-model-payload"
        artifact_path = tmp_dir / "v5.1_20260617_120000.pkl"
        artifact_path.write_bytes(payload)

        artifact = v5_common.build_model_artifact_metadata(
            artifact_path,
            model_key="v5.1",
            training_profile="wip_shortlist",
            metrics={"Advanced_MAPE": {"mean": 1.23}},
            split={"train_start": "2023-01-01"},
            data_end_date="2026-04-30",
            train_end_date="2024-12-31",
            git_commit="abc1234",
        )
        _check(
            artifact["sha256"] == hashlib.sha256(payload).hexdigest(),
            "artifact metadata records SHA-256",
            failures,
        )
        _check(
            artifact["size_bytes"] == len(payload),
            "artifact metadata records size",
            failures,
        )
        v5_common.validate_model_artifact(artifact_path, artifact)

        bad_sha = dict(artifact)
        bad_sha["sha256"] = "0" * 64
        _expect_value_error(
            lambda: v5_common.validate_model_artifact(artifact_path, bad_sha),
            "validate_model_artifact rejects SHA mismatch",
            failures,
        )

        bad_size = dict(artifact)
        bad_size["size_bytes"] = len(payload) + 1
        _expect_value_error(
            lambda: v5_common.validate_model_artifact(artifact_path, bad_size),
            "validate_model_artifact rejects size mismatch",
            failures,
        )

        attached = v5_common.attach_model_artifact(
            {"active_model_key": "v5.1"},
            artifact,
            active=True,
        )
        normalized = v5_common.normalize_model_registry(attached)
        active_artifact = v5_common.get_active_model_artifact(normalized)
        _check(
            normalized["active_model_path"] == artifact["path"],
            "attach_model_artifact updates active model pointer",
            failures,
        )
        _check(
            active_artifact.get("sha256") == artifact["sha256"],
            "get_active_model_artifact returns the active artifact record",
            failures,
        )

        metric_summary = v5_common.summarize_metric_frame(
            pd.DataFrame({"MAPE": [1.0, 2.0, None], "ignored": ["x", "y", "z"]}),
            ["MAPE", "missing"],
        )
        _check(metric_summary["rows"] == 3, "metric summary records row count", failures)
        _check(metric_summary["MAPE"]["mean"] == 1.5, "metric summary records numeric mean", failures)

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
