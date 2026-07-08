# 이 파일은 이전 버전의 세분화 학습 실험 코드를 보관합니다.
import os
import sys
import joblib
import openpyxl
import pandas as pd
import numpy as np
import requests
import re
import time
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
from lightgbm import LGBMRegressor, early_stopping, log_evaluation

# 1. 환경 설정 - 프로젝트 루트의 .env 파일 로드
load_dotenv(find_dotenv())
KMA_API_KEY = os.getenv("KMA_API_KEY")

# 작업 디렉토리 설정
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.v5_common import PATH_ENERGY_SOURCE, load_energy_sheet

MODEL_DIR = BASE_DIR / "trained_model"
CACHE_DIR = BASE_DIR / "weather_cache"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 학습 과정에 사용할 CPU 코어 수 설정
TOTAL_CPU_CORE_NUMBER = os.cpu_count()
OPERATING_CPU_NUMBER = max(1, int(TOTAL_CPU_CORE_NUMBER / 2))

# ==========================================
# 2. 고정 경로 및 날짜 설정
# ==========================================
PATH_INTENSITY = PATH_ENERGY_SOURCE
PATH_HOLIDAY = BASE_DIR / "DB_holiday.xlsx"

TRAIN_START        = pd.Timestamp("2021-01-01")
TRAIN_END          = pd.Timestamp("2025-02-11")
VALIDATION_START   = pd.Timestamp("2025-02-12")
VALIDATION_END     = pd.Timestamp("2025-08-15")
TEST_START         = pd.Timestamp("2025-08-16")
TEST_END           = pd.Timestamp("2026-02-13")

FINAL_TRAIN_END  = VALIDATION_END

STATION_ID_MAP = { "서울": "108", "이천": "203", "부여": "236", "김해": "253" }
STATION_MAP = { "남양주1": "서울", "남양주2": "서울", "김해": "김해", "광주": "이천", "논산": "부여" }
TARGET_MAPS = { "전력": "전력량[kWh]", "연료": "연료량[N㎥]", "용수": "용수량[ton]" }

# ==========================================
# 3. 기상청 API 허브 연동 및 데이터 처리
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
            if response.status_code == 200:
                lines = response.text.strip().split("\n")
                data_rows, col_names = [], []
                for line in lines:
                    if line.startswith("#"):
                        if "TM" in line and "STN" in line: col_names = line.replace("#", "").split()
                        continue
                    row = line.split()
                    if len(row) > 0: data_rows.append(row)
                if data_rows and col_names: all_dfs.append(pd.DataFrame(data_rows, columns=col_names))
        except Exception as e: print(f"    [Error] API 호출 실패: {e}")
        current_start = current_end + pd.Timedelta(days=1)
        if current_start <= end_date: time.sleep(0.1)
    if not all_dfs: return pd.DataFrame()
    df = pd.concat(all_dfs, ignore_index=True)
    df["날짜"] = pd.to_datetime(df["TM"])
    numeric_cols = ["TA_AVG", "RN_DAY", "HM_AVG", "SI_DAY", "SS_DAY", "PA_AVG"]
    for c in numeric_cols:
        if c in df.columns: df[c] = pd.to_numeric(df[c].replace('-', np.nan), errors='coerce').fillna(0)
        else: df[c] = 0.0
    df = df.rename(columns={"TA_AVG": "평균기온", "RN_DAY": "일강수량", "HM_AVG": "상대습도", "SI_DAY": "일사량", "SS_DAY": "일조시간", "PA_AVG": "현지기압"})
    return df[["날짜", "평균기온", "일강수량", "상대습도", "일사량", "일조시간", "현지기압"]]

