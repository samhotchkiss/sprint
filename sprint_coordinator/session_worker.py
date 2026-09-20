"""Configured-pane session adapter. All session messages go through tmux-send.

Operator supplies the pane and private assignment directory. Jobs never choose a
pane or messaging path. Delivery is a command array::

    [tmux-send, --no-stash, --wait, 0, <pane>, --file, <prompt>]

Verification stays on. The process never uses ``shell=True``, ``tmux send-keys``,
``paste-buffer``, ``--force``, or ``--no-verify``. tmux-send exit 0 means the
instruction was submitted, not that the session finished the job.

Durable assignment states: prepared → delivering → delivered → completed.
Restart reads the manifest/result before any send and never blindly redelivers.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
from typing import Any, Callable


STATUS_COMPLETED = "completed"
STATUS_PENDING = "pending"
STATUS_UNCERTAIN = "uncertain"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"

MANIFEST_PREPARED = "prepared"
MANIFEST_DELIVERING = "delivering"
MANIFEST_DELIVERED = "delivered"
MANIFEST_COMPLETED = "completed"
MANIFEST_UNCERTAIN = "uncertain"
MANIFEST_BLOCKED = "blocked"

HOLD_STATES = {
    MANIFEST_DELIVERING,
    MANIFEST_DELIVERED,
    MANIFEST_UNCERTAIN,
    MANIFEST_BLOCKED,
}

PANE_RE = re.compile(r"^%[0-9]+$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
JOB_OVERRIDE_KEYS = {
    "pane", "target", "tmux_send", "tmux-send", "allow_code", "worktree",
    "executable", "command", "tmux",
}
FORBIDDEN_CODE_ACTIONS = ("merge", "push", "deploy")
RESPONSE_KINDS = {"response", "reply"}
CODE_KINDS = {"code"}

MAX_ASSIGNMENT_ID = 128
MAX_JOB_BYTES = 262144
MAX_RESULT_BYTES = 65536
MAX_TEXT_CHARS = 32000
MAX_PROMPT_BYTES = 8192

DEFAULT_TMUX_SEND = Path("~/.local/bin/tmux-send").expanduser()
DEFAULT_TRANSPORT_ROOT = Path("~/.config/sprint/session-transport").expanduser()
DEFAULT_ASSIGNMENTS = DEFAULT_TRANSPORT_ROOT / "assignments"
DEFAULT_WAIT_SECONDS = 30.0
DEFAULT_SEND_TIMEOUT = 20.0
DEFAULT_POLL_INTERVAL = 0.2

TMUX_EXIT_REASONS = {
    3: (STATUS_BLOCKED, "pane_not_found"),
    4: (STATUS_UNCERTAIN, "send_unverified"),
    5: (STATUS_BLOCKED, "dialog_refusal"),
    6: (STATUS_BLOCKED, "draft_refusal"),
}

Sleep = Callable[[float], None]
Clock = Callable[[], float]


def adapter_result(status: str, reason: str | None = None, **fields: Any) -> dict:
    payload = {"ok": status == STATUS_COMPLETED, "status": status}
    if reason:
        payload["reason"] = reason
        payload["error"] = reason
    payload.update(fields)
    return payload


def canonical_pane(value: Any) -> str | None:
    if not isinstance(value, str) or not PANE_RE.fullmatch(value):
        return None
    return value


def canonical_assignment_id(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_ASSIGNMENT_ID:
        return None
    if value.startswith("-") or ".." in value or "/" in value or "\\" in value:
        return None
    if not ASSIGNMENT_RE.fullmatch(value):
        return None
    return value


def _absolute_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("path_not_absolute")
    return path.resolve()


def _absolute_executable(path: Path) -> Path | None:
    path = Path(path).expanduser()
    if not path.is_absolute():
        return None
    path = path.resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return path


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, data: str, mode: int = 0o600) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, mode)


def _write_json_private(path: Path, value: Any) -> None:
    _write_private(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _job_kind(job: dict) -> str:
    kind = job.get("kind", "response")
    if kind in RESPONSE_KINDS:
        return "response"
    if kind in CODE_KINDS:
        return "code"
    return str(kind)


def _submit_helper_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "bin" / "sprint-session-worker")


def _prompt_text(job_path: Path, result_path: Path, job_dir: Path, job: dict) -> str:
    helper = job.get("submit_helper") or _submit_helper_path()
    lines = [
        "Sprint session assignment.",
        "",
        "Read the job JSON at:",
        str(job_path),
        "",
        "Write one atomic candidate result.json at:",
        str(result_path),
        "",
        "You may use the submit helper (no session messaging):",
        "%s submit-result --job-dir %s --text \"<candidate>\"" % (helper, job_dir),
        "",
        "Include assignment_id and token from the job. This result is a candidate.",
        "Do not claim checks ran. The coordinator verifies independently.",
        "Do not parse this instruction as completion. Session stdout is not a result.",
    ]
    if _job_kind(job) == "code":
        lines.extend([
            "",
            "Code execution is operator-enabled for this job only.",
            "Worktree: %s" % job.get("worktree"),
            "Task scope: %s" % (job.get("task_scope") or ""),
            "Forbidden by default: %s." % ", ".join(FORBIDDEN_CODE_ACTIONS),
        ])
    else:
        lines.extend([
            "",
            "This is a response assignment. Do not merge, push, deploy, or run unbounded commands.",
        ])
    return "\n".join(lines) + "\n"


def _pane_paths(transport_root: Path, pane: str) -> tuple[Path, Path]:
    ident = pane[1:]
    return (
        transport_root / ("pane-%s.lock" % ident),
        transport_root / ("pane-%s.active.json" % ident),
    )


class PaneLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self.handle = handle
        return True

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


def _marker_payload(assignment_id: str, job_dir: Path, pane: str, state: str) -> dict:
    return {
        "assignment_id": assignment_id,
        "job_dir": str(job_dir),
        "pane": pane,
        "state": state,
        "pid": os.getpid(),
    }


def _read_marker(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _clear_marker(path: Path, assignment_id: str) -> None:
    marker = _read_marker(path)
    if marker and marker.get("assignment_id") not in (None, assignment_id):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _validate_incoming_job(raw: Any) -> tuple[dict | None, dict | None]:
    if not isinstance(raw, dict):
        return None, adapter_result(STATUS_FAILED, "invalid_job")
    encoded = json.dumps(raw, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_JOB_BYTES:
        return None, adapter_result(STATUS_FAILED, "job_too_large")
    if any(key in raw for key in JOB_OVERRIDE_KEYS):
        return None, adapter_result(STATUS_FAILED, "job_transport_override")
    assignment_id = canonical_assignment_id(raw.get("assignment_id"))
    if assignment_id is None:
        return None, adapter_result(STATUS_FAILED, "invalid_assignment_id")
    kind = _job_kind(raw)
    if kind not in ("response", "code"):
        return None, adapter_result(STATUS_FAILED, "unsupported_job_kind")
    return raw, None


def _candidate_from_result(payload: dict) -> dict:
    candidate = {
        "assignment_id": payload["assignment_id"],
        "kind": payload["kind"],
        "text": payload["text"],
        "addresses_obligation": payload.get("addresses_obligation"),
        "status_update": payload.get("status_update"),
    }
    return {key: value for key, value in candidate.items() if value is not None}


def _validate_result_file(path: Path, assignment_id: str, token: str,
                          expected_kind: str) -> tuple[dict | None, str | None]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, "invalid_result"
    if size > MAX_RESULT_BYTES:
        return None, "result_too_large"
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid_result"
    if not isinstance(payload, dict):
        return None, "invalid_result"
    if payload.get("assignment_id") != assignment_id:
        return None, "result_assignment_mismatch"
    observed_token = payload.get("token", payload.get("result_token"))
    if observed_token != token:
        return None, "result_token_mismatch"
    kind = payload.get("kind")
    if expected_kind == "response" and kind != "reply":
        return None, "invalid_result_kind"
    if expected_kind == "code" and kind not in ("reply", "code_result"):
        return None, "invalid_result_kind"
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        return None, "invalid_result_text"
    if kind == "reply":
        if type(payload.get("addresses_obligation")) is not bool:
            return None, "invalid_result"
        if type(payload.get("status_update")) is not bool:
            return None, "invalid_result"
    if payload.get("checks") or payload.get("adapter") == "trusted":
        return None, "untrusted_evidence_rejected"
    cleaned = {
        "ok": True,
        "assignment_id": assignment_id,
        "kind": kind,
        "text": text,
        "addresses_obligation": payload.get("addresses_obligation"),
        "status_update": payload.get("status_update"),
    }
    if cleaned["addresses_obligation"] is None:
        cleaned.pop("addresses_obligation")
    if cleaned["status_update"] is None:
        cleaned.pop("status_update")
    return cleaned, None


def _tmux_send_command(executable: Path, pane: str, prompt_path: Path) -> list[str]:
    return [str(executable), "--no-stash", "--wait", "0", pane, "--file", str(prompt_path)]


def _forbidden_send_args(command: list[str]) -> bool:
    banned = {"--force", "--no-verify", "send-keys", "paste-buffer"}
    return any(part in banned for part in command)


def run_job(
    job: dict,
    *,
    pane: str,
    assignments_dir: Path,
    transport_root: Path,
    tmux_send: Path,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    send_timeout: float = DEFAULT_SEND_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    allow_code: bool = False,
    worktree: Path | None = None,
    task_scope: str = "",
    reconcile: bool = False,
    sleep: Sleep = time.sleep,
    monotonic: Clock = time.monotonic,
) -> dict:
    pane_id = canonical_pane(pane)
    if pane_id is None:
        return adapter_result(STATUS_BLOCKED, "invalid_pane")
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        return adapter_result(STATUS_FAILED, "invalid_wait")
    if not math.isfinite(send_timeout) or send_timeout <= 0:
        return adapter_result(STATUS_FAILED, "invalid_send_timeout")
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        return adapter_result(STATUS_FAILED, "invalid_poll_interval")

    try:
        assignments_dir = _ensure_private_dir(_absolute_dir(Path(assignments_dir)))
        transport_root = _ensure_private_dir(_absolute_dir(Path(transport_root)))
    except ValueError:
        return adapter_result(STATUS_FAILED, "path_not_absolute")

    parsed, error = _validate_incoming_job(job)
    if error is not None:
        return error
    job = parsed
    assignment_id = canonical_assignment_id(job["assignment_id"])
    kind = _job_kind(job)
    if reconcile:
        return _reconcile(
            kind=kind, assignment_id=assignment_id, pane_id=pane_id,
            assignments_dir=assignments_dir, transport_root=transport_root,
        )
    executable = _absolute_executable(Path(tmux_send))
    if executable is None:
        if not Path(tmux_send).expanduser().is_absolute():
            return adapter_result(STATUS_FAILED, "tmux_send_not_absolute")
        return adapter_result(STATUS_FAILED, "tmux_send_missing")
    if kind == "code":
        if not allow_code:
            return adapter_result(STATUS_BLOCKED, "code_not_enabled",
                                  assignment_id=assignment_id)
        if worktree is None:
            return adapter_result(STATUS_FAILED, "missing_worktree",
                                  assignment_id=assignment_id)
        worktree = Path(worktree).expanduser()
        if not worktree.is_absolute() or not worktree.is_dir():
            return adapter_result(STATUS_FAILED, "invalid_worktree",
                                  assignment_id=assignment_id)
        if not isinstance(task_scope, str) or not task_scope.strip():
            return adapter_result(STATUS_FAILED, "missing_task_scope",
                                  assignment_id=assignment_id)
        worktree = worktree.resolve()

    job_dir = (assignments_dir / assignment_id).resolve()
    if not job_dir.is_relative_to(assignments_dir.resolve()):
        return adapter_result(STATUS_FAILED, "invalid_assignment_id")

    lock_path, marker_path = _pane_paths(transport_root, pane_id)
    lock = PaneLock(lock_path)
    if not lock.acquire():
        return adapter_result(STATUS_BLOCKED, "pane_busy", assignment_id=assignment_id,
                              pane=pane_id)

    interrupted = {"value": False}

    def on_interrupt(signum, _frame):
        interrupted["value"] = True
        raise KeyboardInterrupt

    previous = {}
    try:
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, on_interrupt)
        except ValueError:
            previous = {}
        return _run_locked(
            job=job, kind=kind, assignment_id=assignment_id, pane_id=pane_id,
            job_dir=job_dir, marker_path=marker_path, executable=executable,
            wait_seconds=wait_seconds, send_timeout=send_timeout,
            poll_interval=poll_interval, worktree=worktree, task_scope=task_scope,
            sleep=sleep, monotonic=monotonic, interrupted=interrupted,
        )
    except KeyboardInterrupt:
        return _interrupt_outcome(job_dir, assignment_id, marker_path, pane_id)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        lock.release()


def _interrupt_outcome(job_dir: Path, assignment_id: str, marker_path: Path,
                       pane: str) -> dict:
    manifest = _load_manifest(job_dir)
    state = (manifest or {}).get("state")
    identity = {
        "assignment_id": assignment_id,
        "job_dir": str(job_dir),
        "pane": pane,
        "manifest_state": state,
    }
    if state == MANIFEST_DELIVERED:
        _write_marker(marker_path, assignment_id, job_dir, pane, state)
        return adapter_result(STATUS_PENDING, "interrupted", **identity)
    if state in (MANIFEST_DELIVERING, MANIFEST_UNCERTAIN) or state is None:
        if manifest is not None:
            _set_manifest_state(job_dir, MANIFEST_UNCERTAIN, tmux_send_exit=None)
            _write_marker(marker_path, assignment_id, job_dir, pane, MANIFEST_UNCERTAIN)
        return adapter_result(STATUS_UNCERTAIN, "interrupted", **identity)
    return adapter_result(STATUS_UNCERTAIN, "interrupted", **identity)


def _load_manifest(job_dir: Path) -> dict | None:
    path = job_dir / "manifest.json"
    if not path.exists():
        return None
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _set_manifest_state(job_dir: Path, state: str, **updates: Any) -> dict:
    manifest = _load_manifest(job_dir) or {}
    manifest["state"] = state
    manifest.update({key: value for key, value in updates.items() if value is not None})
    _write_json_private(job_dir / "manifest.json", manifest)
    return manifest


def _write_marker(path: Path, assignment_id: str, job_dir: Path, pane: str,
                  state: str) -> None:
    _write_json_private(path, _marker_payload(assignment_id, job_dir, pane, state))


def _run_locked(
    *,
    job: dict,
    kind: str,
    assignment_id: str,
    pane_id: str,
    job_dir: Path,
    marker_path: Path,
    executable: Path,
    wait_seconds: float,
    send_timeout: float,
    poll_interval: float,
    worktree: Path | None,
    task_scope: str,
    sleep: Sleep,
    monotonic: Clock,
    interrupted: dict,
) -> dict:
    marker = _read_marker(marker_path)
    if marker:
        other = marker.get("assignment_id")
        other_state = marker.get("state")
        if other and other != assignment_id and other_state in HOLD_STATES:
            return adapter_result(
                STATUS_BLOCKED, "pane_held", assignment_id=assignment_id,
                pane=pane_id, held_assignment_id=other, job_dir=str(job_dir),
            )

    if not job_dir.exists():
        os.mkdir(job_dir, 0o700)
    os.chmod(job_dir, 0o700)

    job_path = job_dir / "job.json"
    prompt_path = job_dir / "prompt.txt"
    result_path = job_dir / "result.json"
    manifest = _load_manifest(job_dir)

    if manifest:
        return _resume(
            manifest=manifest, kind=kind, assignment_id=assignment_id,
            pane_id=pane_id, job_dir=job_dir, marker_path=marker_path,
            job_path=job_path, result_path=result_path, executable=executable,
            wait_seconds=wait_seconds, send_timeout=send_timeout,
            poll_interval=poll_interval, sleep=sleep, monotonic=monotonic,
            interrupted=interrupted, reconcile=False,
        )

    token = secrets.token_hex(16)
    stored = dict(job)
    stored["assignment_id"] = assignment_id
    stored["result_token"] = token
    stored["token"] = token
    stored["job_dir"] = str(job_dir)
    stored["result_path"] = str(result_path)
    stored["pane"] = pane_id
    stored["submit_helper"] = _submit_helper_path()
    stored["forbidden_actions"] = list(FORBIDDEN_CODE_ACTIONS)
    if kind == "code":
        stored["code_enabled"] = True
        stored["worktree"] = str(worktree)
        stored["task_scope"] = task_scope.strip()
        stored["allow_code"] = True
    else:
        stored["code_enabled"] = False
    # Operator identity is recorded in the private job file after validation.
    # Incoming jobs may not set these keys; see JOB_OVERRIDE_KEYS.
    prompt = _prompt_text(job_path, result_path, job_dir, stored)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        return adapter_result(STATUS_FAILED, "prompt_too_large",
                              assignment_id=assignment_id)
    _write_json_private(job_path, stored)
    _write_private(prompt_path, prompt)
    prepared = {
        "assignment_id": assignment_id,
        "pane": pane_id,
        "state": MANIFEST_PREPARED,
        "job_path": str(job_path),
        "prompt_path": str(prompt_path),
        "result_path": str(result_path),
        "token": token,
        "kind": kind,
        "tmux_send_exit": None,
    }
    _write_json_private(job_dir / "manifest.json", prepared)
    _write_marker(marker_path, assignment_id, job_dir, pane_id, MANIFEST_PREPARED)
    return _deliver(
        assignment_id=assignment_id, pane_id=pane_id, job_dir=job_dir,
        marker_path=marker_path, executable=executable, prompt_path=prompt_path,
        result_path=result_path, token=token, kind=kind,
        wait_seconds=wait_seconds, send_timeout=send_timeout,
        poll_interval=poll_interval, sleep=sleep, monotonic=monotonic,
        interrupted=interrupted,
    )


def _reconcile(
    *,
    kind: str,
    assignment_id: str,
    pane_id: str,
    assignments_dir: Path,
    transport_root: Path,
) -> dict:
    job_dir = (assignments_dir / assignment_id).resolve()
    identity = {
        "assignment_id": assignment_id,
        "job_dir": str(job_dir),
        "pane": pane_id,
        "result_path": str(job_dir / "result.json"),
    }
    if not job_dir.is_dir():
        return adapter_result(STATUS_PENDING, "not_prepared", **identity)
    if not job_dir.is_relative_to(assignments_dir.resolve()):
        return adapter_result(STATUS_FAILED, "invalid_assignment_id")
    manifest = _load_manifest(job_dir)
    if not manifest:
        return adapter_result(STATUS_PENDING, "not_prepared", **identity)
    _lock_path, marker_path = _pane_paths(transport_root, pane_id)
    return _resume(
        manifest=manifest, kind=kind, assignment_id=assignment_id, pane_id=pane_id,
        job_dir=job_dir, marker_path=marker_path, job_path=job_dir / "job.json",
        result_path=job_dir / "result.json", executable=None,
        wait_seconds=0.0, send_timeout=DEFAULT_SEND_TIMEOUT,
        poll_interval=DEFAULT_POLL_INTERVAL, sleep=time.sleep,
        monotonic=time.monotonic, interrupted={"value": False}, reconcile=True,
    )


def _try_complete(result_path: Path, assignment_id: str, token: str | None,
                  kind: str, job_dir: Path, marker_path: Path,
                  identity: dict) -> dict | None:
    if not token or not result_path.exists():
        return None
    cleaned, error = _validate_result_file(result_path, assignment_id, token, kind)
    if cleaned:
        _set_manifest_state(job_dir, MANIFEST_COMPLETED)
        _clear_marker(marker_path, assignment_id)
        return _completed_payload(cleaned, identity)
    _set_manifest_state(job_dir, MANIFEST_BLOCKED, reason=error)
    _clear_marker(marker_path, assignment_id)
    return adapter_result(STATUS_FAILED, error, **identity)


def _resume(
    *,
    manifest: dict,
    kind: str,
    assignment_id: str,
    pane_id: str,
    job_dir: Path,
    marker_path: Path,
    job_path: Path,
    result_path: Path,
    executable: Path | None,
    wait_seconds: float,
    send_timeout: float,
    poll_interval: float,
    sleep: Sleep,
    monotonic: Clock,
    interrupted: dict,
    reconcile: bool,
) -> dict:
    if manifest.get("assignment_id") not in (None, assignment_id):
        return adapter_result(STATUS_FAILED, "assignment_mismatch",
                              assignment_id=assignment_id, job_dir=str(job_dir))
    if manifest.get("pane") not in (None, pane_id):
        return adapter_result(STATUS_BLOCKED, "pane_mismatch",
                              assignment_id=assignment_id, job_dir=str(job_dir))
    state = manifest.get("state")
    token = manifest.get("token")
    if not token and job_path.exists():
        try:
            stored = _read_json(job_path)
        except (OSError, json.JSONDecodeError):
            stored = None
        if isinstance(stored, dict):
            token = stored.get("token") or stored.get("result_token")
    identity = {
        "assignment_id": assignment_id,
        "job_dir": str(job_dir),
        "pane": pane_id,
        "manifest_state": state,
        "result_path": str(result_path),
    }
    completed = _try_complete(
        result_path, assignment_id, token, kind, job_dir, marker_path, identity)
    if completed is not None:
        return completed
    if state == MANIFEST_COMPLETED:
        _clear_marker(marker_path, assignment_id)
        return adapter_result(STATUS_FAILED, "invalid_result", **identity)
    if state == MANIFEST_UNCERTAIN:
        _write_marker(marker_path, assignment_id, job_dir, pane_id, state)
        return adapter_result(STATUS_UNCERTAIN, "already_uncertain",
                              tmux_send_exit=manifest.get("tmux_send_exit"), **identity)
    if state == MANIFEST_BLOCKED:
        reason = manifest.get("reason") or "already_blocked"
        _write_marker(marker_path, assignment_id, job_dir, pane_id, state)
        return adapter_result(STATUS_BLOCKED, reason,
                              tmux_send_exit=manifest.get("tmux_send_exit"), **identity)
    if state == MANIFEST_DELIVERING:
        if reconcile:
            return adapter_result(STATUS_PENDING, "in_flight", **identity)
        _set_manifest_state(job_dir, MANIFEST_UNCERTAIN)
        _write_marker(marker_path, assignment_id, job_dir, pane_id, MANIFEST_UNCERTAIN)
        identity["manifest_state"] = MANIFEST_UNCERTAIN
        return adapter_result(STATUS_UNCERTAIN, "already_uncertain", **identity)
    if state == MANIFEST_DELIVERED:
        if reconcile:
            _write_marker(marker_path, assignment_id, job_dir, pane_id, state)
            return adapter_result(STATUS_PENDING, "wait_timeout", **identity)
        _write_marker(marker_path, assignment_id, job_dir, pane_id, state)
        return _wait_for_result(
            assignment_id=assignment_id, pane_id=pane_id, job_dir=job_dir,
            marker_path=marker_path, result_path=result_path, token=token,
            kind=kind, wait_seconds=wait_seconds, poll_interval=poll_interval,
            sleep=sleep, monotonic=monotonic, interrupted=interrupted,
        )
    if state == MANIFEST_PREPARED:
        if reconcile:
            return adapter_result(STATUS_PENDING, "not_sent", **identity)
        stored = _read_json(job_path) if job_path.exists() else None
        prompt_path = Path(manifest.get("prompt_path") or (job_dir / "prompt.txt"))
        if not isinstance(stored, dict) or not prompt_path.exists():
            return adapter_result(STATUS_FAILED, "incomplete_prepared_job", **identity)
        if executable is None:
            return adapter_result(STATUS_FAILED, "tmux_send_missing", **identity)
        return _deliver(
            assignment_id=assignment_id, pane_id=pane_id, job_dir=job_dir,
            marker_path=marker_path, executable=executable,
            prompt_path=prompt_path, result_path=result_path,
            token=stored.get("token") or token,
            kind=kind, wait_seconds=wait_seconds, send_timeout=send_timeout,
            poll_interval=poll_interval, sleep=sleep, monotonic=monotonic,
            interrupted=interrupted,
        )
    return adapter_result(STATUS_FAILED, "unknown_manifest_state", **identity)


def _deliver(
    *,
    assignment_id: str,
    pane_id: str,
    job_dir: Path,
    marker_path: Path,
    executable: Path,
    prompt_path: Path,
    result_path: Path,
    token: str,
    kind: str,
    wait_seconds: float,
    send_timeout: float,
    poll_interval: float,
    sleep: Sleep,
    monotonic: Clock,
    interrupted: dict,
) -> dict:
    command = _tmux_send_command(executable, pane_id, prompt_path)
    if _forbidden_send_args(command):
        return adapter_result(STATUS_FAILED, "forbidden_send_args",
                              assignment_id=assignment_id)
    _set_manifest_state(job_dir, MANIFEST_DELIVERING)
    _write_marker(marker_path, assignment_id, job_dir, pane_id, MANIFEST_DELIVERING)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=send_timeout,
            check=False,
            close_fds=True,
        )
        exit_code = int(completed.returncode)
    except subprocess.TimeoutExpired:
        _set_manifest_state(job_dir, MANIFEST_UNCERTAIN, tmux_send_exit=None,
                            reason="send_timeout")
        _write_marker(marker_path, assignment_id, job_dir, pane_id, MANIFEST_UNCERTAIN)
        return adapter_result(
            STATUS_UNCERTAIN, "send_timeout", assignment_id=assignment_id,
            job_dir=str(job_dir), pane=pane_id, manifest_state=MANIFEST_UNCERTAIN,
        )
    except OSError:
        _set_manifest_state(job_dir, MANIFEST_BLOCKED, reason="spawn_failed")
        _clear_marker(marker_path, assignment_id)
        return adapter_result(
            STATUS_BLOCKED, "spawn_failed", assignment_id=assignment_id,
            job_dir=str(job_dir), pane=pane_id,
        )

    if exit_code == 0:
        _set_manifest_state(job_dir, MANIFEST_DELIVERED, tmux_send_exit=0)
        _write_marker(marker_path, assignment_id, job_dir, pane_id, MANIFEST_DELIVERED)
        return _wait_for_result(
            assignment_id=assignment_id, pane_id=pane_id, job_dir=job_dir,
            marker_path=marker_path, result_path=result_path, token=token,
            kind=kind, wait_seconds=wait_seconds, poll_interval=poll_interval,
            sleep=sleep, monotonic=monotonic, interrupted=interrupted,
        )

    status, reason = TMUX_EXIT_REASONS.get(exit_code, (STATUS_BLOCKED, "send_failed"))
    manifest_state = MANIFEST_UNCERTAIN if status == STATUS_UNCERTAIN else MANIFEST_BLOCKED
    _set_manifest_state(job_dir, manifest_state, tmux_send_exit=exit_code, reason=reason)
    if status == STATUS_UNCERTAIN or exit_code in (5, 6):
        _write_marker(marker_path, assignment_id, job_dir, pane_id, manifest_state)
    else:
        _clear_marker(marker_path, assignment_id)
    return adapter_result(
        status, reason, assignment_id=assignment_id, job_dir=str(job_dir),
        pane=pane_id, tmux_send_exit=exit_code, manifest_state=manifest_state,
    )


def _wait_for_result(
    *,
    assignment_id: str,
    pane_id: str,
    job_dir: Path,
    marker_path: Path,
    result_path: Path,
    token: str,
    kind: str,
    wait_seconds: float,
    poll_interval: float,
    sleep: Sleep,
    monotonic: Clock,
    interrupted: dict,
) -> dict:
    identity = {
        "assignment_id": assignment_id,
        "job_dir": str(job_dir),
        "pane": pane_id,
        "manifest_state": MANIFEST_DELIVERED,
        "result_path": str(result_path),
    }
    deadline = monotonic() + wait_seconds
    while True:
        if interrupted["value"]:
            _write_marker(marker_path, assignment_id, job_dir, pane_id, MANIFEST_DELIVERED)
            return adapter_result(STATUS_PENDING, "interrupted", **identity)
        if result_path.exists():
            cleaned, error = _validate_result_file(result_path, assignment_id, token, kind)
            if error:
                _set_manifest_state(job_dir, MANIFEST_BLOCKED, reason=error)
                _clear_marker(marker_path, assignment_id)
                return adapter_result(STATUS_FAILED, error, **identity)
            _set_manifest_state(job_dir, MANIFEST_COMPLETED)
            _clear_marker(marker_path, assignment_id)
            return _completed_payload(cleaned, identity)
        remaining = deadline - monotonic()
        if remaining <= 0:
            _write_marker(marker_path, assignment_id, job_dir, pane_id, MANIFEST_DELIVERED)
            return adapter_result(STATUS_PENDING, "wait_timeout", **identity)
        sleep(min(poll_interval, remaining))


def _completed_payload(cleaned: dict, identity: dict) -> dict:
    payload = adapter_result(STATUS_COMPLETED)
    payload.update(identity)
    payload["manifest_state"] = MANIFEST_COMPLETED
    payload.update({
        "kind": cleaned["kind"],
        "text": cleaned["text"],
        "candidate": _candidate_from_result(cleaned),
    })
    if "addresses_obligation" in cleaned:
        payload["addresses_obligation"] = cleaned["addresses_obligation"]
    if "status_update" in cleaned:
        payload["status_update"] = cleaned["status_update"]
    payload["assignment_id"] = cleaned["assignment_id"]
    return payload


def submit_result(
    job_dir: Path,
    *,
    text: str,
    kind: str = "reply",
    addresses_obligation: bool = True,
    status_update: bool = False,
) -> dict:
    job_dir = Path(job_dir)
    job_path = job_dir / "job.json"
    if not job_path.is_file():
        return adapter_result(STATUS_FAILED, "missing_job")
    try:
        job = _read_json(job_path)
    except (OSError, json.JSONDecodeError):
        return adapter_result(STATUS_FAILED, "invalid_job")
    if not isinstance(job, dict):
        return adapter_result(STATUS_FAILED, "invalid_job")
    assignment_id = job.get("assignment_id")
    token = job.get("token") or job.get("result_token")
    if not assignment_id or not token:
        return adapter_result(STATUS_FAILED, "missing_result_identity")
    if kind not in ("reply", "code_result"):
        return adapter_result(STATUS_FAILED, "invalid_result_kind")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        return adapter_result(STATUS_FAILED, "invalid_result_text")
    payload = {
        "ok": True,
        "assignment_id": assignment_id,
        "token": token,
        "kind": kind,
        "text": text,
        "addresses_obligation": bool(addresses_obligation),
        "status_update": bool(status_update),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_RESULT_BYTES:
        return adapter_result(STATUS_FAILED, "result_too_large")
    result_path = job_dir / "result.json"
    _write_json_private(result_path, payload)
    return {
        "ok": True,
        "status": "written",
        "path": str(result_path),
        "assignment_id": assignment_id,
    }


def _parse_argv(argv: list[str] | None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = "run"
    if argv and argv[0] in ("run", "submit-result", "reconcile"):
        command = argv.pop(0)
    parser = argparse.ArgumentParser(
        description="tmux-send session worker. No direct tmux or provider messaging.")
    if command == "submit-result":
        parser.add_argument("--job-dir", required=True, type=Path)
        parser.add_argument("--text")
        parser.add_argument("--kind", default="reply", choices=("reply", "code_result"))
        parser.add_argument("--addresses-obligation", action="store_true", default=True)
        parser.add_argument("--status-update", action="store_true")
        args = parser.parse_args(argv)
        args.command = command
        return args
    parser.add_argument("--pane", required=True)
    parser.add_argument("--assignments-dir", type=Path, default=DEFAULT_ASSIGNMENTS)
    parser.add_argument("--transport-root", type=Path, default=DEFAULT_TRANSPORT_ROOT)
    parser.add_argument("--tmux-send", type=Path, default=DEFAULT_TMUX_SEND)
    parser.add_argument("--wait-seconds", type=float, default=DEFAULT_WAIT_SECONDS)
    parser.add_argument("--send-timeout", type=float, default=DEFAULT_SEND_TIMEOUT)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--allow-code", action="store_true")
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--task-scope", default="")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="read durable result/manifest only; never send a session message")
    args = parser.parse_args(argv)
    args.command = command
    if command == "reconcile":
        args.reconcile = True
    return args


def _load_job_bytes(raw: bytes | None) -> dict | None:
    data = sys.stdin.buffer.read() if raw is None else raw
    try:
        job = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return job if isinstance(job, dict) else None


def main(argv: list[str] | None = None, input_bytes: bytes | None = None) -> int:
    args = _parse_argv(argv)
    if args.command == "submit-result":
        text = args.text
        if text is None:
            text = sys.stdin.read() if input_bytes is None else input_bytes.decode("utf-8")
        result = submit_result(
            args.job_dir, text=text, kind=args.kind,
            addresses_obligation=args.addresses_obligation,
            status_update=args.status_update,
        )
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    job = _load_job_bytes(input_bytes)
    if job is None:
        print(json.dumps(adapter_result(STATUS_FAILED, "invalid_job_json"), sort_keys=True))
        return 0
    result = run_job(
        job,
        pane=args.pane,
        assignments_dir=args.assignments_dir,
        transport_root=args.transport_root,
        tmux_send=args.tmux_send,
        wait_seconds=args.wait_seconds,
        send_timeout=args.send_timeout,
        poll_interval=args.poll_interval,
        allow_code=bool(args.allow_code),
        worktree=args.worktree,
        task_scope=args.task_scope,
        reconcile=bool(getattr(args, "reconcile", False)),
    )
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
