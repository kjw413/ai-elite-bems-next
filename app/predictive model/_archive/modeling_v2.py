# 이 파일은 v2 예측 모델을 학습하고 평가합니다.
import re
import os
import sys
import pandas as pd
import numpy as np
import openpyxl
from pathlib import Path
from scipy.optimize import minimize
from lightgbm import LGBMRegressor

# =========================
# 설정
# =========================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import PATH_ENERGY_SOURCE, load_energy_sheet

PATH_WEATHER = BASE_DIR / "DB_weather.xlsx"
PATH_HOLIDAY = BASE_DIR / "DB_holiday.xlsx"
PATH_INTENSITY = PATH_ENERGY_SOURCE

TRAIN_START = pd.Timestamp("2023-01-01")
TRAIN_END   = pd.Timestamp("2025-06-30")
VALID_START = pd.Timestamp("2025-07-01")
VALID_END   = pd.Timestamp("2025-09-30")
TEST_START  = pd.Timestamp("2025-10-01")
TEST_END    = pd.Timestamp("2026-02-11")

STATION_MAP = {
    "남양주1": "서울",
    "남양주2": "서울",
    "김해": "김해",
    "광주": "이천",
    "논산": "부여"
}

TARGETS = {
    "전력": "전력량[kWh]",
    "연료": "연료량[N㎥]",
    "용수": "용수량[ton]",
}

# =========================
# 유틸
# =========================
# 실제값과 예측값의 오차 비율을 계산합니다.
def mape(y_true, y_pred, eps=1.0):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = np.maximum(np.abs(y_true), eps)
    return float(np.mean(np.abs((y_true - y_pred) / denom)) * 100.0)

# 주어진 날짜가 영업일인지 확인합니다.
def is_workday(ts, holiday_set):
    return (ts.dayofweek < 5) and (ts.date() not in holiday_set)

# =========================
# 데이터 로드
# =========================
# 날씨 데이터를 불러옵니다.
def load_weather():
    xl = pd.ExcelFile(PATH_WEATHER)
    weather_dict = {}
    for sheet in xl.sheet_names:
        df_w = pd.read_excel(PATH_WEATHER, sheet_name=sheet)
        # 컬럼명 정규화 (괄호 등 대응)
        col_map = {}
        for c in df_w.columns:
            if '일시' in str(c): col_map[c] = 'date'
            elif '평균기온' in str(c): col_map[c] = '평균기온'
            elif '일강수량' in str(c): col_map[c] = '일강수량'
        df_w = df_w.rename(columns=col_map)
        weather_dict[sheet] = df_w
    return weather_dict

# 휴일 데이터를 불러옵니다.
def load_holidays():
    hol = pd.read_excel(PATH_HOLIDAY)
    hol["날짜"] = pd.to_datetime(hol["날짜"])
    return set(hol["날짜"].dt.date.tolist())

# 목록 공장 관련 처리를 담당합니다.
def list_plants():
    wb = openpyxl.load_workbook(PATH_INTENSITY, read_only=True, data_only=True)
    return wb.sheetnames

# =========================
# Feature Engineering
# =========================
# 피처 데이터를 만듭니다.
def make_features(df):

    d = df.copy()
    d["date"] = pd.to_datetime(d["날짜"])
    d = d.sort_values("date")

    d["mix_ton"] = d["믹스생산량[kg]"] / 1000.0 if "믹스생산량[kg]" in d.columns else 0.0

    d["dow"] = d["date"].dt.dayofweek
    d["month"] = d["date"].dt.month

    for col in ["mix_ton", "평균기온", "일강수량"]:
        if col in d.columns:
            d[f"{col}_lag1"] = d[col].shift(1)
            d[f"{col}_lag7"] = d[col].shift(7)
            d[f"{col}_r7mean"] = d[col].rolling(7, min_periods=1).mean()

    return d

# =========================
# 특수 이벤트 제거
# =========================
# 특이 이벤트를 감지합니다.
def detect_special_events(d):

    rolling_median = d["mix_ton"].rolling(30, min_periods=7).median()
    cond_low_mix = (d["mix_ton"] < rolling_median * 0.1) | (d["mix_ton"] < 1.0)

    d["is_special_event"] = cond_low_mix
    return d

# =========================
# X, y 생성
# =========================
# prepare xy 관련 처리를 담당합니다.
def prepare_xy(d, ycol):
    y = d[ycol].astype(float).values
    X = d.select_dtypes(include=[np.number]).drop(columns=[ycol], errors="ignore").copy()
    # LightGBM용 특수문자 제거
    X.columns = [re.sub(r'[^a-zA-Z0-9가-힣]', '_', str(c)) for c in X.columns]
    return X, y