# 날씨 데이터 값을 가져옵니다.
def get_weather_data(stn_name, start_date, end_date):
    """기존 날씨 엑셀 파일 우선 확인 후 부족하면 API 허브에서 호출 (이후 캐싱)"""
    stn_id = STATION_ID_MAP.get(stn_name)
    if not stn_id: raise ValueError(f"알 수 없는 지점명: {stn_name}")
    
    path_weather_excel = BASE_DIR / "DB_weather.xlsx"
    df_history = pd.DataFrame()
    
    # 1. 엑셀 데이터 파일 로드 (사용자 수동 업데이트 데이터 최우선)
    if path_weather_excel.exists():
        try:
            # openpyxl 엔진을 사용하여 안전하게 로드
            df_hist_raw = pd.read_excel(path_weather_excel, sheet_name=stn_name)
            
            # 유연한 컬럼 매핑 (단위 포함 대응)
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
            
            # 필수 컬럼 추출 및 표준화
            needed = ["날짜", "평균기온", "일강수량", "상대습도", "현지기압", "일조시간", "일사량"]
            final_cols = []
            for n in needed:
                if n in col_map:
                    final_cols.append(col_map[n])
                else:
                    df_hist_raw[n] = 0.0
                    final_cols.append(n)
            
            df_history = df_hist_raw[final_cols].copy()
            df_history.columns = needed
            df_history["날짜"] = pd.to_datetime(df_history["날짜"])
            
            # 숫자형 변환 및 결측치 처리
            for c in df_history.columns:
                if c != "날짜":
                    df_history[c] = pd.to_numeric(df_history[c], errors='coerce').fillna(0)
                    
        except Exception as e:
            print(f"  [Warning] 기존 날씨 엑셀({stn_name}) 읽기 실패: {e}")
            df_history = pd.DataFrame()

    # 2. 로컬 캐시(CSV) 병합
    cache_file = CACHE_DIR / f"weather_{stn_id}.csv"
    if cache_file.exists():
        try:
            df_cache = pd.read_csv(cache_file)
            df_cache["날짜"] = pd.to_datetime(df_cache["날짜"])
            if not df_history.empty:
                # 엑셀 데이터가 있는 지점은 엑셀을 우선하되 없는 날짜만 캐시에서 보충
                df_history = pd.concat([df_history, df_cache], ignore_index=True).drop_duplicates(subset=["날짜"], keep="first")
            else:
                df_history = df_cache
        except: pass

    # 3. 데이터 충분성 확인
    if not df_history.empty:
        df_history = df_history.sort_values("날짜")
        has_full_range = (df_history["날짜"].min() <= start_date) and (df_history["날짜"].max() >= end_date)
        # 상대습도가 0이 아닌 유효한 데이터인지 확인 (사용자 업데이트 확인용)
        has_valid_data = not (df_history["상대습도"] == 0).all()
        
        if has_full_range and has_valid_data:
            return df_history[(df_history["날짜"] >= start_date) & (df_history["날짜"] <= end_date)]

    # 4. 부족한 경우에만 API 호출 (최신 데이터 보충용)
    api_start = df_history["날짜"].max() + pd.Timedelta(days=1) if not df_history.empty else start_date
    if api_start <= end_date:
        print(f"  -> {stn_name} 부족한 데이터 추가 수집 중... ({api_start.date()} ~ {end_date.date()})")
        df_new = fetch_weather_from_apihub(stn_id, api_start, end_date)
        if not df_new.empty:
            df_history = pd.concat([df_history, df_new], ignore_index=True).drop_duplicates(subset=["날짜"], keep="last")
            df_history.sort_values(by="날짜").to_csv(cache_file, index=False)
            
    if not df_history.empty:
        return df_history[(df_history["날짜"] >= start_date) & (df_history["날짜"] <= end_date)]
    
    return pd.DataFrame()

# ==========================================
# 4. 모델링 함수
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

# 실제값과 예측값의 오차 비율을 계산합니다.
def mape(y_true, y_pred, eps=1.0): return np.mean(np.abs((y_true - y_pred) / np.maximum(np.abs(y_true), eps))) * 100

MODEL_FEATURE_SETS = {
    "M1": ["lag1", "lag7", "mix_ton", "month", "dayofweek", "현지기압"],
    "M2": ["r7mean", "mix_lag1", "month", "dayofweek"],
    "M3": ["mix_ton", "평균기온", "일강수량", "상대습도", "Solar_Index", "month", "dayofweek"],
    "M4": ["HDD", "CDD", "mix_ton", "상대습도", "Solar_Index", "THI", "month", "dayofweek"]
}

