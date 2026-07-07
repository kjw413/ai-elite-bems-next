# 이 파일은 v5 예측 모델을 학습하고 결과 파일을 저장합니다.
import re
import os
import requests
import json
import joblib
import pandas as pd
import numpy as np
import openpyxl
import warnings
from pathlib import Path
from scipy.optimize import minimize
from lightgbm import LGBMRegressor
from xgboost import XGBRegressor
from catboost import CatBoostRegressor

warnings.filterwarnings("ignore")

# =========================
# 1. 설정 및 경로
# =========================
import sys
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import (
    PATH_ENERGY_SOURCE,
    detect_special_events,
    get_safe_features,
    load_energy_sheet,
    make_features,
)

PATH_WEATHER   = BASE_DIR / "DB_weather.xlsx"
PATH_INTENSITY = PATH_ENERGY_SOURCE
PATH_HOLIDAY   = BASE_DIR / "DB_holiday.xlsx"
PATH_CACHE     = BASE_DIR / "weather_cache.json"

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
OPERATING_CPU_NUMBER = max(1, os.cpu_count() - 1)

# =========================
# 2. 유틸리티 및 데이터 처리
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
# 3. 고정밀 기상 데이터 시스템
# =========================
# 날씨 데이터 값을 가져옵니다.
def get_weather_data(stn_name, start_date, end_date):
    df_xl = pd.DataFrame()
    if Path(PATH_WEATHER).exists():
        try:
            df_xl = pd.read_excel(PATH_WEATHER, sheet_name=stn_name)
            df_xl["날짜"] = pd.to_datetime(df_xl["일시"])
        except: pass

    cache = {}
    if Path(PATH_CACHE).exists():
        with open(PATH_CACHE, "r", encoding="utf-8") as f: cache = json.load(f)

    # Missing date logic via API (Skipped as we rely on cache/excel mostly)
    
    df_w = df_xl.copy()
    col_map = {
        '평균기온(°C)': '평균기온', 
        '일강수량(mm)': '일강수량', 
        '평균 상대습도(%)': '상대습도', 
        '합계 일사량(MJ/m2)': '일사량', 
        '합계 일조시간(hr)': '일조시간'
    }
    if not df_w.empty:
        df_w = df_w.rename(columns=col_map)
        
        # 필수 컬럼 보장
        for c in ['평균기온', '일강수량', '상대습도', '일사량', '일조시간']:
            if c not in df_w.columns: df_w[c] = 0.0
            
        df_w = df_w.fillna(0)
        return df_w[["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"]]
    else:
        # 빈 데이터프레임 반환
        return pd.DataFrame(columns=["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간"])

# =========================
# 4. 모델링 엔진
# =========================

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

