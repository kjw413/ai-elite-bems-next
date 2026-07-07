# 김해 5/12, 5/18, 5/22 일자별 특이 품목 상세 분석.
# analyze_gimhae_may_outliers.py 의 후속 — UCL 초과일 각각에서
# 어떤 품목이 정상분포 대비 비정상적으로 활성/증가했는지 추출.
from __future__ import annotations

import sys
from pathlib import Path
import re
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import PATH_WIP_SUMMARY, resolve_plant_sheet

ITEM_MASTER_CSV = BASE_DIR / "wip_analysis" / "wip_item_master.csv"
TARGET_DATES = [pd.Timestamp("2026-05-12"), pd.Timestamp("2026-05-18"), pd.Timestamp("2026-05-22")]
NORMAL_START = pd.Timestamp("2026-04-01")
NORMAL_END = pd.Timestamp("2026-05-26")
PLANT = "김해"
OUT_CSV = BASE_DIR / "energy usage" / "gimhae_per_day_drivers.csv"


def load_master() -> dict[str, str]:
    if not ITEM_MASTER_CSV.exists():
        return {}
    df = pd.read_csv(ITEM_MASTER_CSV, dtype={"ItemCode": str})
    return dict(zip(df["ItemCode"].astype(str), df["item_name"].astype(str)))


def main() -> None:
    master = load_master()
    sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, PLANT) or PLANT
    df = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
    df.columns = [str(c).strip() for c in df.columns]
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"]).copy()
    code_cols = [c for c in df.columns if re.fullmatch(r"\d{6}", str(c))]
    for c in code_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    base = df[(df["날짜"] >= NORMAL_START) & (df["날짜"] <= NORMAL_END)].copy()

    # 정상 기준 = 5/12, 5/18, 5/22 제외, 5월의 다른 영업일들
    other_days = base[~base["날짜"].isin(TARGET_DATES)].copy()

    rows: list[dict] = []
    for code in code_cols:
        norm = other_days[code]
        norm_mean = float(norm.mean()) if len(norm) > 0 else 0.0
        norm_std = float(norm.std(ddof=0)) if len(norm) > 1 else 0.0
        nz_norm = int((norm > 0).sum())
        max_norm = float(norm.max()) if len(norm) > 0 else 0.0

        row: dict = {
            "item_code": code,
            "item_name": master.get(code, ""),
            "normal_mean": round(norm_mean, 1),
            "normal_max": round(max_norm, 1),
            "nz_normal_days": nz_norm,
        }

        for tdate in TARGET_DATES:
            v_series = base.loc[base["날짜"] == tdate, code]
            v = float(v_series.iloc[0]) if not v_series.empty else 0.0
            z = (v - norm_mean) / norm_std if norm_std > 0 else (np.inf if v > 0 else 0.0)
            ratio = v / norm_mean if norm_mean > 0 else (np.inf if v > 0 else 0.0)
            row[f"v_{tdate.strftime('%m%d')}"] = round(v, 1)
            row[f"z_{tdate.strftime('%m%d')}"] = round(z, 2) if np.isfinite(z) else None
            row[f"r_{tdate.strftime('%m%d')}"] = round(ratio, 2) if np.isfinite(ratio) else None

        rows.append(row)

    out = pd.DataFrame(rows)

    # 각 날짜에 대한 top 드라이버 추출 (활성 + 정상 대비 큰 값)
    for tdate in TARGET_DATES:
        v_col = f"v_{tdate.strftime('%m%d')}"
        z_col = f"z_{tdate.strftime('%m%d')}"
        r_col = f"r_{tdate.strftime('%m%d')}"
        sub = out[(out[v_col] > 0)].copy()
        # 카테고리 분리
        new_lines = sub[sub["nz_normal_days"] == 0].sort_values(v_col, ascending=False)
        spike = sub[(sub["nz_normal_days"] > 0) & ((sub[z_col].fillna(0) >= 1.5) | (sub[r_col].fillna(0) >= 2.0))]
        spike = spike.sort_values(z_col, ascending=False)

        print(f"\n{'='*70}\n[{tdate.strftime('%Y-%m-%d')}] 특이 품목\n{'='*70}")
        print("\n-- [A] 5월 다른 영업일엔 한 번도 안 만들었던 신규 활성 품목 --")
        if new_lines.empty:
            print("  (없음)")
        else:
            print(new_lines[["item_code", "item_name", v_col]].head(15).to_string(index=False))

        print(f"\n-- [B] 정상 대비 z>=1.5 또는 ratio>=2.0 (=정상일보다 비정상 많음) --")
        if spike.empty:
            print("  (없음)")
        else:
            print(spike[["item_code", "item_name", v_col, "normal_mean", z_col, r_col]].head(20).to_string(index=False))

    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n[OK] CSV: {OUT_CSV}")


if __name__ == "__main__":
    main()
