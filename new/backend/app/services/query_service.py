"""
Query Service
=============
데이터 조회 및 집계 서비스.
"""
# 이 파일은 DB에서 화면용 데이터를 가져오고 가공합니다.

import pandas as pd
import streamlit as st
from app.domain.factories import (
    ENERGY_UNIT_CALC_MAP,
    FACTORY_FILTER_OPTIONS,
    FACTORY_QUERY_ORDER,
    YOY_FACTORY_DEFS,
    expand_factory_filter,
    filter_factory_frame,
    recalc_unit_rates,
)
from app.database.db_connection import get_connection
from app.services.production_actual_service import overlay_actual_production

# 사용량 컬럼 (합계 대상)
USAGE_COLUMNS = [
    "freezing_power_kwh",
    "air_compressor_kwh",
    "total_power_kwh",
    "fuel_nm3",
    "water_ton",
    "wastewater_ton",
    "mix_prod_kg",
]

# 원단위 컬럼 (폐수 원단위는 폐기 — 폐수/용수 비로 대체)
UNIT_CONSUMPTION_COLUMNS = [
    "power_per_ton_kwh",
    "fuel_per_ton_nm3",
    "water_per_ton_ton",
]

# 원단위 계산 매핑: 원단위 컬럼 → 사용량 컬럼
UNIT_CALC_MAP = dict(ENERGY_UNIT_CALC_MAP)


# 일별 데이터 값을 가져옵니다.
# 동일 파라미터로 매 rerun마다 DB를 재조회하지 않도록 ttl=120s 캐시.
# 데이터 변경 직후엔 st.cache_data.clear()로 무효화함.
@st.cache_data(ttl=120, show_spinner=False)
def get_daily_data(
    factories: list[str] = None,
    date_from: str = None,
    date_to: str = None,
) -> pd.DataFrame:
    """
    일별 데이터 조회.

    Parameters
    ----------
    factories : list[str] or None
        공장 코드 목록, None이면 전체
    date_from, date_to : str
        기간 필터 (YYYY-MM-DD)
    """
    query = "SELECT * FROM energy_daily WHERE 1=1"
    params = []

    db_factories = []
    if factories and "전체" not in factories and "전사" not in factories:
        db_factories = expand_factory_filter(factories)
        placeholders = ", ".join(["%s"] * len(db_factories))
        query += f" AND factory IN ({placeholders})"
        params.extend(db_factories)

    if date_from:
        query += " AND date >= %s"
        params.append(date_from)

    if date_to:
        query += " AND date <= %s"
        params.append(date_to)

    query += " ORDER BY date, factory"

    conn = get_connection()
    try:
        df = pd.read_sql_query(query, conn, params=params)
        if not df.empty:
            df = df.sort_values(by=['date', 'factory']).reset_index(drop=True)
            for col in USAGE_COLUMNS + UNIT_CONSUMPTION_COLUMNS:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
    finally:
        conn.close()

    # 생산량은 전 공장 공통으로 DB_생산실적(production_daily.actual_qty) 합계로 교체한다.
    # RawDB_에너지의 mix_prod_kg는 원본 보존용이며 화면·원단위 계산에는 사용하지 않는다.
    df = overlay_actual_production(df)

    # 필터 처리 및 계산 로직
    if factories is not None:
        num_cols = [c for c in df.columns if c not in ("date", "factory", "id", "created_at", "updated_at", "changed_by")]
        parts = []

        if "전체" in factories:
            parts.append(df)
            
        if "전사" in factories:
            if not df.empty:
                jeonsa_src = df.copy()
                jeonsa_src["factory"] = "전사"
                jeonsa_agg = jeonsa_src.groupby(["date", "factory"], as_index=False)[num_cols].sum()
                jeonsa_agg = recalc_unit_rates(jeonsa_agg)
                parts.append(jeonsa_agg)

        if "남양주" in factories:
            f10_src = filter_factory_frame(df, "남양주").copy()
            if not f10_src.empty:
                f10_src["factory"] = "남양주"
                f10_agg = f10_src.groupby(["date", "factory"], as_index=False)[num_cols].sum()
                f10_agg = recalc_unit_rates(f10_agg)
                parts.append(f10_agg)
                
        # 개별 요청된 실제 공장 데이터 유지
        target_physical = [f for f in factories if f not in ("전사", "남양주", "전체")]
        if target_physical:
            parts.append(df[df["factory"].isin(target_physical)])

        if parts:
            df = pd.concat(parts, ignore_index=True)
            df = df.sort_values(by=['date', 'factory']).reset_index(drop=True)
        else:
            df = pd.DataFrame()

    return df



