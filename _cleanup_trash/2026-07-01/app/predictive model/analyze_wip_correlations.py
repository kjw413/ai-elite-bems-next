# 이 파일은 재공품과 에너지 사용량의 상관관계를 분석합니다.
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Iterable

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from wip_feature_shortlist import iter_priority_specs


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import (
    PATH_ENERGY_SOURCE,
    PATH_WIP_ITEM_MASTER,
    PATH_WIP_SUMMARY as SHARED_WIP_SUMMARY,
    load_energy_sheet,
)

OUTPUT_DIR = BASE_DIR / "wip_analysis"

PATH_ENERGY = PATH_ENERGY_SOURCE
PATH_HOLIDAY = BASE_DIR / "DB_holiday.xlsx"
PATH_PERFORMANCE = BASE_DIR / "energy usage" / "performance_test_results_v5.xlsx"
PATH_WIP_SUMMARY = SHARED_WIP_SUMMARY
PATH_WIP_RAW = PATH_WIP_ITEM_MASTER

ANALYSIS_START = pd.Timestamp("2023-01-01")
MIN_NONZERO_DAYS = 40
MIN_COVERAGE = 0.05
MAX_RECOMMENDATIONS = 5
REDUNDANT_CORR_THRESHOLD = 0.85
UNKNOWN_NAME = "(품명 미확인)"
WORKDAYS_ONLY = True

PLANTS = ["남양주1", "남양주2", "김해", "광주", "논산"]
TARGETS = {
    "전력": "전력량[kWh]",
    "연료": "연료량[N㎥]",
    "용수": "용수량[ton]",
}


@dataclass(frozen=True)
class MetricThreshold:
    label: str
    min_abs_mix_adjusted: float


SIGNAL_LEVELS = [
    MetricThreshold("Strong", 0.35),
    MetricThreshold("Moderate", 0.20),
    MetricThreshold("Weak", 0.00),
]

CANDIDATE_PRIORITY = {
    "High": 0,
    "Medium": 1,
    "Watch": 2,
    "Low": 3,
}


# 휴일 set 데이터를 불러옵니다.
def load_holiday_set(path: Path) -> set[pd.Timestamp.date]:
    if not path.exists():
        return set()

    df = pd.read_excel(path)
    if "날짜" not in df.columns:
        return set()

    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    return set(df["날짜"].dropna().dt.date.tolist())


# 주어진 날짜가 영업일인지 확인합니다.
def is_workday(ts: pd.Timestamp, holiday_set: set[pd.Timestamp.date]) -> bool:
    return ts.dayofweek < 5 and ts.date() not in holiday_set


# 품목 코드 값을 일정한 형식으로 맞춥니다.
def normalize_item_code(value: object) -> str:
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


# 여러 값 중 대표값 하나를 고릅니다.
def first_mode_or_first(series: pd.Series) -> str:
    cleaned = (
        series.dropna()
        .astype(str)
        .str.strip()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "NaN": pd.NA})
        .dropna()
    )
    if cleaned.empty:
        return ""

    mode = cleaned.mode()
    if not mode.empty:
        return mode.iloc[0]
    return cleaned.iloc[0]


# fallback 품목 기준표 from 요약 관련 처리를 담당합니다.
def _fallback_item_master_from_summary(summary_path: Path) -> pd.DataFrame:
    if not summary_path.exists():
        return pd.DataFrame(columns=["ItemCode", "item_name", "unit", "source_plants"])

    rows: list[dict[str, str]] = []
    xls = pd.ExcelFile(summary_path)
    for sheet in xls.sheet_names:
        df = pd.read_excel(summary_path, sheet_name=sheet, nrows=0)
        item_codes = [normalize_item_code(col) for col in df.columns if str(col) != "날짜"]
        for item_code in item_codes:
            if item_code:
                rows.append({"ItemCode": item_code, "source_plant": sheet})

    if not rows:
        return pd.DataFrame(columns=["ItemCode", "item_name", "unit", "source_plants"])

    master = pd.DataFrame(rows).drop_duplicates()
    master = (
        master.groupby("ItemCode", as_index=False)
        .agg(source_plants=("source_plant", lambda s: ",".join(sorted(set(s.astype(str))))))
        .sort_values("ItemCode")
        .reset_index(drop=True)
    )
    master["item_name"] = UNKNOWN_NAME
    master["unit"] = "-"
    return master[["ItemCode", "item_name", "unit", "source_plants"]]


