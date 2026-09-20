from __future__ import annotations

import json
import subprocess
import threading
import time


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


class TaskPool:
    """Run bounded work off the scheduling thread. Results are applied by the owner."""

    def __init__(self):
        self._lock = threading.Lock()
        self._done = []
        self._threads = []
        self._closed = False
        self._inflight = set()

    def submit(self, kind: str, key: str, fn, *args, **kwargs) -> bool:
        token = "%s:%s" % (kind, key)
        with self._lock:
            if self._closed:
                return False
            if token in self._inflight:
                return False
            self._inflight.add(token)

        def work():
            result, err = None, None
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                err = exc
            with self._lock:
                self._inflight.discard(token)
                if not self._closed:
                    self._done.append((kind, key, result, err))

        thread = threading.Thread(target=work, daemon=True, name=token)
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return True

    def pop_done(self) -> list:
        with self._lock:
            items = list(self._done)
            self._done.clear()
        return items

    def prune(self) -> None:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]

    def alive(self) -> int:
        self.prune()
        with self._lock:
            return len(self._inflight)

    def join(self, timeout: float) -> None:
        deadline = time.time() + max(0.0, float(timeout))
        self.prune()
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            thread.join(remaining)
        self.prune()

    def close(self, timeout: float = 1.0) -> None:
        with self._lock:
            self._closed = True
        self.join(timeout)


class SubprocessRunner:
    def __init__(self, timeout: float):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._procs = set()
        self._closed = False

    def run(self, command: list, job: dict) -> dict:
        with self._lock:
            if self._closed:
                raise WorkerError("closed")
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise WorkerError("spawn_failed") from exc
        with self._lock:
            if self._closed:
                try:
                    proc.kill()
                except OSError:
                    pass
                raise WorkerError("closed")
            self._procs.add(proc)
        try:
            out, _err = proc.communicate(
                json.dumps(job, sort_keys=True).encode("utf-8"),
                timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                proc.kill()
                proc.communicate()
            except OSError:
                pass
            raise WorkerError("timeout") from exc
        finally:
            with self._lock:
                self._procs.discard(proc)
        if proc.returncode != 0:
            raise WorkerError("exit_%s" % proc.returncode)
        try:
            payload = json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise WorkerError("invalid_json")
        return payload

    def close(self) -> None:
        with self._lock:
            self._closed = True
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=0.2)
            except Exception:
                pass


class InProcessRunner:
    def __init__(self, handlers: dict):
        self.handlers = handlers
        self._closed = False

    def run(self, command: list, job: dict) -> dict:
        if self._closed:
            raise WorkerError("closed")
        key = tuple(command)
        handler = self.handlers.get(key)
        if handler is None:
            handler = self.handlers.get(command[0] if command else "")
        if handler is None:
            raise WorkerError("no_handler")
        return handler(job)

    def close(self) -> None:
        self._closed = True
