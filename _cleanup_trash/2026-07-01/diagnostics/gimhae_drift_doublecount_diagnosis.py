# 김해 연료/전력 — 223579 drift 사후보정이 재학습 모델에서 이중계산되어
# 여름 과대예측을 만드는지 검증. raw(보정 미적용) vs raw+drift(=운영) vs 실측 분해.
#
# 배경: DRIFT_CORRECTIONS 는 223579 신설라인이 학습데이터에 없던 시절 가산하던 가드.
# 2026-06-25 전체재학습이 223579 가동기(2026-03-25~)를 학습에 포함 → 모델이 이미 학습 →
# 그 위에 drift 가산 = 이중계산. 본 스크립트로 정량 확인.
#
# 실행: PYTHONIOENCODING=utf-8 python diagnostics/gimhae_drift_doublecount_diagnosis.py
from __future__ import annotations

import sys
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import joblib

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.v5_common import (  # noqa: E402
    BOTTLING_AGG_FEATURE, N_ACTIVE_SKUS_FEATURE, PATH_WIP_SUMMARY, STATION_MAP, WEATHER_COLS,
    build_feature_frame, compute_bottling_aggregate, detect_special_events, fill_weather_gaps,
    is_workday, load_holidays_excel, load_model_registry, load_n_active_skus_series,
    load_weather_station_excel, make_features, resolve_model_path, resolve_plant_sheet,
    spec_is_quantile,
)
from app.services.usage_prediction_v5_service import (  # noqa: E402
    DRIFT_CORRECTIONS, _fetch_energy_history, _to_korean_schema,
)
from app.database.db_connection import get_connection  # noqa: E402

FAC = "김해"
TARGETS = {"연료": "연료량[N㎥]", "전력": "전력량[kWh]"}


def find_specs(obj, trail=()):
    if isinstance(obj, dict):
        if spec_is_quantile(obj):
            return [(trail, obj)]
        out = []
        for k, v in obj.items():
            out += find_specs(v, trail + (k,))
        return out
    return []


def main():
    reg = load_model_registry()
    blob = joblib.load(resolve_model_path(reg.get("active_model_path")))
    specs = {t: s for t, s in find_specs(blob)}
    hs = load_holidays_excel()
    conn = get_connection()

    for tgt, ycol in TARGETS.items():
        key = [t for t in specs if FAC in t and tgt in t][0]
        spec = specs[key]
        feats, mtypes = spec["features"], spec["m_types"]
        algos = spec.get("algos_per_model_type", 3)
        wip_cols = spec["wip_feature_cols"]
        ea = spec.get("wip_bottling_ea_codes", [])
        short = spec.get("wip_shortlist_codes", [])
        drift = DRIFT_CORRECTIONS.get((FAC, tgt))
        # DRIFT_CORRECTIONS 값은 (item_code, alpha, effective_start, effective_end).
        # 본 진단은 '보정 만료 전' 운영(=raw+drift)을 재현해 이중계산을 보이는 게 목적이라
        # 윈도우와 무관히 alpha 를 가산한다(effective_end 도입 전 운영 동작 재현).
        code = drift[0] if drift else None
        alpha = float(drift[1]) if drift else 0.0
        eff = drift[2] if drift else None

        df = _to_korean_schema(_fetch_energy_history(FAC, date(2021, 1, 1), date(2026, 6, 28)))
        w = load_weather_station_excel(STATION_MAP[FAC])
        df = df.merge(w, on="날짜", how="left")
        for c in WEATHER_COLS:
            if c not in df.columns:
                df[c] = np.nan
        df = fill_weather_gaps(df, WEATHER_COLS)
        sheet = resolve_plant_sheet(PATH_WIP_SUMMARY, FAC) or FAC
        wdf = pd.read_excel(PATH_WIP_SUMMARY, sheet_name=sheet)
        wdf.columns = [str(c).strip() for c in wdf.columns]
        wdf["날짜"] = pd.to_datetime(wdf["날짜"], errors="coerce")
        allc = list(dict.fromkeys(list(short) + list(ea) + ([code] if code else [])))
        wraw = wdf[["날짜"] + [c for c in allc if c in wdf.columns]].rename(
            columns={c: f"wip_{c}" for c in allc})
        df = df.merge(wraw, on="날짜", how="left").merge(
            load_n_active_skus_series(PATH_WIP_SUMMARY, FAC), on="날짜", how="left")
        df = df[df["날짜"].apply(lambda t: is_workday(pd.Timestamp(t), hs))].copy()
        d = detect_special_events(make_features(df, ycol, hs))
        d = d[~d["is_special_event"]].copy().sort_values("date").reset_index(drop=True)
        for c in d.columns:
            if str(c).startswith("wip_"):
                d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)
        d[N_ACTIVE_SKUS_FEATURE] = pd.to_numeric(d.get(N_ACTIVE_SKUS_FEATURE, 0.0), errors="coerce").fillna(0.0)
        d[BOTTLING_AGG_FEATURE] = compute_bottling_aggregate(d, ea)
        X = build_feature_frame(d, wip_cols)

        def p50(Xf):
            a = np.zeros(len(Xf))
            ws = list(np.asarray(spec["weights_by_q"][0.5], float))
            for i, m in enumerate(spec["models_by_q"][0.5]):
                if ws[i] == 0:
                    continue
                a += ws[i] * np.expm1(m.predict(Xf[feats[mtypes[i // algos]]]))
            return a

        te = ((d["date"] >= pd.Timestamp("2026-05-01")) & (d["date"] <= pd.Timestamp("2026-06-28"))).to_numpy()
        sub = d.loc[te, ["date", ycol]].copy()
        sub["raw"] = p50(X.loc[te])
        qty = d.loc[te, f"wip_{code}"].to_numpy() if code and f"wip_{code}" in d.columns else np.zeros(te.sum())
        sub["drift_add"] = alpha * qty
        sub["raw+drift"] = sub["raw"] + sub["drift_add"]
        op = pd.read_sql_query(
            "SELECT pred_date,pred_value FROM prediction_log WHERE factory='김해' AND target=%s AND pred_date>='2026-05-01'",
            conn, params=(tgt,))
        op["date"] = pd.to_datetime(op["pred_date"])
        sub = sub.merge(op[["date", "pred_value"]], on="date", how="left").rename(columns={"pred_value": "운영"})
        y = sub[ycol].to_numpy(float)
        def e(p):
            return float(np.mean((y - p) / y) * 100)
        print(f"\n===== 김해 {tgt}  (drift: {code} α={alpha} eff={eff.date() if eff else None}) =====")
        print(f"  drift 가산 평균 = {sub['drift_add'].mean():+.0f} {ycol.split('[')[-1].rstrip(']')}/일  (5~6월)")
        print(f"  평균오차%(실측−예측)/실측:  raw={e(sub['raw'].values):+.1f}%   "
              f"raw+drift={e(sub['raw+drift'].values):+.1f}%   운영로그={e(sub['운영'].values):+.1f}%")
        print(f"  |운영 − (raw+drift)| 평균 = {np.mean(np.abs(sub['운영']-sub['raw+drift'])):.0f}  (작으면 drift가 운영을 설명)")
        print(f"  → 판정: raw가 운영보다 |오차| 작으면 drift 이중계산(보정 끄는 게 정확). "
              f"raw|{abs(e(sub['raw'].values)):.1f}|  운영|{abs(e(sub['운영'].values)):.1f}|")
    conn.close()


if __name__ == "__main__":
    main()
