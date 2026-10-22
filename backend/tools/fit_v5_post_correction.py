r"""v5 사후 보정(비근무일 배수·롤링 편향 보정) 재추정 및 백테스트 스크립트.

배경
----
``app/services/v5_post_correction.py`` 의 비근무일 배수(NONWORK_MULTIPLIERS)는 예측
성능 검토 파일(prediction_performance_review.xlsx)의 ``일별 예측오차`` 시트로 추정했다.
모델을 재학습하거나 운영 분포가 바뀌면 이 스크립트로 배수를 다시 추정하고, 현재 운영
예측(시트의 ``예측값``)과 새 보정 결과를 같은 날짜로 비교한다.

DB·모델 파일은 쓰지 않는다. 시트의 ``보정 전 예측``(드리프트 보정 후, 휴일 보정 전)을
모델 출력으로 보고 서비스와 같은 함수로 보정을 다시 적용한다.

실행
----
Git Bash::

    .venv/Scripts/python.exe backend/tools/fit_v5_post_correction.py \
        --source prediction_performance_review.xlsx --fit-before 2026-04-01

``--fit-before`` 를 주면 그 전날까지로 배수를 추정해 이후 구간에서 평가한다(시간 분할
검증). 생략하면 전체 기간으로 추정한 배수를 출력한다 — 서비스에 반영할 값이다.
``--json-out`` 으로 추정 배수를 파일로 저장할 수 있다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd  # noqa: E402

from app.services.v5_post_correction import (  # noqa: E402
    DEFAULT_BIAS_CONFIG,
    BiasCorrectionConfig,
    apply_nonwork_correction,
    compute_bias_factor,
    fit_nonwork_multipliers,
)

SHEET_NAME = "일별 예측오차"
COLUMN_MAP = {
    "날짜": "date",
    "공장": "factory",
    "에너지원": "target",
    "예측값": "current_pred",
    "실측값": "actual",
    "보정 전 예측": "raw_pred",
    "휴일 보정배수": "holiday_factor",
    "근무일": "workday_label",
    "생산량 (kg)": "mix_prod_kg",
}


# 검토 파일의 일별 시트(또는 같은 헤더의 CSV)를 읽어 영문 컬럼으로 바꿉니다.
def load_daily_rows(source: Path) -> pd.DataFrame:
    if source.suffix.lower() == ".csv":
        df = pd.read_csv(source, encoding="utf-8-sig")
    else:
        raw = pd.read_excel(source, sheet_name=SHEET_NAME, header=None)
        header_rows = raw.index[raw.iloc[:, 0].astype(str).str.strip().eq("날짜")]
        if len(header_rows) == 0:
            raise ValueError(f"'{SHEET_NAME}' 시트에서 '날짜' 헤더 행을 찾지 못했습니다.")
        df = pd.read_excel(source, sheet_name=SHEET_NAME, header=int(header_rows[0]))
    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:
        raise ValueError(f"필수 컬럼 누락: {missing}")
    out = df[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    for c in ("current_pred", "actual", "raw_pred", "holiday_factor", "mix_prod_kg"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["is_workday"] = out["workday_label"].astype(str).str.strip().eq("근무일")
    return out.dropna(subset=["date"]).sort_values(["factory", "target", "date"]).reset_index(drop=True)


# 근무일 표시로 공휴일 집합(평일인데 비근무일인 날)을 복원합니다.
def holiday_set_from_rows(rows: pd.DataFrame) -> set:
    days = rows.drop_duplicates(["factory", "date"])
    weekday_off = days[(~days["is_workday"]) & (days["date"].dt.dayofweek < 5)]
    return set(weekday_off["date"].dt.date)


# 새 보정 파이프라인(비근무일 배수 → 근무일 롤링 편향)을 일별로 재적용합니다.
def replay_new_corrections(
    rows: pd.DataFrame,
    holiday_set: set,
    multipliers: dict | None = None,
    bias_config: BiasCorrectionConfig = DEFAULT_BIAS_CONFIG,
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for (factory, target), g in rows.groupby(["factory", "target"], sort=False):
        g = g.sort_values("date").copy()
        new_pred: list[float] = []
        bias_factor: list[float] = []
        history: list[dict] = []
        for r in g.itertuples(index=False):
            pred = float(r.raw_pred)
            factor = 1.0
            if not r.is_workday:
                pred, _ = apply_nonwork_correction(
                    pred, factory, target, r.date.date(), holiday_set, r.mix_prod_kg, multipliers
                )
            else:
                hist_df = pd.DataFrame(history) if history else None
                factor, _ = compute_bias_factor(hist_df, r.date.date(), bias_config)
                history.append({
                    "pred_date": r.date,
                    "pred_ref": pred,
                    "actual": r.actual,
                    "mix_prod_kg": r.mix_prod_kg,
                })
                pred *= factor
            new_pred.append(pred)
            bias_factor.append(factor)
        g["new_pred"] = new_pred
        g["bias_factor"] = bias_factor
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def _mape(actual: pd.Series, pred: pd.Series) -> float:
    m = actual.abs() > 0
    if not m.any():
        return float("nan")
    return float(((pred[m] - actual[m]).abs() / actual[m].abs()).mean() * 100.0)


def _wape(actual: pd.Series, pred: pd.Series) -> float:
    denom = actual.abs().sum()
    return float((pred - actual).abs().sum() / denom * 100.0) if denom else float("nan")


# 구간별 현재/신규 지표 표를 만듭니다.
def build_report(replayed: pd.DataFrame, eval_from: pd.Timestamp | None) -> dict[str, pd.DataFrame]:
    d = replayed if eval_from is None else replayed[replayed["date"] >= eval_from]
    d = d.dropna(subset=["actual", "current_pred", "new_pred"])
    segments = {
        "근무일": d[d["is_workday"]],
        "연휴 직후(기존 배수 적용일)": d[d["is_workday"] & (d["holiday_factor"].round(2) != 1.0)],
        "비근무일": d[~d["is_workday"]],
    }
    reports: dict[str, pd.DataFrame] = {}
    for name, seg in segments.items():
        rows = []
        for (factory, target), g in seg.groupby(["factory", "target"]):
            rows.append({
                "공장": factory,
                "에너지원": target,
                "n": len(g),
                "MAPE 현재": _mape(g["actual"], g["current_pred"]),
                "MAPE 신규": _mape(g["actual"], g["new_pred"]),
                "WAPE 현재": _wape(g["actual"], g["current_pred"]),
                "WAPE 신규": _wape(g["actual"], g["new_pred"]),
            })
        table = pd.DataFrame(rows)
        if not table.empty:
            by_target = []
            for target, g in seg.groupby("target"):
                by_target.append({
                    "공장": "합계",
                    "에너지원": target,
                    "n": len(g),
                    "MAPE 현재": _mape(g["actual"], g["current_pred"]),
                    "MAPE 신규": _mape(g["actual"], g["new_pred"]),
                    "WAPE 현재": _wape(g["actual"], g["current_pred"]),
                    "WAPE 신규": _wape(g["actual"], g["new_pred"]),
                })
            table = pd.concat([table, pd.DataFrame(by_target)], ignore_index=True)
        reports[name] = table
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="prediction_performance_review.xlsx 또는 같은 헤더의 CSV")
    parser.add_argument("--fit-before", help="이 날짜 전까지로 배수를 추정하고 이후를 평가 (YYYY-MM-DD)")
    parser.add_argument("--eval-from", help="평가 시작일 (기본: --fit-before, 없으면 전체)")
    parser.add_argument("--json-out", help="추정 배수를 저장할 JSON 경로")
    args = parser.parse_args(argv)

    rows = load_daily_rows(Path(args.source))
    holiday_set = holiday_set_from_rows(rows)
    fit_before = pd.Timestamp(args.fit_before) if args.fit_before else None
    eval_from = pd.Timestamp(args.eval_from) if args.eval_from else fit_before

    nonwork = rows[~rows["is_workday"]]
    if fit_before is not None:
        nonwork = nonwork[nonwork["date"] < fit_before]
    multipliers = fit_nonwork_multipliers(nonwork)

    print("# 추정 비근무일 배수 (실측/보정 전 예측 중앙값)")
    print(json.dumps(multipliers, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(multipliers, ensure_ascii=False, indent=2), encoding="utf-8")

    replayed = replay_new_corrections(rows, holiday_set, multipliers=multipliers)
    period = "전체 기간" if eval_from is None else f"{eval_from:%Y-%m-%d} 이후"
    with pd.option_context("display.width", 200, "display.max_rows", 200):
        for name, table in build_report(replayed, eval_from).items():
            print(f"\n# {name} — {period}")
            print(table.round(2).to_string(index=False) if not table.empty else "(해당 없음)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
