# 이 파일은 통합된 일별 생산실적 데이터셋(DB_생산실적.xlsx 의 'daily' 시트)을
# production_daily 테이블에 반영하고 공장 재분류 잔존 행을 정리하는 동기화 서비스입니다.
#
# 트리거: app.main 의 매 Streamlit rerun (auto_sync_production_once) — mtime 변경 시에만 실제 UPSERT
# Skip 조건: 소스 파일 mtime과 동기화 정책 버전이 모두 동일
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
from app.domain.factories import NAMYANGJU_F10A_CODE, NAMYANGJU_F10B_CODE
from app.services.production_dw_service import DEFAULT_OUTPUT_PATH

logger = logging.getLogger(__name__)

# 동기화 상태 파일 (마지막 mtime/시각/행수 기록)
SYNC_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "predictive model" / "energy usage" / "_production_dw_sync_state.json"

# 마지막 동기화 결과 (UI 배지/디버깅용 모듈 캐시). 매 rerun마다 갱신됨.
_last_sync_result: dict[str, Any] | None = None

# 배치 단위 (executemany 한 번에 보낼 행 수)
_BATCH_SIZE = 5000

# 정책 변경 후에는 파일 mtime이 같아도 한 번 다시 대조한다.
SYNC_POLICY_VERSION = 2
BACKUP_DIR = SYNC_STATE_PATH.parent / "production_sync_backups"

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
    if df.empty:
        raise ValueError("'daily' 시트가 비어 있어 동기화를 중단합니다.")

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
    if df["date"].isna().any() or df.duplicated(["date", "factory", "item_code"]).any():
        raise ValueError("'daily' 시트에 날짜 누락 또는 날짜·공장·품목 중복이 있습니다.")
    # 남양주1은 냉장 MY, 남양주2는 그 외다. RPA가 냉동 조회에 냉장
    # 품목을 함께 담은 경우 통합 과정에서 두 공장에 같은 품목이 생길 수 있다.
    cold_my = df["category1"].eq("냉장") & df["category2"].eq("MY")
    invalid_factory = (
        (df["factory"].eq(NAMYANGJU_F10A_CODE) & ~cold_my)
        | (df["factory"].eq(NAMYANGJU_F10B_CODE) & cold_my)
    )
    if invalid_factory.any():
        raise ValueError(
            f"남양주 공장 분류 오류 {int(invalid_factory.sum()):,}행: "
            "냉장 MY는 남양주1이어야 합니다. 원본 수집 결과를 확인하세요."
        )

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


def _stale_factory_keys(records: list[tuple], snapshot: dict[tuple, tuple]) -> list[tuple]:
    """원본에는 없고 같은 날짜·품목이 다른 공장으로 남은 DB 키만 찾는다.

    원본에 해당 날짜·공장 자체가 없는 과거 이력이나, 모든 공장에서 사라진
    품목은 자동 삭제하지 않는다. 원본에 존재하는 키는 항상 보존한다.
    """
    source_keys = {(r[0], r[3], r[1]) for r in records}
    source_items = {(r[0], r[1]) for r in records}
    source_groups = {(r[0], r[3]) for r in records}
    return sorted(
        key for key in snapshot
        if key not in source_keys
        and (key[0], key[2]) in source_items
        and (key[0], key[1]) in source_groups
    )


