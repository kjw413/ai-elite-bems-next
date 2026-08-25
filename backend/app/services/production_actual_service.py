"""운영 기준 실제 생산량 조회와 선택적 energy_daily 생산량 보정.

생산 KPI·예측 특성처럼 운영 생산량이 필요한 소비처는
``production_daily.actual_qty`` 합계를 사용한다. 광주는 판매용 재공품 환산량을
추가한다. 일반 생산량 오버레이는 ``DB_에너지.xlsx`` 원단위를 유지하지만,
광주 에너지 조회는 재공품 포함 생산량을 분모로 5개 원단위를 다시 계산한다.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

import pandas as pd

from app.database.db_connection import get_connection
from app.domain.factories import (
    FACTORY_CODE_TO_KR,
    expand_factory_members,
)
from app.services.production_correction_service import (
    get_wip_daily,
    operational_production_sum_sql,
)


ACTUAL_PRODUCTION_COLUMN = "actual_prod_kg"

GWANGJU_FACTORY = "광주"
GWANGJU_ENERGY_RATE_COLUMNS = (
    ("freezing_power_kwh", "freezing_power_per_ton_kwh"),
    ("air_compressor_kwh", "air_compressor_per_ton_kwh"),
    ("total_power_kwh", "power_per_ton_kwh"),
    ("fuel_nm3", "fuel_per_ton_nm3"),
    ("water_ton", "water_per_ton_ton"),
)


def _normalize_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def fetch_actual_production(
    date_from: date | str,
    date_to: date | str,
) -> pd.DataFrame:
    """기간 내 공장×일자별 운영 기준 생산량을 에너지 공장명으로 반환.

    기본값은 DB_생산실적 완제품 합계이며, 광주는 판매용 재공품 9개 품목의
    믹스 환산 kg를 일자별로 더한다. 완제품 실적이 없는 날도 재공품 실적이
    있으면 광주 생산량 행을 생성한다.
    """
    quantity_expression, quantity_params = operational_production_sum_sql()
    sql = f"""
        SELECT date, factory,
               {quantity_expression} AS actual_prod_kg
        FROM production_daily
        WHERE date BETWEEN %s AND %s
        GROUP BY date, factory
        ORDER BY date, factory
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            sql,
            (*quantity_params, date_from, date_to),
        )
        actual = pd.DataFrame(
            cursor.fetchall(),
            columns=["date", "factory", ACTUAL_PRODUCTION_COLUMN],
        )
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()

    if actual.empty:
        actual = pd.DataFrame(columns=["date", "factory", ACTUAL_PRODUCTION_COLUMN])
    else:
        actual = actual.copy()
        actual["date"] = pd.to_datetime(actual["date"], errors="coerce").dt.date
        actual["factory"] = actual["factory"].map(FACTORY_CODE_TO_KR)
        actual[ACTUAL_PRODUCTION_COLUMN] = pd.to_numeric(
            actual[ACTUAL_PRODUCTION_COLUMN], errors="coerce",
        ).fillna(0.0)
        actual = actual.dropna(subset=["date", "factory"])

    # DB_재공품.xlsx 기반 광주 판매용 재공품 7개 품목을 별도 합산한다.
    # production_daily에 기록되는 129998·129999는 위 SQL 식에서 이미 환산됐다.
    # get_wip_daily()가 품목별 환산계수와 상류 RPA의 외탁 제외 결과를 적용한
    # 일자별 mix-equivalent kg를 반환한다.
    wip = get_wip_daily("광주")
    if wip is not None and not wip.empty:
        start = _normalize_date(date_from)
        end = _normalize_date(date_to)
        wip = wip.copy()
        wip["date"] = pd.to_datetime(wip["date"], errors="coerce").dt.date
        wip["total_wip_kg"] = pd.to_numeric(
            wip["total_wip_kg"], errors="coerce",
        ).fillna(0.0)
        wip = wip.dropna(subset=["date"])
        if start is not None:
            wip = wip[wip["date"] >= start]
        if end is not None:
            wip = wip[wip["date"] <= end]
        if not wip.empty:
            wip = wip.rename(columns={"total_wip_kg": ACTUAL_PRODUCTION_COLUMN})
            wip["factory"] = "광주"
            actual = pd.concat(
                [actual, wip[["date", "factory", ACTUAL_PRODUCTION_COLUMN]]],
                ignore_index=True,
            )

    if actual.empty:
        return pd.DataFrame(columns=["date", "factory", ACTUAL_PRODUCTION_COLUMN])
    return (
        actual.groupby(["date", "factory"], as_index=False)[ACTUAL_PRODUCTION_COLUMN]
        .sum()
        .sort_values(["date", "factory"])
        .reset_index(drop=True)
    )


