from __future__ import annotations

import ctypes
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.v5_common import TRAINED_MODEL_DIR

LOCK_PATH = TRAINED_MODEL_DIR / "v5_training.lock"
DEFAULT_MAX_STALE_HOURS = 24
STARTING_MAX_STALE_MINUTES = 5
HEARTBEAT_INTERVAL_SECONDS = 60


def _now_iso() -> str:
    return datetime.now().isoformat()


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _parse_legacy_lock(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    if "pid" in data and "trigger_pid" not in data:
        data["trigger_pid"] = data["pid"]
    return data


def read_training_lock() -> dict[str, Any]:
    if not LOCK_PATH.exists():
        return {}
    try:
        text = LOCK_PATH.read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return _parse_legacy_lock(text)


def _pid_exists(pid: object) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False

    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information,
            False,
            pid_int,
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False

    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _lock_is_reclaimable(lock_data: dict[str, Any], *, max_stale_hours: int) -> bool:
    state = str(lock_data.get("state") or "").strip().lower()
    worker_pid = lock_data.get("worker_pid")
    heartbeat_at = _parse_iso(lock_data.get("heartbeat_at"))
    created_at = _parse_iso(lock_data.get("created_at") or lock_data.get("started_at"))
    now = datetime.now()

    if worker_pid and not _pid_exists(worker_pid):
        return True

    if heartbeat_at and now - heartbeat_at > timedelta(hours=max_stale_hours):
        return True

    if state in {"starting", ""} and not worker_pid:
        basis = created_at
        if basis is None and LOCK_PATH.exists():
            try:
                basis = datetime.fromtimestamp(LOCK_PATH.stat().st_mtime)
            except Exception:
                basis = None
        if basis and now - basis > timedelta(minutes=STARTING_MAX_STALE_MINUTES):
            return True

    if not lock_data and LOCK_PATH.exists():
        try:
            mtime = datetime.fromtimestamp(LOCK_PATH.stat().st_mtime)
            return now - mtime > timedelta(hours=max_stale_hours)
        except Exception:
            return False

    return False


# 현재 진행 중으로 보이는 재학습이 사실은 죽었는지(전원 차단/크래시) 판단합니다.
#
# UI/상태 조회에서 "running" 인데 워커가 살아있지 않은 경우를 감지하기 위한 공개 헬퍼.
# 잠금 heartbeat 스레드는 60초 간격으로 갱신되므로, heartbeat 정체 임계값은 분 단위로 잡습니다.
# (학습 단계 하나가 수 분 걸려도 heartbeat 스레드는 계속 도므로 오탐이 적습니다.)
def training_run_is_stale(*, max_stale_minutes: int = 5) -> bool:
    lock_data = read_training_lock()
    # 잠금이 사라졌으면 워커가 정상/비정상 종료된 것 → 진행 중 아님.
    if not lock_data:
        return True

    worker_pid = lock_data.get("worker_pid")
    # 워커 프로세스가 죽었으면(재부팅 후 PID 소멸 등) 즉시 중단으로 간주.
    if worker_pid and not _pid_exists(worker_pid):
        return True

    # 워커 PID 가 아직 안 잡혔거나(starting) heartbeat 가 오래 정체 → 중단.
    heartbeat_at = _parse_iso(lock_data.get("heartbeat_at"))
    basis = heartbeat_at or _parse_iso(lock_data.get("created_at") or lock_data.get("started_at"))
    if basis and datetime.now() - basis > timedelta(minutes=max_stale_minutes):
        return True

    return False


def acquire_training_lock(
    *,
    trigger_mode: str,
    run_id: str | None = None,
    max_stale_hours: int = DEFAULT_MAX_STALE_HOURS,
) -> bool:
    TRAINED_MODEL_DIR.mkdir(parents=True, exist_ok=True)

    if LOCK_PATH.exists():
        lock_data = read_training_lock()
        if _lock_is_reclaimable(lock_data, max_stale_hours=max_stale_hours):
            try:
                LOCK_PATH.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                return False
        else:
            return False

    lock_data = {
        "schema_version": 1,
        "state": "starting",
        "trigger_pid": os.getpid(),
        "worker_pid": None,
        "run_id": run_id,
        "trigger_mode": trigger_mode,
        "created_at": _now_iso(),
        "heartbeat_at": _now_iso(),
    }

    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, ensure_ascii=False, indent=2)
        return True
    except FileExistsError:
        return False


