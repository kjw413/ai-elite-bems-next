r"""v5 예측 모델 빠른 성능 평가 스크립트.

지정한 기간을 웹 예측 경로(``predict_v5``)로 다시 예측하고 실측과 비교해 공장×에너지원별
MAPE·WAPE·편향·밴드 적중률을 출력한다. prediction_log·모델 파일·DB는 쓰지 않는다.

- 모델 출력(사후 보정 전)과 최종 예측(드리프트·비근무일·편향 보정 후)을 함께 평가한다.
- 근무일 편향 보정 이력은 웹 누락이력 생성과 같이 시작일 이전 prediction_log 로 시작하고,
  평가 구간 안에서는 이 스크립트가 낸 예측을 이어 붙인다. prediction_log 에
  pred_before_bias 컬럼이 없으면 웹처럼 꺼지며, ``--bias on`` 이면 이력을 빈 상태로 시작한다.
- 에너지 이력·생산량·실측은 시작 시 한 번 읽어 메모리에서 재사용한다.
- MAPE 는 실측 0 인 날을 제외하고, WAPE = Σ|오차| / Σ|실측|, 편향 = Σ오차 / Σ|실측|.

실행 (저장소 루트, Git Bash)::

    .venv/Scripts/python.exe backend/tools/evaluate_v5_model.py
    .venv/Scripts/python.exe backend/tools/evaluate_v5_model.py --start 2026-09-01 --end 2026-09-30 \
        --factory 김해 광주 --target 전력 --csv-out C:/Temp/eval_daily.csv
    .venv/Scripts/python.exe backend/tools/evaluate_v5_model.py \
        --model "backend/app/predictive model/energy usage/v5.3_20260625_130733.pkl"

기본 기간은 실측이 있는 마지막 날까지 최근 30일(``--days``)이다. 모델 로딩(2.6GB)에
1~2분이 걸리고, 그 뒤 공장·일자당 0.3초 안팎이다.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
warnings.filterwarnings("ignore", message=r"[\s\S]*loading a serialized model")

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from threadpoolctl import threadpool_limits  # noqa: E402

import streamlit  # noqa: E402,F401

# streamlit 은 import 시 자체 로거 레벨을 정하므로 그 뒤에 bare mode 경고를 끈다.
for _name in list(logging.root.manager.loggerDict):
    if _name.startswith("streamlit"):
        logging.getLogger(_name).setLevel(logging.ERROR)

load_dotenv(BACKEND_ROOT / ".env")
from app.services import usage_prediction_v5_service as svc  # noqa: E402
from app.services.production_actual_service import fetch_actual_production  # noqa: E402

# _build_feature_source_frame 의 기본 lookback 과 같아야 한다.
LOOKBACK_DAYS = 450
TARGETS = list(svc.TARGET_SPECS.keys())
SCOPES = {"전체": None, "근무일": True, "비근무일": False}


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _git_revision() -> str:
    """추론 코드가 어느 커밋 기준인지 남긴다(작업 트리에 미커밋 변경이 있으면 표시)."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=BACKEND_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "app/services"], cwd=BACKEND_ROOT, capture_output=True, text=True
        ).stdout.strip()
        return f"{head}{' (app/services 미커밋 변경 있음)' if dirty else ''}"
    except Exception:
        return "unknown"


def _load_model(model_arg: str | None) -> tuple[dict, Path]:
    if not model_arg:
        return svc.get_active_model(), svc.get_active_model_path()
    path = Path(model_arg).resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    model = joblib.load(path)
    # predict_v5 가 활성 모델 대신 지정한 모델을 쓰게 한다(이 프로세스 안에서만).
    svc.get_active_model = lambda: model
    svc.get_active_model_path = lambda: path
    return model, path


def _install_data_cache(factories: list[str], start: date, end: date) -> tuple[dict, dict]:
    """에너지 이력·실측·생산량을 한 번 조회하고, 서비스의 DB 조회를 메모리 조회로 바꾼다."""
    loader = svc._fetch_energy_history
    history = {f: loader(f, start - timedelta(days=LOOKBACK_DAYS), end) for f in factories}
    for frame in history.values():
        frame["date"] = pd.to_datetime(frame["date"]).dt.date

    actual_map: dict[tuple[str, date, str], float | None] = {}
    for factory, frame in history.items():
        for row in frame.to_dict("records"):
            for target in TARGETS:
                val = row[svc.TARGET_SPECS[target]["db_col"]]
                actual_map[factory, row["date"], target] = None if pd.isna(val) else float(val)

    production = fetch_actual_production(start, end)
    production_map = {
        (str(r.factory), pd.Timestamp(r.date).date()): float(r.actual_prod_kg) for r in production.itertuples()
    }

    def cached_history(factory: str, lo: date, hi: date) -> pd.DataFrame:
        if factory not in history:
            return loader(factory, lo, hi)
        frame = history[factory]
        return frame.loc[frame["date"].between(lo, hi)].copy()

    svc._fetch_energy_history = cached_history
    svc._fetch_actual_value = lambda factory, target, day: actual_map.get((factory, day, target))
    return actual_map, production_map


