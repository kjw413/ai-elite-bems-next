# 김해 연료 예측 과대예측(offset) 진단
#
# 관측: 2026년 5월 둘째 주경부터 연료 예측 P50 > 실측(과대예측) offset 발생.
# 질문: 원인이 (A) 계절/기저부하(난방·공정열) 변화인가,
#       (B) 특정 재공품/완제품(품목 믹스) 변화인가?
#
# 실행: PYTHONIOENCODING=utf-8 python diagnostics/gimhae_fuel_offset_diagnosis.py
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 200)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database.db_connection import get_connection  # noqa: E402
from app.services.v5_common import (  # noqa: E402
    PATH_WIP_SUMMARY,
    PATH_WIP_ITEM_MASTER,
    STATION_MAP,
    load_holidays_excel,
    load_weather_station_excel,
    resolve_plant_sheet,
)

FACTORY = "김해"
TARGET = "연료"
OUT = []
def P(*a):
    OUT.append(" ".join(str(x) for x in a))
    print(*a)


# ----------------------------------------------------------------------------
def fetch_energy() -> pd.DataFrame:
    q = """
        SELECT date, mix_prod_kg, fuel_nm3, fuel_per_ton_nm3,
               total_power_kwh, freezing_power_kwh, air_compressor_kwh,
               water_ton
        FROM energy_daily WHERE factory=%s ORDER BY date
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(q, conn, params=(FACTORY,))
    finally:
        conn.close()
    df["date"] = pd.to_datetime(df["date"])
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["mix_ton"] = df["mix_prod_kg"] / 1000.0
    return df


def fetch_predlog() -> pd.DataFrame:
    q = """
        SELECT pred_date, pred_value, pred_p05, pred_p95, actual_value, mix_prod_kg, band_status
        FROM prediction_log
        WHERE factory=%s AND target=%s AND actual_value IS NOT NULL
        ORDER BY pred_date
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(q, conn, params=(FACTORY, TARGET))
    finally:
        conn.close()
    df["date"] = pd.to_datetime(df["pred_date"])
    for c in ["pred_value", "pred_p05", "pred_p95", "actual_value", "mix_prod_kg"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def attach_weather(df: pd.DataFrame) -> pd.DataFrame:
    station = STATION_MAP.get(FACTORY)
    w = load_weather_station_excel(station).rename(columns={"날짜": "date"})
    w["date"] = pd.to_datetime(w["date"])
    w["T"] = pd.to_numeric(w["평균기온"], errors="coerce")
    w["RH"] = pd.to_numeric(w["상대습도"], errors="coerce")
    return df.merge(w[["date", "T", "RH"]], on="date", how="left")


def load_wip() -> tuple[pd.DataFrame, dict]:
    sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, FACTORY) or FACTORY
    wdf = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
    wdf.columns = [str(c).strip() for c in wdf.columns]
    wdf["날짜"] = pd.to_datetime(wdf["날짜"], errors="coerce")
    wdf = wdf.dropna(subset=["날짜"]).rename(columns={"날짜": "date"})
    code_cols = [c for c in wdf.columns if str(c).isdigit()]
    for c in code_cols:
        wdf[c] = pd.to_numeric(wdf[c], errors="coerce").fillna(0.0)
    # code -> name map
    name_map = {}
    try:
        im = pd.read_excel(PATH_WIP_ITEM_MASTER, sheet_name=0)
        im["ItemCode"] = pd.to_numeric(im["ItemCode"], errors="coerce")
        for _, r in im.dropna(subset=["ItemCode"]).iterrows():
            code = str(int(r["ItemCode"]))
            nm = str(r.get("Item 명", "")).strip()
            if code not in name_map and nm and nm != "nan":
                name_map[code] = nm
    except Exception as e:
        P("  [warn] item master read:", repr(e))
    return wdf[["date"] + code_cols], name_map


