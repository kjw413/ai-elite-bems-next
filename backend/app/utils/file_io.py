from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_file_lock(
    lock_path: Path,
    *,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 0.2,
) -> Iterator[None]:
    """Acquire a simple cross-process lock using atomic lock-file creation."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None

    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for file lock: {lock_path}")
            time.sleep(poll_seconds)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def atomic_save_workbook(wb, target_path: Path) -> Path:
    """Save an openpyxl workbook to a temporary file, then atomically replace target."""
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_handle = tempfile.NamedTemporaryFile(
        prefix=f".{target.stem}.",
        suffix=target.suffix,
        dir=target.parent,
        delete=False,
    )
    tmp_path = Path(tmp_handle.name)
    tmp_handle.close()

    try:
        wb.save(tmp_path)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return target
