# M-type 앙상블 ablation: M2(거의 죽음)·M4 제거 시 성능 영향 측정.
# 대표 (공장,대상)에서 m_types 구성을 바꿔 train_plant_target_quantile 로 학습→test 지표 비교.
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.v5_common import (  # noqa: E402
    STATION_MAP, TARGET_SPECS, detect_special_events, fill_weather_gaps,
    is_workday, load_holidays_excel, load_weather_station_excel, make_features, WEATHER_COLS,
)
from app.services.usage_prediction_v5_service import _fetch_energy_history, _to_korean_schema  # noqa: E402
from app.services.v5_quantile_training import train_plant_target_quantile  # noqa: E402

N_EST = int(os.getenv("N_EST", "400"))
TRAIN_END = pd.Timestamp("2025-12-31")
VALID_START, VALID_END = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-04-30")
TEST_START, TEST_END = pd.Timestamp("2026-05-01"), pd.Timestamp("2026-06-22")

# 가중치 감사에서 M2/M3/M4 분포가 다른 대표 케이스 선정.
CASES = [
    ("남양주2", "전력"),   # M1+M3 (냉방)
    ("김해", "용수"),       # M1+M2(0.32) — M2 가중 있는 케이스
    ("남양주1", "용수"),    # M3(0.57)+M4(0.43)
    ("김해", "연료"),       # M1 단독(1.0)
]
CONFIGS = {
    "full M1-4": ("M1", "M2", "M3", "M4"),
    "drop M2":   ("M1", "M3", "M4"),
    "drop M2+M4": ("M1", "M3"),
}


def build_frame(plant, ycol):
    hs = load_holidays_excel()
    df = _to_korean_schema(_fetch_energy_history(plant, date(2021, 1, 1), date(2026, 6, 22)))
    w = load_weather_station_excel(STATION_MAP[plant])
    df = df.merge(w, on="날짜", how="left")
    for c in WEATHER_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = fill_weather_gaps(df, WEATHER_COLS)
    df = df[df["날짜"].apply(lambda t: is_workday(pd.Timestamp(t), hs))].copy()
    d = detect_special_events(make_features(df, ycol, hs))
    return d[~d["is_special_event"]].copy(), hs


def main():
    out = [f"n_est={N_EST}  test={TEST_START.date()}~{TEST_END.date()}",
           "지표: MAPE_p50↓ / PICP_90(목표0.9) / Pinball_p50↓ / MPIW_90↓", ""]
    for plant, tgt in CASES:
        ycol = TARGET_SPECS[tgt]["model_col"]
        d, _ = build_frame(plant, ycol)
        mtr = (d["date"] <= TRAIN_END).to_numpy()
        mva = ((d["date"] >= VALID_START) & (d["date"] <= VALID_END)).to_numpy()
        mte = ((d["date"] >= TEST_START) & (d["date"] <= TEST_END)).to_numpy()
        out.append(f"[{plant} {tgt}]  train={mtr.sum()} valid={mva.sum()} test={mte.sum()}")
        base = None
        for name, mtypes in CONFIGS.items():
            r = train_plant_target_quantile(d, ycol, [], mtr, mva, mte,
                                            n_estimators=N_EST, m_types=mtypes)
            m = r.metrics
            tag = ""
            if base is None:
                base = m["MAPE_p50"]
            else:
                tag = f"  (ΔMAPE {m['MAPE_p50']-base:+.2f}p)"
            out.append(f"  {name:11s} MAPE={m['MAPE_p50']:5.2f}%  PICP={m['PICP_90']:.2f}  "
                       f"Pin50={m['Pinball_p50']:.4f}  MPIW={m['MPIW_90']:.0f}{tag}")
        out.append("")
    rep = "\n".join(out)
    (Path(__file__).parent / "_ablation_mtypes.txt").write_text(rep, encoding="utf-8")
    print(rep)


if __name__ == "__main__":
    main()
