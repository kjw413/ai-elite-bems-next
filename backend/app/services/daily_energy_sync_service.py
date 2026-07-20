# 이 파일은 일일 에너지 데이터(생산량/전력/연료/용수/폐수)를 사전 정의된
# 학습 소스 엑셀에서 자동으로 가져와 energy_daily 테이블에 UPSERT하는 서비스입니다.
#
# 동기화 대상: v5_common.PATH_ENERGY_SOURCE  (예: E:\DB_MIS\RawDB_에너지.xlsx)
# 트리거 시점: app.main 의 매 Streamlit rerun (auto_sync_once).
#   - 무거운 동기화는 RawDB 파일 mtime이 변경됐을 때만 실행됨 (sync_daily_energy_from_source 내부 mtime 비교).
#   - mtime 동일 시에는 stat + JSON 1회만 읽고 즉시 return (수 ms).
#
# 기존 upload_service.upload_excel 와 비교한 차이점:
#   - 입력 컬럼이 한글 → 영문으로 역매핑한 뒤 동일 검증 로직 사용
#   - '전사' 등 비-공장 시트는 자동 skip
#   - DB write는 항상 admin 자격증명 (viewer 세션에서도 startup sync 가능)
#   - change_type='AUTO_SYNC', upload_batch.status='auto_sync'
#   - 파일 mtime을 기록해 변경 없으면 skip
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.database.db_connection import managed_connection, managed_cursor
from app.services.v5_common import (
    PATH_ENERGY_SOURCE,
    PROJECT_ROOT,
)
from app.services.validation_service import ValidationError, validate_data
from app.utils.excel_parser import (
    EXPECTED_COLUMNS,
    NUMERIC_COLUMNS,
    SHEET_TO_FACTORY_MAP,
)

logger = logging.getLogger(__name__)


def _date_key(value) -> str:
    return pd.to_datetime(value).strftime("%Y-%m-%d")

# DB INSERT 컬럼 (upload_service와 동일 순서). 폐수 원단위는 폐기됨.
_INSERT_COLUMNS = [
    "factory", "date",
    "freezing_power_kwh", "air_compressor_kwh", "total_power_kwh",
    "fuel_nm3", "water_ton", "wastewater_ton", "mix_prod_kg",
    "power_per_ton_kwh", "fuel_per_ton_nm3",
    "water_per_ton_ton",
]

# 동기화 상태 파일 (마지막 mtime/시각 기록)
SYNC_STATE_PATH = PROJECT_ROOT / "app" / "predictive model" / "energy usage" / "_daily_energy_sync_state.json"

# 마지막 동기화 결과 (UI 배지/디버깅용 모듈 캐시).
# 매 rerun마다 갱신되며, 호출자가 inserted/updated 를 보고 캐시 무효화 결정에 사용.
_last_sync_result: dict[str, Any] | None = None


# 동기화 상태 JSON을 읽습니다.
def _read_state() -> dict[str, Any]:
    if not SYNC_STATE_PATH.exists():
        return {}
    try:
        return json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# 동기화 상태 JSON을 안전하게 저장합니다.
def _write_state(state: dict[str, Any]) -> None:
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SYNC_STATE_PATH.with_suffix(SYNC_STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SYNC_STATE_PATH)