# 월별 데이터 값을 가져옵니다.
@st.cache_data(ttl=120, show_spinner=False)
def get_monthly_data(
    factories: list[str] = None,
    date_from: str = None,
    date_to: str = None,
) -> pd.DataFrame:
    """
    월별 집계 데이터.
    사용량 = SUM(일별 값)
    원단위 = SUM(사용량) / SUM(mix_prod_kg / 1000)  ← 재계산
    """
    daily = get_daily_data(factories, date_from, date_to)
    if daily.empty:
        return pd.DataFrame()

    daily["date"] = pd.to_datetime(daily["date"])
    daily["year_month"] = daily["date"].dt.to_period("M").astype(str)

    # 사용량 합계
    agg_dict = {col: "sum" for col in USAGE_COLUMNS if col in daily.columns}
    monthly = daily.groupby(["factory", "year_month"]).agg(agg_dict).reset_index()

    # 원단위 재계산: SUM(사용량) / SUM(mix_prod_kg / 1000)
    monthly = recalc_unit_rates(monthly)

    return monthly


# 전년 대비 데이터 값을 가져옵니다.
@st.cache_data(ttl=120, show_spinner=False)
def get_yoy_data(
    factory: str,
    year: int,
    metric: str = "total_power_kwh",
) -> pd.DataFrame:
    """
    전년 대비 분석 데이터.
    현재 연도와 전년도 월별 데이터 비교.
    """
    current_year = year
    prev_year = year - 1

    conn = get_connection()
    try:
        if factory in ("전사", "전체"):
            db_factories = []
        elif factory == "남양주":
            db_factories = expand_factory_filter([factory])
        else:
            db_factories = [factory]

        if db_factories:
            placeholders = ", ".join(["%s"] * len(db_factories))
            query = f"""
                SELECT * FROM energy_daily
                WHERE factory IN ({placeholders})
                AND (DATE_FORMAT(date, '%Y') = %s OR DATE_FORMAT(date, '%Y') = %s)
                ORDER BY date
            """
            params = db_factories + [str(current_year), str(prev_year)]
        else:
            query = f"""
                SELECT * FROM energy_daily
                WHERE (DATE_FORMAT(date, '%Y') = %s OR DATE_FORMAT(date, '%Y') = %s)
                ORDER BY date
            """
            params = [str(current_year), str(prev_year)]
            
        df = pd.read_sql_query(query, conn, params=params)
        if not df.empty:
            for col in USAGE_COLUMNS + UNIT_CONSUMPTION_COLUMNS:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame()

    # 전년 비교도 일별 집계 전에 DB_생산실적을 오버레이해 모든 화면의 분모를 통일한다.
    df = overlay_actual_production(df)

    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month

    # 월별 집계
    agg_dict = {col: "sum" for col in USAGE_COLUMNS if col in df.columns}
    monthly = df.groupby(["year", "month"]).agg(agg_dict).reset_index()

    # 원단위 재계산
    monthly = recalc_unit_rates(monthly)

    return monthly


# 공장 값을 가져옵니다.
def get_factories() -> list[str]:
    """고정 공장 목록 반환 (DB 조회 없이 하드코딩된 표준 목록).

    표시 순서: 집계 공장(남양주) → 실공장(남양주1, 남양주2) → 김해 → 광주 → 논산 → 경산.
    이 순서는 대시보드의 FACTORY_OPTIONS / 비교분석 페이지 / AI 보고서 등
    모든 화면에서 동일하게 적용되도록 통일된 표준입니다.
    """
    return list(FACTORY_FILTER_OPTIONS)


