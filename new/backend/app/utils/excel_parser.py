"""
Excel Parser Module
===================
Excel 파일을 읽어 공장별 DataFrame으로 변환.
"""
# 이 파일은 업로드된 엑셀 파일을 공장별 데이터로 읽어 정리합니다.

import pandas as pd
from pathlib import Path

from app.domain.factories import FACTORY_PHYSICAL_DISPLAY_ORDER, SHEET_TO_FACTORY_MAP

# 공장 코드 목록 (UI/시트명 기준: 남양주1, 남양주2)
FACTORY_CODES = list(FACTORY_PHYSICAL_DISPLAY_ORDER)

# Excel 컬럼 매핑 (순서대로). 폐수 원단위(wastewater_per_ton_ton)는 폐기됨.
EXPECTED_COLUMNS = [
    "date",
    "freezing_power_kwh",
    "air_compressor_kwh",
    "total_power_kwh",
    "fuel_nm3",
    "water_ton",
    "wastewater_ton",
    "mix_prod_kg",
    "power_per_ton_kwh",
    "fuel_per_ton_nm3",
    "water_per_ton_ton",
]

# 수치 컬럼 목록
NUMERIC_COLUMNS = EXPECTED_COLUMNS[1:]  # date 제외

# 컬럼 한글 매핑 (UI 출력용)
COLUMN_DISPLAY_NAMES = {
    "date": "날짜",
    "factory": "공장",
    "freezing_power_kwh": "냉동전력량 (kWh)",
    "air_compressor_kwh": "공압기 (kWh)",
    "total_power_kwh": "전력량 (kWh)",
    "fuel_nm3": "연료량 (Nm³)",
    "water_ton": "용수량 (ton)",
    "wastewater_ton": "폐수량 (ton)",
    "mix_prod_kg": "믹스생산량 (kg)",
    "power_per_ton_kwh": "전력 원단위 (kWh/ton)",
    "fuel_per_ton_nm3": "연료 원단위 (Nm³/ton)",
    "water_per_ton_ton": "용수 원단위 (ton/ton)",
    "created_at": "생성일시",
    "updated_at": "수정일시",
    "changed_by": "변경자",
}


# 엑셀 데이터를 읽어 정리합니다.
def parse_excel(file_path_or_buffer, filename: str = "") -> dict[str, pd.DataFrame]:
    """
    Excel 파일을 파싱하여 {공장코드: DataFrame} 딕셔너리를 반환.

    Parameters
    ----------
    file_path_or_buffer : str, Path, or file-like
        Excel 파일 경로 또는 Streamlit UploadedFile
    filename : str
        파일 이름 (확장자 판별용)

    Returns
    -------
    dict[str, pd.DataFrame]
        공장코드를 키로, 해당 시트의 DataFrame을 값으로 갖는 딕셔너리
    """
    # 확장자 판별
    if isinstance(file_path_or_buffer, (str, Path)):
        filename = str(file_path_or_buffer)

    ext = Path(filename).suffix.lower() if filename else ".xlsx"
    engine = "xlrd" if ext == ".xls" else "openpyxl"

    # 모든 시트 읽기
    try:
        all_sheets = pd.read_excel(
            file_path_or_buffer,
            sheet_name=None,  # 모든 시트
            engine=engine,
        )
    except Exception as e:
        raise ValueError(f"Excel 파일 읽기 실패: {e}")

    result = {}

    for sheet_name, df in all_sheets.items():
        sheet_upper = str(sheet_name).strip().upper()
        factory_code = SHEET_TO_FACTORY_MAP.get(sheet_upper)
        if factory_code:
            df = df.copy()

            # 1. Transposed 데이터 확인 (첫 번째 열에 항목명이 들어있는지 체크)
            if not df.empty and len(df.columns) > 0:
                first_col_values = [str(v).replace(" ", "") for v in df.iloc[:, 0].dropna().values]
                is_transposed = any("냉동전력량" in v or "전력량" in v for v in first_col_values)
                
                if is_transposed:
                    metric_keys = [
                        "냉동전력량", "공압기", "공업기", "공기압축기", "전력량",
                        "연료량", "용수량", "폐수량", "mix생산량", "믹스생산량",
                        "전력원단위", "전력단위", "연료원단위", "연료단위",
                        "용수원단위", "용수단위",
                    ]
                    metric_mask = df.iloc[:, 0].apply(
                        lambda v: any(k in str(v).strip().lower().replace(" ", "") for k in metric_keys)
                    )
                    df = df.loc[metric_mask].copy()
                    # 행열 반전 수행
                    df = df.set_index(df.columns[0])
                    df = df.T
                    df = df.reset_index()
                    
                    # 반전 후 첫 번째 컬럼(기존 헤더의 날짜들) 이름을 'date'로 임시 지정
                    columns = list(df.columns)
                    columns[0] = "date"
                    df.columns = columns

            # 2. 컬럼명 정규화 및 한글 -> 영문 매핑
            kor_to_eng = {
                "날짜": "date",
                "일자": "date",
                "냉동전력량": "freezing_power_kwh",
                "공압기": "air_compressor_kwh",
                "공업기": "air_compressor_kwh",
                "공기압축기": "air_compressor_kwh",
                "전력량": "total_power_kwh",
                "연료량": "fuel_nm3",
                "용수량": "water_ton",
                "폐수량": "wastewater_ton",
                "mix생산량": "mix_prod_kg",
                "믹스생산량": "mix_prod_kg",
                "전력원단위": "power_per_ton_kwh",
                "전력단위": "power_per_ton_kwh",
                "연료원단위": "fuel_per_ton_nm3",
                "연료단위": "fuel_per_ton_nm3",
                "용수원단위": "water_per_ton_ton",
                "용수단위": "water_per_ton_ton",
            }
            
            new_cols = []
            for c in df.columns:
                c_str = str(c).strip().lower().replace(" ", "")
                matched = False
                for k, v in kor_to_eng.items():
                    if k in c_str:
                        new_cols.append(v)
                        matched = True
                        break
                if not matched:
                    # 영문 컬럼이거나 매핑에 없는 경우 기본 소문자/공백제거 적용
                    new_cols.append(str(c).strip().lower().replace(" ", "_"))
            
            df.columns = new_cols
            
            # 3. 수치 컬럼 콤마 제거 및 float 변환
            #    MIS 시스템에서 "29,717" 형태의 문자열이 유입되면
            #    pd.to_numeric이 NaN을 반환하여 데이터가 0으로 유실되는 문제 방지
            for col in NUMERIC_COLUMNS:
                if col in df.columns:
                    df[col] = (
                        df[col]
                        .astype(str)
                        .str.replace(",", "", regex=False)
                        .str.strip()
                    )
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

            # DB 저장용 공장 코드(남양주1/남양주2/김해/광주/논산) 설정
            df["factory"] = factory_code
            result[factory_code] = df

    return result


# 컬럼 to korean 이름을 바꿉니다.
def rename_columns_to_korean(df: pd.DataFrame) -> pd.DataFrame:
    """DataFrame 컬럼명을 한글 표시명으로 변환 (UI 출력용)."""
    return df.rename(columns=COLUMN_DISPLAY_NAMES)


# 표시 이름 값을 가져옵니다.
def get_display_name(column: str) -> str:
    """영문 컬럼명에 대한 한글 표시명 반환."""
    return COLUMN_DISPLAY_NAMES.get(column, column)
