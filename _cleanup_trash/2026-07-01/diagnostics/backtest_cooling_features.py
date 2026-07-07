# 남양주2 전력 백테스트: 신규 냉방 피처(OLD/NEW) × lag 정책(fresh/frozen/recursive).
#
# - fresh    : 매일 직전 실측을 lag 로 사용(단일일 선행, 이상적). offset≈0.
# - frozen   : 타깃 lag 을 test 직전 실측에 동결(현재 운영의 다중일-선행 미래 구간).
# - recursive: 예측치를 다음날 lag/r7mean/intensity 로 피드(제안된 수정). 정체 해소,
#              대신 오차 누적.
#
# 실행: PYTHONIOENCODING=utf-8 python diagnostics/backtest_cooling_features.py
# env : BT_TRAIN_END(기본 2025-12-31, v5.3-replica), BT_VALID_START/END, N_EST
from __future__ import annotations

import os
import sys
from collections import deque
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.v5_common import (  # noqa: E402
    STATION_MAP, build_feature_frame, detect_special_events, fill_weather_gaps,
    is_workday, load_holidays_excel, load_weather_station_excel, make_features,
    select_features, WEATHER_COLS,
)
from app.services.usage_prediction_v5_service import _fetch_energy_history, _to_korean_schema  # noqa: E402
from app.services.v5_quantile_training import (  # noqa: E402
    QUANTILES, LOWER_Q, MEDIAN_Q, UPPER_Q, DEFAULT_M_TYPES, ALGOS_PER_MODEL_TYPE,
    train_quantile_models, compute_quantile_weights, enforce_quantile_monotonicity,
)

FACTORY, YCOL = "남양주2", "전력량[kWh]"
NEW_COLS = ["CDD16", "CDD10", "CDD16_r14mean", "평균기온_r14mean", "평균기온_r30mean"]
STALE = ["lag1", "r7mean", "intensity_lag1"]
N_EST = int(os.getenv("N_EST", "600"))

TRAIN_END = pd.Timestamp(os.getenv("BT_TRAIN_END", "2025-12-31"))
VALID_START = pd.Timestamp(os.getenv("BT_VALID_START", "2026-01-01"))
VALID_END = pd.Timestamp(os.getenv("BT_VALID_END", "2026-04-30"))
TEST_START, TEST_END = pd.Timestamp("2026-05-01"), pd.Timestamp("2026-06-22")


def build_frame() -> pd.DataFrame:
    hs = load_holidays_excel()
    df = _to_korean_schema(_fetch_energy_history(FACTORY, date(2021, 1, 1), date(2026, 6, 22)))
    w = load_weather_station_excel(STATION_MAP[FACTORY])
    df = df.merge(w, on="날짜", how="left")
    for c in WEATHER_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = fill_weather_gaps(df, WEATHER_COLS)
    df = df[df["날짜"].apply(lambda t: is_workday(pd.Timestamp(t), hs))].copy()
    d_full = detect_special_events(make_features(df, YCOL, hs))
    return d_full[~d_full["is_special_event"]].copy()


def train(d, mode, masks):
    """OLD/NEW 피처로 36모델 앙상블 학습 → 예측에 필요한 핸들 반환."""
    mtr, mva, mte = masks
    y = d[YCOL].to_numpy(dtype=float)
    X_all = build_feature_frame(d, [])
    if mode == "old":
        X_all = X_all.drop(columns=[c for c in NEW_COLS if c in X_all.columns])
    feat_by_mt, models_by_q, val_p = {}, {q: [] for q in QUANTILES}, {q: [] for q in QUANTILES}
    for mt in DEFAULT_M_TYPES:
        Xs = select_features(X_all, mt)
        feat_by_mt[mt] = list(Xs.columns)
        for q in QUANTILES:
            for m in train_quantile_models(Xs[mtr], y[mtr], alpha=q, n_estimators=N_EST):
                models_by_q[q].append(m)
                val_p[q].append(np.expm1(m.predict(Xs[mva])))
    weights_by_q, ens_val = {}, {}
    for q in QUANTILES:
        w = compute_quantile_weights(val_p[q], y[mva], q)
        weights_by_q[q] = w
        ens_val[q] = sum(wt * p for wt, p in zip(w, val_p[q]))
    p05v, _, p95v = enforce_quantile_monotonicity(ens_val[LOWER_Q], ens_val[MEDIAN_Q], ens_val[UPPER_Q])
    yva = y[mva]
    scores = np.maximum(p05v - yva, yva - p95v)
    n = len(yva)
    qlvl = min(0.90 * (1 + 1 / n), 1.0) if n else 0.90
    qhat = max(float(np.quantile(scores, qlvl)) if scores.size else 0.0, 0.0)
    return {"X_all": X_all, "feat_by_mt": feat_by_mt, "models_by_q": models_by_q,
            "weights_by_q": weights_by_q, "qhat": qhat}