# 품목 기준표 데이터를 불러옵니다.
def load_item_master(path: Path, summary_path: Path | None = None) -> pd.DataFrame:
    if not path.exists():
        return _fallback_item_master_from_summary(summary_path or PATH_WIP_SUMMARY)

    rows: list[pd.DataFrame] = []
    try:
        xls = pd.ExcelFile(path)
        for sheet in xls.sheet_names:
            df = pd.read_excel(path, sheet_name=sheet, usecols=["ItemCode", "Item 명", "단위"])
            df["ItemCode"] = df["ItemCode"].map(normalize_item_code)
            df["source_plant"] = sheet
            rows.append(df)
    except Exception:
        return _fallback_item_master_from_summary(summary_path or PATH_WIP_SUMMARY)

    master = pd.concat(rows, ignore_index=True)
    master = master[master["ItemCode"] != ""].copy()
    master = (
        master.groupby("ItemCode", as_index=False)
        .agg(
            item_name=("Item 명", first_mode_or_first),
            unit=("단위", first_mode_or_first),
            source_plants=("source_plant", lambda s: ",".join(sorted(set(s.astype(str))))),
        )
        .sort_values("ItemCode")
        .reset_index(drop=True)
    )
    master["item_name"] = master["item_name"].replace("", UNKNOWN_NAME)
    master["unit"] = master["unit"].replace("", "-")
    return master


# 안전한 corr 관련 처리를 담당합니다.
def safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 3:
        return np.nan
    if np.nanstd(a[mask]) == 0 or np.nanstd(b[mask]) == 0:
        return np.nan
    return float(np.corrcoef(a[mask], b[mask])[0, 1])


# 안전한 spearman 관련 처리를 담당합니다.
def safe_spearman(a: np.ndarray, b: np.ndarray) -> float:
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 3:
        return np.nan
    result = spearmanr(a[mask], b[mask], nan_policy="omit")
    return float(result.statistic) if result.statistic is not None else np.nan


# residualize against 믹스 관련 처리를 담당합니다.
def residualize_against_mix(values: np.ndarray, mix_prod: np.ndarray) -> np.ndarray:
    values = values.astype(float)
    mix_prod = mix_prod.astype(float)
    mask = np.isfinite(values) & np.isfinite(mix_prod)
    if mask.sum() < 3:
        return np.full(values.shape, np.nan, dtype=float)

    x = np.column_stack([np.ones(mask.sum(), dtype=float), mix_prod[mask]])
    beta, *_ = np.linalg.lstsq(x, values[mask], rcond=None)
    residuals = np.full(values.shape, np.nan, dtype=float)
    residuals[mask] = values[mask] - (x @ beta)
    return residuals


# 신호을 분류합니다.
def classify_signal(abs_mix_adjusted: float) -> str:
    if not np.isfinite(abs_mix_adjusted):
        return "Weak"
    for threshold in SIGNAL_LEVELS:
        if abs_mix_adjusted >= threshold.min_abs_mix_adjusted:
            return threshold.label
    return "Weak"


# 후보을 분류합니다.
def classify_candidate(abs_mix_adjusted: float, coverage: float, nonzero_days: int) -> str:
    if abs_mix_adjusted >= 0.35 and coverage >= 0.15 and nonzero_days >= 80:
        return "High"
    if abs_mix_adjusted >= 0.20 and coverage >= 0.10 and nonzero_days >= 40:
        return "Medium"
    if abs_mix_adjusted >= 0.15:
        return "Watch"
    return "Low"


# format 후보 관련 처리를 담당합니다.
def format_candidate(row: pd.Series) -> str:
    return f"{row['item_code']} ({row['item_name']})"


# 성능 table 데이터를 불러옵니다.
def load_performance_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["plant", "target", "Advanced_MAPE"])
    return pd.read_excel(path)


# 분석 데이터프레임 데이터를 만듭니다.
def build_analysis_frame(
    plant: str,
    holiday_set: set[pd.Timestamp.date],
) -> tuple[pd.DataFrame, list[str], pd.Timestamp, pd.Timestamp]:
    energy_df = load_energy_sheet(PATH_ENERGY, plant)
    energy_df = energy_df[["날짜", *TARGETS.values(), "믹스생산량[kg]"]].copy()
    wip_df = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=plant)

    energy_df["날짜"] = pd.to_datetime(energy_df["날짜"], errors="coerce")
    wip_df["날짜"] = pd.to_datetime(wip_df["날짜"], errors="coerce")

    item_cols = [normalize_item_code(col) for col in wip_df.columns if col != "날짜"]
    wip_df = wip_df.rename(columns={old: new for old, new in zip(wip_df.columns[1:], item_cols)})

    merged = energy_df.merge(wip_df, on="날짜", how="inner")
    merged = merged[merged["날짜"] >= ANALYSIS_START].copy()
    merged = merged.sort_values("날짜").reset_index(drop=True)

    if WORKDAYS_ONLY:
        merged = merged[merged["날짜"].map(lambda ts: is_workday(ts, holiday_set))].copy()

    if merged.empty:
        raise ValueError(f"{plant}: analysis frame is empty.")

    start_date = merged["날짜"].min()
    end_date = merged["날짜"].max()
    return merged, item_cols, start_date, end_date


