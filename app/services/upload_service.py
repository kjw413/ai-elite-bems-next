"""
Upload Service
==============
Excel 업로드 → 검증 → DB 적재 파이프라인.
UPSERT 정책: 동일 (factory, date)가 존재하면 덮어쓰기 + 감사 기록.
"""
# 이 파일은 엑셀 업로드부터 저장과 재학습 호출까지 이어줍니다.

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.utils.excel_parser import parse_excel, NUMERIC_COLUMNS
from app.services.validation_service import validate_file_extension, validate_all
from app.services.audit_service import record_audit_batch, get_current_user
from app.database.db_connection import managed_connection, managed_cursor


# 업로드 원본 보관 디렉토리
# 업로드 dir 값을 가져옵니다.
def _get_uploads_dir() -> Path:
    base_dir = Path(__file__).resolve().parent.parent.parent
    uploads_dir = base_dir / "data" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return uploads_dir


# DB 적재 컬럼 (date, factory 포함). 폐수 원단위(wastewater_per_ton_ton)는 폐기됨.
INSERT_COLUMNS = [
    "factory", "date",
    "freezing_power_kwh", "air_compressor_kwh", "total_power_kwh",
    "fuel_nm3", "water_ton", "wastewater_ton", "mix_prod_kg",
    "power_per_ton_kwh", "fuel_per_ton_nm3",
    "water_per_ton_ton",
]


def _date_key(value) -> str:
    return pd.to_datetime(value).strftime("%Y-%m-%d")