# 모델을 학습합니다.
def train_models(X_tr, y_tr, n_estimators=3000):
    lgbm = LGBMRegressor(n_estimators=n_estimators, learning_rate=0.05, num_leaves=31, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
    lgbm.fit(X_tr, np.log1p(y_tr))
    
    xgb = XGBRegressor(n_estimators=n_estimators, learning_rate=0.05, max_depth=6, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
    xgb.fit(X_tr, np.log1p(y_tr))
    
    cat = CatBoostRegressor(iterations=n_estimators, learning_rate=0.05, depth=6, verbose=0, random_seed=42, thread_count=OPERATING_CPU_NUMBER)
    cat.fit(X_tr, np.log1p(y_tr))
    
    return [lgbm, xgb, cat]

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
# 5. 실행 및 앙상블 저장
# =========================
# 생산 작업을 실행합니다.
def run_production():
    holiday_set = load_holidays()
    
    if not Path(PATH_INTENSITY).exists():
        print(f"Error: {PATH_INTENSITY} not found.")
        return pd.DataFrame(), {}
        
    wb = openpyxl.load_workbook(PATH_INTENSITY, read_only=True, data_only=True)
    plants = [p for p in wb.sheetnames if p in STATION_MAP]
    
    results = []
    saved_models_dict = {} # plant -> target -> {models, weights, m_types, features}
    
    for plant in plants:
        print(f"Processing {plant}...")
        df_raw = load_energy_sheet(PATH_INTENSITY, plant)
        if "날짜" not in df_raw.columns:
            continue
        df_raw["날짜"] = pd.to_datetime(df_raw["날짜"])
        
        stn_name = STATION_MAP[plant]
        df_weather = get_weather_data(stn_name, TRAIN_START, TEST_END)
        
        if not df_weather.empty:
            df = df_raw.merge(df_weather, on="날짜", how="left")
        else:
            df = df_raw.copy()
            
        df = df[df["날짜"].apply(lambda x: is_workday(x, holiday_set))]
        saved_models_dict[plant] = {}
        
        for tgt_name, ycol in TARGETS.items():
            if ycol not in df.columns: continue
            
            d_full = detect_special_events(make_features(df, ycol, holiday_set))
            d = d_full[~d_full["is_special_event"]].copy()
            
            mtr = (d["date"] >= TRAIN_START) & (d["date"] <= TRAIN_END)
            mva = (d["date"] >= VALID_START) & (d["date"] <= VALID_END)
            mte = (d["date"] >= TEST_START) & (d["date"] <= TEST_END)
            
            if mtr.sum() < 100 or mva.sum() < 30 or mte.sum() < 30: continue
            
            y = d[ycol].values
            ytr, yva, yte = y[mtr], y[mva], y[mte]
            
            X_all = get_safe_features(d)
            
            val_preds = []
            te_preds = []
            m_types = ["M1", "M2", "M3", "M4"]
            
            plant_target_models = []
            features_used = {}
            
            # 12개 모델 (M1~M4 * 3) 학습
            for mt in m_types:
                X_sub = select_features(X_all, mt)
                features_used[mt] = list(X_sub.columns)
                
                Xtr, Xva, Xte = X_sub[mtr], X_sub[mva], X_sub[mte]
                models = train_models(Xtr, ytr)
                
                for mdl in models:
                    plant_target_models.append(mdl)
                    val_preds.append(np.expm1(mdl.predict(Xva)))
                    te_preds.append(np.expm1(mdl.predict(Xte)))
            
            opt_weights = compute_optimal_weights(val_preds, yva)
            ensemble_pred = sum(w * p for w, p in zip(opt_weights, te_preds))
            final_mape = mape(yte, ensemble_pred)
            
            # 모델 딕셔너리에 저장
            saved_models_dict[plant][tgt_name] = {
                "models": plant_target_models,
                "weights": opt_weights,
                "m_types": m_types,
                "features": features_used,
                "target_col": ycol,
                "feature_spec_version": "1.1"
            }
            
            results.append({
                "plant": plant, 
                "target": tgt_name,
                "Advanced_MAPE": final_mape
            })
            print(f"  -> {tgt_name} 완료 | Ensemble MAPE: {final_mape:.2f}%")
            
    return pd.DataFrame(results), saved_models_dict

# 이 파일의 전체 실행 흐름을 시작합니다.
def main():
    df, models_dict = run_production()
    
    output_dir = BASE_DIR / "energy usage"
    output_dir.mkdir(parents=True, exist_ok=True)

    excel_output = output_dir / "performance_test_results_v5.xlsx"
    pkl_output = output_dir / "v5.pkl"
    
    if df.empty:
        print("\n[알림] 예측 결과가 비어 있어 파일을 저장하지 않았습니다.")
        return

    try:
        # 성능 테스트 결과 Excel 저장
        df.to_excel(excel_output, index=False)
        print(f"\n[성공] 성능 테스트 엑셀 표 저장 완료: {excel_output}")
        
        # 모델 `.pkl` 저장
        joblib.dump(models_dict, pkl_output)
        print(f"[성공] 예측 모델 저장 완료 (.pkl): {pkl_output}")
        
    except Exception as e:
        print(f"\n[오류] 저장 중 문제가 발생했습니다: {e}")

if __name__ == "__main__":
    main()
