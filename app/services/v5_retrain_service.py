# 이 파일은 백그라운드 재학습 작업을 시작하고 상태를 관리합니다.
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.v5_common import PROJECT_ROOT, STATUS_PATH, TRAINED_MODEL_DIR, read_json
from app.services.v5_retrain_lock import (
    LOCK_PATH,
    acquire_training_lock,
    mark_worker_started,
    release_training_lock,
)

LOG_PATH = TRAINED_MODEL_DIR / "v5_training.log"


# JSON atomic 데이터를 저장합니다.
def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# v5 retrain을 시작합니다.
def trigger_v5_retrain(
    changed_factories: list[str] | None = None,
    trigger_mode: str = "manual",
) -> dict[str, Any]:
    """
    Fire-and-forget retraining process.
    """
    # run_id: 이 재학습 실행을 식별하는 고유값. 잠금 소유권(run_id+worker_pid)과
    # 상태 JSON 추적에 사용해, 이전 워커가 새 실행의 잠금/상태를 덮어쓰는 경쟁 조건을 막는다.
    run_id = uuid.uuid4().hex

    if not acquire_training_lock(trigger_mode=trigger_mode, run_id=run_id):
        return {"started": False, "message": "이미 학습 작업이 실행 중입니다."}

    started_at = datetime.now().isoformat()
    # 직전 학습의 데이터 마지막 날짜를 우선 노출(워커가 새 데이터 수집 후 갱신).
    prev_status = read_json(STATUS_PATH, {})
    prev_data_end = prev_status.get("data_end_date")
    _write_json_atomic(
        STATUS_PATH,
        {
            "status": "running",
            "started_at": started_at,
            "ended_at": None,
            "mode": "running",
            "run_id": run_id,
            "trigger_mode": trigger_mode,
            "message": "재학습 시작",
            "error": None,
            "new_model_path": None,
            "progress_current": 0,
            "progress_total": 0,
            "progress_pct": 0.0,
            "current_step": None,
            "current_factory": None,
            "current_target": None,
            "data_end_date": prev_data_end,
            "lock_path": str(LOCK_PATH),
            "trigger_pid": os.getpid(),
            "worker_pid": None,
        },
    )

    worker_path = Path(__file__).resolve().parent / "v5_retrain_worker.py"
    args = [sys.executable, str(worker_path)]

    if changed_factories:
        args.extend(["--factories", ",".join(changed_factories)])

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(LOG_PATH, "a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                args,
                cwd=str(PROJECT_ROOT),
                stdout=log_file,
                stderr=log_file,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,  # type: ignore[attr-defined]
            )
            mark_worker_started(process.pid, run_id=run_id, trigger_mode=trigger_mode)
            status = read_json(STATUS_PATH, {})
            if isinstance(status, dict):
                status["worker_pid"] = process.pid
                status["lock_path"] = str(LOCK_PATH)
                _write_json_atomic(STATUS_PATH, status)
        finally:
            try:
                log_file.close()
            except Exception:
                pass
        return {
            "started": True,
            "message": "재학습을 시작했습니다.",
            "run_id": run_id,
            "worker_pid": process.pid,
        }
    except Exception as e:
        release_training_lock(owner_run_id=run_id)

        _write_json_atomic(
            STATUS_PATH,
            {
                "status": "fail",
                "started_at": started_at,
                "ended_at": datetime.now().isoformat(),
                "mode": "start_fail",
                "run_id": run_id,
                "trigger_mode": trigger_mode,
                "message": "재학습 시작 실패",
                "error": str(e),
                "new_model_path": None,
                "progress_current": 0,
                "progress_total": 0,
                "progress_pct": 0.0,
                "current_step": None,
                "current_factory": None,
                "current_target": None,
                "data_end_date": prev_data_end,
                "lock_path": str(LOCK_PATH),
                "trigger_pid": os.getpid(),
                "worker_pid": None,
            },
        )
        return {"started": False, "message": f"학습 시작 실패: {e}", "run_id": run_id}
