# 이 파일은 통합된 일별 생산실적 데이터셋(DB_생산실적.xlsx 의 'daily' 시트)을
# production_daily 테이블에 UPSERT 하는 동기화 서비스입니다.
#
# 트리거: app.main 의 매 Streamlit rerun (auto_sync_production_once) — mtime 변경 시에만 실제 UPSERT
# Skip 조건: 마지막 동기화 이후 소스 파일 mtime 변화 없음 (변경 없으면 stat+JSON 1회로 즉시 return)
#
# 설계 의도:
#   - daily_energy_sync_service 와 동일한 패턴 (state JSON / 매 rerun mtime 비교 / admin 자격증명)
#   - 0인 일자도 그대로 적재 (사용자 명시: 상관관계/완전성 확보용)
#   - 275k+ 행 → executemany 배치 INSERT...ON DUPLICATE KEY UPDATE
#   - 소스 파일이 없으면 graceful skip (앱 기동은 막지 않음)
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.database.db_connection import get_connection
from app.services.production_dw_service import DEFAULT_OUTPUT_PATH

logger = logging.getLogger(__name__)

# 동기화 상태 파일 (마지막 mtime/시각/행수 기록)
SYNC_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "predictive model" / "energy usage" / "_production_dw_sync_state.json"

# 마지막 동기화 결과 (UI 배지/디버깅용 모듈 캐시). 매 rerun마다 갱신됨.
_last_sync_result: dict[str, Any] | None = None

# 배치 단위 (executemany 한 번에 보낼 행 수)
_BATCH_SIZE = 5000

_INSERT_SQL = """
INSERT INTO production_daily
    (date, item_code, item_name, factory, category1, category2, planned_qty, actual_qty)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    item_name   = VALUES(item_name),
    category1   = VALUES(category1),
    category2   = VALUES(category2),
    planned_qty = VALUES(planned_qty),
    actual_qty  = VALUES(actual_qty),
    updated_at  = CURRENT_TIMESTAMP
"""


# 동기화 상태 JSON을 읽습니다.
def _read_state() -> dict[str, Any]:
    if not SYNC_STATE_PATH.exists():
        return {}
    try:
        return json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# 동기화 상태 JSON을 안전하게 저장합니다 (atomic rename).
def _write_state(state: dict[str, Any]) -> None:
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SYNC_STATE_PATH.with_suffix(SYNC_STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, SYNC_STATE_PATH)


# 'daily' 시트를 DataFrame으로 로드 후 DB 적재 형식으로 변환합니다.
def _load_daily_sheet(src_path: Path) -> pd.DataFrame:
    """consolidated xlsx의 'daily' 시트를 읽어 DB INSERT 가능한 형태로 정리."""
    df = pd.read_excel(src_path, sheet_name="daily", engine="openpyxl")

    # 필수 컬럼 검증
    required = {
        "date", "item_code", "item_name", "factory",
        "category1", "category2", "planned_qty", "actual_qty",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"'daily' 시트에 필수 컬럼 누락: {missing}")

    # 타입 정규화
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["item_code"] = df["item_code"].astype(str)
    df["item_name"] = df["item_name"].fillna("").astype(str)
    df["factory"] = df["factory"].astype(str)
    df["category1"] = df["category1"].astype(str)
    # category2 는 None 허용 (NaN → None)
    df["category2"] = df["category2"].where(df["category2"].notna(), None)
    df["planned_qty"] = pd.to_numeric(df["planned_qty"], errors="coerce").fillna(0.0)
    df["actual_qty"] = pd.to_numeric(df["actual_qty"], errors="coerce").fillna(0.0)

    return df


# DataFrame을 (date, item_code, ...) 튜플 리스트로 변환합니다.
def _df_to_records(df: pd.DataFrame) -> list[tuple]:
    """executemany 입력용 튜플 리스트 생성. NaN/NaT 안전 처리."""
    records: list[tuple] = []
    for r in df.itertuples(index=False):
        c2 = r.category2
        if c2 is None or (isinstance(c2, float) and pd.isna(c2)):
            c2 = None
        records.append((
            r.date,
            str(r.item_code),
            str(r.item_name) if r.item_name is not None else "",
            str(r.factory),
            str(r.category1),
            c2,
            float(r.planned_qty),
            float(r.actual_qty),
        ))
    return records


_SELECT_EXISTING_SQL = """
SELECT date, factory, item_code, item_name, category1, category2, planned_qty, actual_qty
FROM production_daily
"""


def _comparable(name: Any, cat1: Any, cat2: Any, planned: Any, actual: Any) -> tuple:
    """DB 값과 엑셀 값을 같은 형태로 정규화 — 부동소수 표현차로 오탐하지 않도록 반올림."""
    return (
        str(name or ""),
        str(cat1 or ""),
        None if cat2 is None else str(cat2),
        round(float(planned or 0.0), 6),
        round(float(actual or 0.0), 6),
    )


def _existing_snapshot() -> dict[tuple, tuple]:
    """(date, factory, item_code) → 비교 대상 값. 변경분만 UPSERT하기 위한 현재 DB 상태."""
    conn = get_connection(admin=True)
    try:
        cur = conn.cursor()
        try:
            cur.execute(_SELECT_EXISTING_SQL)
            snapshot: dict[tuple, tuple] = {}
            for row_date, factory, item_code, item_name, cat1, cat2, planned, actual in cur:
                snapshot[(row_date, str(factory), str(item_code))] = _comparable(
                    item_name, cat1, cat2, planned, actual,
                )
            return snapshot
        finally:
            cur.close()
    finally:
        conn.close()