# 한글 컬럼을 가진 학습 소스 엑셀을 읽어 공장별 DataFrame dict로 변환합니다.
def _parse_korean_excel(path: Path) -> dict[str, pd.DataFrame]:
    """학습 소스 엑셀(한글 컬럼)을 영문 컬럼 형식으로 변환하여 반환.

    - 시트명이 :data:`SHEET_TO_FACTORY_MAP`에 매칭되는 시트만 사용
    - '전사' 등 비-공장 시트는 자동 무시
    - MIS 형식(행=항목, 열=날짜)의 Transposed 데이터도 자동 감지·변환
    - 컬럼명은 부분매칭(substring)으로 영문화 (단위 접미사 [kWh] 등 흡수)
    - 수치 컬럼의 콤마(,) 천 단위 구분자 자동 제거
    """
    # 부분매칭용 한글→영문 매핑 (excel_parser.py와 동일)
    _KOR_SUBSTR_MAP: dict[str, str] = {
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

    all_sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    out: dict[str, pd.DataFrame] = {}
    for sheet_name, df in all_sheets.items():
        sheet_upper = str(sheet_name).strip().upper()
        factory_code = SHEET_TO_FACTORY_MAP.get(sheet_upper)
        if not factory_code:
            continue  # '전사', 빈 시트 등은 skip

        df = df.copy()

        # ── 1. Transposed 데이터 감지 (행=항목, 열=날짜) ──
        if not df.empty and len(df.columns) > 0:
            first_col_values = [str(v).replace(" ", "") for v in df.iloc[:, 0].dropna().values]
            is_transposed = any("냉동전력량" in v or "전력량" in v for v in first_col_values)

            if is_transposed:
                metric_keys = [k for k in _KOR_SUBSTR_MAP if k not in ("날짜", "일자")]
                metric_mask = df.iloc[:, 0].apply(
                    lambda v: any(k in str(v).strip().lower().replace(" ", "") for k in metric_keys)
                )
                df = df.loc[metric_mask].copy()
                df = df.set_index(df.columns[0])
                df = df.T
                df = df.reset_index()
                columns = list(df.columns)
                columns[0] = "date"
                df.columns = columns

        # ── 2. 컬럼명 부분매칭 한글→영문 변환 ──
        new_cols: list[str] = []
        for c in df.columns:
            c_str = str(c).strip().lower().replace(" ", "")
            matched = False
            for k, v in _KOR_SUBSTR_MAP.items():
                if k in c_str:
                    new_cols.append(v)
                    matched = True
                    break
            if not matched:
                new_cols.append(str(c).strip().lower().replace(" ", "_"))
        df.columns = new_cols

        # ── 3. 수치 컬럼 콤마 제거 및 float 변환 ──
        for col in NUMERIC_COLUMNS:
            if col in df.columns:
                df[col] = (
                    df[col]
                    .astype(str)
                    .str.replace(",", "", regex=False)
                    .str.strip()
                )
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

        # ── 4. date 컬럼 정규화 ──
        # RPA가 Excel에 텍스트(`'26-05-14`)로 쓰면 pd.to_datetime이 NaT로 처리해
        # 신규 행이 silently 누락된다 (실제 발생 사례). 아포스트로피 제거 + YY-MM-DD
        # 포맷을 명시적으로 시도한 뒤, 실패 시 일반 파싱으로 폴백한다.
        if "date" in df.columns:
            df["date"] = df["date"].apply(_coerce_date)
            df = df.dropna(subset=["date"]).reset_index(drop=True)

        df["factory"] = factory_code
        out[factory_code] = df
    return out


# 다양한 형식의 셀 값을 datetime.date로 정규화한다.
# Excel/openpyxl이 datetime으로 인식한 셀은 그대로, 텍스트(`'YY-MM-DD`)로 인식한 셀은
# 아포스트로피를 떼고 명시 포맷으로 파싱한다.
def _coerce_date(v: Any) -> Any:
    if v is None:
        return pd.NaT
    if isinstance(v, (datetime, pd.Timestamp)):
        return pd.Timestamp(v).normalize()
    s = str(v).strip().lstrip("'").strip()
    if not s:
        return pd.NaT
    for fmt in ("%Y-%m-%d", "%y-%m-%d", "%Y/%m/%d", "%y/%m/%d"):
        try:
            ts = pd.to_datetime(s, format=fmt)
            if pd.notna(ts):
                return ts.normalize()
        except (ValueError, TypeError):
            continue
    return pd.to_datetime(s, errors="coerce")


# 검증 결과를 적용해 정제된 데이터프레임만 반환합니다.
def _validate(parsed: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[ValidationError]]:
    cleaned: dict[str, pd.DataFrame] = {}
    errors: list[ValidationError] = []
    for factory, df in parsed.items():
        # 필수 컬럼 누락 체크 (영문 매핑 후 검사)
        missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
        if missing:
            errors.append(
                ValidationError(
                    sheet=factory, row=None, column=None,
                    reason=f"필수 컬럼 누락: {', '.join(missing)}",
                )
            )
            continue
        cdf, errs = validate_data(factory, df)
        errors.extend(errs)
        if not errs:
            cleaned[factory] = cdf
    return cleaned, errors


# energy_daily에 UPSERT하고 변경분을 audit/upload_batch에 기록합니다.
def _upsert_and_audit(cleaned: dict[str, pd.DataFrame], filename: str) -> dict[str, Any]:
    user = "auto_sync"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    updated = 0
    audit_records: list[dict[str, Any]] = []

    # admin 자격증명 강제 — viewer 세션에서도 startup sync가 가능하도록.
    with managed_connection(admin=True) as conn:
        existing_rows: dict[tuple[str, str], dict] = {}
        select_cursor = conn.cursor(dictionary=True)
        try:
            for factory, df in cleaned.items():
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

        placeholders = ", ".join(["%s"] * (len(_INSERT_COLUMNS) + 3))
        col_names = ", ".join(_INSERT_COLUMNS + ["created_at", "updated_at", "changed_by"])
        insert_sql = f"INSERT INTO energy_daily ({col_names}) VALUES ({placeholders})"

        update_values: list[tuple] = []
        insert_values: dict[tuple[str, str], tuple] = {}

        for factory, df in cleaned.items():
            for _, row in df.iterrows():
                date_val = row["date"]
                key = (factory, _date_key(date_val))
                numeric_values: list[float] = []
                for col in NUMERIC_COLUMNS:
                    try:
                        numeric_values.append(float(row.get(col, 0)))
                    except (ValueError, TypeError):
                        numeric_values.append(0.0)

                existing = existing_rows.get(key)
                if existing:
                    has_change = False
                    for col, new_val in zip(NUMERIC_COLUMNS, numeric_values):
                        old_val = existing.get(col, 0)
                        if abs(float(old_val or 0) - new_val) > 1e-9:
                            has_change = True
                            audit_records.append({
                                "factory": factory,
                                "date": date_val,
                                "column_name": col,
                                "old_value": old_val,
                                "new_value": new_val,
                                "change_type": "AUTO_SYNC",
                            })

                    if has_change:
                        update_values.append(tuple(numeric_values + [now, user, factory, date_val]))
                else:
                    insert_values[key] = tuple([factory, date_val] + numeric_values + [now, now, user])

        write_cursor = conn.cursor()
        try:
            if update_values:
                write_cursor.executemany(update_sql, update_values)
                updated = len(update_values)
            if insert_values:
                write_cursor.executemany(insert_sql, list(insert_values.values()))
                inserted = len(insert_values)
        finally:
            write_cursor.close()

        conn.commit()

    # 감사 이력 기록 (변경분만)
    if audit_records:
        try:
            # admin 자격증명으로 직접 기록 (audit_service.record_audit_batch는 세션 자격증명 사용)
            with managed_cursor(admin=True) as (conn, cur):
                cur.executemany(
                    """
                    INSERT INTO energy_daily_audit
                        (factory, date, column_name, old_value, new_value, change_type, changed_at, changed_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            r["factory"], r["date"], r["column_name"],
                            str(r.get("old_value")) if r.get("old_value") is not None else None,
                            str(r.get("new_value")) if r.get("new_value") is not None else None,
                            r.get("change_type", "AUTO_SYNC"),
                            now, user,
                        )
                        for r in audit_records
                    ],
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[daily_energy_sync] audit 기록 실패(계속 진행): {exc}")

    # upload_batch에 자동 동기화 흔적 남기기 (성공 시에만)
    try:
        with managed_cursor(admin=True) as (conn, cur):
            cur.execute(
                """
                INSERT INTO upload_batch (filename, uploaded_at, uploaded_by, record_count, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (filename, now, user, inserted + updated, "auto_sync", None),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"[daily_energy_sync] upload_batch 기록 실패(계속 진행): {exc}")

    return {"inserted": inserted, "updated": updated}


# 학습 소스 파일을 읽어 energy_daily에 UPSERT 합니다.
def sync_daily_energy_from_source(
    source_path: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """일일 에너지 데이터를 학습 소스에서 동기화합니다.

    Parameters
    ----------
    source_path:
        엑셀 파일 경로. 미지정 시 :data:`PATH_ENERGY_SOURCE` 사용.
    force:
        True면 mtime이 동일해도 재처리합니다.

    Returns
    -------
    dict with keys:
        success, source_path, file_mtime, file_unchanged,
        inserted, updated, errors, message
    """
    src = Path(source_path) if source_path else PATH_ENERGY_SOURCE

    base = {
        "source_path": str(src),
        "file_mtime": None,
        "file_unchanged": False,
        "inserted": 0,
        "updated": 0,
        "errors": [],
    }

    if not src.exists():
        msg = f"원본 파일을 찾을 수 없습니다: {src}"
        logger.warning(f"[daily_energy_sync] {msg}")
        return {**base, "success": False, "errors": [msg], "message": msg}

    file_mtime = datetime.fromtimestamp(src.stat().st_mtime).isoformat()
    base["file_mtime"] = file_mtime

    state = _read_state()
    if not force and state.get("last_mtime") == file_mtime:
        msg = "원본 파일 변경 없음 — 동기화 건너뜀"
        logger.info(f"[daily_energy_sync] {msg} ({src})")
        return {**base, "success": True, "file_unchanged": True, "message": msg}

    try:
        parsed = _parse_korean_excel(src)
    except PermissionError as exc:
        msg = f"파일이 다른 프로그램(예: Excel)에 의해 잠겨 있습니다: {exc}"
        logger.error(f"[daily_energy_sync] {msg}")
        return {**base, "success": False, "errors": [msg], "message": msg}
    except Exception as exc:
        msg = f"엑셀 읽기 실패: {exc}"
        logger.error(f"[daily_energy_sync] {msg}")
        return {**base, "success": False, "errors": [msg], "message": msg}

    if not parsed:
        msg = "유효한 공장 시트가 없습니다 (남양주1/남양주2/김해/광주/논산/경산 시트 필요)"
        logger.warning(f"[daily_energy_sync] {msg}")
        return {**base, "success": False, "errors": [msg], "message": msg}

    cleaned, val_errors = _validate(parsed)
    if not cleaned:
        msg = f"검증 실패로 적재할 데이터가 없습니다 ({len(val_errors)}건 오류)"
        logger.warning(f"[daily_energy_sync] {msg}")
        return {
            **base, "success": False,
            "errors": [str(e) for e in val_errors[:10]],
            "message": msg,
        }

    try:
        upsert_result = _upsert_and_audit(cleaned, src.name)
    except Exception as exc:
        msg = f"DB 적재 실패: {exc}"
        logger.error(f"[daily_energy_sync] {msg}")
        return {**base, "success": False, "errors": [msg], "message": msg}

    inserted = upsert_result["inserted"]
    updated = upsert_result["updated"]

    # 다음 호출 시 mtime 비교에 사용할 상태 저장
    _write_state({
        "last_mtime": file_mtime,
        "last_sync_at": datetime.now().isoformat(),
        "last_inserted": inserted,
        "last_updated": updated,
        "last_factories": sorted(cleaned.keys()),
        "last_validation_errors": [str(e) for e in val_errors[:10]],
    })

    msg = f"자동 동기화 완료 — 신규 {inserted}건 / 갱신 {updated}건 (시트 {len(cleaned)}개)"
    if val_errors:
        msg += f" / 검증 경고 {len(val_errors)}건"
    logger.info(f"[daily_energy_sync] {msg}")

    return {
        **base,
        "success": True,
        "inserted": inserted,
        "updated": updated,
        "errors": [str(e) for e in val_errors[:10]],
        "message": msg,
    }


# 매 호출 시 RawDB 파일 mtime 비교로 변경 여부를 판단해 동기화합니다.
# 변경 없을 때(가장 흔한 경우)는 sync_daily_energy_from_source 내부에서 즉시 return하므로
# 매 Streamlit rerun에서 호출되어도 부하가 무시할 수준(stat + JSON 1회)입니다.
def auto_sync_once() -> dict[str, Any] | None:
    """app.main에서 매 rerun마다 호출. mtime 변경 시에만 실제 동기화 수행."""
    global _last_sync_result

    try:
        _last_sync_result = sync_daily_energy_from_source()
    except Exception as exc:
        logger.error(f"[daily_energy_sync] auto_sync_once 예외: {exc}")
        _last_sync_result = {
            "success": False,
            "source_path": str(PATH_ENERGY_SOURCE),
            "file_mtime": None,
            "file_unchanged": False,
            "inserted": 0,
            "updated": 0,
            "errors": [str(exc)],
            "message": f"자동 동기화 예외: {exc}",
        }

    return _last_sync_result


# UI에 표시할 현재 동기화 상태를 반환합니다.
def get_daily_energy_sync_status() -> dict[str, Any]:
    """데이터 업로드 페이지의 자동 동기화 패널에서 사용합니다."""
    state = _read_state()
    src = PATH_ENERGY_SOURCE
    file_exists = src.exists()
    file_mtime = (
        datetime.fromtimestamp(src.stat().st_mtime).isoformat() if file_exists else None
    )

    return {
        "source_path": str(src),
        "file_exists": file_exists,
        "file_mtime": file_mtime,
        "last_sync_at": state.get("last_sync_at"),
        "last_mtime": state.get("last_mtime"),
        "last_inserted": int(state.get("last_inserted", 0) or 0),
        "last_updated": int(state.get("last_updated", 0) or 0),
        "last_factories": list(state.get("last_factories", []) or []),
        "is_up_to_date": bool(file_exists and state.get("last_mtime") == file_mtime),
    }


# 강제 재동기화 (UI 버튼용). 모듈 캐시 무시.
def force_resync() -> dict[str, Any]:
    """UI에서 '지금 동기화' 버튼이 눌렸을 때 호출."""
    global _last_sync_result
    result = sync_daily_energy_from_source(force=True)
    _last_sync_result = result
    return result