def _actual_map(
    actual: pd.DataFrame | Iterable[dict] | None,
) -> dict[tuple[date, str], float]:
    """실적 행을 일자×공장 맵으로 변환하고 중복 행은 합산한다."""
    if actual is None:
        return {}
    frame = actual if isinstance(actual, pd.DataFrame) else pd.DataFrame(list(actual))
    if frame.empty:
        return {}
    required = {"date", "factory", ACTUAL_PRODUCTION_COLUMN}
    if not required.issubset(frame.columns):
        raise ValueError(f"생산실적 데이터 필수 컬럼 누락: {sorted(required - set(frame.columns))}")
    result: dict[tuple[date, str], float] = {}
    for row in frame.itertuples(index=False):
        normalized = _normalize_date(row.date)
        if normalized is None:
            continue
        value = pd.to_numeric(row.actual_prod_kg, errors="coerce")
        key = (normalized, str(row.factory))
        result[key] = result.get(key, 0.0) + (0.0 if pd.isna(value) else float(value))
    return result


def correct_gwangju_energy_frame(
    energy: pd.DataFrame,
    *,
    actual: pd.DataFrame | Iterable[dict] | None = None,
) -> pd.DataFrame:
    """광주 에너지 행을 재공품 포함 운영 생산량 기준으로 보정한다.

    ``energy_daily`` 원본은 변경하지 않는다. 광주 행만 ``mix_prod_kg``를
    완제품+지정 재공품 환산 생산량으로 교체하고, 존재하는 5개 원단위 열을
    사용량 / 보정 생산톤으로 다시 계산한다. 생산량이 0이면 원단위도 0이다.
    광주 외 공장의 생산량과 저장 원단위는 그대로 유지한다.
    """
    if energy is None or energy.empty:
        return energy
    required = {"date", "factory"}
    if not required.issubset(energy.columns):
        raise ValueError(f"에너지 데이터 필수 컬럼 누락: {sorted(required - set(energy.columns))}")

    out = energy.copy()
    gwangju_mask = out["factory"].astype(str).eq(GWANGJU_FACTORY)
    if not gwangju_mask.any():
        return out

    normalized_dates = pd.to_datetime(out["date"], errors="coerce").dt.date
    if actual is None:
        valid_dates = normalized_dates[gwangju_mask].dropna()
        actual = (
            fetch_actual_production(valid_dates.min(), valid_dates.max())
            if not valid_dates.empty
            else pd.DataFrame(columns=["date", "factory", ACTUAL_PRODUCTION_COLUMN])
        )

    production_by_key = _actual_map(actual)
    corrected_production = [
        production_by_key.get((day, GWANGJU_FACTORY), 0.0)
        for day in normalized_dates[gwangju_mask]
    ]
    out.loc[gwangju_mask, "mix_prod_kg"] = corrected_production

    production_ton = (
        pd.to_numeric(out.loc[gwangju_mask, "mix_prod_kg"], errors="coerce")
        .fillna(0.0)
        .div(1000.0)
    )
    positive_production = production_ton > 0
    for usage_col, unit_col in GWANGJU_ENERGY_RATE_COLUMNS:
        if usage_col not in out.columns or unit_col not in out.columns:
            continue
        usage = pd.to_numeric(out.loc[gwangju_mask, usage_col], errors="coerce").fillna(0.0)
        corrected_rate = usage.div(production_ton.where(positive_production)).fillna(0.0)
        out.loc[gwangju_mask, unit_col] = corrected_rate
    return out


