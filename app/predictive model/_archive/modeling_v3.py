# 이 파일은 v3 예측 모델을 학습하고 평가합니다.
import os
import re
import sys
import requests
import openpyxl
import numpy as np
import pandas as pd
from pathlib import Path
from lightgbm import LGBMRegressor
from scipy.optimize import minimize

# ==========================================
# 1. 초기 설정 및 API 키 (환경 변수 권장)
# ==========================================
KMA_API_KEY = os.getenv("KMA_API_KEY", "")  # .env의 KMA_API_KEY 사용 (키 하드코딩 금지)
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import PATH_ENERGY_SOURCE, load_energy_sheet

MODEL_DIR = BASE_DIR / "trained_model"
CACHE_DIR = BASE_DIR / "weather_cache"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TOTAL_CPU_CORE_NUMBER = os.cpu_count()
OPERATING_CPU_NUMBER = max(1, int(TOTAL_CPU_CORE_NUMBER / 2))

# ==========================================
# 2. 경로 및 날짜 설정
# ==========================================
PATH_INTENSITY = PATH_ENERGY_SOURCE
PATH_HOLIDAY = BASE_DIR / "DB_holiday.xlsx"

TRAIN_START        = pd.Timestamp("2021-01-01")
TRAIN_END          = pd.Timestamp("2025-06-30")
VALIDATION_START   = pd.Timestamp("2025-07-01")
VALIDATION_END     = pd.Timestamp("2025-09-30")
TEST_START         = pd.Timestamp("2025-10-01")
TEST_END           = pd.Timestamp("2026-02-13")

STATION_ID_MAP = { "서울": "108", "이천": "203", "부여": "236", "김해": "253" }
STATION_MAP = { "남양주1": "서울", "남양주2": "서울", "김해": "김해", "광주": "이천", "논산": "부여" }
TARGET_MAPS = { "전력": "전력량[kWh]", "연료": "연료량[N㎥]", "용수": "용수량[ton]" }

# ==========================================
# 3. 날씨 데이터 처리
# ==========================================
# 날씨 from apihub 데이터를 조회합니다.
def fetch_weather_from_apihub(stn_id, start_date, end_date):
    url = "https://apihub.kma.go.kr/api/typ01/url/kma_sfcdd3.php"
    current_start = start_date
    all_dfs = []
    while current_start <= end_date:
        current_end = min(current_start + pd.Timedelta(days=364), end_date)
        s_dt, e_dt = current_start.strftime("%Y%m%d"), current_end.strftime("%Y%m%d")
        params = {"tm1": s_dt, "tm2": e_dt, "stn": stn_id, "help": "0", "authKey": KMA_API_KEY}
        try:
            print(f"    -> API 호출 중... ({s_dt}~{e_dt}, 지점:{stn_id})")
            response = requests.get(url, params=params, timeout=30)
            if response.status_code == 200 and "#" not in response.text[:10]:
                lines = [line for line in response.text.split("\n") if line.strip() and not line.startswith("#")]
                if len(lines) > 2:
                    cols = ["날짜", "지점"] + [f"COL_{i}" for i in range(2, 60)]
                    df = pd.read_csv(pd.io.common.StringIO("\n".join(lines)), sep=r"\s+", names=cols, index_col=False)
                    # 필요한 컬럼 매핑 (kma_sfcdd3.php 명세 기준)
                    # 1:날짜(TM), 11:평균기온(TA_AVG), 13:일강수량(RN_DAY), 19:평균상대습도(HM_AVG), 35:합계일조시간(SS_DAY), 37:합계일사량(SI_DAY), 23:평균현지기압(PA_AVG)
                    df_res = pd.DataFrame()
                    df_res["날짜"] = pd.to_datetime(df["날짜"], format="%Y%m%d")
                    df_res["TA_AVG"] = df["COL_11"]
                    df_res["RN_DAY"] = df["COL_13"]
                    df_res["HM_AVG"] = df["COL_19"]
                    df_res["SS_DAY"] = df["COL_35"]
                    df_res["SI_DAY"] = df["COL_37"]
                    df_res["PA_AVG"] = df["COL_23"]
                    all_dfs.append(df_res)
        except: pass
        current_start = current_end + pd.Timedelta(days=1)
    if not all_dfs: return pd.DataFrame()
    df = pd.concat(all_dfs).drop_duplicates(subset=["날짜"])
    numeric_cols = ["TA_AVG", "RN_DAY", "HM_AVG", "SI_DAY", "SS_DAY", "PA_AVG"]
    for c in numeric_cols:
        if c in df.columns: df[c] = pd.to_numeric(df[c].replace('-', np.nan), errors='coerce').fillna(0)
        else: df[c] = 0.0
    df = df.rename(columns={"TA_AVG": "평균기온", "RN_DAY": "일강수량", "HM_AVG": "상대습도", "SI_DAY": "일사량", "SS_DAY": "일조시간", "PA_AVG": "현지기압"})
    return df[["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간", "현지기압"]]