def _backup_stale_rows(rows: list[dict], source_path: Path) -> Path:
    """삭제 전 전체 행(id·생성/수정 시각 포함)을 복구 가능한 JSON으로 보존한다."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    backup = BACKUP_DIR / f"production_daily_{now:%Y%m%d_%H%M%S_%f}.json"
    with backup.open("x", encoding="utf-8") as handle:
        json.dump(
            {"table": "production_daily", "created_at": now.isoformat(),
             "source": str(source_path), "rows": rows},
            handle, ensure_ascii=False, indent=2, default=str,
        )
        handle.flush()
        os.fsync(handle.fileno())
    return backup


def _apply_sync_changes(
    records: list[tuple],
    stale_keys: list[tuple],
    snapshot: dict[tuple, tuple],
    source_path: Path,
) -> tuple[int, int, int, str | None]:
    """백업·잔존 행 삭제·UPSERT를 수행한다. DB 변경은 하나의 트랜잭션이다."""
    if not records and not stale_keys:
        return 0, 0, 0, None

    # admin 자격증명 — viewer 세션에서도 startup sync가 동작하도록.
    conn = get_connection(admin=True)
    affected_total = 0
    batches = 0
    deleted = 0
    backup_path = None
    try:
        conn.start_transaction()
        cur = conn.cursor(dictionary=True)
        try:
            stale_rows: list[dict] = []
            for i in range(0, len(stale_keys), _BATCH_SIZE):
                batch = stale_keys[i:i + _BATCH_SIZE]
                placeholders = ",".join(["(%s,%s,%s)"] * len(batch))
                cur.execute(
                    "SELECT * FROM production_daily "
                    f"WHERE (date, factory, item_code) IN ({placeholders}) FOR UPDATE",
                    tuple(value for key in batch for value in key),
                )
                stale_rows.extend(cur.fetchall())
            for row in stale_rows:
                key = (row["date"], str(row["factory"]), str(row["item_code"]))
                current = _comparable(
                    row["item_name"], row["category1"], row["category2"],
                    row["planned_qty"], row["actual_qty"],
                )
                if snapshot.get(key) != current:
                    raise RuntimeError("대조 후 DB가 변경되어 삭제를 중단합니다. 다시 동기화하세요.")
            if stale_rows:
                backup_path = str(_backup_stale_rows(stale_rows, source_path))
                delete_records = [
                    (row["date"], row["factory"], row["item_code"]) for row in stale_rows
                ]
                for i in range(0, len(delete_records), _BATCH_SIZE):
                    cur.executemany(
                        "DELETE FROM production_daily WHERE date=%s AND factory=%s AND item_code=%s",
                        delete_records[i:i + _BATCH_SIZE],
                    )
                    deleted += cur.rowcount
            for i in range(0, len(records), _BATCH_SIZE):
                batch = records[i:i + _BATCH_SIZE]
                cur.executemany(_INSERT_SQL, batch)
                # rowcount: INSERT는 +1, UPDATE는 +2 (mysql executemany 합산)
                affected_total += cur.rowcount
                batches += 1
            conn.commit()
        finally:
            cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return affected_total, batches, deleted, backup_path


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

    if not force and last_mtime == mtime and state.get("sync_policy_version") == SYNC_POLICY_VERSION:
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
        if src.stat().st_mtime != mtime:
            raise RuntimeError("읽는 중 원본 파일이 변경되어 동기화를 중단합니다.")
        snapshot = _existing_snapshot()
        targets = _changed_records(records, snapshot)
        stale_keys = _stale_factory_keys(records, snapshot)
        incremental = True
        affected, batches, deleted, backup_path = _apply_sync_changes(targets, stale_keys, snapshot, src)
        dt = (datetime.now() - t0).total_seconds()

        new_state = {
            "sync_policy_version": SYNC_POLICY_VERSION,
            "last_mtime": mtime,
            "last_sync_at": t0.strftime("%Y-%m-%d %H:%M:%S"),
            "last_rows": len(records),
            "last_changed": len(targets),
            "last_affected": affected,
            "last_deleted": deleted,
            "last_delete_backup": backup_path,
            "last_batches": batches,
            "last_duration_sec": round(dt, 2),
            "last_incremental": incremental,
            "src": str(src),
        }
        _write_state(new_state)

        scope = f"변경 {len(targets):,}/{len(records):,}행" if incremental else f"전량 {len(records):,}행"
        result.update(
            status="synced",
            message=f"동기화 완료 — {scope} / 잔존 {deleted:,}행 삭제 / {batches} 배치 / {dt:.1f}s",
            rows=len(records),
            changed=len(targets),
            affected=affected,
            deleted=deleted,
            delete_backup=backup_path,
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
