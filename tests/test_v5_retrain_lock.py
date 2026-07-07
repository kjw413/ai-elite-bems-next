from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import v5_retrain_lock as lock


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _write_lock(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    failures: list[str] = []
    original_lock_path = lock.LOCK_PATH

    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "v5_training.lock"
        lock.LOCK_PATH = lock_path

        try:
            _check(
                lock.acquire_training_lock(trigger_mode="manual"),
                "can acquire a new training lock",
                failures,
            )
            data = lock.read_training_lock()
            _check(data.get("state") == "starting", "new lock starts in starting state", failures)
            _check(data.get("trigger_pid") == os.getpid(), "new lock records trigger pid", failures)

            lock.mark_worker_started(os.getpid(), trigger_mode="manual")
            data = lock.read_training_lock()
            _check(data.get("state") == "running", "worker start marks lock running", failures)
            _check(data.get("worker_pid") == os.getpid(), "worker start records worker pid", failures)
            lock.touch_training_lock(worker_pid=os.getpid() + 1, state="running")
            data = lock.read_training_lock()
            _check(
                data.get("worker_pid") == os.getpid(),
                "heartbeat from another pid cannot take over the lock",
                failures,
            )
            _check(
                not lock.acquire_training_lock(trigger_mode="manual"),
                "active worker lock blocks another retrain",
                failures,
            )

            _check(
                not lock.release_training_lock(owner_pid=os.getpid() + 1),
                "wrong owner pid cannot release worker lock",
                failures,
            )
            _check(lock_path.exists(), "lock remains after wrong-owner release", failures)
            _check(
                lock.release_training_lock(owner_pid=os.getpid()),
                "owner pid can release worker lock",
                failures,
            )
            _check(not lock_path.exists(), "lock file is removed after owner release", failures)

            _write_lock(
                lock_path,
                {
                    "schema_version": 1,
                    "state": "running",
                    "worker_pid": 99999999,
                    "created_at": datetime.now().isoformat(),
                    "heartbeat_at": datetime.now().isoformat(),
                },
            )
            _check(
                lock.acquire_training_lock(trigger_mode="manual"),
                "dead worker pid lock is reclaimed",
                failures,
            )
            lock.release_training_lock()

            _write_lock(
                lock_path,
                {
                    "schema_version": 1,
                    "state": "running",
                    "worker_pid": os.getpid(),
                    "created_at": datetime.now().isoformat(),
                    "heartbeat_at": (datetime.now() - timedelta(hours=25)).isoformat(),
                },
            )
            _check(
                lock.acquire_training_lock(trigger_mode="manual", max_stale_hours=24),
                "stale heartbeat lock is reclaimed even if pid is alive",
                failures,
            )
            lock.release_training_lock()

            _write_lock(
                lock_path,
                {
                    "schema_version": 1,
                    "state": "starting",
                    "worker_pid": None,
                    "created_at": (datetime.now() - timedelta(minutes=10)).isoformat(),
                    "heartbeat_at": (datetime.now() - timedelta(minutes=10)).isoformat(),
                },
            )
            _check(
                lock.acquire_training_lock(trigger_mode="manual"),
                "stale starting lock without worker pid is reclaimed",
                failures,
            )
        finally:
            lock.release_training_lock()
            lock.LOCK_PATH = original_lock_path

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
