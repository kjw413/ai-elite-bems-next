from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import v5_common  # noqa: E402


select_features = v5_common.select_features


SOURCE_FILES = [
    ROOT / "app" / "services" / "v5_retrain_worker.py",
    ROOT / "app" / "predictive model" / "modeling_v5.1.py",
    ROOT / "app" / "predictive model" / "modeling_v5.3.py",
]


def _check(cond: bool, msg: str, failures: list[str]) -> None:
    print(("  OK " if cond else "  NG ") + msg)
    if not cond:
        failures.append(msg)


def _has_function(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(node, ast.FunctionDef) and node.name == name for node in ast.walk(tree))


def main() -> int:
    failures: list[str] = []

    X = pd.DataFrame(
        {
            "log_mix_ton": [1.0],
            "lag1": [2.0],
            "r7mean": [3.0],
            "intensity_lag1": [4.0],
            "dist_to_h": [5.0],
            "dist_from_h": [6.0],
            "is_holiday": [0],
            "is_pre_holiday": [1],
            "is_post_holiday": [0],
            "is_post_long_weekend": [1],
            "consecutive_nonwork_before": [3],
            "dow": [2],
            "month": [5],
            "HDD": [0.0],
            "CDD": [2.0],
            "THI": [70.0],
            "평균기온": [22.0],
            "상대습도": [55.0],
            "Solar": [10.0],
            "unsafe_target": [999.0],
            "wip_260014": [1.0],
            "wip_bottling_ea_log": [2.0],
            "wip_n_active_skus": [3.0],
        }
    )

    m1_cols = list(select_features(X, "M1").columns)
    for col in [
        "is_holiday",
        "is_pre_holiday",
        "is_post_holiday",
        "is_post_long_weekend",
        "consecutive_nonwork_before",
        "dist_to_h",
        "dist_from_h",
        "wip_260014",
        "wip_bottling_ea_log",
        "wip_n_active_skus",
    ]:
        _check(col in m1_cols, f"M1 keeps {col}", failures)
    _check("unsafe_target" not in m1_cols, "M1 drops unsafe target-like column", failures)

    built = v5_common.build_feature_frame(X, ["wip_260014", "wip_missing_code"])
    _check("wip_260014" in built.columns, "build_feature_frame appends present WIP feature", failures)
    _check("wip_missing_code" in built.columns, "build_feature_frame appends missing WIP feature", failures)
    _check(float(built["wip_missing_code"].iloc[0]) == 0.0, "missing WIP feature is filled with zero", failures)

    _check(
        list(select_features(X, "M2").columns)
        == ["log_mix_ton", "HDD", "CDD", "wip_260014", "wip_bottling_ea_log", "wip_n_active_skus"],
        "M2 feature subset is canonical",
        failures,
    )
    _check(
        list(select_features(X, "M3").columns)
        == ["log_mix_ton", "평균기온", "상대습도", "Solar", "wip_260014", "wip_bottling_ea_log", "wip_n_active_skus"],
        "M3 feature subset is canonical",
        failures,
    )
    _check(
        list(select_features(X, "M4").columns)
        == ["log_mix_ton", "HDD", "CDD", "THI", "wip_260014", "wip_bottling_ea_log", "wip_n_active_skus"],
        "M4 feature subset is canonical",
        failures,
    )

    for path in SOURCE_FILES:
        _check(not _has_function(path, "select_features"), f"{path.name} does not redefine select_features", failures)
        _check(not _has_function(path, "load_holidays"), f"{path.name} does not redefine load_holidays", failures)
        _check(not _has_function(path, "is_workday"), f"{path.name} does not redefine is_workday", failures)
        _check(not _has_function(path, "get_safe_features_with_wip"), f"{path.name} does not redefine WIP feature frame", failures)
        _check(not _has_function(path, "_build_feature_frame"), f"{path.name} does not redefine build_feature_frame", failures)

    _check(v5_common.V5_1_TRAIN_START == pd.Timestamp("2023-01-01"), "v5.1 train start is centralized", failures)
    _check(v5_common.V5_3_TRAIN_START == pd.Timestamp("2021-01-01"), "v5.3 train start is centralized", failures)
    _check(v5_common.WIP_AVAILABLE_START == pd.Timestamp("2023-01-01"), "WIP availability start is centralized", failures)

    original_holiday_path = v5_common.PATH_HOLIDAY
    try:
        v5_common.PATH_HOLIDAY = ROOT / "tests" / "_missing_DB_holiday.xlsx"
        holidays = v5_common.load_holidays_excel()
    finally:
        v5_common.PATH_HOLIDAY = original_holiday_path
    _check(
        date(date.today().year, 5, 1) in holidays,
        "May 1 is patched even when holiday workbook is missing",
        failures,
    )

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll v5 feature pipeline parity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
