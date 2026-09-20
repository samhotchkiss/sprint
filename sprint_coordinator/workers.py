from __future__ import annotations

import json
import subprocess
import threading


class WorkerError(RuntimeError):
    pass


class SlotGate:
    def __init__(self, concurrency: int, reserved_response: int):
        self.concurrency = concurrency
        self.reserved_response = reserved_response
        self._lock = threading.Lock()
        self.running = {"response": 0, "code": 0}

    def counts(self) -> dict:
        with self._lock:
            return dict(self.running)

    def max_code(self) -> int:
        return max(0, self.concurrency - self.reserved_response)

    def can_start(self, role: str) -> bool:
        with self._lock:
            total = self.running["response"] + self.running["code"]
            if total >= self.concurrency:
                return False
            if role == "code":
                return self.running["code"] < self.max_code()
            return True

    def acquire(self, role: str) -> bool:
        with self._lock:
            total = self.running["response"] + self.running["code"]
            if total >= self.concurrency:
                return False
            if role == "code" and self.running["code"] >= self.max_code():
                return False
            self.running[role] = self.running.get(role, 0) + 1
            return True

    def release(self, role: str) -> None:
        with self._lock:
            self.running[role] = max(0, self.running.get(role, 0) - 1)


class SubprocessRunner:
    def __init__(self, timeout: float):
        self.timeout = timeout

    def run(self, command: list, job: dict) -> dict:
        try:
            proc = subprocess.run(
                command,
                input=json.dumps(job, sort_keys=True).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkerError("timeout") from exc
        except OSError as exc:
            raise WorkerError("spawn_failed") from exc
        if proc.returncode != 0:
            raise WorkerError("exit_%s" % proc.returncode)
        try:
            payload = json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise WorkerError("invalid_json")
        return payload


class InProcessRunner:
    def __init__(self, handlers: dict):
        self.handlers = handlers

    def run(self, command: list, job: dict) -> dict:
        key = tuple(command)
        handler = self.handlers.get(key)
        if handler is None:
            handler = self.handlers.get(command[0] if command else "")
        if handler is None:
            raise WorkerError("no_handler")
        return handler(job)
