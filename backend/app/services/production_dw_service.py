# 이 파일은 production_daily 테이블 "조회" 헬퍼만 제공합니다.
#
# MIS 원본 수집·형식 재가공(build_dataset 등 build 파이프라인)은 별도 프로젝트
# AI-Elite_MIS_RPA(mis_rpa/production_builder.py)로 이관되었습니다.
# 웹은 서버 기동 시 production_dw_sync_service 가 DEFAULT_OUTPUT_PATH(= RPA 산출물
# DB_생산실적.xlsx)를 읽어 production_daily 테이블에 UPSERT 합니다.
from __future__ import annotations

import pandas as pd

from app.config.paths import sampled_db_path
from app.services.production_correction_service import finished_production_filter_sql

# RPA(AI-Elite_MIS_RPA)가 생성하는 통합 파일. production_dw_sync_service 가 startup 에 읽어 적재.
# 경로는 .env(SAMPLED_DB_DIR / PRODUCTION_DW_XLSX)로 오버라이드 가능 — RPA 측과 동일해야 함.
DEFAULT_OUTPUT_PATH = sampled_db_path("DB_생산실적.xlsx", "PRODUCTION_DW_XLSX")

# ─────────────────────────────────────────────────────────────
# DB 조회 헬퍼 — production_daily 테이블 활용
# (UI 페이지/AI 서비스에서 import 해서 사용)
# ─────────────────────────────────────────────────────────────


def _build_filter_clause(
    factories: list[str] | None,
    category1_values: list[str] | None,
    category2_values: list[str] | None,
) -> tuple[str, list]:
    """필터 조건 → WHERE 절 + 파라미터 리스트.
    category2_values 에 None 이 포함되면 'IS NULL' OR 절 추가.
    """
    clauses: list[str] = []
    params: list = []
    if factories:
        clauses.append(f"factory IN ({','.join(['%s']*len(factories))})")
        params.extend(factories)
    if category1_values:
        clauses.append(f"category1 IN ({','.join(['%s']*len(category1_values))})")
        params.extend(category1_values)
    if category2_values:
        non_null = [s for s in category2_values if s is not None]
        has_null = any(s is None for s in category2_values)
        sub_parts = []
        if non_null:
            sub_parts.append(f"category2 IN ({','.join(['%s']*len(non_null))})")
            params.extend(non_null)
        if has_null:
            sub_parts.append("category2 IS NULL")
        if sub_parts:
            clauses.append("(" + " OR ".join(sub_parts) + ")")
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    finished_filter, finished_params = finished_production_filter_sql()
    return where + finished_filter, params + list(finished_params)


def query_production_daily(
    year: int,
    month: int,
    factories: list[str] | None = None,
    category1_values: list[str] | None = None,
    category2_values: list[str] | None = None,
) -> pd.DataFrame:
    """단일 (year, month) 조회 → tidy DataFrame.

    Columns: date, item_code, item_name, factory, category1, category2,
             planned_qty, actual_qty
    """
    from app.database.db_connection import get_connection

    where, params = _build_filter_clause(factories, category1_values, category2_values)
    sql = (
        "SELECT date, item_code, item_name, factory, category1, category2, "
        "planned_qty, actual_qty FROM production_daily "
        "WHERE YEAR(date)=%s AND MONTH(date)=%s" + where +
        " ORDER BY date, factory, item_code"
    )
    full_params = [int(year), int(month)] + params
    conn = get_connection()
    try:
        df = pd.read_sql_query(sql, conn, params=tuple(full_params))
    finally:
        conn.close()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def query_production_range(
    date_from: str,
    date_to: str,
    factories: list[str] | None = None,
    category1_values: list[str] | None = None,
    category2_values: list[str] | None = None,
) -> pd.DataFrame:
    """기간 조회 -> tidy DataFrame."""
    from app.database.db_connection import get_connection

    where, params = _build_filter_clause(factories, category1_values, category2_values)
    sql = (
        "SELECT date, item_code, item_name, factory, category1, category2, "
        "planned_qty, actual_qty FROM production_daily "
        "WHERE date BETWEEN %s AND %s" + where +
        " ORDER BY date, factory, item_code"
    )
    full_params = [date_from, date_to] + params
    conn = get_connection()
    try:
        df = pd.read_sql_query(sql, conn, params=tuple(full_params))
    finally:
        conn.close()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def query_production_date_range() -> tuple[str | None, str | None]:
    """production_daily 테이블의 날짜 범위 조회."""
    from app.database.db_connection import get_connection

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(date), MAX(date) FROM production_daily")
        row = cursor.fetchone()
        if row and row[0]:
            return (
                row[0].strftime("%Y-%m-%d") if pd.notnull(row[0]) else None,
                row[1].strftime("%Y-%m-%d") if pd.notnull(row[1]) else None,
            )
        return None, None
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()