# 업로드 엑셀 관련 처리를 담당합니다.
def upload_excel(
    file_path_or_buffer,
    filename: str,
    save_original: bool = True,
) -> dict:
    """
    Excel 업로드 전체 파이프라인.

    Returns
    -------
    dict with keys:
        success: bool
        message: str
        errors: list[dict]  (에러 발생 시)
        record_count: int   (성공 시 적재 건수)
    """
    user = get_current_user()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 파일 확장자 검증
    ext_errors = validate_file_extension(filename)
    if ext_errors:
        _record_batch(filename, user, 0, "fail", ext_errors[0].reason)
        return {
            "success": False,
            "message": "파일 형식 오류",
            "errors": [e.to_dict() for e in ext_errors],
            "record_count": 0,
        }

    # 2. Excel 파싱
    try:
        parsed_data = parse_excel(file_path_or_buffer, filename)
    except ValueError as e:
        _record_batch(filename, user, 0, "fail", str(e))
        return {
            "success": False,
            "message": str(e),
            "errors": [],
            "record_count": 0,
        }

    # 3. 데이터 검증
    cleaned_data, validation_errors = validate_all(parsed_data)

    if validation_errors:
        _record_batch(filename, user, 0, "fail", f"{len(validation_errors)}건 검증 오류")
        return {
            "success": False,
            "message": f"데이터 검증 실패: {len(validation_errors)}건의 오류가 발견되었습니다.",
            "errors": [e.to_dict() for e in validation_errors],
            "record_count": 0,
        }

    # 4. DB 적재 (UPSERT)
    total_count = 0
    all_audit_records = []

    try:
        with managed_connection() as conn:
            existing_rows: dict[tuple[str, str], dict] = {}
            select_cursor = conn.cursor(dictionary=True)
            try:
                for factory, df in cleaned_data.items():
                    dates = sorted({_date_key(v) for v in df["date"].tolist()})
                    if not dates:
                        continue
                    placeholders = ", ".join(["%s"] * len(dates))
                    select_cursor.execute(
                        f"SELECT * FROM energy_daily WHERE factory = %s AND date IN ({placeholders})",
                        (factory, *dates),
                    )
                    for existing in select_cursor.fetchall():
                        existing_rows[(str(existing["factory"]), _date_key(existing["date"]))] = dict(existing)
            finally:
                select_cursor.close()

            set_clause = ", ".join([f"{col} = %s" for col in NUMERIC_COLUMNS])
            set_clause += ", updated_at = %s, changed_by = %s"
            update_sql = f"UPDATE energy_daily SET {set_clause} WHERE factory = %s AND date = %s"

            placeholders = ", ".join(["%s"] * (len(INSERT_COLUMNS) + 3))
            col_names = ", ".join(INSERT_COLUMNS + ["created_at", "updated_at", "changed_by"])
            insert_sql = f"INSERT INTO energy_daily ({col_names}) VALUES ({placeholders})"

            update_values: list[tuple] = []
            insert_values: dict[tuple[str, str], tuple] = {}

            for factory, df in cleaned_data.items():
                for _, row in df.iterrows():
                    date_val = row["date"]
                    key = (factory, _date_key(date_val))
                    numeric_values = [float(row.get(col, 0)) for col in NUMERIC_COLUMNS]
                    existing = existing_rows.get(key)

                    if existing:
                        for col, new_val in zip(NUMERIC_COLUMNS, numeric_values):
                            old_val = existing.get(col, 0)
                            if abs(float(old_val or 0) - new_val) > 1e-9:
                                all_audit_records.append({
                                    "factory": factory,
                                    "date": date_val,
                                    "column_name": col,
                                    "old_value": old_val,
                                    "new_value": new_val,
                                    "change_type": "UPLOAD",
                                })
                        update_values.append(tuple(numeric_values + [now, user, factory, date_val]))
                    else:
                        insert_values[key] = tuple([factory, date_val] + numeric_values + [now, now, user])

                    total_count += 1

            write_cursor = conn.cursor()
            try:
                if update_values:
                    write_cursor.executemany(update_sql, update_values)
                if insert_values:
                    write_cursor.executemany(insert_sql, list(insert_values.values()))
            finally:
                write_cursor.close()

            conn.commit()
    except Exception as e:
        _record_batch(filename, user, 0, "fail", str(e))
        return {
            "success": False,
            "message": f"데이터베이스 저장 실패: {e}",
            "errors": [],
            "record_count": 0,
        }

    # 5. 감사 이력 기록 (commit 이후이므로 실패해도 업로드 성공을 실패로 위장하지 않음)
    if all_audit_records:
        try:
            record_audit_batch(all_audit_records)
        except Exception as exc:
            print(f"[upload_service] audit 기록 실패(계속 진행): {exc}")

    # 6. 업로드 배치 기록
    _record_batch(filename, user, total_count, "success", None)

    # 7. 원본 파일 보관
    if save_original and isinstance(file_path_or_buffer, (str, Path)):
        try:
            dest = _get_uploads_dir() / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{filename}"
            shutil.copy2(str(file_path_or_buffer), str(dest))
        except Exception:
            pass

    msg = f"업로드 완료: {total_count}건의 데이터가 저장되었습니다."

    return {
        "success": True,
        "message": msg,
        "errors": [],
        "record_count": total_count,
        "retrain_started": False,
        "retrain_message": None,
    }


# 일괄을 기록합니다.
def _record_batch(filename: str, user: str, count: int, status: str, error_msg: Optional[str]):
    """upload_batch 테이블에 배치 기록."""
    try:
        with managed_cursor() as (conn, cursor):
            cursor.execute(
                """
                INSERT INTO upload_batch (filename, uploaded_at, uploaded_by, record_count, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    filename,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    user,
                    count,
                    status,
                    error_msg,
                ),
            )
            conn.commit()
    except Exception:
        pass


# 업로드 이력 값을 가져옵니다.
def get_upload_history(limit: int = 50) -> list[dict]:
    """업로드 이력 조회."""
    with managed_cursor(dictionary=True) as (_conn, cursor):
        cursor.execute(
            "SELECT * FROM upload_batch ORDER BY uploaded_at DESC LIMIT %s",
            (limit,),
        )
        result = cursor.fetchall()
        return result