# 날짜 범위 값을 가져옵니다.
@st.cache_data(ttl=300, show_spinner=False)
def get_date_range() -> tuple[str, str]:
    """DB에 존재하는 날짜 범위 조회."""
    conn = get_connection()
    cursor = None  # cursor 생성 실패 시 finally의 UnboundLocalError 방지
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(date), MAX(date) FROM energy_daily")
        row = cursor.fetchone()
        if row and row[0]:
            return row[0].strftime("%Y-%m-%d") if pd.notnull(row[0]) else None, row[1].strftime("%Y-%m-%d") if pd.notnull(row[1]) else None
        return None, None
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()


# 기록 개수 값을 가져옵니다.
@st.cache_data(ttl=120, show_spinner=False)
def get_record_count() -> int:
    """전체 레코드 수 조회."""
    conn = get_connection()
    cursor = None  # cursor 생성 실패 시 finally의 UnboundLocalError 방지
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM energy_daily")
        return cursor.fetchone()[0]
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()


# ──────────────────────────────────────────────────────────
# 대시보드 전용 쿼리 함수
# ──────────────────────────────────────────────────────────

# (label, db_code)
# db_code=None: 전사(전체), "남양주": 남양주1+남양주2 합산, 나머지: 해당 코드만
FACTORY_ORDER = list(FACTORY_QUERY_ORDER)


# calc 단위 rate 관련 처리를 담당합니다.
def _calc_unit_rate(df: pd.DataFrame, usage_col: str, prod_col: str = "mix_prod_kg"):
    """집계된 DF에서 원단위 계산: SUM(usage) / SUM(prod_kg/1000)"""
    total_prod_ton = df[prod_col].sum() / 1000
    if total_prod_ton <= 0:
        return None
    return df[usage_col].sum() / total_prod_ton


# 필터 공장 관련 처리를 담당합니다.
def _filter_factory(df: pd.DataFrame, factory_label: str, db_code) -> pd.DataFrame:
    """전사 또는 특정 공장 필터.
    db_code=None  → 전사(전체)
    db_code='남양주' → 남양주1 + 남양주2 합산
    그 외          → 해당 코드만
    """
    return filter_factory_frame(df, db_code)


# 7day trend 값을 가져옵니다.
@st.cache_data(ttl=120, show_spinner=False)
def get_7day_trend(base_date: str, factory: str = "전사") -> pd.DataFrame:
    """
    기준일 기준 최근 7일간 일별 집계 반환.

    Parameters
    ----------
    base_date : str
        기준일 (YYYY-MM-DD)
    factory : str
        '전사' | '남양주1' | '남양주2' | '남양주' | '김해' | '광주' | '논산'

    반환 컨럼: date, mix_prod_kg, power_per_ton, fuel_per_ton, water_per_ton, wastewater_ratio
    """
    base = pd.to_datetime(base_date)
    date_from = (base - pd.Timedelta(days=6)).strftime("%Y-%m-%d")

    # 사업장 필터
    if factory == "전사":
        factories_param = None
    elif factory == "남양주":
        factories_param = expand_factory_filter([factory])
    else:
        factories_param = [factory]

    df = get_daily_data(factories=factories_param, date_from=date_from, date_to=base_date)
    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])

    agg = df.groupby("date").agg({
        "mix_prod_kg": "sum",
        "total_power_kwh": "sum",
        "fuel_nm3": "sum",
        "water_ton": "sum",
        "wastewater_ton": "sum",
    }).reset_index()

    agg["power_per_ton"] = agg.apply(
        lambda r: r["total_power_kwh"] / (r["mix_prod_kg"] / 1000) if r["mix_prod_kg"] > 0 else None, axis=1)
    agg["fuel_per_ton"] = agg.apply(
        lambda r: r["fuel_nm3"] / (r["mix_prod_kg"] / 1000) if r["mix_prod_kg"] > 0 else None, axis=1)
    agg["water_per_ton"] = agg.apply(
        lambda r: r["water_ton"] / (r["mix_prod_kg"] / 1000) if r["mix_prod_kg"] > 0 else None, axis=1)
    # 폐수/용수 = 폐수량 / 용수량 (소수점 비율). 용수량 0이면 산출 불가(None).
    agg["wastewater_ratio"] = agg.apply(
        lambda r: r["wastewater_ton"] / r["water_ton"] if r["water_ton"] > 0 else None, axis=1)

    return agg.sort_values("date").reset_index(drop=True)


