"""
Production / Energy Mix Correction Service
==========================================
에너지 DB(`energy_daily.mix_prod_kg`)와 생산실적 DB(`production_daily.actual_qty`)
사이에 존재하는 체계적 차이를 일관된 정의로 보정합니다.

배경
----
* `production_daily` : 빙그레가 자사 명의로 출하하는 **완제품** 일별 실적 (KG)
* `energy_daily.mix_prod_kg` : RawDB_에너지에서 동기화된 raw 믹스 톤 값. 광주의 경우
  자사 완제품(production_daily) 분량과 거의 일치 — **외부 판매용 재공품은 빠져있음**.

특히 광주(광주공장)은 자사 발효유/스낵 외에도, 자사 명의 완제품이 아닌
**외부 판매용 반제품**(탈지분유·살균유·생크림·유크림믹스 등)을 별도로 생산합니다.
이 재공품 활동은 유틸리티는 소비하지만 `energy_daily.mix_prod_kg` 에는 누락돼 있어,
raw 값을 분모로 쓰면 광주 원단위가 비현실적으로 높게 나옵니다.

광주 일부 품목은 **수분을 제거한 무게**(예: 탈지분유 분말)로 재공품 실적이 기록돼
믹스 톤 단위와 직접 합산할 수 없습니다 → ItemCode 별 ``WIP_MIX_CONVERSION`` 환산
계수를 곱해 mix-equivalent kg 로 정규화합니다.

이 모듈의 분해 API는 RawDB_에너지와 DB_생산실적의 차이를 진단하는 보조 도구로
유지합니다. 광주 WIP는 ``DB_재공품.xlsx`` 기반 7품목과 내부 MIS 사정으로
``production_daily``에 기록되는 2품목(129998·129999)을 원천별로 환산하며,
``production_actual_service``에서 운영 화면·메일·원단위 분모에 합산합니다.

이 모듈은 `DB_재공품.xlsx`(공장×일자×품번)을 합쳐 다음 4가지 KG 정의를 한 번에 계산합니다:

    energy_mix_kg          : energy_daily.mix_prod_kg
    finished_kg            : production_daily.SUM(actual_qty)  (자사 완제품)
    wip_kg                 : DB_재공품.xlsx 환산 합계         (믹스 환산 kg)
    accounted_kg           : finished_kg + wip_kg              (관측 가능한 모든 생산)
    residual_kg            : energy_mix_kg - accounted_kg     (≈ 외주/임가공 추정)

원본 간 차이를 별도 분석해야 할 때만 dict로 반환합니다.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.domain.factories import (
    FACTORY_KR_TO_CODE,
    FACTORY_PHYSICAL_DISPLAY_ORDER,
    NAMYANGJU_PARENT_CODE,
    expand_factory_filter,
)
from app.services.v5_common import PATH_WIP_SUMMARY

logger = logging.getLogger(__name__)


# ── 모듈 캐시 ────────────────────────────────────────────────
# DB_재공품.xlsx 는 28개월 × 5공장 / 수백 품번이라 매 호출 reparse 하기 무거움 → 캐시.
_WIP_CACHE: dict[str, pd.DataFrame] = {}
_WIP_CACHE_MTIME: float | None = None
_WIP_LOCK = threading.Lock()

# 다운스트림 st.cache_data(예: query_service 보정 결과) 무효화 판단용 마커.
# _WIP_CACHE_MTIME(로더 캐시)과 독립적으로 관리해 상호 간섭을 막는다.
_WIP_CACHE_CLEAR_MTIME: float | None = None

# 공장별 WIP 단위 신뢰도.
# DB_재공품 의 일부 시트(김해 등)는 KG 가 아닌 EA/BT/CS2 단위 품목까지 합산되어 있어
# kg-환산이 어려우므로, "원본 합산을 그대로 KG로 신뢰할 수 있는 공장" 만 white-list 한다.
# 향후 단위 보정이 확장되면 새 공장을 추가하면 된다.
WIP_TRUSTED_FACTORIES: set[str] = {"광주"}


# 광주(F30) 재공품 → 믹스 환산계수.
# Why: 광주공장만 일부 재공품(예: 260014 탈지분유 = 분말 형태)이 자사 중간제품이 아니라
#   외부로 그대로 판매되며, 수분을 제거한 후 무게로 실적이 기록된다. 따라서 그대로 합산하면
#   생산량이(mix-kg) 과소 집계되기 때문에, ItemCode 별 가중치를 곱한 뒤 행합산한다.
# 외탁 제외(2026-07-09): 광주 260014(탈지분유)·260016(생크림 냉동)은 Job_Number가 숫자로만
#   구성된 경우 외탁 생산분으로, 자공장 에너지를 사용하지 않는다. 이 제외는 상류 RPA 빌더
#   (E:\AI-Elite_MIS_RPA\mis_rpa\wip_refactoring.py 의 OUTSOURCED_EXCLUDE_ITEMCODES)가
#   DB_재공품.xlsx 적재 시점에 실적량 0 처리로 수행하므로, 이 모듈은 파일 값을
#   그대로 신뢰하면 된다 (Job 단위 정보는 요약본에 없음).
WIP_MIX_CONVERSION: dict[str, dict[str, float]] = {
    "광주": {
        "260014": 10.91954,  # 탈지분유 (분말; 수분 제거 후 무게)
        "260016": 1.00000,   # 생크림(냉동)
        "260039": 1.00000,   # 살균유
        "260042": 4.00000,   # 유크림믹스 (농축; 수분 제거 후 무게)
        "260047": 1.00000,   # 생크림(냉장)
        "260351": 1.00000,   # 살균탈지유(수)
        "260352": 1.00000,   # 살균탈지유
    },
}

# 내부 MIS 사정으로 DB_생산실적.xlsx에 기록되지만 실제 분류는 재공품인 품목.
# 이 값들은 DB_재공품.xlsx에 없으므로 WIP_MIX_CONVERSION과 원천을 분리한다.
# 운영 생산량에서는 production_daily 완제품 합계에서 제외한 뒤 아래 계수로
# mix-equivalent kg를 계산해 재공품으로 다시 합산한다.
PRODUCTION_RECORDED_WIP_MIX_CONVERSION: dict[str, dict[str, float]] = {
    "광주": {
        "129998": 10.91954,  # 탈지분유(수) — 기존 재공품 탈지분유와 동일 환산
        "129999": 1.00000,   # 생크림(35%)(수)
    },
}


def _sql_column(alias: str, column: str) -> str:
    """내부 고정 alias를 SQL 컬럼명에 붙인다."""
    return f"{alias}.{column}" if alias else column


def all_wip_mix_conversion(factory: str) -> dict[str, float]:
    """공장의 모든 WIP 품목 환산표(DB_재공품 + 생산실적 기록 재공품)."""
    key = str(factory).strip()
    combined = dict(WIP_MIX_CONVERSION.get(key, {}))
    combined.update(PRODUCTION_RECORDED_WIP_MIX_CONVERSION.get(key, {}))
    return combined


def finished_production_filter_sql(alias: str = "") -> tuple[str, tuple[Any, ...]]:
    """완제품 SQL에서 광주 WIP 품목을 제외하는 AND 절과 파라미터."""
    factory_col = _sql_column(alias, "factory")
    item_col = _sql_column(alias, "item_code")
    codes = tuple(all_wip_mix_conversion("광주"))
    if not codes:
        return "", ()
    placeholders = ",".join(["%s"] * len(codes))
    return (
        f" AND NOT ({factory_col} = %s AND {item_col} IN ({placeholders}))",
        (FACTORY_KR_TO_CODE["광주"], *codes),
    )


def production_recorded_wip_sum_sql(alias: str = "") -> tuple[str, tuple[Any, ...]]:
    """production_daily에 기록된 WIP만 환산하는 SQL 합계식."""
    factory_col = _sql_column(alias, "factory")
    item_col = _sql_column(alias, "item_code")
    qty_col = _sql_column(alias, "actual_qty")
    recorded = PRODUCTION_RECORDED_WIP_MIX_CONVERSION.get("광주", {})
    if not recorded:
        return "0", ()
    cases: list[str] = []
    params: list[Any] = []
    for code, factor in recorded.items():
        cases.append(
            f"WHEN {factory_col} = %s AND {item_col} = %s "
            f"THEN {qty_col} * %s"
        )
        params.extend((FACTORY_KR_TO_CODE["광주"], code, float(factor)))
    return f"SUM(CASE {' '.join(cases)} ELSE 0 END)", tuple(params)


def operational_production_sum_sql(alias: str = "") -> tuple[str, tuple[Any, ...]]:
    """완제품 + production_daily 기록 WIP 환산량의 SQL 합계식."""
    factory_col = _sql_column(alias, "factory")
    item_col = _sql_column(alias, "item_code")
    qty_col = _sql_column(alias, "actual_qty")
    all_codes = tuple(all_wip_mix_conversion("광주"))

    params: list[Any] = []
    if all_codes:
        placeholders = ",".join(["%s"] * len(all_codes))
        finished = (
            f"SUM(CASE WHEN {factory_col} = %s AND {item_col} IN ({placeholders}) "
            f"THEN 0 ELSE {qty_col} END)"
        )
        params.extend((FACTORY_KR_TO_CODE["광주"], *all_codes))
    else:
        finished = f"SUM({qty_col})"

    production_wip, production_wip_params = production_recorded_wip_sum_sql(alias)
    return f"({finished} + {production_wip})", tuple(params) + production_wip_params


@dataclass(frozen=True)
class ProductionBreakdown:
    """단일 (factory, date) 또는 합계 구간의 KG 분해 결과."""
    factory: str
    energy_mix_kg: float
    finished_kg: float
    wip_kg: float
    accounted_kg: float       # finished + wip
    residual_kg: float        # energy_mix - accounted (>=0 일수록 외주/임가공)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "factory": self.factory,
            "energy_mix_kg": float(self.energy_mix_kg),
            "finished_kg": float(self.finished_kg),
            "wip_kg": float(self.wip_kg),
            "accounted_kg": float(self.accounted_kg),
            "residual_kg": float(self.residual_kg),
            "notes": self.notes,
        }


# ── DB_재공품.xlsx 로더 ─────────────────────────────────────
def _load_wip_workbook() -> dict[str, pd.DataFrame]:
    """DB_재공품.xlsx 의 모든 공장 시트를 (date, total_wip_kg) DataFrame 으로 변환.

    공장 시트는 [date, item1, item2, ...] wide 포맷. item 컬럼들의 행별 합을 total_wip_kg 로 사용.
    날짜 컬럼은 첫 컬럼 (인코딩 깨진 한글 헤더 안전 처리).
    """
    global _WIP_CACHE, _WIP_CACHE_MTIME

    src = Path(PATH_WIP_SUMMARY)
    if not src.exists():
        logger.info(f"[wip] 파일 없음 — {src}")
        return {}

    mtime = src.stat().st_mtime
    if _WIP_CACHE and _WIP_CACHE_MTIME == mtime:
        return _WIP_CACHE

    with _WIP_LOCK:
        if _WIP_CACHE and _WIP_CACHE_MTIME == mtime:
            return _WIP_CACHE
        try:
            sheets = pd.read_excel(src, sheet_name=None, engine="openpyxl")
        except Exception as exc:
            logger.error(f"[wip] 워크북 로드 실패: {exc}")
            return {}

        out: dict[str, pd.DataFrame] = {}
        for name, df in sheets.items():
            if df is None or df.empty:
                continue
            # 첫 컬럼 = 날짜
            cols = list(df.columns)
            df2 = df.rename(columns={cols[0]: "date"})
            df2["date"] = pd.to_datetime(df2["date"], errors="coerce")
            df2 = df2.dropna(subset=["date"])
            item_cols = [c for c in df2.columns if c != "date"]
            if not item_cols:
                continue
            num = df2[item_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)

            factor_map = WIP_MIX_CONVERSION.get(str(name).strip())
            if factor_map:
                # 분모 합산은 WIP_MIX_CONVERSION에 명시된 품목만 대상.
                # Why: 광주 WIP 시트에는 분말/원유 외에도 병/캡/포장재(220xxx 등)
                #   다수 품목이 포함돼 있으나, 이들은 AI 예측 모델 피처용일 뿐
                #   에너지 원단위 분모 합산 대상이 아니다. 전체 컬럼을 합산하면
                #   mix_prod_kg 분모가 비현실적으로 부풀려져 원단위가 1/10~1/40으로
                #   왜곡된다 (현장 검증 결과 정상 광주 분모 대비 ~6배 과대).
                target_cols = [c for c in item_cols if str(c).strip() in factor_map]
                if not target_cols:
                    total = pd.Series(0.0, index=num.index)
                else:
                    weights = pd.Series(
                        {c: float(factor_map[str(c).strip()]) for c in target_cols},
                        dtype="float64",
                    )
                    total = num[target_cols].mul(weights, axis=1).sum(axis=1)
            else:
                # 환산표 미정의 공장 — 신뢰 white-list 밖이면 어차피 사용 안 함
                total = num.sum(axis=1)
            out[str(name).strip().upper()] = (
                pd.DataFrame({"date": df2["date"].values, "total_wip_kg": total.values})
                .groupby("date", as_index=False)["total_wip_kg"].sum()
                .sort_values("date")
                .reset_index(drop=True)
            )

        _WIP_CACHE = out
        _WIP_CACHE_MTIME = mtime
        return out


def reload_wip(force: bool = True) -> dict[str, pd.DataFrame]:
    """캐시를 초기화하고 다시 로드한다 (테스트/관리자용)."""
    global _WIP_CACHE, _WIP_CACHE_MTIME
    if force:
        with _WIP_LOCK:
            _WIP_CACHE = {}
            _WIP_CACHE_MTIME = None
    return _load_wip_workbook()


def wip_changed_needs_cache_clear() -> bool:
    """DB_재공품.xlsx 가 '마지막 확인 시점' 이후 변경됐으면 True(1회성) 반환.

    재공품은 DB 동기화 대상이 아니라 엑셀 직접 읽기라, 파일만 단독으로 바뀌면
    query_service 의 ttl=120s @st.cache_data 만료 전까지 화면에 옛 보정값이 남는다.
    app.main 이 매 rerun 에서 이 함수를 호출하고, True 일 때 st.cache_data.clear()
    를 수행하면 재공품 파일 단독 변경도 즉시 반영된다.

    - 파일이 없으면 False.
    - 최초 관측 시엔 기준선(mtime)만 기록하고 False (기동 직후 불필요한 clear 방지).
    - 이후 mtime 이 달라지면 True 를 1회 반환하고 기준선을 갱신한다.
    - 로더의 _WIP_CACHE_MTIME 과 독립된 마커라, 로더 재로드 여부와 무관하게 동작한다.
    """
    global _WIP_CACHE_CLEAR_MTIME
    src = Path(PATH_WIP_SUMMARY)
    if not src.exists():
        return False
    mtime = src.stat().st_mtime
    if _WIP_CACHE_CLEAR_MTIME is None:
        _WIP_CACHE_CLEAR_MTIME = mtime
        return False
    if _WIP_CACHE_CLEAR_MTIME != mtime:
        _WIP_CACHE_CLEAR_MTIME = mtime
        return True
    return False


def get_wip_daily(factory: str) -> pd.DataFrame:
    """단일 공장의 일별 WIP(KG). 신뢰 공장이 아니면 0 으로 채운 빈 시리즈 반환.

    Returns
    -------
    DataFrame with columns: date, total_wip_kg
    """
    wb = _load_wip_workbook()
    f = factory.upper()
    if f not in WIP_TRUSTED_FACTORIES:
        return pd.DataFrame(columns=["date", "total_wip_kg"])
    return wb.get(f, pd.DataFrame(columns=["date", "total_wip_kg"])).copy()


# 광주 재공품 품목명 — WIP_MIX_CONVERSION의 ItemCode와 짝을 이루는 표시 라벨.
# '재공품 믹스' 카드(생산실적 분석 > 제품 믹스)에서 품목별 구성비를 보여줄 때 쓴다.
WIP_ITEM_LABELS: dict[str, dict[str, str]] = {
    "광주": {
        "260014": "탈지분유", "260016": "생크림(냉동)", "260039": "살균유",
        "260042": "유크림믹스", "260047": "생크림(냉장)",
        "260351": "살균탈지유(수)", "260352": "살균탈지유",
        "129998": "탈지분유(수)", "129999": "생크림(35%)(수)",
    },
}

# ── 품목별 detail 캐시 (믹스 % 계산 전용) ──────────────────────
# _load_wip_workbook()은 로드 즉시 날짜별로 합산해 품목 detail을 버리므로,
# '재공품 믹스' 계산에는 별도로 품목별 long-format을 보존하는 캐시가 필요하다.
# 같은 파일을 다시 읽는 이중 비용이 있지만 mtime 캐시라 최초 1회 + 파일 변경 시에만 발생한다.
_WIP_ITEM_CACHE: dict[str, pd.DataFrame] = {}
_WIP_ITEM_CACHE_MTIME: float | None = None
_WIP_ITEM_LOCK = threading.Lock()


def _load_wip_workbook_items() -> dict[str, pd.DataFrame]:
    """공장별 (date, item_code, kg) long-format — 믹스 환산계수 적용 후 값.

    믹스 환산표(WIP_MIX_CONVERSION)가 없는 공장은 품목별 구성비 계산 대상이
    아니므로(신뢰 whitelist 밖) 건너뛴다.
    """
    global _WIP_ITEM_CACHE, _WIP_ITEM_CACHE_MTIME

    src = Path(PATH_WIP_SUMMARY)
    if not src.exists():
        return {}

    mtime = src.stat().st_mtime
    if _WIP_ITEM_CACHE and _WIP_ITEM_CACHE_MTIME == mtime:
        return _WIP_ITEM_CACHE

    with _WIP_ITEM_LOCK:
        if _WIP_ITEM_CACHE and _WIP_ITEM_CACHE_MTIME == mtime:
            return _WIP_ITEM_CACHE
        try:
            sheets = pd.read_excel(src, sheet_name=None, engine="openpyxl")
        except Exception as exc:
            logger.error(f"[wip-items] 워크북 로드 실패: {exc}")
            return {}

        out: dict[str, pd.DataFrame] = {}
        for name, df in sheets.items():
            if df is None or df.empty:
                continue
            factory_key = str(name).strip()
            factor_map = WIP_MIX_CONVERSION.get(factory_key)
            if not factor_map:
                continue
            cols = list(df.columns)
            df2 = df.rename(columns={cols[0]: "date"})
            df2["date"] = pd.to_datetime(df2["date"], errors="coerce")
            df2 = df2.dropna(subset=["date"])
            item_cols = [c for c in df2.columns if c != "date"]
            target_cols = [c for c in item_cols if str(c).strip() in factor_map]
            if not target_cols:
                continue
            num = df2[target_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
            long_rows = [
                pd.DataFrame({
                    "date": df2["date"].values,
                    "item_code": str(col).strip(),
                    "kg": (num[col] * float(factor_map[str(col).strip()])).values,
                })
                for col in target_cols
            ]
            out[factory_key.upper()] = pd.concat(long_rows, ignore_index=True)

        _WIP_ITEM_CACHE = out
        _WIP_ITEM_CACHE_MTIME = mtime
        return out


def get_wip_item_totals(
    factory: str,
    date_from: str | pd.Timestamp,
    date_to: str | pd.Timestamp,
) -> list[dict[str, Any]]:
    """DB_재공품.xlsx 기반 기간 내 재공품 품목별 mix-equivalent kg.

    production_daily에 기록된 재공품은 호출부가
    PRODUCTION_RECORDED_WIP_MIX_CONVERSION으로 별도 합산한다.
    """
    f = factory.upper()
    if f not in WIP_TRUSTED_FACTORIES:
        return []
    items = _load_wip_workbook_items()
    df = items.get(f)
    if df is None or df.empty:
        return []
    df_from = pd.to_datetime(date_from).date()
    df_to = pd.to_datetime(date_to).date()
    mask = (df["date"].dt.date >= df_from) & (df["date"].dt.date <= df_to)
    scoped = df.loc[mask]
    if scoped.empty:
        return []
    totals = scoped.groupby("item_code")["kg"].sum()
    totals = totals[totals > 0]
    labels = WIP_ITEM_LABELS.get(f, {})
    rows = [
        {"item_code": str(code), "name": labels.get(code, code), "kg": float(kg)}
        for code, kg in totals.items()
    ]
    rows.sort(key=lambda r: r["kg"], reverse=True)
    return rows


def wip_mix_from_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """품목별 kg 행을 재공품 구성비(%) 응답으로 정규화한다."""
    combined: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("item_code", "")).strip()
        if not code:
            continue
        kg = pd.to_numeric(row.get("kg"), errors="coerce")
        if pd.isna(kg) or float(kg) <= 0:
            continue
        current = combined.setdefault(
            code,
            {"name": str(row.get("name") or code), "kg": 0.0},
        )
        current["kg"] += float(kg)
    grand_total = sum(float(row["kg"]) for row in combined.values())
    if grand_total <= 0:
        return []
    result = [
        {"name": row["name"], "value": round(float(row["kg"]) / grand_total * 100, 1)}
        for row in combined.values()
    ]
    result.sort(key=lambda row: row["value"], reverse=True)
    return result


def get_wip_mix(
    factory: str,
    date_from: str | pd.Timestamp,
    date_to: str | pd.Timestamp,
) -> list[dict[str, Any]]:
    """DB_재공품.xlsx 기반 재공품 품목별 구성비(%) — 호환용 API."""
    return wip_mix_from_totals(get_wip_item_totals(factory, date_from, date_to))


# ── 핵심 보정 API ────────────────────────────────────────────
def get_breakdown(
    factory: str,
    date_from: str | pd.Timestamp,
    date_to: str | pd.Timestamp,
) -> ProductionBreakdown:
    """기간 단위 합계 분해.

    factory 는 남양주/남양주1/남양주2/김해/광주/논산 모두 지원.
    남양주은 production_daily 가 합산된 코드를 사용하므로 finished_kg 계산은 내부에서 자동 분기.
    """
    from app.database.db_connection import get_connection

    f = factory.upper()
    energy_factory_codes = _energy_factory_codes(f)
    prod_factory_codes = _prod_factory_codes(f)

    df_from = pd.to_datetime(date_from).date()
    df_to = pd.to_datetime(date_to).date()

    conn = get_connection()
    try:
        # energy mix
        if energy_factory_codes:
            qmarks = ",".join(["%s"] * len(energy_factory_codes))
            row = pd.read_sql_query(
                f"SELECT COALESCE(SUM(mix_prod_kg),0) AS s "
                f"FROM energy_daily WHERE factory IN ({qmarks}) AND date BETWEEN %s AND %s",
                conn, params=tuple(energy_factory_codes) + (df_from, df_to),
            )
            energy_mix_kg = float(row.iloc[0]["s"]) if len(row) else 0.0
        else:
            energy_mix_kg = 0.0

        # production_daily: 완제품과 생산실적으로 기록된 WIP를 분리한다.
        production_wip_kg = 0.0
        if prod_factory_codes:
            qmarks = ",".join(["%s"] * len(prod_factory_codes))
            operational_expr, operational_params = operational_production_sum_sql()
            production_wip_expr, production_wip_params = production_recorded_wip_sum_sql()
            row = pd.read_sql_query(
                f"SELECT COALESCE({operational_expr},0) AS operational_kg, "
                f"COALESCE({production_wip_expr},0) AS production_wip_kg "
                f"FROM production_daily WHERE factory IN ({qmarks}) AND date BETWEEN %s AND %s",
                conn,
                params=(
                    *operational_params,
                    *production_wip_params,
                    *prod_factory_codes,
                    df_from,
                    df_to,
                ),
            )
            operational_kg = float(row.iloc[0]["operational_kg"]) if len(row) else 0.0
            production_wip_kg = float(row.iloc[0]["production_wip_kg"]) if len(row) else 0.0
            finished_kg = operational_kg - production_wip_kg
        else:
            finished_kg = 0.0
    finally:
        conn.close()

    # WIP = production_daily 기록 재공품 + DB_재공품.xlsx(신뢰 공장 한정)
    wip_kg = production_wip_kg
    note_bits: list[str] = []
    for f_code in (energy_factory_codes or [f]):
        wdf = get_wip_daily(f_code)
        if wdf.empty:
            continue
        m = (wdf["date"].dt.date >= df_from) & (wdf["date"].dt.date <= df_to)
        wip_kg += float(wdf.loc[m, "total_wip_kg"].sum())
    if not energy_factory_codes:
        note_bits.append("energy_factory_codes=미정의")

    accounted = finished_kg + wip_kg
    residual = energy_mix_kg - accounted

    if f == "광주":
        note_bits.append(
            "광주 mix_prod_kg(raw)는 자사 완제품 분량이며 외부판매 재공품은 별도. "
            "프론트엔드는 accounted_kg(=완제품+재공품 환산)를 분모로 사용."
        )
    if energy_mix_kg > 0 and accounted > 0 and residual / energy_mix_kg > 0.4:
        note_bits.append(
            f"residual 비중 {residual / energy_mix_kg:.0%} — WIP/임가공 외 추가 누락 가능"
        )

    return ProductionBreakdown(
        factory=f,
        energy_mix_kg=energy_mix_kg,
        finished_kg=finished_kg,
        wip_kg=wip_kg,
        accounted_kg=accounted,
        residual_kg=residual,
        notes=" / ".join(note_bits),
    )


def get_breakdown_daily(
    factory: str,
    date_from: str | pd.Timestamp,
    date_to: str | pd.Timestamp,
) -> pd.DataFrame:
    """일자별 분해 — UI 그래프용. 결과 컬럼:
    date, factory, energy_mix_kg, finished_kg, wip_kg, accounted_kg, residual_kg
    """
    from app.database.db_connection import get_connection

    f = factory.upper()
    energy_factory_codes = _energy_factory_codes(f)
    prod_factory_codes = _prod_factory_codes(f)

    df_from = pd.to_datetime(date_from).date()
    df_to = pd.to_datetime(date_to).date()

    conn = get_connection()
    try:
        if energy_factory_codes:
            qmarks = ",".join(["%s"] * len(energy_factory_codes))
            energy_df = pd.read_sql_query(
                f"SELECT date, SUM(mix_prod_kg) AS energy_mix_kg "
                f"FROM energy_daily WHERE factory IN ({qmarks}) AND date BETWEEN %s AND %s "
                f"GROUP BY date",
                conn, params=tuple(energy_factory_codes) + (df_from, df_to),
            )
        else:
            energy_df = pd.DataFrame(columns=["date", "energy_mix_kg"])

        if prod_factory_codes:
            qmarks = ",".join(["%s"] * len(prod_factory_codes))
            operational_expr, operational_params = operational_production_sum_sql()
            production_wip_expr, production_wip_params = production_recorded_wip_sum_sql()
            prod_df = pd.read_sql_query(
                f"SELECT date, {operational_expr} AS operational_kg, "
                f"{production_wip_expr} AS production_wip_kg "
                f"FROM production_daily WHERE factory IN ({qmarks}) AND date BETWEEN %s AND %s "
                f"GROUP BY date",
                conn,
                params=(
                    *operational_params,
                    *production_wip_params,
                    *prod_factory_codes,
                    df_from,
                    df_to,
                ),
            )
            prod_df["finished_kg"] = (
                pd.to_numeric(prod_df["operational_kg"], errors="coerce").fillna(0.0)
                - pd.to_numeric(prod_df["production_wip_kg"], errors="coerce").fillna(0.0)
            )
        else:
            prod_df = pd.DataFrame(
                columns=["date", "operational_kg", "production_wip_kg", "finished_kg"]
            )
    finally:
        conn.close()

    energy_df["date"] = pd.to_datetime(energy_df["date"]) if not energy_df.empty else pd.Series([], dtype="datetime64[ns]")
    prod_df["date"] = pd.to_datetime(prod_df["date"]) if not prod_df.empty else pd.Series([], dtype="datetime64[ns]")

    # WIP: production_daily 기록 WIP + DB_재공품.xlsx WIP 합산
    wip_parts = []
    if not prod_df.empty:
        wip_parts.append(
            prod_df[["date", "production_wip_kg"]]
            .rename(columns={"production_wip_kg": "total_wip_kg"})
        )
    for f_code in (energy_factory_codes or [f]):
        wdf = get_wip_daily(f_code)
        if not wdf.empty:
            mask = (wdf["date"].dt.date >= df_from) & (wdf["date"].dt.date <= df_to)
            wip_parts.append(wdf.loc[mask].copy())
    if wip_parts:
        wip_df = (
            pd.concat(wip_parts, ignore_index=True)
            .groupby("date", as_index=False)["total_wip_kg"].sum()
            .rename(columns={"total_wip_kg": "wip_kg"})
        )
    else:
        wip_df = pd.DataFrame(columns=["date", "wip_kg"])

    out = energy_df.merge(prod_df[["date", "finished_kg"]], on="date", how="outer")
    out = out.merge(wip_df, on="date", how="outer")
    for c in ("energy_mix_kg", "finished_kg", "wip_kg"):
        if c not in out.columns:
            out[c] = 0.0
    out[["energy_mix_kg", "finished_kg", "wip_kg"]] = out[
        ["energy_mix_kg", "finished_kg", "wip_kg"]
    ].fillna(0.0)
    out["accounted_kg"] = out["finished_kg"] + out["wip_kg"]
    out["residual_kg"] = out["energy_mix_kg"] - out["accounted_kg"]
    out["factory"] = f
    out = out.sort_values("date").reset_index(drop=True)
    return out[
        ["date", "factory", "energy_mix_kg", "finished_kg",
         "wip_kg", "accounted_kg", "residual_kg"]
    ]


# ── 헬퍼: 코드 매핑 ───────────────────────────────────────────
# energy_daily      : 한글(남양주1/남양주2/김해/광주/논산)
# production_daily  : F-code (F10A/F10B/F20/F30/F40)
#   * 남양주는 사내 DW 가 F10 통합으로 추출되지만 production_dw_service.parse_sheet 가
#     "냉장+MY → F10A, 그 외 → F10B" 룰로 자동 분리하므로 DB 적재 시점에는 F10A/F10B 만 존재.
def _energy_factory_codes(f: str) -> list[str]:
    f = f.upper()
    if f in {"전사", "ALL"}:
        return list(FACTORY_PHYSICAL_DISPLAY_ORDER)
    expanded = expand_factory_filter([f])
    if expanded:
        return expanded
    if f in FACTORY_PHYSICAL_DISPLAY_ORDER:
        return [f]
    # 비표준 코드 — 그대로 시도
    return [f]


def _prod_factory_codes(f: str) -> list[str]:
    f = f.upper()
    # 마이그레이션(2026-05-04) 이후 production_daily 의 남양주 데이터는
    # 신규 F10A(=남양주1, 냉장+MY 전담) / F10B(=남양주2, 그 외) 로 분리 적재됨.
    # legacy F10 fallback 도 함께 포함해 재동기화 누락 환경에서도 누락 없이 조회.
    if f in ("남양주1", "남양주2"):
        return [FACTORY_KR_TO_CODE[f], NAMYANGJU_PARENT_CODE]
    if f == "남양주":
        return [
            FACTORY_KR_TO_CODE["남양주1"],
            FACTORY_KR_TO_CODE["남양주2"],
            NAMYANGJU_PARENT_CODE,
        ]
    if f in FACTORY_KR_TO_CODE:
        return [FACTORY_KR_TO_CODE[f]]
    if f in {"전사", "ALL"}:
        return [
            FACTORY_KR_TO_CODE[factory]
            for factory in FACTORY_PHYSICAL_DISPLAY_ORDER
        ] + [NAMYANGJU_PARENT_CODE]
    return [f]


# ── AI/리포트용 텍스트 빌더 ───────────────────────────────────
def build_breakdown_caption(b: ProductionBreakdown) -> str:
    """KPI 카드/캡션에 사용할 한 줄 요약."""
    if b.energy_mix_kg <= 0:
        return f"{b.factory} 데이터 없음"
    pct_finished = b.finished_kg / b.energy_mix_kg * 100 if b.energy_mix_kg else 0
    pct_wip = b.wip_kg / b.energy_mix_kg * 100 if b.energy_mix_kg else 0
    pct_residual = b.residual_kg / b.energy_mix_kg * 100 if b.energy_mix_kg else 0
    bits = [
        f"완제품 {pct_finished:.0f}%",
        f"재공품 {pct_wip:.0f}%",
    ]
    if abs(pct_residual) > 1:
        bits.append(f"외주 {pct_residual:.0f}%")
    return " / ".join(bits)


__all__ = [
    "ProductionBreakdown",
    "WIP_TRUSTED_FACTORIES",
    "WIP_MIX_CONVERSION",
    "PRODUCTION_RECORDED_WIP_MIX_CONVERSION",
    "WIP_ITEM_LABELS",
    "all_wip_mix_conversion",
    "finished_production_filter_sql",
    "operational_production_sum_sql",
    "production_recorded_wip_sum_sql",
    "get_wip_daily",
    "get_wip_item_totals",
    "get_wip_mix",
    "wip_mix_from_totals",
    "get_breakdown",
    "get_breakdown_daily",
    "build_breakdown_caption",
    "reload_wip",
    "wip_changed_needs_cache_clear",
]