def _predict_q(h, Xrows, q):
    """가중 앙상블로 분위수 q 예측 (Xrows: 전체 피처 보유 DataFrame)."""
    models, w = h["models_by_q"][q], h["weights_by_q"][q]
    acc = np.zeros(len(Xrows))
    for i, m in enumerate(models):
        mt = DEFAULT_M_TYPES[i // ALGOS_PER_MODEL_TYPE]
        acc += float(w[i]) * np.expm1(m.predict(Xrows[h["feat_by_mt"][mt]]))
    return acc


def predict_policy(d, h, masks, policy):
    """fresh/frozen/recursive lag 정책으로 test 구간 P50/밴드 예측."""
    mtr, mva, mte = masks
    X = h["X_all"]
    test_idx = list(np.asarray(d.index[mte.values]))
    Xt = X.loc[test_idx].copy()

    if policy == "fresh":
        pass  # 실측 lag 그대로
    elif policy == "frozen":
        anchor = test_idx[0]
        for c in STALE:
            if c in Xt.columns:
                Xt[c] = X.loc[anchor, c]
    elif policy == "recursive":
        # 직전 7영업일 실측으로 시드, 예측치를 다음날 lag 로 피드.
        y = d[YCOL].to_numpy(dtype=float)
        pre = np.asarray(d.index[(d["date"] < TEST_START).values])
        hist = deque(y[pre][-7:].tolist(), maxlen=7)
        prev_mix = float(d.loc[pre[-1], "mix_ton"]) if len(pre) else 0.0
        for idx in test_idx:
            if "lag1" in Xt.columns:
                Xt.at[idx, "lag1"] = hist[-1]
            if "r7mean" in Xt.columns:
                Xt.at[idx, "r7mean"] = float(np.mean(hist))
            if "intensity_lag1" in Xt.columns:
                Xt.at[idx, "intensity_lag1"] = hist[-1] / (prev_mix + 1e-6)
            p50_i = float(_predict_q(h, Xt.loc[[idx]], MEDIAN_Q)[0])
            hist.append(p50_i)
            prev_mix = float(d.at[idx, "mix_ton"])
    else:
        raise ValueError(policy)

    p05 = _predict_q(h, Xt, LOWER_Q)
    p50 = _predict_q(h, Xt, MEDIAN_Q)
    p95 = _predict_q(h, Xt, UPPER_Q)
    p05, p50, p95 = enforce_quantile_monotonicity(p05, p50, p95)
    p05, p95 = p05 - h["qhat"], p95 + h["qhat"]
    return {"p05": p05, "p50": p50, "p95": p95,
            "yte": d.loc[test_idx, YCOL].to_numpy(dtype=float),
            "dates": d.loc[test_idx, "date"].to_numpy()}


def line(tag, r):
    yte, p50 = r["yte"], r["p50"]
    resid = yte - p50
    mape = np.mean(np.abs(resid) / np.maximum(np.abs(yte), 1.0)) * 100
    picp = np.mean((yte >= r["p05"]) & (yte <= r["p95"])) * 100
    df = pd.DataFrame({"m": pd.to_datetime(r["dates"]), "rp": resid / yte * 100})
    by = df.groupby(df["m"].dt.to_period("M").astype(str))["rp"].mean()
    jun = by.get("2026-06", float("nan"))
    return (f"  {tag:18s} MAPE={mape:5.2f}%  평균잔차={resid.mean():+7.0f}kWh "
            f"({resid.mean()/yte.mean()*100:+5.1f}%)  6월={jun:+5.1f}%  PICP90={picp:3.0f}%")


def main():
    out = []
    d = build_frame()
    masks = (
        (d["date"] <= TRAIN_END),
        (d["date"] >= VALID_START) & (d["date"] <= VALID_END),
        (d["date"] >= TEST_START) & (d["date"] <= TEST_END),
    )
    out.append(f"train={masks[0].sum()} valid={masks[1].sum()} test={masks[2].sum()}  "
               f"(train≤{TRAIN_END.date()}, test={TEST_START.date()}~{TEST_END.date()}, n_est={N_EST})")
    out.append("(+잔차=실측>예측=과소예측. 6월=6월 평균잔차%. 0에 가까울수록 좋음)\n")
    for mode in ["old", "new"]:
        h = train(d, mode, masks)
        out.append(f"[{mode.upper()} 피처]")
        for pol in ["fresh", "frozen", "recursive"]:
            out.append(line(pol, predict_policy(d, h, masks, pol)))
        out.append("")
    rep = "\n".join(out)
    (Path(__file__).parent / "_backtest_cooling.txt").write_text(rep, encoding="utf-8")
    print(rep)


if __name__ == "__main__":
    main()