# 날씨 데이터 값을 가져옵니다.
def get_weather_data(stn_name, start_date, end_date):
    stn_id = STATION_ID_MAP.get(stn_name)
    if not stn_id: raise ValueError(f"알 수 없는 지점명: {stn_name}")
    path_weather_excel = BASE_DIR / "DB_weather.xlsx"
    df_history = pd.DataFrame()
    
    if path_weather_excel.exists():
        try:
            df_hist_raw = pd.read_excel(path_weather_excel, sheet_name=stn_name)
            col_map = {}
            for c in df_hist_raw.columns:
                c_str = str(c)
                if '일시' in c_str or '날짜' in c_str: col_map['날짜'] = c
                elif '기온' in c_str: col_map['평균기온'] = c
                elif '강수' in c_str: col_map['일강수량'] = c
                elif '습도' in c_str: col_map['상대습도'] = c
                elif '기압' in c_str: col_map['현지기압'] = c
                elif '일조' in c_str: col_map['일조시간'] = c
                elif '일사' in c_str: col_map['일사량'] = c
            needed = ["날짜", "평균기온", "일강수량", "상대습도", "현지기압", "일조시간", "일사량"]
            final_cols = []
            for n in needed:
                if n in col_map: final_cols.append(col_map[n])
                else: df_hist_raw[n] = 0.0; final_cols.append(n)
            df_history = df_hist_raw[final_cols].copy(); df_history.columns = needed
            df_history["날짜"] = pd.to_datetime(df_history["날짜"])
            for c in df_history.columns:
                if c != "날짜": df_history[c] = pd.to_numeric(df_history[c], errors='coerce').fillna(0)
        except Exception as e: print(f"  [Warning] 기존 날씨 엑셀({stn_name}) 읽기 실패: {e}")

    cache_file = CACHE_DIR / f"weather_{stn_id}.csv"
    if cache_file.exists():
        try:
            df_cache = pd.read_csv(cache_file); df_cache["날짜"] = pd.to_datetime(df_cache["날짜"])
            df_history = pd.concat([df_history, df_cache], ignore_index=True).drop_duplicates(subset=["날짜"], keep="first") if not df_history.empty else df_cache
        except: pass

    if not df_history.empty:
        df_history = df_history.sort_values("날짜")
        if (df_history["날짜"].min() <= start_date) and (df_history["날짜"].max() >= end_date) and not (df_history["상대습도"] == 0).all():
            return df_history[(df_history["날짜"] >= start_date) & (df_history["날짜"] <= end_date)]

    api_start = df_history["날짜"].max() + pd.Timedelta(days=1) if not df_history.empty else start_date
    if api_start <= end_date:
        print(f"  -> {stn_name} 부족 데이터 API 보충 중... ({api_start.date()} ~ {end_date.date()})")
        df_new = fetch_weather_from_apihub(stn_id, api_start, end_date)
        if not df_new.empty:
            df_history = pd.concat([df_history, df_new], ignore_index=True).drop_duplicates(subset=["날짜"], keep="last")
            df_history.sort_values(by="날짜").to_csv(cache_file, index=False)
    return df_history[(df_history["날짜"] >= start_date) & (df_history["날짜"] <= end_date)] if not df_history.empty else pd.DataFrame()