# lgbm 모델을 학습합니다.
def train_lgbm(X_tr, y_tr, X_va, y_va, n_estimators=5000):
    model = LGBMRegressor(n_estimators=n_estimators, learning_rate=0.03, num_leaves=63, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
    model.fit(X_tr, np.log1p(y_tr), eval_set=[(X_va, np.log1p(y_va))], callbacks=[early_stopping(300), log_evaluation(0)])
    return model

# 생산 작업을 실행합니다.
def run_production():
    holiday_set = load_holidays()
    if not Path(PATH_INTENSITY).exists(): return
    try: wb = openpyxl.load_workbook(PATH_INTENSITY, read_only=True, data_only=True)
    except Exception: return
    for plant in wb.sheetnames:
        print(f"\n[Plant: {plant}] Process Start")
        df_raw = load_energy_sheet(PATH_INTENSITY, plant); df_raw["날짜"] = pd.to_datetime(df_raw["날짜"])
        stn_name = STATION_MAP.get(plant); df_weather = get_weather_data(stn_name, TRAIN_START, TEST_END)
        if df_weather.empty: continue
        df = df_raw.merge(df_weather, on="날짜", how="left"); df = df[df["날짜"].apply(lambda x: is_workday(x, holiday_set))]
        if "전력량[kWh]" in df.columns and "냉동전력량[kWh]" in df.columns and "공압기[kWh]" in df.columns:
            df["기타전력량[kWh]"] = (df["전력량[kWh]"] - df["냉동전력량[kWh]"] - df["공압기[kWh]"]).clip(lower=0)
        for tgt_name, ycol_raw in TARGET_MAPS.items():
            if ycol_raw not in df.columns: continue
            if tgt_name == "전력" and "기타전력량[kWh]" in df.columns:
                sub_targets = {"냉동": ("냉동전력량[kWh]", "M4"), "공압": ("공압기[kWh]", "M1"), "기타": ("기타전력량[kWh]", "M1")}
                f_models, fs_dict, p_dict = {}, {}, {}
                for sub_nm, (sub_col, sub_mtype) in sub_targets.items():
                    d_sub = detect_special_events(make_features(df, sub_col)); d_sub = d_sub[~d_sub["is_special_event"]]
                    mask_tr, mask_va, mask_tot = (d_sub["날짜"] <= TRAIN_END), (d_sub["날짜"] >= VALIDATION_START) & (d_sub["날짜"] <= VALIDATION_END), (d_sub["날짜"] <= VALIDATION_END)
                    fs = MODEL_FEATURE_SETS[sub_mtype]; fs_dict[sub_nm] = fs
                    X_tr, y_tr = prepare_xy(d_sub[mask_tr], sub_col, fs); X_va, y_va = prepare_xy(d_sub[mask_va], sub_col, fs)
                    mdl = train_lgbm(X_tr, y_tr, X_va, y_va); p_dict[sub_nm] = np.expm1(mdl.predict(X_va))
                    fm = LGBMRegressor(n_estimators=mdl.best_iteration_, learning_rate=0.03, num_leaves=63, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
                    fm.fit(prepare_xy(d_sub[mask_tot], sub_col, fs)[0], np.log1p(prepare_xy(d_sub[mask_tot], sub_col, fs)[1])); f_models[sub_nm] = fm
                pred_tot = p_dict["냉동"] + p_dict["공압"] + p_dict["기타"]
                d_full_tot = make_features(df, "전력량[kWh]"); mask_va_tot = (d_full_tot["날짜"] >= VALIDATION_START) & (d_full_tot["날짜"] <= VALIDATION_END)
                y_val = df[mask_va_tot]["전력량[kWh]"].values[:len(pred_tot)]
                save_obj = {"type": "COMPOSITE", "models": f_models, "features": fs_dict, "val_mape": mape(y_val, pred_tot), "resid_std": np.std(y_val - pred_tot)}
                joblib.dump(save_obj, MODEL_DIR / f"{plant}_{tgt_name}_Model_Composite.pkl")
            else:
                d_full = detect_special_events(make_features(df, ycol_raw)); d_full = d_full[~d_full["is_special_event"]]
                mask_tr, mask_va, mask_tot = (d_full["날짜"] <= TRAIN_END), (d_full["날짜"] >= VALIDATION_START) & (d_full["날짜"] <= VALIDATION_END), (d_full["날짜"] <= VALIDATION_END)
                mtype = "M4" if tgt_name == "연료" else "M1"; fs = MODEL_FEATURE_SETS[mtype]
                X_tr, y_tr = prepare_xy(d_full[mask_tr], ycol_raw, fs); X_va, y_va = prepare_xy(d_full[mask_va], ycol_raw, fs)
                mdl = train_lgbm(X_tr, y_tr, X_va, y_va); v_mape = mape(y_va, np.expm1(mdl.predict(X_va)))
                fm = LGBMRegressor(n_estimators=mdl.best_iteration_, learning_rate=0.03, num_leaves=63, random_state=42, n_jobs=OPERATING_CPU_NUMBER)
                fm.fit(prepare_xy(d_full[mask_tot], ycol_raw, fs)[0], np.log1p(prepare_xy(d_full[mask_tot], ycol_raw, fs)[1]))
                save_obj = {"type": "SINGLE", "model_type": mtype, "features": fs, "model": fm, "val_mape": v_mape, "resid_std": np.std(y_va - np.expm1(mdl.predict(X_va)))}
                joblib.dump(save_obj, MODEL_DIR / f"{plant}_{tgt_name}_Model_{mtype}.pkl")

if __name__ == "__main__": run_production()
