"""Convert the reviewed prediction exports to typed JSON for Excel authoring."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEXT_KEYS = {"factory", "target", "date", "pred_date", "month", "unit", "evaluation_split", "band_status", "model_group", "feature", "missing_weather_columns", "scope"}


def read_csv(name):
    with (ROOT / name).open(encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            record = {}
            for key, value in row.items():
                if value == "":
                    record[key] = None
                elif key in TEXT_KEYS:
                    record[key] = value
                elif value in ("True", "False", "true", "false"):
                    record[key] = value.lower() == "true"
                else:
                    try:
                        number = float(value)
                        record[key] = number if math.isfinite(number) else None
                    except ValueError:
                        record[key] = value
            rows.append(record)
    return rows


DEFINITIONS = {
    "log_mix_ton": "ln(1 + 당일 생산량(kg) / 1,000)",
    "lag1": "직전 이력 행의 해당 에너지원 사용량. 이력은 영업일만 포함",
    "r7mean": "직전 7개 영업일 이력 사용량 평균. 당일 사용량 제외",
    "intensity_lag1": "lag1 / (직전 이력 행 생산량(ton) + 0.000001)",
    "dist_to_h": "다음 공휴일까지의 달력일수. 최대 30일",
    "dist_from_h": "이전 공휴일 이후 달력일수. 최대 30일",
    "is_holiday": "당일이 주말 또는 공휴일이면 1",
    "is_pre_holiday": "다음 달력일이 주말 또는 공휴일이면 1",
    "is_post_holiday": "전날이 주말 또는 공휴일이면 1",
    "consecutive_nonwork_before": "당일 직전 연속 비영업일 수. 최대 7일",
    "dow": "요일. 월요일 0, 일요일 6",
    "month": "월. 1~12",
    "log_mix_ton_lag1": "직전 이력 행의 log_mix_ton",
    "log_mix_ton_r7mean": "당일을 포함한 최근 7개 이력 행의 log_mix_ton 평균",
    "평균기온": "해당 일자의 평균기온 (°C)",
    "평균기온_lag1": "직전 이력 행의 평균기온 (°C)",
    "평균기온_r7mean": "당일을 포함한 최근 7개 이력 행의 평균기온 평균",
    "상대습도": "해당 일자의 평균 상대습도 (%)",
    "HDD": "max(18 − 평균기온, 0)",
    "CDD": "max(평균기온 − 22, 0)",
    "THI": "현 서비스 정의: 0.72 × (평균기온 + 상대습도) + 40.6",
    "wip_bottling_ea_log": "선택된 보틀링 EA 품목 수량 합을 1,000으로 나눈 뒤 ln(1+x)",
    "wip_n_active_skus": "해당 공장 WIP 전체 품목 중 수량이 양수인 품목 수",
}


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("completed_at"):
        raise RuntimeError("Inference manifest has not been marked completed")
    metadata = json.loads((ROOT / "model_metadata.json").read_text(encoding="utf-8"))
    variables = []
    for row in read_csv("model_variables.csv"):
        feature = row["feature"]
        meaning = DEFINITIONS.get(feature)
        if meaning is None and feature.startswith("wip_"):
            meaning = f"WIP 품목 {feature.removeprefix('wip_')}의 해당 일자 수량. 원천 단위 유지"
        variables.append({
            "factory": row["factory"], "target": row["target"],
            "model_group": row["model_group"], "feature": feature,
            "description": meaning or "서비스 피처 생성 코드 참조",
            "position": row["position"],
            "training_start": metadata.get(f'{row["factory"]}/{row["target"]}', {}).get("effective_train_start"),
        })
    notes = [
        ["분석 기간", f'{manifest["date_from"]}~{manifest["date_to"]}, 양끝 포함 {manifest["calendar_days"]}일'],
        ["활성 모델", "v5.3. LightGBM·XGBoost·CatBoost × M1~M4, 분위수별 가중 앙상블"],
        ["모델 식별 SHA-256", manifest["active_model_sha256"]],
        ["운영 이력", "분석 결과는 별도 파일에 저장했습니다. 기존 운영 예측 이력을 덮어쓰지 않았습니다."],
        ["소스 기준 구간", "train_overlap: ~2025-12-31, validation_overlap: 2026-01-01~03-31, source_test_window: 2026-04-01~05-15, post_source_test: 2026-05-16~. 실제 학습종료일을 증명하는 메타데이터는 없습니다."],
        ["에너지 이력", "각 예측일 이전의 영업일 실측 이력만 사용합니다. 예측일 당일 에너지 실측은 오차 계산에만 사용합니다."],
        ["웹 표시 단위", "이 파일은 원단위입니다. 웹 화면은 사용량을 1,000으로 나누어 표시하므로 화면 값과 비교할 때 단위를 맞추세요."],
        ["웹 APE", "웹 호환 값은 |예측−실측| / max(|실측|, 1) × 100입니다. 실측 0도 큰 오차로 계산되므로 일반 MAPE와 구분합니다."],
        ["일별 입력 공백", "모델별로 사용하지 않는 변수의 공백입니다. 모델 변수 목록에서 해당 공장·에너지원의 사용 여부를 확인하세요."],
        ["시계열 변수", "에너지 lag1/r7mean은 당일 제외입니다. 생산량·기온 r7mean은 당일 포함입니다. 이전 이력 행은 달력일과 다를 수 있습니다."],
        ["이동 MAPE", "7일·30일 달력일 이동창에서 유효 APE만 평균합니다. 시작 후 6일·29일은 확보된 부분창을 사용하며, 각 창의 유효일 수를 함께 제공합니다."],
        ["근무일과 특별일", "근무일은 평일 중 공휴일이 아닌 날입니다. 특별일은 생산량이 최근 중앙값 대비 매우 적거나 1 ton 미만인 날로 서비스가 판정합니다."],
        ["개별 알고리즘 결과", "별도 CSV의 LightGBM·XGBoost·CatBoost 원시 예측은 최종 사후보정 전 값입니다. 최종 예측과 단순 비교해 교체 효과를 확정할 수 없습니다."],
    ]
    output = {
        "manifest": manifest, "daily": read_csv("daily_predictions.csv"),
        "feature_inputs": read_csv("daily_model_inputs.csv"),
        "summary": read_csv("summary.csv"), "monthly": read_csv("monthly_metrics.csv"),
        "split": read_csv("split_metrics.csv"), "features": variables, "notes": notes,
    }
    if (ROOT / "recent_previous_60d_changes.csv").exists():
        output["recent_changes"] = [row for row in read_csv("recent_previous_60d_changes.csv") if row["scope"] == "regular_workdays"]
    with (ROOT / "workbook_data.json").open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"daily_rows": len(output["daily"]), "feature_rows": len(output["feature_inputs"]), "model_variable_rows": len(variables)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