def correct_gwangju_energy_rows(
    rows: Iterable[dict],
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    *,
    actual: pd.DataFrame | Iterable[dict] | None = None,
) -> list[dict]:
    """dict 에너지 행에 :func:`correct_gwangju_energy_frame`을 적용한다."""
    copied = [dict(row) for row in rows]
    if not copied or not any(str(row.get("factory")) == GWANGJU_FACTORY for row in copied):
        return copied

    if actual is None and (date_from is not None or date_to is not None):
        frame_dates = pd.to_datetime(
            [row.get("date") for row in copied if str(row.get("factory")) == GWANGJU_FACTORY],
            errors="coerce",
        )
        valid_dates = [value.date() for value in frame_dates if not pd.isna(value)]
        start = _normalize_date(date_from) or (min(valid_dates) if valid_dates else None)
        end = _normalize_date(date_to) or (max(valid_dates) if valid_dates else None)
        actual = (
            fetch_actual_production(start, end)
            if start is not None and end is not None
            else pd.DataFrame(columns=["date", "factory", ACTUAL_PRODUCTION_COLUMN])
        )

    corrected = correct_gwangju_energy_frame(pd.DataFrame(copied), actual=actual)
    return corrected.to_dict("records")


def overlay_actual_production(
    energy: pd.DataFrame,
    *,
    actual: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """energy DataFrame의 생산량만 운영 기준 생산량으로 교체한다.

    광주는 DB_생산실적+판매용 재공품 환산량, 그 외 공장은 DB_생산실적이다.
    생산실적 행이 없는 공장·일자는 RawDB 예측값으로 되돌아가지 않고 0으로 둔다.
    DB_에너지의 원단위 수식 결과 열은 수정하지 않는다.
    """
    if energy is None or energy.empty:
        return energy
    required = {"date", "factory"}
    if not required.issubset(energy.columns):
        raise ValueError(f"에너지 데이터 필수 컬럼 누락: {sorted(required - set(energy.columns))}")

    out = energy.copy()
    normalized_dates = pd.to_datetime(out["date"], errors="coerce").dt.date
    if actual is None:
        valid_dates = normalized_dates.dropna()
        if valid_dates.empty:
            out["mix_prod_kg"] = 0.0
            return out
        actual = fetch_actual_production(valid_dates.min(), valid_dates.max())

    production_by_key = _actual_map(actual)
    out["mix_prod_kg"] = [
        production_by_key.get((d, str(factory)), 0.0)
        for d, factory in zip(normalized_dates, out["factory"])
    ]
    return out


def overlay_actual_production_rows(
    rows: Iterable[dict],
    date_from: date | str,
    date_to: date | str,
    *,
    actual: pd.DataFrame | None = None,
) -> list[dict]:
    """메일용 dict 행의 ``mix_prod_kg``를 운영 기준 생산량으로 교체."""
    copied = [dict(row) for row in rows]
    if not copied:
        return copied
    if actual is None:
        actual = fetch_actual_production(date_from, date_to)
    production_by_key = _actual_map(actual)
    for row in copied:
        key = (_normalize_date(row.get("date")), str(row.get("factory")))
        row["mix_prod_kg"] = production_by_key.get(key, 0.0)
    return copied


def get_actual_production_kg(factory: str, target_date: date | str) -> float | None:
    """단일 일자의 운영 생산량. 광주는 WIP 포함, 집계 공장은 구성 공장 합계."""
    actual = fetch_actual_production(target_date, target_date)
    if actual.empty:
        return None
    # expand_factory_members("전사") 는 AGGREGATE_FACTORY_MEMBERS 를 통해 이미
    # FACTORY_PHYSICAL_DISPLAY_ORDER 와 동일한 튜플을 반환한다 — 예전엔 이 함수가
    # "전사"/"전체"를 별도 분기로 하드코딩했는데, 그러면 "전사(경산 제외)" 같은
    # 새 집계 라벨이 이 분기를 타지 않아도 결과가 같아 버그가 드러나지 않는다.
    # 딕셔너리 조회 하나로 합쳐 새 라벨도 자동으로 옳게 동작하게 한다.
    members = set(expand_factory_members(factory))
    selected = actual[actual["factory"].isin(members)]
    if selected.empty:
        return None
    return float(selected[ACTUAL_PRODUCTION_COLUMN].sum())


__all__ = [
    "ACTUAL_PRODUCTION_COLUMN",
    "GWANGJU_ENERGY_RATE_COLUMNS",
    "correct_gwangju_energy_frame",
    "correct_gwangju_energy_rows",
    "fetch_actual_production",
    "get_actual_production_kg",
    "operational_production_sum_sql",
    "overlay_actual_production",
    "overlay_actual_production_rows",
]
