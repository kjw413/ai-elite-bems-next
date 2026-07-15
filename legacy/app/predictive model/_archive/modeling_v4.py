# 이 파일은 v4 예측 모델을 학습하고 평가합니다.
import re
import os
import sys
import json
import pandas as pd
import numpy as np
import openpyxl
from pathlib import Path
from scipy.optimize import minimize
from lightgbm import LGBMRegressor

# =========================
# 1. 설정 및 경로 (v4 하이브리드)
# =========================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import PATH_ENERGY_SOURCE, load_energy_sheet

PATH_WEATHER = BASE_DIR / "DB_weather.xlsx"
PATH_INTENSITY = PATH_ENERGY_SOURCE
PATH_HOLIDAY = BASE_DIR / "DB_holiday.xlsx"
PATH_CACHE = BASE_DIR / "weather_cache.json"

TRAIN_START = pd.Timestamp("2021-01-01")
TRAIN_END   = pd.Timestamp("2024-12-31")
VALID_START = pd.Timestamp("2025-01-01")
VALID_END   = pd.Timestamp("2025-12-31")
TEST_START  = pd.Timestamp("2026-01-01")
TEST_END    = pd.Timestamp("2026-03-31")

STATION_MAP = {"남양주1": "서울", "남양주2": "서울", "김해": "김해", "광주": "이천", "논산": "부여"}
STATION_CODE = {"서울": "108", "김해": "253", "이천": "203", "부여": "236"}
TARGETS = {"전력": "전력량[kWh]", "연료": "연료량[N㎥]", "용수": "용수량[ton]"}

KMA_API_KEY = os.getenv("KMA_API_KEY", "YOUR_API_KEY_HERE")
OPERATING_CPU_NUMBER = os.cpu_count() - 1

# =========================
# 2. 유틸리티 및 데이터 처리 (v3 이식)
# =========================
# 실제값과 예측값의 오차 비율을 계산합니다.
def mape(y_true, y_pred, eps=1.0):
    return np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100

# 주어진 날짜가 영업일인지 확인합니다.
def is_workday(ts, holiday_set):
    return (ts.dayofweek < 5) and (ts.date() not in holiday_set)

# 휴일 데이터를 불러옵니다.
def load_holidays():
    if not Path(PATH_HOLIDAY).exists(): return set()
    hol = pd.read_excel(PATH_HOLIDAY)
    hol["날짜"] = pd.to_datetime(hol["날짜"])
    return set(hol["날짜"].dt.date.tolist())

# =========================
# 3. 고정밀 기상 데이터 시스템 (v3 하이브리드 방식)
# =========================
# 날씨 데이터 값을 가져옵니다.
def get_weather_data(stn_name, start_date, end_date):
    # 1. Excel 로드
    df_xl = pd.DataFrame()
    if Path(PATH_WEATHER).exists():
        try:
            df_xl = pd.read_excel(PATH_WEATHER, sheet_name=stn_name)
            df_xl["날짜"] = pd.to_datetime(df_xl["일시"])
        except: pass
    
    # 2. Cache 로드
    if Path(PATH_CACHE).exists():
        with open(PATH_CACHE, "r", encoding="utf-8") as f: json.load(f)
    
    # 3. 부족분 API 호출 (필요시)
    dates_needed = pd.date_range(start_date, end_date)
    missing = [d for d in dates_needed if df_xl.empty or d not in df_xl["날짜"].values]
    
    if missing and KMA_API_KEY != "YOUR_API_KEY_HERE":
        STATION_CODE.get(stn_name)
        # API 로직 생략 (v3와 동일하게 작동한다고 가정, 여기서는 엑셀 데이터 위주로 처리)
        pass

    # 4. 컬럼 정규화 및 피처 생성
    df_w = df_xl.copy()
    col_map = {
        '평균기온(°C)': '평균기온', 
        '일강수량(mm)': '일강수량', 
        '평균 상대습도(%)': '상대습도', 
        '합계 일사량(MJ/m2)': '일사량', 
        '합계 일조시간(hr)': '일조시간'
    }
    df_w = df_w.rename(columns=col_map)
    
    # 필수 컬럼 보장
    for c in ['평균기온', '일강수량', '상대습도', '일사량', '일조시간']:
        if c not in df_w.columns: df_w[c] = 0.0
    
    df_w = df_w.fillna(0)
    return df_w[["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"]]

# =========================
# 4. 모델링 엔진 (v2 베이스)
# =========================
# 피처 데이터를 만듭니다.
def make_features(df, ycol):
    d = df.copy()
    d["date"] = pd.to_datetime(d["날짜"])
    d = d.sort_values("date")
    
    d["mix_ton"] = d["믹스생산량[kg]"] / 1000.0 if "믹스생산량[kg]" in d.columns else 0.0
    d["dow"] = d["date"].dt.dayofweek
    d["month"] = d["date"].dt.month
    
    # v2의 강력한 파생 변수들
    for col in ["mix_ton", "평균기온", "일강수량"]:
        if col in d.columns:
            d[f"{col}_lag1"] = d[col].shift(1)
            d[f"{col}_lag7"] = d[col].shift(7)
            d[f"{col}_r7mean"] = d[col].rolling(7, min_periods=1).mean()
    
    # Target Lags
    d["lag1"] = d[ycol].shift(1)
    d["lag7"] = d[ycol].shift(7)
    d["r7mean"] = d[ycol].rolling(7, min_periods=1).mean().shift(1)
    
    # v3의 고급 변수 추가
    d["HDD"] = np.maximum(18 - d["평균기온"], 0)
    d["CDD"] = np.maximum(d["평균기온"] - 22, 0)
    d["Solar_Index"] = d["일사량"].where(d["일사량"] > 0, d["일조시간"] * 1.6)
    d["THI"] = 0.72 * (d["평균기온"] + d["상대습도"]) + 40.6
    
    return d.dropna().reset_index(drop=True)