# ==========================================
# 4. 모델링 엔진 (오차율 최소화 타겟)
# ==========================================
# 휴일 데이터를 불러옵니다.
def load_holidays():
    if Path(PATH_HOLIDAY).exists():
        hol = pd.read_excel(PATH_HOLIDAY); hol["날짜"] = pd.to_datetime(hol["날짜"])
        return set(hol["날짜"].dt.date.tolist())
    return set()

# 주어진 날짜가 영업일인지 확인합니다.
def is_workday(ts, holiday_set): return (ts.dayofweek < 5) and (ts.date() not in holiday_set)

# 피처 데이터를 만듭니다.
def make_features(df, ycol):
    d = df.copy().sort_values("날짜")
    d["month"] = d["날짜"].dt.month; d["dayofweek"] = d["날짜"].dt.dayofweek
    d["mix_ton"] = d["믹스생산량[kg]"] / 1000.0 if "믹스생산량[kg]" in d.columns else 0.0
    d["HDD"] = np.maximum(18 - d["평균기온"], 0); d["CDD"] = np.maximum(d["평균기온"] - 22, 0)
    if "일사량" in d.columns and "일조시간" in d.columns: d["Solar_Index"] = d["일사량"].where(d["일사량"] > 0, d["일조시간"] * 1.6)
    else: d["Solar_Index"] = 0.0
    d["THI"] = 0.72 * (d["평균기온"] + d["상대습도"]) + 40.6 if "상대습도" in d.columns else 0.0
    # Lag Features
    d["lag1"] = d[ycol].shift(1); d["lag7"] = d[ycol].shift(7); d["r7mean"] = d[ycol].rolling(7, min_periods=1).mean().shift(1)
    d["mix_lag1"] = d["mix_ton"].shift(1); d["mix_lag7"] = d["mix_ton"].shift(7)
    return d.dropna().reset_index(drop=True)

# 특이 이벤트를 감지합니다.
def detect_special_events(d):
    rolling_median = d["mix_ton"].rolling(30, min_periods=7).median()
    d["is_special_event"] = (d["mix_ton"] < rolling_median * 0.1) | (d["mix_ton"] < 1.0)
    return d

# prepare xy 관련 처리를 담당합니다.
def prepare_xy(d, ycol, feature_list):
    X = d[feature_list].copy(); X.columns = [re.sub(r'[^a-zA-Z0-9가-힣]', '_', str(c)) for c in X.columns]
    y = d[ycol].values; return X, y

MODEL_FEATURE_SETS = {
    "전력": {
        "M1": ["lag1", "lag7", "r7mean", "mix_ton", "mix_lag1", "mix_lag7", "month", "dayofweek", "현지기압"],
        "M3": ["mix_ton", "평균기온", "일강수량", "상대습도", "Solar_Index", "month", "dayofweek"],
        "M4": ["HDD", "CDD", "mix_ton", "상대습도", "Solar_Index", "THI", "month", "dayofweek"]
    },
    "default": {
        "M1": ["lag1", "lag7", "r7mean", "mix_ton", "mix_lag1", "mix_lag7", "month", "dayofweek"],
        "M3": ["mix_ton", "평균기온", "일강수량", "month", "dayofweek"],
        "M4": ["HDD", "CDD", "mix_ton", "month", "dayofweek"]
    }
}

# 실제값과 예측값의 오차 비율을 계산합니다.
def mape(y_true, y_pred, eps=1.0): return np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100