# comparison 행 데이터를 만듭니다.
def _build_comparison_row(
    label: str,
    factory_label: str,
    df_curr_ytd: pd.DataFrame,
    df_curr_mtd: pd.DataFrame,
    df_prev_full: pd.DataFrame,
    df_prev_month: pd.DataFrame,
    df_prev_ytd: pd.DataFrame,
    df_prev_mtd: pd.DataFrame,
    db_code,
    usage_col: str,
    is_usage: bool = False,
    ratio_spec: tuple[str, str, float] | None = None,
) -> dict:
    """한 행 생성: 구분, 공장, 당해누계, 당해월, 전년실적, 비, 전년동월, 비, 전년동기, 비, 전년동월_MTD, 비

    ratio_spec=(num_col, den_col, scale) 가 주어지면 원단위 대신
    SUM(num)/SUM(den)×scale 비율을 계산한다 (예: 폐수/용수 = 폐수/용수, scale=1).
    """
    # val 값을 가져옵니다.
    def get_val(df):
        sub = _filter_factory(df, factory_label, db_code)
        if sub.empty:
            return None
        if ratio_spec is not None:
            num_col, den_col, scale = ratio_spec
            if num_col not in sub.columns or den_col not in sub.columns:
                return None
            den = sub[den_col].sum()
            if den <= 0:
                return None
            return sub[num_col].sum() / den * scale
        if usage_col not in sub.columns:
            return None
        if is_usage:
            return sub[usage_col].sum()
        return _calc_unit_rate(sub, usage_col)

    curr_ytd_val = get_val(df_curr_ytd)
    curr_mtd_val = get_val(df_curr_mtd)
    prev_full_val = get_val(df_prev_full)
    prev_month_val = get_val(df_prev_month)
    prev_ytd_val = get_val(df_prev_ytd)
    prev_mtd_val = get_val(df_prev_mtd)

    # ratio 관련 처리를 담당합니다.
    def ratio(a, b):
        if a is not None and b is not None and b > 0:
            return a / b
        return None

    return {
        "구분": label,
        "공장": factory_label,
        "당해누계": curr_ytd_val,
        "당해월": curr_mtd_val,
        "전년실적": prev_full_val,
        "전년실적비": ratio(curr_ytd_val, prev_full_val),
        "전년동월": prev_month_val,
        "전년동월비": ratio(curr_ytd_val, prev_month_val),
        "전년동기": prev_ytd_val,
        "전년동기비": ratio(curr_ytd_val, prev_ytd_val),
        "전년동월_MTD": prev_mtd_val,
        "전년동월_MTD비": ratio(curr_mtd_val, prev_mtd_val),
    }


# period params 데이터를 만듭니다.
def _build_period_params(base_date: str) -> dict:
    """
    비교 기간 파라미터 + 진척률 2종 반환.

    진척률 정의
    -------
    annual_progress  : 오늘까지 연누계 일수 / 전년 연간 일수 × 100
    period_progress  : 오늘까지 연누계 일수 / 전년 동월 말일까지의 연누계 일수 × 100
    """
    import calendar
    base = pd.to_datetime(base_date)
    year = base.year
    prev_year = year - 1
    last_day_of_month = calendar.monthrange(prev_year, base.month)[1]

    # 연누계 일수: 1월 1일 ~ base_date
    ytd_days = (base - pd.Timestamp(f"{year}-01-01")).days + 1
    # 전년 연간 일수
    prev_year_days = 366 if calendar.isleap(prev_year) else 365
    # 전년 동월 말일까지의 연누계 일수
    prev_month_end = pd.Timestamp(f"{prev_year}-{base.month:02d}-{last_day_of_month:02d}")
    prev_month_ytd_days = (prev_month_end - pd.Timestamp(f"{prev_year}-01-01")).days + 1

    return {
        "curr_ytd":        (f"{year}-01-01", base_date),
        "curr_mtd":        (f"{year}-{base.month:02d}-01", base_date),
        "prev_full":       (f"{prev_year}-01-01", f"{prev_year}-12-31"),
        "prev_month":      (f"{prev_year}-01-01",
                            f"{prev_year}-{base.month:02d}-{last_day_of_month:02d}"),
        "prev_ytd":        (f"{prev_year}-01-01",
                            f"{prev_year}-{base.month:02d}-{base.day:02d}"),
        "prev_mtd":        (f"{prev_year}-{base.month:02d}-01",
                            f"{prev_year}-{base.month:02d}-{base.day:02d}"),
        # 진척률 2종 (%)
        "annual_progress":  ytd_days / prev_year_days * 100,
        "period_progress":  ytd_days / prev_month_ytd_days * 100,
    }