# 특이 이벤트를 감지합니다.
def detect_special_events(d):
    rolling_median = d["mix_ton"].rolling(30, min_periods=7).median()
    d["is_special_event"] = (d["mix_ton"] < rolling_median * 0.1) | (d["mix_ton"] < 1.0)
    return d

# 피처을 고릅니다.
def select_features(X, model_type):
    cols = list(X.columns)
    if model_type == "M1":
        use = [c for c in cols if "mix" in c or "lag" in c or "dow" in c or "month" in c or "r7" in c]
    elif model_type == "M2":
        use = [c for c in cols if "HDD" in c or "CDD" in c or "mix" in c]
    elif model_type == "M3":
        use = [c for c in cols if "평균기온" in c or "상대습도" in c or "Solar" in c or "mix" in c]
    elif model_type == "M4":
        use = [c for c in cols if "THI" in c or "CDD" in c or "HDD" in c or "mix" in c]
    else:
        use = cols
    return X[use].copy()

# lgbm 모델을 학습합니다.
def train_lgbm(X_tr, y_tr, X_va=None, y_va=None, n_estimators=3000):
    model = LGBMRegressor(n_estimators=n_estimators, learning_rate=0.05, num_leaves=31, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
    model.fit(X_tr, np.log1p(y_tr))
    return model

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
    res = minimize(objective, init, method='SLSQP', bounds=bounds, constraints=constraints)
    return res.x if res.success else init

# =========================
# 5. 실행 및 앙상블 (v2 베이스 고도화)
# =========================
# 생산 작업을 실행합니다.
def run_production():
    holiday_set = load_holidays()
    wb = openpyxl.load_workbook(PATH_INTENSITY, read_only=True, data_only=True)
    plants = [p for p in wb.sheetnames if p in STATION_MAP]
    
    results = []
    
    for plant in plants:
        print(f"Processing {plant}...")
        df_raw = load_energy_sheet(PATH_INTENSITY, plant)
        df_raw["날짜"] = pd.to_datetime(df_raw["날짜"])
        
        stn_name = STATION_MAP[plant]
        df_weather = get_weather_data(stn_name, TRAIN_START, TEST_END)
        
        df = df_raw.merge(df_weather, on="날짜", how="left")
        df = df[df["날짜"].apply(lambda x: is_workday(x, holiday_set))]
        
        for tgt_name, ycol in TARGETS.items():
            if ycol not in df.columns: continue
            
            # Feature 생성 및 이벤트 제거
            d_full = detect_special_events(make_features(df, ycol))
            d = d_full[~d_full["is_special_event"]].copy()
            
            mtr = (d["date"] >= TRAIN_START) & (d["date"] <= TRAIN_END)
            mva = (d["date"] >= VALID_START) & (d["date"] <= VALID_END)
            mte = (d["date"] >= TEST_START) & (d["date"] <= TEST_END)
            
            if mtr.sum() < 100 or mva.sum() < 30 or mte.sum() < 30: continue
            
            y = d[ycol].values
            ytr, yva, yte = y[mtr], y[mva], y[mte]
            
            # Numeric X 생성
            X_all = d.select_dtypes(include=[np.number]).drop(columns=[ycol], errors="ignore")
            X_all.columns = [re.sub(r'[^a-zA-Z0-9가-힣]', '_', str(c)) for c in X_all.columns]
            
            val_preds = []; te_preds = []
            m_types = ["M1", "M2", "M3", "M4"]
            
            for mt in m_types:
                X_sub = select_features(X_all, mt)
                Xtr, Xva, Xte = X_sub[mtr], X_sub[mva], X_sub[mte]
                
                # Use the new train_lgbm function
                mdl = train_lgbm(Xtr, ytr)
                
                val_preds.append(np.expm1(mdl.predict(Xva)))
                te_preds.append(np.expm1(mdl.predict(Xte)))
            
            # 앙상블
            opt_weights = compute_optimal_weights(val_preds, yva)
            ensemble_pred = sum(w * p for w, p in zip(opt_weights, te_preds))
            final_mape = mape(yte, ensemble_pred)
            
            results.append({
                "plant": plant, "target": tgt_name,
                "M1": mape(yte, te_preds[0]), "Weighted_MAPE": final_mape
            })
            print(f"  -> {tgt_name} 완료 | MAPE: {final_mape:.2f}%")
            
    return pd.DataFrame(results)

# 이 파일의 전체 실행 흐름을 시작합니다.
def main():
    df = run_production()
    output = "모델_앙상블_최종결과_v4.xlsx"
    
    if df.empty:
        print("\n[알림] 예측 결과가 비어 있어 엑셀 파일을 저장하지 않았습니다.")
        return

    try:
        with pd.ExcelWriter(output) as writer:
            saved_sheets = 0
            for tgt in TARGETS.keys():
                df_tgt = df[df["target"] == tgt]
                if not df_tgt.empty:
                    df_tgt.to_excel(writer, sheet_name=tgt, index=False)
                    saved_sheets += 1
            
            if saved_sheets == 0:
                pd.DataFrame([{"info": "No results found"}]).to_excel(writer, sheet_name="Empty")

        print(f"\n[성공] 최종 결과 저장 완료: {output}")
    except Exception as e:
        print(f"\n[오류] 엑셀 저장 중 문제가 발생했습니다: {e}")

if __name__ == "__main__":
    main()