# ============================================================================
def main():
    energy = attach_weather(fetch_energy())
    holidays = load_holidays_excel()
    energy["is_work"] = energy["date"].apply(lambda d: d.dayofweek < 5 and d.date() not in holidays)
    energy["HDD"] = np.maximum(18 - energy["T"], 0)
    energy["CDD"] = np.maximum(energy["T"] - 22, 0)
    energy["year"] = energy["date"].dt.year
    energy["mon"] = energy["date"].dt.month
    energy["ym"] = energy["date"].dt.to_period("M").astype(str)
    energy["fpt"] = energy["fuel_nm3"] / energy["mix_ton"].replace(0, np.nan)

    # =====================================================================
    P("=" * 78)
    P("[A] 예측 잔차(prediction_log 연료) — offset 확인 / 시점 / 부호")
    P("=" * 78)
    pl = attach_weather(fetch_predlog())
    pl = pl.merge(energy[["date", "fuel_nm3", "mix_ton", "fpt", "HDD", "CDD"]], on="date", how="left")
    pl["resid"] = pl["actual_value"] - pl["pred_value"]          # +면 과소예측, −면 과대예측
    pl["resid_pct"] = pl["resid"] / pl["pred_value"] * 100
    pl["ym"] = pl["date"].dt.to_period("M").astype(str)
    pl["half"] = pl["ym"] + np.where(pl["date"].dt.day <= 15, "_상", "_하")
    P(f"기간 {pl['date'].min().date()}~{pl['date'].max().date()}  n={len(pl)}")
    P(f"전체 평균 잔차 = {pl['resid'].mean():+.0f} Nm³ ({pl['resid_pct'].mean():+.1f}%)  (음수=과대예측)")
    P("\n반월별 잔차 추이 (resid_pct 음수가 과대예측):")
    half = pl.groupby("half").agg(
        n=("resid", "size"), T=("T", "mean"),
        pred=("pred_value", "mean"), actual=("actual_value", "mean"),
        resid=("resid", "mean"), resid_pct=("resid_pct", "mean"),
        mix_ton=("mix_ton", "mean"), fpt=("fpt", "mean"),
        n_under=("band_status", lambda s: (s == "under").sum()),
    ).round(1)
    P(half.to_string())
    P("\n상관: corr(resid, T)=%.3f  corr(resid, HDD)=%.3f  corr(resid, mix_ton)=%.3f  corr(resid, fpt)=%.3f"
      % (pl["resid"].corr(pl["T"]), pl["resid"].corr(pl["HDD"]),
         pl["resid"].corr(pl["mix_ton"]), pl["resid"].corr(pl["fpt"])))

    # =====================================================================
    P("\n" + "=" * 78)
    P("[B] 연료 = 기저(base) + 한계원단위(slope)*생산  — 월별 회귀로 base vs slope 분해")
    P("=" * 78)
    P("  base(절편)가 여름에 내려가면 → 기저부하(난방/공정열) 계절감소")
    P("  slope(기울기)가 내려가면 → 생산 단위당 연료(품목 믹스/효율) 변화")
    em = energy[(energy["date"] >= "2025-10-01") & energy["is_work"]].copy()
    rows = []
    for ym, g in em.groupby("ym"):
        g = g.dropna(subset=["fuel_nm3", "mix_ton"])
        g = g[g["mix_ton"] > 0]
        if len(g) < 6:
            continue
        b, a = np.polyfit(g["mix_ton"], g["fuel_nm3"], 1)
        rows.append({
            "ym": ym, "n": len(g), "T": round(g["T"].mean(), 1),
            "mix_ton": round(g["mix_ton"].mean(), 1),
            "fuel": round(g["fuel_nm3"].mean(), 0),
            "base_절편": round(a, 0), "slope_원단위": round(b, 2),
            "fpt": round(g["fpt"].mean(), 2),
        })
    P(pd.DataFrame(rows).to_string(index=False))

    # 저생산일(기저 근사) 연료 — 생산이 거의 없는 날의 연료 = 순수 기저부하
    P("\n  [B2] 저생산일(mix_ton<30일중앙값*0.25) 연료 = 기저부하 근사, 월별:")
    rmed = energy["mix_ton"].rolling(30, min_periods=7).median()
    energy["is_low"] = (energy["mix_ton"] < rmed * 0.25)
    low = energy[(energy["date"] >= "2025-10-01") & energy["is_low"] & energy["is_work"]]
    lb = low.groupby("ym").agg(n=("fuel_nm3", "size"), T=("T", "mean"),
                               mix_ton=("mix_ton", "mean"), fuel_base=("fuel_nm3", "mean")).round(1)
    P("  " + lb.to_string().replace("\n", "\n  "))

    # =====================================================================
    P("\n" + "=" * 78)
    P("[C] 계절성 점검 — 연도별 같은 '월' 연료 & 연료원단위 (2026이 비정상 저하인가?)")
    P("=" * 78)
    P("  (정상 계절패턴이면 매년 여름 연료/원단위 하락 → 모델이 못 따라간 것;")
    P("   2026만 과거대비 낮으면 구조적/품목 변화)")
    fz = energy.pivot_table(index="mon", columns="year", values="fuel_nm3", aggfunc="mean").round(0)
    P("\n  월별 평균 연료(Nm³):")
    P("  " + fz.to_string().replace("\n", "\n  "))
    fptp = energy[energy["is_work"]].pivot_table(index="mon", columns="year", values="fpt", aggfunc="mean").round(2)
    P("\n  월별 평균 연료원단위(Nm³/mix-ton, 영업일):")
    P("  " + fptp.to_string().replace("\n", "\n  "))
    # 연료 vs 기온 곡선 (난방 관계): 연도별 기울기/24도 레벨
    P("\n  연료 vs 기온 선형적합(영업일, 2023~):  기울기 음수=난방형(추울수록↑)")
    ew = energy[(energy["is_work"]) & (energy["year"] >= 2023)].dropna(subset=["T", "fuel_nm3"])
    for yr, g in ew.groupby("year"):
        g = g[g["mix_ton"] > 0]
        if len(g) < 30:
            continue
        b, a = np.polyfit(g["T"], g["fuel_nm3"], 1)
        P(f"    {yr}: n={len(g):>4}  기울기={b:>8.1f} Nm³/℃  24℃레벨={a+b*24:>7.0f}  평균T={g['T'].mean():.1f}")

    # =====================================================================
    P("\n" + "=" * 78)
    P("[D] 품목 믹스(재공품) 변화 — 4월→5월→6월 / offset 전후")
    P("=" * 78)
    wip, name_map = load_wip()
    code_cols = [c for c in wip.columns if c != "date"]
    wip_e = wip.merge(energy[["date", "fuel_nm3", "mix_ton", "fpt", "is_work"]], on="date", how="inner")
    wip_e = wip_e[wip_e["is_work"]]
    # 월별 품목 평균 처리량
    wip_e["ym"] = wip_e["date"].dt.to_period("M").astype(str)
    recent = wip_e[wip_e["date"] >= "2026-02-01"]
    mon_mean = recent.groupby("ym")[code_cols].mean()
    # offset 전(2026-02~04) vs offset 후(2026-05~06) 비교
    pre = wip_e[(wip_e["date"] >= "2026-02-01") & (wip_e["date"] < "2026-05-01")][code_cols].mean()
    post = wip_e[(wip_e["date"] >= "2026-05-01")][code_cols].mean()
    chg = pd.DataFrame({"pre_2~4월": pre, "post_5~6월": post})
    chg["delta"] = chg["post_5~6월"] - chg["pre_2~4월"]
    chg["name"] = [name_map.get(c, "?") for c in chg.index]
    # 절대 변화량 큰 품목
    P("\n  [D1] 처리량 절대변화 큰 품목 TOP (pre 2~4월 vs post 5~6월 평균):")
    big = chg.reindex(chg["delta"].abs().sort_values(ascending=False).index).head(15)
    P("  " + big[["name", "pre_2~4월", "post_5~6월", "delta"]].round(0).to_string().replace("\n", "\n  "))

    # 각 품목이 연료원단위(fpt)와 얼마나 연동되는가 (연료집약 품목 식별)
    P("\n  [D2] 각 품목 처리량 vs 일별 연료(fuel_nm3) 상관 (영업일 2025-10~, 연료집약 품목):")
    base = wip_e[wip_e["date"] >= "2025-10-01"].copy()
    corr_rows = []
    for c in code_cols:
        if (base[c] > 0).sum() < 15:
            continue
        cc = base[c].corr(base["fuel_nm3"])
        cf = base[c].corr(base["fpt"])
        corr_rows.append({"code": c, "name": name_map.get(c, "?"),
                          "n_active": int((base[c] > 0).sum()),
                          "corr_fuel": round(cc, 3), "corr_fpt": round(cf, 3),
                          "mean_vol": round(base[c].mean(), 0)})
    cdf = pd.DataFrame(corr_rows).sort_values("corr_fuel", ascending=False)
    P("  연료와 양(+)의 상관 TOP10:")
    P("  " + cdf.head(10).to_string(index=False).replace("\n", "\n  "))
    P("\n  연료와 음(−)의 상관 TOP10:")
    P("  " + cdf.tail(10).to_string(index=False).replace("\n", "\n  "))

    # offset 후 줄어든 연료양(+)상관 품목 = 과대예측 용의자
    P("\n  [D3] 용의자 = (연료와 +상관 강함) AND (5~6월 처리량 감소) 교집합:")
    susp = cdf.merge(chg.reset_index().rename(columns={"index": "code"})[["code", "delta", "pre_2~4월", "post_5~6월"]], on="code")
    susp = susp[(susp["corr_fuel"] > 0.2) & (susp["delta"] < 0)].sort_values("delta")
    P("  " + susp.head(15).to_string(index=False).replace("\n", "\n  "))

    # =====================================================================
    P("\n" + "=" * 78)
    P("[E] 판별 회귀 — 잔차를 (기온/HDD) vs (용의 품목) 중 무엇이 설명하나")
    P("=" * 78)
    reg = pl.merge(wip[["date"] + code_cols], on="date", how="left")
    susp_codes = susp.head(6)["code"].tolist() if len(susp) else []
    P("  용의 품목 코드:", [(c, name_map.get(c, "?")) for c in susp_codes])
    sub = reg.dropna(subset=["resid", "T", "mix_ton"]).copy()
    # 단변량 설명력 (resid ~ x): R^2
    def r2(x, y):
        m = (~x.isna()) & (~y.isna())
        if m.sum() < 10:
            return float("nan")
        b, a = np.polyfit(x[m], y[m], 1)
        yhat = a + b * x[m]
        ss = 1 - ((y[m] - yhat) ** 2).sum() / ((y[m] - y[m].mean()) ** 2).sum()
        return round(ss, 3)
    P(f"  R^2(resid ~ T)         = {r2(sub['T'], sub['resid'])}")
    P(f"  R^2(resid ~ HDD)       = {r2(sub['HDD'], sub['resid'])}")
    P(f"  R^2(resid ~ mix_ton)   = {r2(sub['mix_ton'], sub['resid'])}")
    if susp_codes:
        sub["susp_sum"] = sub[susp_codes].sum(axis=1)
        P(f"  R^2(resid ~ 용의품목합) = {r2(sub['susp_sum'], sub['resid'])}")
        for c in susp_codes:
            P(f"    R^2(resid ~ {c} {name_map.get(c,'?')}) = {r2(sub[c], sub['resid'])}")

    Path(ROOT / "diagnostics" / "_gimhae_fuel_offset.txt").write_text("\n".join(OUT), encoding="utf-8")
    print("\n[saved] diagnostics/_gimhae_fuel_offset.txt")


if __name__ == "__main__":
    main()