def _last_actual_date(actual_map: dict, factories: list[str], targets: list[str]) -> date | None:
    """선택한 모든 공장·대상에 실측이 있는 마지막 날."""
    last = []
    for factory in factories:
        days = [d for (f, d, t), v in actual_map.items() if f == factory and t in targets and v is not None]
        if not days:
            return None
        last.append(max(days))
    return min(last)


def _predict_range(
    factories: list[str],
    targets: list[str],
    dates: list[date],
    production_map: dict,
    bias_on: bool,
) -> tuple[pd.DataFrame, list[str]]:
    holiday_set = svc._cached_holidays()
    rows, errors = [], []
    for factory in factories:
        started = time.perf_counter()
        bias_history = None
        if bias_on:
            bias_history = svc._load_bias_history_or_empty(
                factory,
                dates[0] - timedelta(days=svc.DEFAULT_BIAS_CONFIG.lookback_days),
                dates[0] - timedelta(days=1),
                targets,
                holiday_set,
            )
        for day in dates:
            prod = production_map.get((factory, day), 0.0)
            try:
                out = svc.predict_v5(factory, day, prod, targets=targets, bias_history=bias_history)
            except Exception as exc:  # 한 날짜 실패로 전체 평가를 멈추지 않는다.
                errors.append(f"{factory} {day}: {exc}")
                continue
            workday = svc.is_workday(pd.Timestamp(day), holiday_set)
            for target, r in out["results"].items():
                if bias_history is not None and workday:
                    svc._upsert_bias_history(
                        bias_history, target, day, r["pred_before_bias"], r.get("actual"), prod
                    )
                # 사후 보정 순서: 드리프트 → 비근무일 배수 → 근무일 편향. 가장 앞 단계의 입력이 모델 출력이다.
                model_pred = r.get("pred_before_drift", r.get("pred_before_nonwork_adj", r["pred_before_bias"]))
                rows.append(dict(
                    factory=factory, date=day, target=target, unit=r["unit"],
                    actual=r.get("actual"), model_pred=float(model_pred), final_pred=float(r["pred"]),
                    pred_p05=r.get("pred_p05"), pred_p95=r.get("pred_p95"),
                    is_workday=bool(r["is_workday"]), is_special_event=bool(r["is_special_event"]),
                    production_kg=prod,
                    nonwork_factor=(r.get("nonwork_correction") or {}).get("factor"),
                    bias_factor=(r.get("bias_correction") or {}).get("factor"),
                    drift_adjustment=(r.get("drift_correction") or {}).get("adjustment"),
                ))
        print(f"  {factory}: {len(dates)}일 예측 {time.perf_counter() - started:.1f}s", flush=True)
    return pd.DataFrame(rows), errors


def _metrics(group: pd.DataFrame) -> pd.Series:
    valid = group.dropna(subset=["actual"])
    actual = valid["actual"].astype(float)
    denom = actual.abs().sum()
    out = {"n": len(valid)}
    for name in ("model", "final"):
        err = valid[f"{name}_pred"] - actual
        nonzero = actual != 0
        out[f"MAPE_{name}"] = (err[nonzero].abs() / actual[nonzero].abs()).mean() * 100 if nonzero.any() else np.nan
        out[f"WAPE_{name}"] = err.abs().sum() / denom * 100 if denom else np.nan
    out["bias_final"] = (valid["final_pred"] - actual).sum() / denom * 100 if denom else np.nan
    band = valid.dropna(subset=["pred_p05", "pred_p95"])
    out["band_cov"] = actual.loc[band.index].between(band["pred_p05"], band["pred_p95"]).mean() * 100 if len(band) else np.nan
    return pd.Series(out)