# full 지표를 계산합니다.
def compute_full_metrics(
    plant: str,
    target: str,
    target_col: str,
    frame: pd.DataFrame,
    item_cols: Iterable[str],
    item_master: pd.DataFrame,
) -> pd.DataFrame:
    target_values = pd.to_numeric(frame[target_col], errors="coerce").to_numpy(dtype=float)
    mix_prod = pd.to_numeric(frame["믹스생산량[kg]"], errors="coerce").to_numpy(dtype=float)
    target_resid = residualize_against_mix(target_values, mix_prod)

    rows: list[dict[str, object]] = []
    item_lookup = item_master.set_index("ItemCode").to_dict("index")

    for item_code in item_cols:
        item_values = pd.to_numeric(frame[item_code], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        nonzero_days = int(np.count_nonzero(item_values))
        coverage = nonzero_days / len(frame)
        if nonzero_days < MIN_NONZERO_DAYS or coverage < MIN_COVERAGE:
            continue

        item_resid = residualize_against_mix(item_values, mix_prod)
        mix_adjusted = safe_corr(item_resid, target_resid)
        if not np.isfinite(mix_adjusted):
            continue

        lookup = item_lookup.get(item_code, {})
        rows.append(
            {
                "plant": plant,
                "target": target,
                "target_col": target_col,
                "item_code": item_code,
                "item_name": lookup.get("item_name", UNKNOWN_NAME) or UNKNOWN_NAME,
                "unit": lookup.get("unit", "-") or "-",
                "source_plants": lookup.get("source_plants", ""),
                "nonzero_days": nonzero_days,
                "coverage": coverage,
                "pearson_corr": safe_corr(item_values, target_values),
                "spearman_corr": safe_spearman(item_values, target_values),
                "mix_adjusted_corr": mix_adjusted,
                "abs_mix_adjusted_corr": abs(mix_adjusted),
                "activity_mean_when_nonzero": float(item_values[item_values != 0].mean()) if nonzero_days else 0.0,
                "activity_p95": float(np.percentile(item_values[item_values != 0], 95)) if nonzero_days else 0.0,
            }
        )

    metrics = pd.DataFrame(rows)
    if metrics.empty:
        return metrics

    metrics["signal_level"] = metrics["abs_mix_adjusted_corr"].map(classify_signal)
    metrics["candidate_level"] = metrics.apply(
        lambda row: classify_candidate(
            row["abs_mix_adjusted_corr"],
            row["coverage"],
            int(row["nonzero_days"]),
        ),
        axis=1,
    )
    metrics = metrics.sort_values(
        ["abs_mix_adjusted_corr", "coverage", "nonzero_days", "abs_mix_adjusted_corr"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    return metrics


# recommendations을 고릅니다.
def pick_recommendations(metrics: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    if metrics.empty:
        return metrics

    selected_rows: list[pd.Series] = []
    selected_codes: list[str] = []

    for _, row in metrics.iterrows():
        code = row["item_code"]
        candidate_values = pd.to_numeric(frame[code], errors="coerce").fillna(0.0).to_numpy(dtype=float)

        redundant = False
        for selected_code in selected_codes:
            selected_values = (
                pd.to_numeric(frame[selected_code], errors="coerce").fillna(0.0).to_numpy(dtype=float)
            )
            pair_corr = safe_corr(candidate_values, selected_values)
            if np.isfinite(pair_corr) and abs(pair_corr) >= REDUNDANT_CORR_THRESHOLD:
                redundant = True
                break

        if redundant:
            continue

        selected_rows.append(row)
        selected_codes.append(code)
        if len(selected_rows) >= MAX_RECOMMENDATIONS:
            break

    selected = pd.DataFrame(selected_rows).reset_index(drop=True)
    if selected.empty:
        return selected

    selected.insert(0, "recommendation_rank", range(1, len(selected) + 1))
    return selected


# 신호 요약 데이터를 만듭니다.
def build_signal_summary(
    performance_df: pd.DataFrame,
    full_df: pd.DataFrame,
    rec_df: pd.DataFrame,
    plant_meta_df: pd.DataFrame,
) -> pd.DataFrame:
    summary_rows: list[dict[str, object]] = []
    grouped_full = {(p, t): g for (p, t), g in full_df.groupby(["plant", "target"])}
    grouped_rec = {(p, t): g for (p, t), g in rec_df.groupby(["plant", "target"])}
    meta_lookup = plant_meta_df.set_index(["plant", "target"]).to_dict("index")

    for plant in PLANTS:
        for target in TARGETS:
            full_group = grouped_full.get((plant, target), pd.DataFrame())
            rec_group = grouped_rec.get((plant, target), pd.DataFrame())
            meta = meta_lookup.get((plant, target), {})
            top_candidates = (
                rec_group.apply(format_candidate, axis=1).tolist() if not rec_group.empty else []
            )
            summary_rows.append(
                {
                    "plant": plant,
                    "target": target,
                    "Advanced_MAPE": float(
                        performance_df.loc[
                            (performance_df["plant"] == plant) & (performance_df["target"] == target),
                            "Advanced_MAPE",
                        ].iloc[0]
                    )
                    if not performance_df.empty
                    and not performance_df.loc[
                        (performance_df["plant"] == plant) & (performance_df["target"] == target),
                        "Advanced_MAPE",
                    ].empty
                    else np.nan,
                    "analysis_rows": meta.get("analysis_rows", np.nan),
                    "analysis_start": meta.get("analysis_start", pd.NaT),
                    "analysis_end": meta.get("analysis_end", pd.NaT),
                    "max_abs_mix_adjusted_corr": float(full_group["abs_mix_adjusted_corr"].max()) if not full_group.empty else np.nan,
                    "signal_level": classify_signal(float(full_group["abs_mix_adjusted_corr"].max())) if not full_group.empty else "Weak",
                    "recommended_feature_count": int(len(rec_group)),
                    "top_1": top_candidates[0] if len(top_candidates) >= 1 else "",
                    "top_2": top_candidates[1] if len(top_candidates) >= 2 else "",
                    "top_3": top_candidates[2] if len(top_candidates) >= 3 else "",
                }
            )

    summary = pd.DataFrame(summary_rows)
    summary = summary.sort_values(["Advanced_MAPE", "max_abs_mix_adjusted_corr"], ascending=[False, False])
    return summary.reset_index(drop=True)


# 공장 통합 데이터를 만듭니다.
def build_plant_union(recommendation_df: pd.DataFrame) -> pd.DataFrame:
    if recommendation_df.empty:
        return recommendation_df

    # best 후보 level 관련 처리를 담당합니다.
    def best_candidate_level(series: pd.Series) -> str:
        labels = [label for label in series.astype(str) if label in CANDIDATE_PRIORITY]
        if not labels:
            return "Low"
        return min(labels, key=lambda label: CANDIDATE_PRIORITY[label])

    union = (
        recommendation_df.groupby(["plant", "item_code", "item_name", "unit"], as_index=False)
        .agg(
            targets=("target", lambda s: ", ".join(sorted(set(s.astype(str))))),
            best_candidate_level=("candidate_level", best_candidate_level),
            mean_abs_mix_adjusted_corr=("abs_mix_adjusted_corr", "mean"),
            max_abs_mix_adjusted_corr=("abs_mix_adjusted_corr", "max"),
            min_recommendation_rank=("recommendation_rank", "min"),
        )
        .sort_values(["plant", "min_recommendation_rank", "max_abs_mix_adjusted_corr"], ascending=[True, True, False])
        .reset_index(drop=True)
    )
    return union


# 우선순위 우선 목록 데이터를 만듭니다.
def build_priority_shortlist(
    full_df: pd.DataFrame,
    performance_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if full_df.empty:
        empty = pd.DataFrame()
        return empty, empty

    perf_lookup: dict[tuple[str, str], float] = {}
    if not performance_df.empty:
        perf_lookup = (
            performance_df.set_index(["plant", "target"])["Advanced_MAPE"].astype(float).to_dict()
        )

    detail_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []

    for spec in iter_priority_specs():
        group = full_df[
            (full_df["plant"] == spec.plant) & (full_df["target"] == spec.target)
        ].copy()
        if group.empty:
            raise ValueError(
                f"Manual shortlist target is missing from correlation results: "
                f"{spec.priority} {spec.plant}-{spec.target}"
            )

        group = group.set_index("item_code", drop=False)
        selected_rows_for_target: list[dict[str, object]] = []

        for item_rank, item_code in enumerate(spec.item_codes, start=1):
            if item_code not in group.index:
                raise ValueError(
                    f"Configured item_code '{item_code}' is missing for "
                    f"{spec.priority} {spec.plant}-{spec.target}."
                )

            row = group.loc[item_code]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]

            row_dict = row.to_dict()
            row_dict["priority"] = spec.priority
            row_dict["item_rank"] = item_rank
            row_dict["selection_note"] = spec.note
            row_dict["Advanced_MAPE"] = float(
                perf_lookup.get((spec.plant, spec.target), np.nan)
            )

            selected_rows_for_target.append(row_dict)
            detail_rows.append(row_dict)

        summary_rows.append(
            {
                "priority": spec.priority,
                "plant": spec.plant,
                "target": spec.target,
                "Advanced_MAPE": float(perf_lookup.get((spec.plant, spec.target), np.nan)),
                "selected_item_count": len(selected_rows_for_target),
                "selected_item_codes": ", ".join(spec.item_codes),
                "selected_items": ", ".join(
                    f"{row['item_code']} ({row['item_name']})"
                    for row in selected_rows_for_target
                ),
                "max_selected_abs_mix_adjusted_corr": max(
                    float(row["abs_mix_adjusted_corr"]) for row in selected_rows_for_target
                ),
                "selection_note": spec.note,
            }
        )

    priority_summary_df = pd.DataFrame(summary_rows).sort_values("priority").reset_index(drop=True)
    priority_detail_df = pd.DataFrame(detail_rows).sort_values(
        ["priority", "item_rank"]
    ).reset_index(drop=True)
    return priority_summary_df, priority_detail_df


# 데이터 품질 메모 데이터를 만듭니다.
def build_data_quality_notes(
    plant_frames: dict[str, pd.DataFrame],
    wip_output_book: Path,
    wip_raw_book: Path,
) -> pd.DataFrame:
    notes: list[dict[str, object]] = []

    wip_output_f10_same = (
        pd.read_excel(wip_output_book, sheet_name="남양주1").equals(
            pd.read_excel(wip_output_book, sheet_name="남양주2")
        )
    )
    wip_raw_f10_same: bool | None = None
    has_separate_raw_book = False
    try:
        has_separate_raw_book = wip_raw_book.exists() and wip_raw_book.resolve() != wip_output_book.resolve()
    except Exception:
        has_separate_raw_book = wip_raw_book.exists()

    if has_separate_raw_book:
        try:
            wip_raw_f10_same = pd.read_excel(wip_raw_book, sheet_name="남양주1").equals(
                pd.read_excel(wip_raw_book, sheet_name="남양주2")
            )
        except Exception:
            wip_raw_f10_same = None

    notes.append(
        {
            "topic": "남양주1_남양주2_WIP_identical",
            "value": bool(wip_output_f10_same and (wip_raw_f10_same in (None, True))),
            "note": (
                "정리본과 원본(존재 시) 기준으로 남양주1/남양주2 시트 동일 여부를 점검했습니다."
                if wip_raw_f10_same is not None
                else "원본 item master 파일이 없어 정리본 기준으로 남양주1/남양주2 동일 여부를 점검했습니다."
            ),
        }
    )

    for plant, frame in plant_frames.items():
        notes.append(
            {
                "topic": f"{plant}_analysis_period",
                "value": f"{frame['날짜'].min().date()} ~ {frame['날짜'].max().date()}",
                "note": f"{plant} 분석 행수 {len(frame)}건",
            }
        )

    notes.append(
        {
            "topic": "selection_rule",
            "value": f"nonzero_days>={MIN_NONZERO_DAYS}, coverage>={MIN_COVERAGE:.0%}, top<={MAX_RECOMMENDATIONS}",
            "note": "추천 후보는 mix-adjusted correlation 기준으로 정렬 후, 상호 상관 0.85 이상이면 중복으로 제외했습니다.",
        }
    )
    return pd.DataFrame(notes)


# round for export 관련 처리를 담당합니다.
def round_for_export(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    rounded = df.copy()
    for col in rounded.columns:
        if pd.api.types.is_float_dtype(rounded[col]):
            rounded[col] = rounded[col].round(4)
    return rounded


# 이 파일의 전체 실행 흐름을 시작합니다.
def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    holiday_set = load_holiday_set(PATH_HOLIDAY)
    item_master = load_item_master(PATH_WIP_RAW, summary_path=PATH_WIP_SUMMARY)
    performance_df = load_performance_table(PATH_PERFORMANCE)

    all_full_metrics: list[pd.DataFrame] = []
    all_recommendations: list[pd.DataFrame] = []
    plant_frames: dict[str, pd.DataFrame] = {}
    meta_rows: list[dict[str, object]] = []

    for plant in PLANTS:
        frame, item_cols, analysis_start, analysis_end = build_analysis_frame(plant, holiday_set)
        plant_frames[plant] = frame

        for target, target_col in TARGETS.items():
            meta_rows.append(
                {
                    "plant": plant,
                    "target": target,
                    "analysis_rows": len(frame),
                    "analysis_start": analysis_start,
                    "analysis_end": analysis_end,
                }
            )

            full_metrics = compute_full_metrics(
                plant=plant,
                target=target,
                target_col=target_col,
                frame=frame,
                item_cols=item_cols,
                item_master=item_master,
            )
            if full_metrics.empty:
                continue

            recommendations = pick_recommendations(full_metrics, frame)
            all_full_metrics.append(full_metrics)
            all_recommendations.append(recommendations)

    full_df = pd.concat(all_full_metrics, ignore_index=True)
    recommendation_df = pd.concat(all_recommendations, ignore_index=True)
    meta_df = pd.DataFrame(meta_rows)
    signal_summary_df = build_signal_summary(performance_df, full_df, recommendation_df, meta_df)
    plant_union_df = build_plant_union(recommendation_df)
    priority_summary_df, priority_detail_df = build_priority_shortlist(full_df, performance_df)
    data_quality_df = build_data_quality_notes(plant_frames, PATH_WIP_SUMMARY, PATH_WIP_RAW)

    signal_summary_df.to_csv(OUTPUT_DIR / "wip_signal_summary.csv", index=False, encoding="utf-8-sig")
    recommendation_df.to_csv(OUTPUT_DIR / "wip_feature_recommendations.csv", index=False, encoding="utf-8-sig")
    full_df.to_csv(OUTPUT_DIR / "wip_correlation_full.csv", index=False, encoding="utf-8-sig")
    plant_union_df.to_csv(OUTPUT_DIR / "wip_feature_union_by_plant.csv", index=False, encoding="utf-8-sig")
    priority_summary_df.to_csv(
        OUTPUT_DIR / "wip_priority_shortlist_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    priority_detail_df.to_csv(
        OUTPUT_DIR / "wip_priority_shortlist_detail.csv",
        index=False,
        encoding="utf-8-sig",
    )
    item_master.to_csv(OUTPUT_DIR / "wip_item_master.csv", index=False, encoding="utf-8-sig")
    data_quality_df.to_csv(OUTPUT_DIR / "wip_data_quality_notes.csv", index=False, encoding="utf-8-sig")

    excel_path = OUTPUT_DIR / "wip_correlation_analysis.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        round_for_export(signal_summary_df).to_excel(writer, sheet_name="summary", index=False)
        round_for_export(recommendation_df).to_excel(writer, sheet_name="recommendations", index=False)
        round_for_export(plant_union_df).to_excel(writer, sheet_name="plant_union", index=False)
        round_for_export(priority_summary_df).to_excel(
            writer,
            sheet_name="priority_summary",
            index=False,
        )
        round_for_export(priority_detail_df).to_excel(
            writer,
            sheet_name="priority_detail",
            index=False,
        )
        round_for_export(data_quality_df).to_excel(writer, sheet_name="data_quality", index=False)

        for (plant, target), group in full_df.groupby(["plant", "target"]):
            sheet_name = f"{plant}_{target}"
            round_for_export(group).to_excel(writer, sheet_name=sheet_name[:31], index=False)

    print(f"[DONE] WIP correlation analysis saved to: {excel_path}")
    print(f"[DONE] Summary CSV: {OUTPUT_DIR / 'wip_signal_summary.csv'}")
    print(f"[DONE] Recommendations CSV: {OUTPUT_DIR / 'wip_feature_recommendations.csv'}")
    print(f"[DONE] Priority shortlist summary CSV: {OUTPUT_DIR / 'wip_priority_shortlist_summary.csv'}")
    print(f"[DONE] Priority shortlist detail CSV: {OUTPUT_DIR / 'wip_priority_shortlist_detail.csv'}")


if __name__ == "__main__":
    main()
