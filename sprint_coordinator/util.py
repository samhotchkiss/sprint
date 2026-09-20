from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class Clock:
    def __init__(self, start: float = 0.0):
        self.t = float(start)
        self._live = start is None

    @classmethod
    def live(cls) -> "Clock":
        inst = cls(0.0)
        inst._live = True
        return inst

    def now(self) -> float:
        if self._live:
            import time
            return time.time()
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t
