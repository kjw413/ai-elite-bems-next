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

프론트엔드 표시는 ``query_service._apply_gwangju_correction`` 이 광주 행의 mix_prod_kg
를 ``accounted_kg`` 로 교체하므로, 모든 대시보드/차트가 자동으로 보정된 원단위를 봅니다.
``residual_kg`` 는 광주에서 음수로 나올 수 있으며(=정의상 mix - accounted), 광주는
외주 부담이 raw mix 에 잡혀있지 않다는 신호입니다.

이 모듈은 `DB_재공품.xlsx`(공장×일자×품번)을 합쳐 다음 4가지 KG 정의를 한 번에 계산합니다:

    energy_mix_kg          : energy_daily.mix_prod_kg
    finished_kg            : production_daily.SUM(actual_qty)  (자사 완제품)
    wip_kg                 : DB_재공품.xlsx 환산 합계         (믹스 환산 kg)
    accounted_kg           : finished_kg + wip_kg              (관측 가능한 모든 생산)
    residual_kg            : energy_mix_kg - accounted_kg     (≈ 외주/임가공 추정)

상위 페이지/AI 가 어떤 분모로 원단위를 보고 싶은지에 따라 골라 쓸 수 있도록 dict로 반환합니다.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

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

# 공장별 WIP 단위 신뢰도.
# DB_재공품 의 일부 시트(김해 등)는 KG 가 아닌 EA/BT/CS2 단위 품목까지 합산되어 있어
# kg-환산이 어려우므로, "원본 합산을 그대로 KG로 신뢰할 수 있는 공장" 만 white-list 한다.
# 향후 단위 보정이 확장되면 새 공장을 추가하면 된다.
WIP_TRUSTED_FACTORIES: set[str] = {"광주"}


# 광주(F30) 재공품 → 믹스 환산계수.
# Why: 광주공장만 일부 재공품(예: 260014 탈지분유 = 분말 형태)이 자사 중간제품이 아니라
#   외부로 그대로 판매되며, 수분을 제거한 후 무게로 실적이 기록된다. 그대로 합산하면
#   energy_daily.mix_prod_kg(=수분 포함 믹스 톤) 단위와 불일치 → 광주 wip_kg 가 과소 집계되어
#   `accounted_kg` 가 실제보다 작아지고 residual(외주 잔차) 이 과대 추정되는 정합성 오류 발생.
# How to apply: 시트명(`광주`) 기준으로 ItemCode 별 가중치를 곱한 뒤 행합산한다.
#   값이 1.0 인 품목은 이미 믹스 무게로 기록돼 환산이 필요 없다.
#   여기에 없는 ItemCode 는 기본 1.0 으로 처리.
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

        # finished products (production_daily)
        if prod_factory_codes:
            qmarks = ",".join(["%s"] * len(prod_factory_codes))
            row = pd.read_sql_query(
                f"SELECT COALESCE(SUM(actual_qty),0) AS s "
                f"FROM production_daily WHERE factory IN ({qmarks}) AND date BETWEEN %s AND %s",
                conn, params=tuple(prod_factory_codes) + (df_from, df_to),
            )
            finished_kg = float(row.iloc[0]["s"]) if len(row) else 0.0
        else:
            finished_kg = 0.0
    finally:
        conn.close()

    # WIP from DB_재공품.xlsx (신뢰 공장 한정)
    wip_kg = 0.0
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
            prod_df = pd.read_sql_query(
                f"SELECT date, SUM(actual_qty) AS finished_kg "
                f"FROM production_daily WHERE factory IN ({qmarks}) AND date BETWEEN %s AND %s "
                f"GROUP BY date",
                conn, params=tuple(prod_factory_codes) + (df_from, df_to),
            )
        else:
            prod_df = pd.DataFrame(columns=["date", "finished_kg"])
    finally:
        conn.close()

    energy_df["date"] = pd.to_datetime(energy_df["date"]) if not energy_df.empty else pd.Series([], dtype="datetime64[ns]")
    prod_df["date"] = pd.to_datetime(prod_df["date"]) if not prod_df.empty else pd.Series([], dtype="datetime64[ns]")

    # WIP: 합산 (집계 공장이라면 멤버 공장의 WIP 합)
    wip_parts = []
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

    out = energy_df.merge(prod_df, on="date", how="outer")
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
    "get_wip_daily",
    "get_breakdown",
    "get_breakdown_daily",
    "build_breakdown_caption",
    "reload_wip",
]
