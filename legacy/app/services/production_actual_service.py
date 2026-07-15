"""운영 기준 실제 생산량 조회와 energy_daily 오버레이.

에너지 사용량은 ``energy_daily``에서 조회하되 생산량 분모는
``production_daily.actual_qty`` 합계를 기본으로 사용한다. 광주는 여기에
``DB_재공품.xlsx``의 판매용 재공품 7개 품목을 믹스 kg로 환산해 합산한다.
``energy_daily.mix_prod_kg``는 RawDB_에너지 원본 보존용 컬럼이며
화면·메일·분석 계산에는 사용하지 않는다.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

import pandas as pd

from app.database.db_connection import get_connection
from app.domain.factories import (
    FACTORY_CODE_TO_KR,
    FACTORY_KR_TO_CODE,
    FACTORY_PHYSICAL_DISPLAY_ORDER,
    expand_factory_members,
    recalc_unit_rates,
)
from app.services.production_correction_service import WIP_MIX_CONVERSION, get_wip_daily


ACTUAL_PRODUCTION_COLUMN = "actual_prod_kg"


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

    기본값은 DB_생산실적 합계이며, 광주는 판매용 재공품 7개 품목의
    믹스 환산 kg를 일자별로 더한다. 완제품 실적이 없는 날도 재공품 실적이
    있으면 광주 생산량 행을 생성한다.
    """
    gwangju_wip_codes = tuple(WIP_MIX_CONVERSION["광주"])
    wip_placeholders = ",".join(["%s"] * len(gwangju_wip_codes))
    sql = f"""
        SELECT date, factory,
               SUM(
                   CASE
                       WHEN factory = %s AND item_code IN ({wip_placeholders}) THEN 0
                       ELSE actual_qty
                   END
               ) AS actual_prod_kg
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
            (FACTORY_KR_TO_CODE["광주"], *gwangju_wip_codes, date_from, date_to),
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

    # 광주 판매용 재공품 7개 품목은 DB_생산실적에 들어오지 않으므로 별도 합산한다.
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


def _actual_map(actual: pd.DataFrame) -> dict[tuple[date, str], float]:
    if actual is None or actual.empty:
        return {}
    required = {"date", "factory", ACTUAL_PRODUCTION_COLUMN}
    if not required.issubset(actual.columns):
        raise ValueError(f"생산실적 데이터 필수 컬럼 누락: {sorted(required - set(actual.columns))}")
    result: dict[tuple[date, str], float] = {}
    for row in actual.itertuples(index=False):
        normalized = _normalize_date(row.date)
        if normalized is None:
            continue
        value = pd.to_numeric(row.actual_prod_kg, errors="coerce")
        result[(normalized, str(row.factory))] = 0.0 if pd.isna(value) else float(value)
    return result


def overlay_actual_production(
    energy: pd.DataFrame,
    *,
    actual: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """energy DataFrame의 생산량을 운영 기준 생산량으로 교체하고 원단위를 재계산.

    광주는 DB_생산실적+판매용 재공품 환산량, 그 외 공장은 DB_생산실적이다.
    생산실적 행이 없는 공장·일자는 RawDB 예측값으로 되돌아가지 않고 0으로 둔다.
    따라서 원단위도 NaN이 되어 미확정 생산량이 실제값처럼 노출되지 않는다.
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
            return recalc_unit_rates(out)
        actual = fetch_actual_production(valid_dates.min(), valid_dates.max())

    production_by_key = _actual_map(actual)
    out["mix_prod_kg"] = [
        production_by_key.get((d, str(factory)), 0.0)
        for d, factory in zip(normalized_dates, out["factory"])
    ]
    return recalc_unit_rates(out)


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
    if factory in ("전사", "전체"):
        members = set(FACTORY_PHYSICAL_DISPLAY_ORDER)
    else:
        members = set(expand_factory_members(factory))
    selected = actual[actual["factory"].isin(members)]
    if selected.empty:
        return None
    return float(selected[ACTUAL_PRODUCTION_COLUMN].sum())


__all__ = [
    "ACTUAL_PRODUCTION_COLUMN",
    "fetch_actual_production",
    "get_actual_production_kg",
    "overlay_actual_production",
    "overlay_actual_production_rows",
]