# 단위 rate comparison 값을 가져옵니다.
@st.cache_data(ttl=120, show_spinner=False)
def get_unit_rate_comparison(base_date: str) -> pd.DataFrame:
    """
    원단위 현황: 전사 + 사업장별 × 4개 원단위.

    컬럼: 구분, 공장, 당해누계, 전년실적, 전년실적비, 전년동월, 전년동월비, 전년동기, 전년동기비
    """
    p = _build_period_params(base_date)
    df_curr_ytd = get_daily_data(date_from=p["curr_ytd"][0], date_to=p["curr_ytd"][1])
    df_curr_mtd = get_daily_data(date_from=p["curr_mtd"][0], date_to=p["curr_mtd"][1])
    df_prev_full = get_daily_data(date_from=p["prev_full"][0], date_to=p["prev_full"][1])
    df_prev_month = get_daily_data(date_from=p["prev_month"][0], date_to=p["prev_month"][1])
    df_prev_ytd = get_daily_data(date_from=p["prev_ytd"][0], date_to=p["prev_ytd"][1])
    df_prev_mtd = get_daily_data(date_from=p["prev_mtd"][0], date_to=p["prev_mtd"][1])

    # (라벨, usage_col, ratio_spec). ratio_spec 이 있으면 원단위 대신 비율을 계산.
    unit_defs = [
        ("전력 원단위\n[kWh/mix-ton]", "total_power_kwh", None),
        ("연료 원단위\n[Nm³/mix-ton]", "fuel_nm3", None),
        ("용수 원단위\n[ton/mix-ton]", "water_ton", None),
        ("폐수/용수", None, ("wastewater_ton", "water_ton", 1.0)),
    ]

    rows = []
    for unit_label, usage_col, ratio_spec in unit_defs:
        for factory_label, db_code in FACTORY_ORDER:
            rows.append(_build_comparison_row(
                unit_label, factory_label,
                df_curr_ytd, df_curr_mtd, df_prev_full, df_prev_month, df_prev_ytd, df_prev_mtd,
                db_code, usage_col, is_usage=False, ratio_spec=ratio_spec,
            ))
    return pd.DataFrame(rows)


# 생산 사용량 comparison 값을 가져옵니다.
@st.cache_data(ttl=120, show_spinner=False)
def get_production_usage_comparison(base_date: str) -> pd.DataFrame:
    """
    생산량 및 사용량 현황: 전사 + 사업장별 × 5개 항목.

    컬럼: 구분, 공장, 당해누계, 전년실적, 전년실적비, 전년동월, 전년동월비, 전년동기, 전년동기비
    """
    p = _build_period_params(base_date)
    df_curr_ytd = get_daily_data(date_from=p["curr_ytd"][0], date_to=p["curr_ytd"][1])
    df_curr_mtd = get_daily_data(date_from=p["curr_mtd"][0], date_to=p["curr_mtd"][1])
    df_prev_full = get_daily_data(date_from=p["prev_full"][0], date_to=p["prev_full"][1])
    df_prev_month = get_daily_data(date_from=p["prev_month"][0], date_to=p["prev_month"][1])
    df_prev_ytd = get_daily_data(date_from=p["prev_ytd"][0], date_to=p["prev_ytd"][1])
    df_prev_mtd = get_daily_data(date_from=p["prev_mtd"][0], date_to=p["prev_mtd"][1])

    usage_defs = [
        ("생산량(DB 실적)\n[ton]", "mix_prod_kg"),
        ("전력 사용량\n[kWh]", "total_power_kwh"),
        ("연료 사용량\n[Nm³]", "fuel_nm3"),
        ("용수 사용량\n[ton]", "water_ton"),
        ("폐수 사용량\n[ton]", "wastewater_ton"),
    ]

    rows = []
    for usage_label, usage_col in usage_defs:
        for factory_label, db_code in FACTORY_ORDER:
            row = _build_comparison_row(
                usage_label, factory_label,
                df_curr_ytd, df_curr_mtd, df_prev_full, df_prev_month, df_prev_ytd, df_prev_mtd,
                db_code, usage_col, is_usage=True,
            )
            # 생산량은 kg → ton 변환
            if usage_col == "mix_prod_kg":
                for k in ["당해누계", "당해월", "전년실적", "전년동월", "전년동기", "전년동월_MTD"]:
                    if row.get(k) is not None:
                        row[k] = row[k] / 1000
            rows.append(row)
    return pd.DataFrame(rows)