# lgbm 모델을 학습합니다.
def train_lgbm(X_tr, y_tr, X_va, y_va, n_estimators=3000):
    model = LGBMRegressor(n_estimators=n_estimators, learning_rate=0.05, num_leaves=31, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
    model.fit(X_tr, np.log1p(y_tr))
    return model

# 최적 weights를 계산합니다.
def compute_optimal_weights(val_preds, y_val):
    # 현재 조건의 오차를 계산합니다.
    def objective(w):
        pred = sum(w[i] * val_preds[i] for i in range(len(w)))
        return mape(y_val, pred)
    cons = ({'type': 'eq', 'fun': lambda w: np.sum(w) - 1.0})
    res = minimize(objective, [1/len(val_preds)]*len(val_preds), method='SLSQP', bounds=[(0,1)]*len(val_preds), constraints=cons)
    return res.x if res.success else [1/len(val_preds)]*len(val_preds)

# 생산 high accuracy 작업을 실행합니다.
def run_production_high_accuracy():
    holiday_set = load_holidays()
    if not Path(PATH_INTENSITY).exists(): return
    wb = openpyxl.load_workbook(PATH_INTENSITY, read_only=True, data_only=True)
    
    for plant in wb.sheetnames:
        if plant not in STATION_MAP: continue
        print(f"\n[Plant: {plant}] 정확도 중심 모델 학습 시작")
        df_raw = load_energy_sheet(PATH_INTENSITY, plant); df_raw["날짜"] = pd.to_datetime(df_raw["날짜"])
        stn_name = STATION_MAP.get(plant); df_weather = get_weather_data(stn_name, TRAIN_START, TEST_END)
        if df_weather.empty: continue
        
        df = df_raw.merge(df_weather, on="날짜", how="left")
        df = df[df["날짜"].apply(lambda x: is_workday(x, holiday_set))]
        
        for tgt_name, ycol in TARGET_MAPS.items():
            if ycol not in df.columns: continue
            print(f"  -> {tgt_name} 모델링 중...")
            
            d_full = detect_special_events(make_features(df, ycol))
            d_clean = d_full[~d_full["is_special_event"]].copy()
            
            mask_tr = (d_clean["날짜"] >= TRAIN_START) & (d_clean["날짜"] <= TRAIN_END)
            mask_va = (d_clean["날짜"] >= VALIDATION_START) & (d_clean["날짜"] <= VALIDATION_END)
            
            f_cfg = MODEL_FEATURE_SETS.get(tgt_name, MODEL_FEATURE_SETS["default"])
            val_preds = []; trained_models = {}
            for mtype, fs in f_cfg.items():
                X_tr, y_tr = prepare_xy(d_clean[mask_tr], ycol, fs)
                X_va, y_va = prepare_xy(d_clean[mask_va], ycol, fs)
                
                mdl = train_lgbm(X_tr, y_tr, X_va, y_va)
                pv = np.expm1(mdl.predict(X_va))
                val_preds.append(pv); trained_models[mtype] = (mdl, fs)

            # 앙상블 가중치 계산
            y_val = d_clean[mask_va][ycol].values
            weights = compute_optimal_weights(val_preds, y_val)
            ensemble_mape = mape(y_val, sum(w * p for w, p in zip(weights, val_preds)))
            
            # 최종 모델 저장 (검증 기간까지 포함하여 재학습)
            final_models_save = {}
            for mtype, (mdl, fs) in trained_models.items():
                X_tot, y_tot = prepare_xy(d_clean[mask_tr | mask_va], ycol, fs)
                fm = LGBMRegressor(n_estimators=mdl.best_iteration_, learning_rate=0.03, num_leaves=63, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
                fm.fit(X_tot, np.log1p(y_tot))
                final_models_save[mtype] = fm
                
            save_obj = {
                "type": "ENSEMBLE",
                "models": final_models_save,
                "features": {mt: trained_models[mt][1] for mt in trained_models},
                "weights": weights,
                "val_mape": ensemble_mape,
                "resid_std": np.std(y_val - sum(w * p for w, p in zip(weights, val_preds)))
            }
            save_path = MODEL_DIR / f"model_{plant}_{tgt_name}.pkl"
            with open(save_path, "wb") as f: pickle.dump(save_obj, f)
            print(f"    -> {tgt_name} 완료 (MAPE: {ensemble_mape:.2f}%)")

if __name__ == "__main__":
    import pickle
    run_production_high_accuracy()
