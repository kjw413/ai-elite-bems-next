"""
Validation Service
==================
업로드 데이터 검증 로직.
"""
# 이 파일은 업로드 데이터의 형식과 값을 확인합니다.

import pandas as pd
from typing import Optional
from app.utils.excel_parser import EXPECTED_COLUMNS, NUMERIC_COLUMNS, get_display_name


class ValidationError:
    """검증 오류 정보 클래스."""

    # 객체를 만들 때 시작값을 저장합니다.
    def __init__(self, sheet: str, row: Optional[int], column: Optional[str], reason: str, value=None):
        self.sheet = sheet
        self.row = row
        self.column = column
        self.reason = reason
        self.value = value

    # 값을 사전 형태로 바꿉니다.
    def to_dict(self) -> dict:
        return {
            "시트": self.sheet,
            "행": self.row,
            "컬럼": get_display_name(self.column) if self.column else "",
            "사유": self.reason,
            "값": str(self.value) if self.value is not None else "",
        }

    # 객체 내용을 읽기 쉬운 문장으로 바꿉니다.
    def __str__(self) -> str:
        parts = [f"시트: {self.sheet}"]
        if self.row is not None:
            parts.append(f"행: {self.row}")
        if self.column:
            parts.append(f"컬럼: {get_display_name(self.column)}")
        parts.append(f"사유: {self.reason}")
        if self.value is not None:
            parts.append(f"값: {self.value}")
        return " | ".join(parts)


# 파일 확장자 상태를 확인합니다.
def validate_file_extension(filename: str) -> list[ValidationError]:
    """파일 확장자 검증."""
    errors = []
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("xlsx", "xls"):
        errors.append(ValidationError(
            sheet="", row=None, column=None,
            reason=f"허용되지 않는 파일 형식입니다. (.xlsx, .xls만 허용)",
            value=f".{ext}"
        ))
    return errors


# 시트 상태를 확인합니다.
def validate_sheets(parsed_data: dict[str, pd.DataFrame]) -> list[ValidationError]:
    """시트 존재 여부 검증."""
    errors = []
    if not parsed_data:
        errors.append(ValidationError(
            sheet="", row=None, column=None,
            reason="공장 코드에 해당하는 시트가 없습니다. (남양주1, 남양주2, 김해, 광주, 논산, 경산)"
        ))
    return errors


# 컬럼 상태를 확인합니다.
def validate_columns(factory: str, df: pd.DataFrame) -> list[ValidationError]:
    """컬럼 구조 검증."""
    errors = []
    existing_cols = set(df.columns)

    for col in EXPECTED_COLUMNS:
        if col not in existing_cols:
            errors.append(ValidationError(
                sheet=factory, row=None, column=col,
                reason=f"필수 컬럼이 누락되었습니다: {get_display_name(col)}"
            ))

    return errors


# 데이터 상태를 확인합니다.
def validate_data(factory: str, df: pd.DataFrame) -> tuple[pd.DataFrame, list[ValidationError]]:
    """
    데이터 값 검증 및 정제.
    - 빈 값 → 0 변환
    - 수치 컬럼 타입 확인
    - 날짜 형식 확인

    Returns
    -------
    (정제된 DataFrame, 에러 목록)
    """
    errors = []
    df = df.copy()

    # 날짜 검증
    if "date" in df.columns:
        for idx, val in df["date"].items():
            try:
                pd.to_datetime(val)
            except (ValueError, TypeError):
                errors.append(ValidationError(
                    sheet=factory, row=int(idx) + 2,  # Excel 행번호 (헤더=1행)
                    column="date",
                    reason="날짜 형식이 올바르지 않습니다 (YYYY-MM-DD)",
                    value=val
                ))

        # 날짜 변환
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    # 수치 컬럼 검증
    for col in NUMERIC_COLUMNS:
        if col not in df.columns:
            continue

        # 빈 값(NaN) → 0
        df[col] = df[col].fillna(0)

        # 수치 변환 시도
        for idx, val in df[col].items():
            if pd.isna(val):
                continue
            try:
                float(val)
            except (ValueError, TypeError):
                errors.append(ValidationError(
                    sheet=factory, row=int(idx) + 2,
                    column=col,
                    reason="숫자가 아닌 값입니다",
                    value=val
                ))

        # 실제 변환
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(float)

    return df, errors


# 전체 상태를 확인합니다.
def validate_all(parsed_data: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[ValidationError]]:
    """
    전체 검증 파이프라인.

    Returns
    -------
    (정제된 {공장: DataFrame}, 전체 에러 목록)
    """
    all_errors = []
    cleaned_data = {}

    # 시트 검증
    sheet_errors = validate_sheets(parsed_data)
    if sheet_errors:
        return {}, sheet_errors

    for factory, df in parsed_data.items():
        # 컬럼 검증
        col_errors = validate_columns(factory, df)
        if col_errors:
            all_errors.extend(col_errors)
            continue

        # 데이터 검증 및 정제
        cleaned_df, data_errors = validate_data(factory, df)
        all_errors.extend(data_errors)

        if not data_errors:
            cleaned_data[factory] = cleaned_df

    return cleaned_data, all_errors