# 월별 전년 대비 요약 값을 가져옵니다.
@st.cache_data(ttl=120, show_spinner=False)
def get_monthly_yoy_summary(year: int, month: int, data_type: str) -> pd.DataFrame:
    """
    특정 연월 기준 공장별 원단위/사용량/생산량 전년비 데이터 반환.

    Parameters
    ----------
    year  : 기준 연도 (현재 연도)
    month : 기준 월
    data_type : "원단위", "사용량", "생산량" 

    반환 컬럼
    ---------
    factory           : 공장 코드 (전사, 남양주1, 남양주2, 남양주, 김해, 광주, 논산)
    curr_power, ...   : 현재 연도·월 집계값
    """
    import calendar as _cal

    prev_year = year - 1
    last_day_curr  = _cal.monthrange(year,      month)[1]
    last_day_prev  = _cal.monthrange(prev_year, month)[1]

    curr_from = f"{year}-{month:02d}-01"
    curr_to   = f"{year}-{month:02d}-{last_day_curr:02d}"
    prev_from = f"{prev_year}-{month:02d}-01"
    prev_to   = f"{prev_year}-{month:02d}-{last_day_prev:02d}"

    df_curr = get_daily_data(date_from=curr_from, date_to=curr_to)
    df_prev = get_daily_data(date_from=prev_from, date_to=prev_to)

    # 공장별 집계 정의
    factory_defs = list(YOY_FACTORY_DEFS)

    # 원단위 뷰의 4번째 지표는 폐수 원단위 대신 폐수/용수 비(wwratio, 폐수량/용수량).
    # 사용량 뷰는 폐수 사용량(raw)을 그대로 유지한다.
    if data_type == "생산량":
        unit_pairs = [("prod", "mix_prod_kg")]
    elif data_type == "원단위":
        unit_pairs = [
            ("power", "total_power_kwh"),
            ("fuel",  "fuel_nm3"),
            ("water", "water_ton"),
            ("wwratio", None),
        ]
    else:  # "사용량"
        unit_pairs = [
            ("power", "total_power_kwh"),
            ("fuel",  "fuel_nm3"),
            ("water", "water_ton"),
            ("waste", "wastewater_ton"),
        ]

    # agg 지표 관련 처리를 담당합니다.
    def _agg_metric(df: pd.DataFrame, db_code, key: str, usage_col: str | None):
        if df.empty:
            return None
        sub = filter_factory_frame(df, db_code)
        if sub.empty:
            return None

        if data_type == "원단위":
            if key == "wwratio":
                total_water = sub["water_ton"].sum()
                if total_water <= 0:
                    return None
                return sub["wastewater_ton"].sum() / total_water
            total_prod_ton = sub["mix_prod_kg"].sum() / 1000
            if total_prod_ton <= 0:
                return None
            return sub[usage_col].sum() / total_prod_ton
        elif data_type == "생산량":
            return sub[usage_col].sum() / 1000
        else: # "사용량"
            return sub[usage_col].sum()

    rows = []
    for label, db_code in factory_defs:
        row = {"factory": label}
        for key, col in unit_pairs:
            row[f"curr_{key}"] = _agg_metric(df_curr, db_code, key, col)
            row[f"prev_{key}"] = _agg_metric(df_prev, db_code, key, col)
        rows.append(row)

    return pd.DataFrame(rows)
