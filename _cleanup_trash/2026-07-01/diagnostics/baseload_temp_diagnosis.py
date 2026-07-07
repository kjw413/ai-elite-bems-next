# 남양주2 전력 offset 진단: 기저부하 vs 기온 곡선(2024~2026) + 2026 잔차 분해
#
# 목적: 2026 전력 예측의 일관된 과소예측(offset)이
#   (1) 기온/계절성 냉동 부하 상승인지(과거 base_load(T) 곡선 위로 떴는지),
#   (2) 구조적 레벨 스텝인지(냉동전력 자체가 한 단계 올라갔는지)
# 를 데이터로 가린다.
#
# 실행: PYTHONIOENCODING=utf-8 python diagnostics/baseload_temp_diagnosis.py
# 산출: diagnostics/_baseload_report.txt, diagnostics/_baseload_diagnosis.png
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database.db_connection import get_connection  # noqa: E402
from app.services.v5_common import (  # noqa: E402
    STATION_MAP,
    load_holidays_excel,
    load_weather_station_excel,
)

FACTORY = "남양주2"
TARGET = "전력"
DATE_FROM = "2024-01-01"
DATE_TO = "2026-06-23"
REF_TEMP = 24.0  # base_load(T) 곡선 비교 기준 기온


def fetch_energy() -> pd.DataFrame:
    q = """
        SELECT date, mix_prod_kg, total_power_kwh, freezing_power_kwh, air_compressor_kwh
        FROM energy_daily
        WHERE factory=%s AND date>=%s AND date<=%s
        ORDER BY date
    """
    conn = get_connection()
    try:
        df = pd.read_sql_query(q, conn, params=(FACTORY, DATE_FROM, DATE_TO))
    finally:
        conn.close()
    df["date"] = pd.to_datetime(df["date"])
    for c in ["mix_prod_kg", "total_power_kwh", "freezing_power_kwh", "air_compressor_kwh"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["mix_ton"] = df["mix_prod_kg"] / 1000.0
    return df


def fetch_predlog() -> pd.DataFrame:
    q = """
        SELECT pred_date, pred_value, pred_p05, pred_p95, actual_value, mix_prod_kg
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
    w = load_weather_station_excel(station)
    w = w.rename(columns={"날짜": "date"})[["date", "평균기온"]]
    w["date"] = pd.to_datetime(w["date"])
    w["T"] = pd.to_numeric(w["평균기온"], errors="coerce")
    return df.merge(w[["date", "T"]], on="date", how="left")


def yearly_curve(sub: pd.DataFrame, ycol: str) -> dict:
    """연도별 ycol ~ a + b*T 선형적합 → 기준기온에서의 레벨과 기울기."""
    res = {}
    for yr, g in sub.groupby(sub["date"].dt.year):
        g = g.dropna(subset=["T", ycol])
        if len(g) < 8:
            continue
        b, a = np.polyfit(g["T"], g[ycol], 1)
        res[int(yr)] = {
            "n": len(g),
            "slope_per_degC": b,
            "level_at_ref": a + b * REF_TEMP,
            "mean": g[ycol].mean(),
            "temp_mean": g["T"].mean(),
        }
    return res


def temp_bin_table(sub: pd.DataFrame, ycol: str) -> pd.DataFrame:
    bins = [16, 18, 20, 22, 24, 26, 28, 40]
    sub = sub.dropna(subset=["T", ycol]).copy()
    sub["tbin"] = pd.cut(sub["T"], bins)
    piv = sub.pivot_table(index="tbin", columns=sub["date"].dt.year,
                          values=ycol, aggfunc="mean", observed=True)
    return piv


def main() -> None:
    out: list[str] = []
    P = lambda *a: out.append(" ".join(str(x) for x in a))

    energy = attach_weather(fetch_energy())
    holidays = load_holidays_excel()
    energy["is_workday"] = energy["date"].apply(
        lambda d: d.dayofweek < 5 and d.date() not in holidays
    )

    # 저생산일(기저부하 근사): mix_ton 이 30일 롤링 중앙값의 10% 미만 또는 절대값 1ton 미만
    rmed = energy["mix_ton"].rolling(30, min_periods=7).median()
    energy["is_lowprod"] = (energy["mix_ton"] < rmed * 0.1) | (energy["mix_ton"] < 1.0)

    P("=" * 70)
    P(f"[1] 월별 평균 (factory={FACTORY})  — 냉동/공압/총전력/기온")
    P("=" * 70)
    energy["ym"] = energy["date"].dt.to_period("M").astype(str)
    monthly = energy.groupby("ym").agg(
        n=("date", "size"),
        T=("T", "mean"),
        mix_ton=("mix_ton", "mean"),
        total_kwh=("total_power_kwh", "mean"),
        freezing_kwh=("freezing_power_kwh", "mean"),
        air_kwh=("air_compressor_kwh", "mean"),
    ).round(1)
    P(monthly.to_string())

    # 동월 YoY 비교 (냉동전력) — 2026이 같은 달 과거 대비 떴는지
    P("")
    P("-" * 70)
    P("[1b] 같은 '월'의 연도별 냉동전력(freezing) 평균 — 구조적 스텝 확인")
    P("-" * 70)
    energy["year"] = energy["date"].dt.year
    energy["mon"] = energy["date"].dt.month
    fz = energy.pivot_table(index="mon", columns="year",
                            values="freezing_power_kwh", aggfunc="mean").round(0)
    P(fz.to_string())
    P("")
    tz = energy.pivot_table(index="mon", columns="year",
                            values="T", aggfunc="mean").round(1)
    P("(참고) 같은 '월'의 연도별 평균기온:")
    P(tz.to_string())

    # base_load(T) 곡선: 저생산일 총전력, 그리고 전일 냉동전력
    P("")
    P("=" * 70)
    P("[2] base_load(T) 곡선 — 2026이 과거 곡선 위로 떴는가?")
    P("=" * 70)
    for label, ycol, sub in [
        ("냉동전력(전체일)", "freezing_power_kwh", energy),
        ("저생산일 총전력", "total_power_kwh", energy[energy["is_lowprod"]]),
    ]:
        P(f"\n--- {label}: 연도별 선형적합 (기준기온 {REF_TEMP}℃) ---")
        cur = yearly_curve(sub, ycol)
        for yr in sorted(cur):
            c = cur[yr]
            P(f"  {yr}: n={c['n']:>4}  기울기={c['slope_per_degC']:>8.1f} kWh/℃  "
              f"{REF_TEMP:.0f}℃레벨={c['level_at_ref']:>9.0f}  "
              f"평균={c['mean']:>9.0f} (T̄={c['temp_mean']:.1f})")
        if {2024, 2025, 2026} <= set(cur):
            base = np.mean([cur[2024]["level_at_ref"], cur[2025]["level_at_ref"]])
            step = cur[2026]["level_at_ref"] - base
            P(f"  >> 2026 {REF_TEMP:.0f}℃레벨이 2024~25 평균 대비 {step:+.0f} kWh "
              f"({step/base*100:+.1f}%)")
        P(f"\n  {label} 기온구간별 연도 평균:")
        P("  " + temp_bin_table(sub, ycol).round(0).to_string().replace("\n", "\n  "))

    # 2026 잔차 분해
    P("")
    P("=" * 70)
    P("[3] 2026 예측 잔차 분해 (prediction_log)")
    P("=" * 70)
    pl = attach_weather(fetch_predlog())
    pl = pl.merge(
        energy[["date", "freezing_power_kwh", "air_compressor_kwh", "total_power_kwh", "mix_ton"]],
        on="date", how="left",
    )
    pl["resid"] = pl["actual_value"] - pl["pred_value"]
    pl["resid_pct"] = pl["resid"] / pl["actual_value"] * 100
    P(f"  n={len(pl)}  기간={pl['date'].min().date()}~{pl['date'].max().date()}")
    P(f"  평균 잔차 = {pl['resid'].mean():+.0f} kWh ({pl['resid_pct'].mean():+.1f}%), "
      f"표준편차={pl['resid'].std():.0f}")
    P(f"  corr(resid, 기온T)        = {pl['resid'].corr(pl['T']):+.3f}")
    P(f"  corr(resid, 냉동전력)     = {pl['resid'].corr(pl['freezing_power_kwh']):+.3f}")
    P(f"  corr(resid, 공압전력)     = {pl['resid'].corr(pl['air_compressor_kwh']):+.3f}")
    P(f"  corr(resid, mix_ton)      = {pl['resid'].corr(pl['mix_ton']):+.3f}")
    P("")
    pl["ym"] = pl["date"].dt.to_period("M").astype(str)
    mres = pl.groupby("ym").agg(
        n=("resid", "size"),
        T=("T", "mean"),
        resid=("resid", "mean"),
        resid_pct=("resid_pct", "mean"),
        freezing=("freezing_power_kwh", "mean"),
    ).round(1)
    P("  월별 잔차 추이:")
    P("  " + mres.to_string().replace("\n", "\n  "))

    report = "\n".join(out)
    (Path(__file__).parent / "_baseload_report.txt").write_text(report, encoding="utf-8")

    # ---- 차트 ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        for f in ["Malgun Gothic", "맑은 고딕", "NanumGothic"]:
            try:
                matplotlib.rcParams["font.family"] = f
                break
            except Exception:
                continue
        matplotlib.rcParams["axes.unicode_minus"] = False

        fig, ax = plt.subplots(2, 2, figsize=(15, 10))
        colors = {2024: "#9ca3af", 2025: "#3b82f6", 2026: "#ef4444"}

        # A: 냉동전력 vs 기온, 연도별
        for yr, g in energy.groupby("year"):
            if yr not in colors:
                continue
            ax[0, 0].scatter(g["T"], g["freezing_power_kwh"], s=10, alpha=0.4,
                             color=colors[yr], label=str(yr))
            gg = g.dropna(subset=["T", "freezing_power_kwh"])
            if len(gg) > 8:
                b, a = np.polyfit(gg["T"], gg["freezing_power_kwh"], 1)
                xs = np.linspace(gg["T"].min(), gg["T"].max(), 50)
                ax[0, 0].plot(xs, a + b * xs, color=colors[yr], lw=2)
        ax[0, 0].set_title("A) 냉동전력 vs 기온 (연도별) — 2026 곡선이 위면 구조적 상승")
        ax[0, 0].set_xlabel("평균기온(℃)"); ax[0, 0].set_ylabel("냉동전력(kWh)")
        ax[0, 0].legend()

        # B: 월별 냉동전력 타임라인
        mtl = energy.groupby("ym").agg(fz=("freezing_power_kwh", "mean")).reset_index()
        mtl["t"] = pd.to_datetime(mtl["ym"] + "-01")
        ax[0, 1].plot(mtl["t"], mtl["fz"], marker="o", ms=3, color="#7c3aed")
        ax[0, 1].set_title("B) 월별 평균 냉동전력 추이 — 스텝/램프 시점")
        ax[0, 1].set_xlabel("월"); ax[0, 1].set_ylabel("냉동전력(kWh)")

        # C: 2026 잔차 vs 기온 / vs 냉동전력
        ax[1, 0].scatter(pl["T"], pl["resid"], s=20, color="#ef4444")
        ax[1, 0].axhline(0, color="k", lw=0.8)
        ax[1, 0].axhline(pl["resid"].mean(), color="#6b7280", ls="--",
                         label=f"평균 {pl['resid'].mean():+.0f}")
        ax[1, 0].set_title("C) 2026 잔차(실측−예측) vs 기온")
        ax[1, 0].set_xlabel("평균기온(℃)"); ax[1, 0].set_ylabel("잔차(kWh)")
        ax[1, 0].legend()

        # D: 2026 잔차 vs 냉동전력
        ax[1, 1].scatter(pl["freezing_power_kwh"], pl["resid"], s=20, color="#0ea5e9")
        ax[1, 1].axhline(0, color="k", lw=0.8)
        ax[1, 1].set_title("D) 2026 잔차 vs 냉동전력 — 우상향이면 냉동부하가 원인")
        ax[1, 1].set_xlabel("냉동전력(kWh)"); ax[1, 1].set_ylabel("잔차(kWh)")

        fig.suptitle(f"{FACTORY} {TARGET} offset 진단", fontsize=14)
        fig.tight_layout()
        fig.savefig(Path(__file__).parent / "_baseload_diagnosis.png", dpi=110)
        out.append("\n[chart] _baseload_diagnosis.png 저장됨")
        (Path(__file__).parent / "_baseload_report.txt").write_text(
            "\n".join(out), encoding="utf-8")
    except Exception as e:
        (Path(__file__).parent / "_baseload_report.txt").write_text(
            report + f"\n[chart 실패] {e!r}", encoding="utf-8")

    print("done")


if __name__ == "__main__":
    main()