def _changed_records(records: list[tuple], snapshot: dict[tuple, tuple]) -> list[tuple]:
    """엑셀 행 중 DB와 실제로 다른 것만 추린다.

    엑셀은 전체 스냅샷이라 매번 30만 행을 전량 UPSERT하면 파일이 조금만 바뀌어도
    2분 이상 걸린다. 그 사이 화면을 새로고침하면 생산실적만 옛 값으로 보여
    '에너지만 갱신된다'로 보이므로, 바뀐 행만 골라 반영 시간을 수 초로 줄인다.
    """
    changed: list[tuple] = []
    for record in records:
        row_date, item_code, item_name, factory, cat1, cat2, planned, actual = record
        key = (row_date, factory, item_code)
        if snapshot.get(key) != _comparable(item_name, cat1, cat2, planned, actual):
            changed.append(record)
    return changed


# 배치 단위로 UPSERT 실행 후 (영향 행수, 배치 수) 반환.
def _bulk_upsert(records: list[tuple]) -> tuple[int, int]:
    if not records:
        return 0, 0

    # admin 자격증명 — viewer 세션에서도 startup sync가 동작하도록.
    conn = get_connection(admin=True)
    affected_total = 0
    batches = 0
    try:
        cur = conn.cursor()
        try:
            for i in range(0, len(records), _BATCH_SIZE):
                batch = records[i:i + _BATCH_SIZE]
                cur.executemany(_INSERT_SQL, batch)
                # rowcount: INSERT는 +1, UPDATE는 +2 (mysql executemany 합산)
                affected_total += cur.rowcount
                batches += 1
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()
    return affected_total, batches


# 외부에서 한 번만 실행되도록 가드되는 상위 함수 (앱 기동시 호출).
def auto_sync_production_once(
    src_path: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """자동 동기화 진입점. app.main 의 매 Streamlit rerun에서 호출된다.

    daily_energy_sync_service.auto_sync_once 와 동일하게, 매 호출 시 소스 파일
    mtime을 state JSON과 비교해 **변경됐을 때만** 실제 UPSERT를 수행한다. 변경이
    없으면 stat + JSON 1회만 읽고 즉시 return(수 ms)하므로, 상시 서버에서 매 rerun
    호출돼도 부하가 무시할 수준이며 서버 재시작 없이 신규 데이터가 반영된다.

    Parameters
    ----------
    src_path : 통합 xlsx 경로 (기본: production_dw_service.DEFAULT_OUTPUT_PATH)
    force    : True면 mtime 변화가 없어도 강제 재동기화

    Returns
    -------
    dict {status, message, ...} — UI/로그용 요약.
        status="synced" 일 때만 실제 DB 변경이 발생한 것이다.
    """
    global _last_sync_result

    src = Path(src_path) if src_path else DEFAULT_OUTPUT_PATH
    result: dict[str, Any] = {"status": "skipped", "src": str(src)}

    if not src.exists():
        result.update(status="missing_source", message=f"통합 파일 없음: {src}")
        logger.debug(f"[production_sync] {result['message']} — skip")
        _last_sync_result = result
        return result

    mtime = src.stat().st_mtime
    state = _read_state()
    last_mtime = state.get("last_mtime")

    if not force and last_mtime == mtime:
        result.update(
            status="unchanged",
            message=f"소스 mtime 동일 — skip (last sync: {state.get('last_sync_at')})",
            mtime=mtime,
            last_rows=state.get("last_rows"),
        )
        # 매 rerun 호출되므로 unchanged 로그는 debug 로 (INFO 스팸 방지)
        logger.debug(f"[production_sync] {result['message']}")
        _last_sync_result = result
        return result

    # 실제 동기화
    try:
        t0 = datetime.now()
        df = _load_daily_sheet(src)
        records = _df_to_records(df)
        # 변경분만 UPSERT — 실패하면 전량 UPSERT로 안전하게 되돌린다(정확성 우선).
        try:
            targets = _changed_records(records, _existing_snapshot())
            incremental = True
        except Exception as diff_error:
            logger.warning(f"[production_sync] 증분 비교 실패 — 전량 UPSERT로 대체: {diff_error}")
            targets, incremental = records, False
        affected, batches = _bulk_upsert(targets)
        dt = (datetime.now() - t0).total_seconds()

        new_state = {
            "last_mtime": mtime,
            "last_sync_at": t0.strftime("%Y-%m-%d %H:%M:%S"),
            "last_rows": len(records),
            "last_changed": len(targets),
            "last_affected": affected,
            "last_batches": batches,
            "last_duration_sec": round(dt, 2),
            "last_incremental": incremental,
            "src": str(src),
        }
        _write_state(new_state)

        scope = f"변경 {len(targets):,}/{len(records):,}행" if incremental else f"전량 {len(records):,}행"
        result.update(
            status="synced",
            message=f"UPSERT 완료 — {scope} / {batches} 배치 / {dt:.1f}s",
            rows=len(records),
            changed=len(targets),
            affected=affected,
            batches=batches,
            duration_sec=round(dt, 2),
            incremental=incremental,
        )
        logger.info(f"[production_sync] {result['message']}")
    except Exception as exc:
        result.update(status="error", message=f"동기화 실패: {exc}")
        logger.error(f"[production_sync] {result['message']}", exc_info=True)

    _last_sync_result = result
    return result


# 마지막 동기화 결과를 외부에서 조회할 때 사용합니다 (UI 배지 등).
def get_last_sync_result() -> dict[str, Any] | None:
    return _last_sync_result


# 상태 JSON을 외부에서 직접 조회.
def get_sync_state() -> dict[str, Any]:
    return _read_state()
