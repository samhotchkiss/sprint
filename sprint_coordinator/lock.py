from __future__ import annotations

import fcntl
import os
from pathlib import Path

from sprint_coordinator.util import chmod_private


def lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / "coordinator.lock"


def acquire(data_dir: Path):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(data_dir)
    handle = open(path, "a+")
    chmod_private(path)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def held(data_dir: Path) -> bool:
    probe = acquire(data_dir)
    if probe is None:
        return True
    probe.close()
    return False