def _summary(daily: pd.DataFrame, factories: list[str], targets: list[str]) -> pd.DataFrame:
    tables = []
    for scope, workday in SCOPES.items():
        frame = daily if workday is None else daily[daily["is_workday"] == workday]
        if frame.empty:
            continue
        table = frame.groupby(["factory", "target"], sort=False).apply(_metrics, include_groups=False).reset_index()
        table = table[table["n"] > 0]
        if table.empty:
            continue
        table["factory"] = pd.Categorical(table["factory"], factories, ordered=True)
        table["target"] = pd.Categorical(table["target"], targets, ordered=True)
        table = table.sort_values(["factory", "target"])
        mean = table.drop(columns=["factory", "target"]).mean(numeric_only=True)
        mean["n"] = table["n"].sum()
        table = pd.concat([table.astype({"factory": str, "target": str}),
                           pd.DataFrame([{"factory": "시리즈 평균", "target": "", **mean}])], ignore_index=True)
        table.insert(0, "scope", scope)
        tables.append(table)
    return pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=_parse_date, help="평가 시작일 YYYY-MM-DD (기본: 종료일 - days + 1)")
    parser.add_argument("--end", type=_parse_date, help="평가 종료일 YYYY-MM-DD (기본: 실측이 있는 마지막 날)")
    parser.add_argument("--days", type=int, default=30, help="--start 를 생략했을 때 기간 일수 (기본 30)")
    parser.add_argument("--factory", nargs="+", help="평가할 공장 (기본: 모델에 있는 전 공장)")
    parser.add_argument("--target", nargs="+", choices=TARGETS, help="평가할 에너지원 (기본: 전체)")
    parser.add_argument("--model", help="활성 모델 대신 평가할 .pkl 경로 (사후 보정 설정은 활성 모델 것을 따른다)")
    parser.add_argument(
        "--bias", choices=["auto", "on", "off"], default="auto",
        help="근무일 편향 보정: auto=웹과 동일(prediction_log.pred_before_bias 컬럼이 있어야 켜짐), on/off=강제",
    )
    parser.add_argument("--csv-out", help="일별 예측·실측 행을 저장할 CSV 경로")
    args = parser.parse_args(argv)

    # predict_v5 가 내부에서 같은 두 함수로 편향 보정 여부를 정하므로 강제 시 둘 다 바꾼다.
    if args.bias == "on":
        svc._bias_correction_enabled = lambda: True
        svc._prediction_log_has_bias_column = lambda: True
    elif args.bias == "off":
        svc._bias_correction_enabled = lambda: False

    os.chdir(BACKEND_ROOT)  # 레지스트리의 상대 모델 경로 기준
    started = time.perf_counter()
    print("모델 로딩 중...", flush=True)
    model, model_path = _load_model(args.model)
    print(f"  {time.perf_counter() - started:.1f}s", flush=True)

    factories = args.factory or [f for f in model if f in svc.STATION_MAP]
    unknown = [f for f in factories if f not in model]
    if unknown:
        parser.error(f"모델에 없는 공장: {unknown} (가능: {list(model)})")
    targets = args.target or TARGETS

    end = args.end or (date.today() - timedelta(days=1))
    start = args.start or (end - timedelta(days=args.days - 1))
    actual_map, production_map = _install_data_cache(factories, start, end)
    if args.end is None:
        last = _last_actual_date(actual_map, factories, targets)
        if last is None:
            print("실측 데이터가 없습니다.", file=sys.stderr)
            return 1
        end = min(end, last)
        start = args.start or (end - timedelta(days=args.days - 1))
    if start > end:
        parser.error(f"시작일({start})이 종료일({end})보다 늦습니다.")
    dates = [ts.date() for ts in pd.date_range(start, end)]

    spec = model[factories[0]][targets[0]]
    bias_on = svc._bias_correction_enabled() and svc._prediction_log_has_bias_column()
    stat = model_path.stat()
    print(f"모델    : {model_path} ({stat.st_size / 1e9:.2f}GB, 수정 {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M})")
    print(f"스펙    : {spec.get('feature_spec_version')}")
    print(f"추론코드: git {_git_revision()}")
    print(f"사후보정: 드리프트={svc._drift_correction_enabled()} 비근무일={svc._nonwork_correction_enabled()} 편향={bias_on}")
    print(f"기간    : {start} ~ {end} ({len(dates)}일) / 공장 {len(factories)}개 / 대상 {', '.join(targets)}", flush=True)

    with threadpool_limits(limits=1):
        daily, errors = _predict_range(factories, targets, dates, production_map, bias_on)

    if errors:
        print(f"\n예측 실패 {len(errors)}건:")
        for line in errors[:20]:
            print(f"  {line}")
    if daily.empty:
        print("평가할 예측이 없습니다.", file=sys.stderr)
        return 1
    missing = int(daily["actual"].isna().sum())
    if missing:
        print(f"\n실측 없음 {missing}행은 지표에서 제외")

    if args.csv_out:
        Path(args.csv_out).parent.mkdir(parents=True, exist_ok=True)
        daily.to_csv(args.csv_out, index=False, encoding="utf-8-sig")
        print(f"\n일별 결과 저장: {args.csv_out}")

    summary = _summary(daily, factories, targets)
    print("\n# 성능 요약 (%) — model=사후 보정 전, final=웹 최종 예측, band_cov=P05~P95 적중률")
    with pd.option_context("display.width", 200, "display.max_rows", None):
        print(summary.round(2).to_string(index=False))
    print(f"\n완료 {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
