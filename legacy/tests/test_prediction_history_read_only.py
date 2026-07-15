from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "app" / "services" / "usage_prediction_v5_service.py"
PAGE_PATH = ROOT / "app" / "pages" / "ai_prediction.py"


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _function_node(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def main() -> int:
    failures: list[str] = []
    service_tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8-sig"))
    page_tree = ast.parse(PAGE_PATH.read_text(encoding="utf-8-sig"))

    history_fn = _function_node(service_tree, "get_prediction_history")
    history_calls = _called_names(history_fn)
    forbidden = {
        "_ensure_prediction_history_rows",
        "predict_v5",
        "predict_v5_batch",
        "_save_prediction_log",
        "generate_missing_prediction_history",
        "is_admin",
    }
    for name in sorted(forbidden):
        _check(
            name not in history_calls,
            f"get_prediction_history must remain read-only and not call {name}",
            failures,
        )

    generate_fn = _function_node(service_tree, "generate_missing_prediction_history")
    generate_calls = _called_names(generate_fn)
    _check(
        "_ensure_prediction_history_rows" in generate_calls,
        "explicit missing-history generator owns prediction creation",
        failures,
    )
    _check(
        "_save_prediction_log" not in history_calls,
        "prediction history query does not write prediction_log",
        failures,
    )

    page_imports_generate = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.services.usage_prediction_v5_service"
        and any(alias.name == "generate_missing_prediction_history" for alias in node.names)
        for node in ast.walk(page_tree)
    )
    _check(
        page_imports_generate,
        "ai_prediction page imports explicit missing-history generator",
        failures,
    )

    history_tab = _function_node(page_tree, "_render_history_tab")
    page_calls = _called_names(history_tab)
    _check(
        "generate_missing_prediction_history" in page_calls,
        "history tab exposes explicit missing prediction generation",
        failures,
    )

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