# =========================
# Feature 선택
# =========================
# 피처을 고릅니다.
def select_features(X, model_type):

    cols = list(X.columns)

    if model_type == "M1":
        use = [c for c in cols if "mix" in c or "lag" in c or "dow" in c or "month" in c]
    elif model_type == "M2":
        use = [c for c in cols if "r7mean" in c or "lag7" in c]
    elif model_type == "M3":
        use = [c for c in cols if "lag1" in c or "lag7" in c]
    elif model_type == "M4":
        use = [c for c in cols if "mix" in c or "기온" in c or "temp" in c]
    else:
        use = cols

    return X[use].copy()

# =========================
# 앙상블
# =========================
# weights를 계산합니다.
def compute_weights(val_mapes):

    inv = [1.0 / max(v, 1e-6) for v in val_mapes]
    s = sum(inv)
    return [v / s for v in inv]

# 최적 weights를 계산합니다.
def compute_optimal_weights(val_preds, y_val):

    n_models = len(val_preds)

    # 현재 조건의 오차를 계산합니다.
    def objective(w):
        pred = sum(w[i] * val_preds[i] for i in range(n_models))
        return mape(y_val, pred)

    constraints = ({'type': 'eq', 'fun': lambda w: np.sum(w) - 1})
    bounds = [(0, 1)] * n_models
    init = np.ones(n_models) / n_models

    result = minimize(objective, init, method='SLSQP', bounds=bounds, constraints=constraints)

    return result.x if result.success else init

# =========================
# 메인 실행
# =========================
# experiment 작업을 실행합니다.
def run_experiment():

    holiday_set = load_holidays()
    weather = load_weather()
    plants = list_plants()

    rows = []

    for plant in plants:
        if plant not in STATION_MAP:
            continue
            
        print(f"Processing {plant}...")
        df = load_energy_sheet(PATH_INTENSITY, plant)
        df["날짜"] = pd.to_datetime(df["날짜"])

        station = STATION_MAP[plant]
        w = weather[station]

        merged = df.merge(w, left_on="날짜", right_on="date", how="left").drop(columns=["date"])

        d = make_features(merged)

        # 🔥 공휴일 제거
        d = d[d["date"].apply(lambda x: is_workday(x, holiday_set))]

        # 🔥 특수 이벤트 제거
        d = detect_special_events(d)
        d = d[~d["is_special_event"]]

        for tgt_name, ycol in TARGETS.items():

            mtr = (d["date"] >= TRAIN_START) & (d["date"] <= TRAIN_END)
            mva = (d["date"] >= VALID_START) & (d["date"] <= VALID_END)
            mte = (d["date"] >= TEST_START) & (d["date"] <= TEST_END)

            if mtr.sum() < 200 or mva.sum() < 50 or mte.sum() < 50:
                continue

            X_full, y = prepare_xy(d, ycol)

            ytr, yva, yte = y[mtr], y[mva], y[mte]

            val_preds = []
            test_preds = []
            single_mape = {}

            for mt in ["M1", "M2", "M3", "M4"]:

                X = select_features(X_full, mt)

                Xtr, Xva, Xte = X[mtr], X[mva], X[mte]

                model = LGBMRegressor(
                    random_state=42,
                    n_estimators=3000,
                    learning_rate=0.03,
                    num_leaves=63
                )

                model.fit(Xtr, np.log1p(ytr))

                pv = np.expm1(model.predict(Xva))
                pt = np.expm1(model.predict(Xte))

                val_preds.append(pv)
                test_preds.append(pt)

                single_mape[mt] = mape(yte, pt)

            # 앙상블
            val_mapes = [mape(yva, p) for p in val_preds]

            w_inv = compute_weights(val_mapes)
            pred_inv = sum(w * p for w, p in zip(w_inv, test_preds))
            m_inv = mape(yte, pred_inv)

            w_opt = compute_optimal_weights(val_preds, yva)
            pred_opt = sum(w * p for w, p in zip(w_opt, test_preds))
            m_opt = mape(yte, pred_opt)

            rows.append({
                "plant": plant,
                "target": tgt_name,
                "M1": single_mape["M1"],
                "M2": single_mape["M2"],
                "M3": single_mape["M3"],
                "M4": single_mape["M4"],
                "Ensemble_INV": m_inv,
                "Ensemble_OPT": m_opt
            })

    return pd.DataFrame(rows)

# =========================
# 저장
# =========================
# 이 파일의 전체 실행 흐름을 시작합니다.
def main():

    df = run_experiment()

    output = "모델_앙상블_비교실험.xlsx"

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for tgt in df["target"].unique():
            df_t = df[df["target"] == tgt]
            df_t.to_excel(writer, sheet_name=tgt, index=False)

    print(f"저장 완료: {output}")

if __name__ == "__main__":
    main()