def query_monthly_summary(
    year_from: int,
    month_from: int,
    year_to: int,
    month_to: int,
    factories: list[str] | None = None,
    category1_values: list[str] | None = None,
    category2_values: list[str] | None = None,
    by: tuple[str, ...] = ("category2",),
) -> pd.DataFrame:
    """기간 내 월별 합계 (그룹키 by 가변).

    plan 합산은 (item_code, year, month) distinct 기준 → 중복 제거된 SUM.
    """
    from app.database.db_connection import get_connection

    where, params = _build_filter_clause(factories, category1_values, category2_values)
    by_list = list(by)
    by_select = ", ".join(by_list) if by_list else ""
    by_group = ", " + by_select if by_select else ""

    # actual 합산
    sql_actual = (
        "SELECT YEAR(date) AS y, MONTH(date) AS m" +
        (", " + by_select if by_select else "") +
        ", SUM(actual_qty) AS monthly_actual "
        "FROM production_daily "
        "WHERE date BETWEEN %s AND %s" + where +
        f" GROUP BY y, m{by_group}"
    )
    # plan 은 (item_code, factory, year, month, [그룹키]) distinct 후 합산.
    # ※ factory 를 distinct 키에 반드시 포함 — 같은 item_code 가 여러 공장에서
    #   생산되는 경우(예: 김해+논산 공통 품목) factory 를 빼면 한 공장 plan 만 살아남음.
    # by 에 'factory' 가 이미 포함되면 중복 추가 안 함.
    inner_extra_keys = [k for k in by_list if k != "factory"]
    inner_extra = (", " + ", ".join(inner_extra_keys)) if inner_extra_keys else ""
    sql_plan = (
        "SELECT y, m" + (", " + by_select if by_select else "") +
        ", SUM(planned_qty) AS monthly_plan FROM ("
        "  SELECT DISTINCT item_code, factory, YEAR(date) AS y, MONTH(date) AS m" +
        inner_extra +
        ", planned_qty FROM production_daily "
        "  WHERE date BETWEEN %s AND %s" + where +
        ") t GROUP BY y, m" + (", " + by_select if by_select else "")
    )

    import calendar
    last_day = calendar.monthrange(year_to, month_to)[1]
    date_from = f"{year_from}-{month_from:02d}-01"
    date_to = f"{year_to}-{month_to:02d}-{last_day:02d}"
    full_params = [date_from, date_to] + params

    conn = get_connection()
    try:
        df_a = pd.read_sql_query(sql_actual, conn, params=tuple(full_params))
        df_p = pd.read_sql_query(sql_plan, conn, params=tuple(full_params))
    finally:
        conn.close()

    if df_a.empty:
        return df_a

    merge_keys = ["y", "m"] + by_list
    out = df_a.merge(df_p, on=merge_keys, how="left").fillna({"monthly_plan": 0.0})
    out["achievement_pct"] = out.apply(
        lambda r: (r["monthly_actual"] / r["monthly_plan"] * 100.0) if r["monthly_plan"] > 0 else 0.0,
        axis=1,
    )
    out = out.rename(columns={"y": "year", "m": "month"})
    out["year_month"] = out["year"].astype(str) + "-" + out["month"].astype(str).str.zfill(2)
    return out


def query_distinct_items(
    factories: list[str] | None = None,
    category1_values: list[str] | None = None,
) -> pd.DataFrame:
    """가용한 (factory × category1 × category2) 조합 목록."""
    from app.database.db_connection import execute_query

    where, params = _build_filter_clause(factories, category1_values, None)
    where_clause = ("WHERE 1=1" + where) if where else ""
    sql = (
        f"SELECT DISTINCT factory, category1, category2 "
        f"FROM production_daily {where_clause} "
        f"ORDER BY factory, category1, category2"
    )
    rows = execute_query(sql, tuple(params))
    return pd.DataFrame(rows)