def _run_id_matches(lock_data: dict[str, Any], run_id: str | None) -> bool:
    """잠금의 run_id 소유권 검사. run_id 미지정이면 항상 통과(하위 호환)."""
    if run_id is None:
        return True
    existing = lock_data.get("run_id")
    if existing in (None, ""):
        return True
    return str(existing) == str(run_id)


def touch_training_lock(
    *,
    worker_pid: int | None = None,
    run_id: str | None = None,
    state: str = "running",
    trigger_mode: str | None = None,
    started_at: str | None = None,
    extra: dict[str, Any] | None = None,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    lock_data = read_training_lock()
    if not lock_data and not create_if_missing:
        return {}

    if not lock_data:
        lock_data = {
            "schema_version": 1,
            "created_at": started_at or _now_iso(),
            "trigger_pid": None,
        }
    else:
        # 소유권 검사: worker_pid 또는 run_id 가 다른 잠금은 건드리지 않는다
        # (이전 워커가 새 run 의 잠금을 덮어쓰는 경쟁 조건 방지).
        if not _run_id_matches(lock_data, run_id):
            return lock_data
        if worker_pid is not None:
            existing_worker_pid = lock_data.get("worker_pid")
            if existing_worker_pid not in (None, ""):
                try:
                    if int(existing_worker_pid) != int(worker_pid):
                        return lock_data
                except (TypeError, ValueError):
                    return lock_data

    lock_data["schema_version"] = 1
    lock_data["state"] = state
    lock_data["heartbeat_at"] = _now_iso()
    if worker_pid is not None:
        lock_data["worker_pid"] = int(worker_pid)
    if run_id is not None:
        lock_data["run_id"] = run_id
    if trigger_mode is not None:
        lock_data["trigger_mode"] = trigger_mode
    if started_at is not None:
        lock_data["started_at"] = started_at
    if extra:
        lock_data.update(extra)

    _write_json_atomic(LOCK_PATH, lock_data)
    return lock_data


def mark_worker_started(
    worker_pid: int,
    *,
    run_id: str | None = None,
    trigger_mode: str | None = None,
) -> dict[str, Any]:
    return touch_training_lock(
        worker_pid=worker_pid,
        run_id=run_id,
        state="running",
        trigger_mode=trigger_mode,
        create_if_missing=False,
    )


def release_training_lock(
    *,
    owner_pid: int | None = None,
    owner_run_id: str | None = None,
) -> bool:
    lock_data = read_training_lock()
    if lock_data:
        # run_id 소유권 검사 — 다른 run 의 잠금은 해제하지 않는다.
        if not _run_id_matches(lock_data, owner_run_id):
            return False
        if owner_pid is not None:
            worker_pid = lock_data.get("worker_pid")
            if worker_pid is not None:
                try:
                    if int(worker_pid) != int(owner_pid):
                        return False
                except (TypeError, ValueError):
                    return False

    try:
        LOCK_PATH.unlink(missing_ok=True)  # type: ignore[call-arg]
        return True
    except Exception:
        return False


def start_training_heartbeat(
    *,
    worker_pid: int,
    trigger_mode: str,
    started_at: str,
    run_id: str | None = None,
    interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
) -> threading.Event:
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                touch_training_lock(
                    worker_pid=worker_pid,
                    run_id=run_id,
                    state="running",
                    trigger_mode=trigger_mode,
                    started_at=started_at,
                    create_if_missing=True,
                )
            except Exception:
                pass

    touch_training_lock(
        worker_pid=worker_pid,
        run_id=run_id,
        state="running",
        trigger_mode=trigger_mode,
        started_at=started_at,
        create_if_missing=True,
    )
    thread = threading.Thread(target=_loop, name="v5-training-heartbeat", daemon=True)
    thread.start()
    return stop_event
