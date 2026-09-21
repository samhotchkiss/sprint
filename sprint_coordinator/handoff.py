"""Portable coordinator owner-switch. Does not kill panes or call a model.

Root wires service integration. This module plans a switch from observed
board APIs, records durable stages, and talks to source/target panes only
through tmux-send. Codex, Claude, and Grok use the same protocol.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from sprint_coordinator.util import chmod_private, read_json, sha256_text


PROVIDERS = ("claude", "codex", "grok")
STAGES = (
    "prepared",
    "source_quiesced",
    "target_registered",
    "target_acknowledged",
    "complete",
    "failed",
)
PANE_RE = re.compile(r"^%[0-9]+$")
RECORD_NAME = "owner-switch.json"
LOCK_NAME = "owner-switch.lock"
DISPATCH_NAME = "dispatch.json"
DISPATCH_LOCK_NAME = "dispatch.lock"
DISPATCH_FIELDS = (
    "target", "scanned", "acknowledged", "pending", "inflight", "wake_times",
)
DEFAULT_TMUX_SEND = Path("~/.local/bin/tmux-send").expanduser()


def canonical_pane(value) -> str | None:
    if isinstance(value, str) and PANE_RE.fullmatch(value):
        return value
    return None


def canonical_provider(value) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip().lower()
    return label if label in PROVIDERS else None


def _write_private(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    chmod_private(path)


def locator_from_dir(board_data_dir: Path) -> dict:
    """Board URL/port from server.json. Token is never copied into the record."""
    info = read_json(Path(board_data_dir) / "server.json", default={}) or {}
    port = info.get("port")
    return {
        "data_dir": str(Path(board_data_dir).resolve()),
        "port": port,
        "url": ("http://127.0.0.1:%s" % port) if port else None,
        "identity": info.get("board_id") or info.get("id") or info.get("name"),
    }


def _board_get(board, path: str) -> dict:
    if hasattr(board, "get") and path.startswith("/"):
        try:
            return board.get(path) or {}
        except TypeError:
            pass
    if path == "/api/settings" and callable(getattr(board, "settings", None)):
        return board.settings() or {}
    if path == "/api/board" and callable(getattr(board, "board", None)):
        return board.board() or {}
    if path == "/api/cursors/orchestrator" and callable(
            getattr(board, "orchestrator_cursor", None)):
        return board.orchestrator_cursor() or {}
    raise RuntimeError("board_api_missing:%s" % path)


def snapshot_dispatch(state: dict | None) -> dict | None:
    """Copy sprint-dispatch delivery fields. Never invent a reset cursor."""
    if not isinstance(state, dict) or not state:
        return None
    return {key: state.get(key) for key in DISPATCH_FIELDS}


def inflight_resolution(state: dict | None, board_cursor: int,
                        abandon_inflight: bool) -> dict:
    """Uncertain inflight is handled by board cursor or explicit abandon."""
    flight = (state or {}).get("inflight")
    if not flight:
        return {"ok": True, "action": "none"}
    try:
        through = int(flight.get("through"))
    except (TypeError, ValueError):
        through = None
    if through is not None and int(board_cursor) >= through:
        return {"ok": True, "action": "cursor_cleared"}
    result = flight.get("result")
    if result not in (0, None):
        if abandon_inflight is True:
            return {"ok": True, "action": "abandoned"}
        return {"ok": False, "reason": "uncertain_inflight", "inflight": dict(flight)}
    return {"ok": True, "action": "clear_after_source_stopped"}


def _dispatch_lock(data_dir: Path):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / DISPATCH_LOCK_NAME, "a+")
    chmod_private(data_dir / DISPATCH_LOCK_NAME)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def rebind_dispatch_target(
    data_dir: Path, *, source_pane: str, target_pane: str, board_cursor: int,
    abandon_inflight: bool, source_stopped: bool,
) -> dict:
    """Point dispatch.json at the new pane under dispatch.lock.

    Preserves scanned/acknowledged/pending/wake_times. Clears inflight only after
    the source monitor stopped. Does not set any cursor to head.
    """
    if source_stopped is not True:
        return {"ok": False, "reason": "source_not_stopped"}
    data_dir = Path(data_dir)
    path = data_dir / DISPATCH_NAME
    lock = _dispatch_lock(data_dir)
    if lock is None:
        return {"ok": False, "reason": "dispatch_lock_held"}
    try:
        state = read_json(path, default=None)
        if not isinstance(state, dict) or not state:
            return {
                "ok": True, "action": "no_dispatch_state", "rebind": None,
                "path": str(path), "lock": str(data_dir / DISPATCH_LOCK_NAME),
            }
        current = state.get("target")
        if current not in (source_pane, target_pane):
            return {"ok": False, "reason": "dispatch_target_mismatch",
                    "target": current}
        gate = inflight_resolution(state, board_cursor, abandon_inflight is True)
        if gate.get("ok") is not True:
            return gate
        before = snapshot_dispatch(state)
        pending = list(state.get("pending") or [])
        scanned = state.get("scanned")
        acknowledged = state.get("acknowledged")
        wake_times = list(state.get("wake_times") or [])
        state["target"] = target_pane
        if gate.get("action") in (
                "cursor_cleared", "abandoned", "clear_after_source_stopped"):
            state["inflight"] = None
        state["pending"] = pending
        state["scanned"] = scanned
        state["acknowledged"] = acknowledged
        state["wake_times"] = wake_times
        _write_private(path, state)
        return {
            "ok": True,
            "action": gate.get("action"),
            "path": str(path),
            "lock": str(data_dir / DISPATCH_LOCK_NAME),
            "before": before,
            "rebind": snapshot_dispatch(state),
        }
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()


def snapshot_cards(board_payload: dict) -> list:
    cards = []
    for raw in board_payload.get("cards") or ():
        if not isinstance(raw, dict) or raw.get("num") is None:
            continue
        cards.append({
            "num": raw.get("num"),
            "title": raw.get("title"),
            "state": raw.get("state"),
            "worktree": raw.get("worktree"),
            "branch": raw.get("branch"),
            "executor": raw.get("executor"),
            "model": raw.get("model"),
            "agent_name": raw.get("agent_name"),
            "pane": raw.get("pane") or raw.get("tmux_pane"),
        })
    return cards


def observe(board) -> dict:
    settings_raw = _board_get(board, "/api/settings")
    settings = settings_raw.get("settings") or {}
    if not settings and "worker" in settings_raw:
        settings = settings_raw
    worker = settings.get("worker") or {}
    board_payload = _board_get(board, "/api/board")
    cursor_raw = _board_get(board, "/api/cursors/orchestrator")
    cursor_seq = int(cursor_raw.get("seq") or 0)
    events = board_payload.get("events") or []
    head = 0
    if events:
        head = max(int(ev.get("seq") or 0) for ev in events)
    if board_payload.get("head") is not None:
        head = max(head, int(board_payload["head"]))
    if callable(getattr(board, "head_seq", None)):
        head = max(head, int(board.head_seq()))
    return {
        "settings": settings,
        "session_tmux_window": settings_raw.get("session_tmux_window"),
        "default_executor": worker.get("default_executor"),
        "cards": snapshot_cards(board_payload),
        "cursor_seq": cursor_seq,
        "head_seq": head,
        "owner": (settings.get("coordinator_owner") or settings_raw.get("coordinator_owner")
                  or {}),
    }


class TmuxSendTransport:
    def __init__(self, executable: Path):
        self.executable = Path(executable)

    def send(self, pane: str, prompt_path: Path) -> dict:
        command = [str(self.executable), "--no-stash", "--wait", "0", pane,
                   "--file", str(prompt_path)]
        try:
            completed = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False, timeout=20, check=False,
                close_fds=True)
            code = int(completed.returncode)
        except subprocess.TimeoutExpired:
            return {"status": "uncertain", "exit": None, "reason": "send_timeout"}
        except OSError:
            return {"status": "blocked", "exit": None, "reason": "spawn_failed"}
        if code == 0:
            return {"status": "delivered", "exit": 0}
        if code == 4:
            return {"status": "uncertain", "exit": 4, "reason": "send_unverified"}
        return {"status": "blocked", "exit": code, "reason": "send_failed"}


class OwnerSwitch:
    def __init__(self, board_data_dir: Path, *, board, transport,
                 project_root: Path | None = None, switch_dir: Path | None = None,
                 clock=None):
        self.board_data_dir = Path(board_data_dir)
        self.board = board
        self.transport = transport
        self.project_root = Path(project_root or ".").resolve()
        self.switch_dir = Path(switch_dir) if switch_dir else self.board_data_dir
        self.clock = clock
        self._lock = None

    def _now(self) -> float:
        if self.clock is not None:
            return float(self.clock.now())
        return time.time()

    def record_path(self) -> Path:
        return self.switch_dir / RECORD_NAME

    def lock_path(self) -> Path:
        return self.switch_dir / LOCK_NAME

    def load(self) -> dict | None:
        return read_json(self.record_path(), default=None)

    def _save(self, rec: dict) -> dict:
        rec["updated_at"] = self._now()
        rec["next_action"] = next_action(rec)
        _write_private(self.record_path(), rec)
        return rec

    def acquire(self) -> bool:
        self.switch_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path(), "a+")
        chmod_private(self.lock_path())
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._lock = handle
        return True

    def release(self) -> None:
        handle = self._lock
        self._lock = None
        if handle is None:
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()

    def plan(self, *, source_pane: str, source_provider: str,
             target_pane: str, target_provider: str,
             authorized: bool = True) -> dict:
        if authorized is not True:
            return {"ok": False, "reason": "not_authorized"}
        src_pane = canonical_pane(source_pane)
        dst_pane = canonical_pane(target_pane)
        src_prov = canonical_provider(source_provider)
        dst_prov = canonical_provider(target_provider)
        if src_pane is None or dst_pane is None:
            return {"ok": False, "reason": "invalid_pane"}
        if src_prov is None or dst_prov is None:
            return {"ok": False, "reason": "invalid_provider"}
        if src_pane == dst_pane:
            return {"ok": False, "reason": "competing_owner_same_pane"}
        existing = self.load()
        if existing and existing.get("stage") not in ("complete", "failed"):
            return {"ok": False, "reason": "switch_in_progress", "id": existing.get("id")}
        observed = observe(self.board)
        default_executor = observed.get("default_executor")
        nonce = secrets.token_hex(16)
        switch_id = "osw-" + sha256_text("%s|%s|%s" % (
            self.board_data_dir, nonce, self._now()))[:16]
        locator = locator_from_dir(self.board_data_dir)
        rec = {
            "id": switch_id,
            "stage": "prepared",
            "nonce": nonce,
            "authorized": True,
            "protocol": "tmux-send",
            "providers": list(PROVIDERS),
            "board": locator,
            "project_root": str(self.project_root),
            "preserve": {
                "data_dir": str(self.board_data_dir.resolve()),
                "project_root": str(self.project_root),
                "default_executor": default_executor,
                "cards": observed["cards"],
            },
            "source": {"pane": src_pane, "provider": src_prov, "role": "main"},
            "target": {"pane": dst_pane, "provider": dst_prov, "role": "main"},
            "cursor": {
                "seq": observed["cursor_seq"],
                "source": "board",
                "never_skip_to_head": True,
            },
            "head_at_plan": observed["head_seq"],
            "ownership": {
                "source_provider": src_prov,
                "target_provider": dst_prov,
                "applied": False,
                "current_provider": src_prov,
            },
            "sends": {},
            "receipts": {},
            "pending_since_plan": False,
            "dispatch": {
                "path": str(self.board_data_dir.resolve() / DISPATCH_NAME),
                "lock": str(self.board_data_dir.resolve() / DISPATCH_LOCK_NAME),
                "at_plan": snapshot_dispatch(
                    read_json(self.board_data_dir / DISPATCH_NAME, default=None)),
            },
            "created_at": self._now(),
        }
        self._save(rec)
        _write_private(self.switch_dir / "nonce", {"switch_id": switch_id, "nonce": nonce})
        return {"ok": True, "send_allowed": False, **rec}

    def status(self) -> dict:
        rec = self.load()
        if rec is None:
            return {"ok": False, "reason": "no_switch"}
        observed = observe(self.board)
        rec["pending_since_plan"] = int(observed["head_seq"]) > int(rec.get("head_at_plan") or 0)
        rec["observed_head"] = observed["head_seq"]
        rec["observed_cursor"] = observed["cursor_seq"]
        rec["observed_default_executor"] = observed.get("default_executor")
        rec["observed_owner"] = observed.get("owner") or {}
        rec["cards_unchanged"] = observed["cards"] == rec.get("preserve", {}).get("cards")
        live = snapshot_dispatch(read_json(self.board_data_dir / DISPATCH_NAME, default=None))
        rec["dispatch_live"] = live
        rec["next_action"] = next_action(rec)
        return {"ok": True, **rec}

    def advance(self) -> dict:
        rec = self.load()
        if rec is None:
            return {"ok": False, "reason": "no_switch"}
        stage = rec.get("stage")
        if stage == "complete":
            return {"ok": True, "send_allowed": False, **rec}
        if stage == "failed":
            return {"ok": False, "reason": rec.get("fail_reason") or "failed", **rec}
        observed = observe(self.board)
        rec["pending_since_plan"] = int(observed["head_seq"]) > int(rec.get("head_at_plan") or 0)
        rec["observed_head"] = observed["head_seq"]
        self._ingest_receipts(rec)
        if stage == "prepared":
            return self._advance_prepared(rec, observed)
        if stage == "source_quiesced":
            return self._advance_source_quiesced(rec)
        if stage == "target_registered":
            return self._advance_target_registered(rec, observed)
        if stage == "target_acknowledged":
            rec["stage"] = "complete"
            self._save(rec)
            return {"ok": True, "send_allowed": False, **rec}
        return {"ok": False, "reason": "unknown_stage", **rec}

    def _advance_prepared(self, rec: dict, observed: dict) -> dict:
        send = rec.get("sends", {}).get("source_quiesce") or {}
        if send.get("status") in ("uncertain",):
            self._save(rec)
            return {"ok": False, "send_allowed": False, "reason": "uncertain", **rec}
        if send.get("status") not in ("delivered",):
            return self._send(rec, "source_quiesce", rec["source"]["pane"],
                              _source_prompt(rec, self.switch_dir))
        if rec.get("receipts", {}).get("source"):
            abandon = rec["receipts"]["source"].get("abandon_inflight") is True
            rebound = rebind_dispatch_target(
                self.board_data_dir,
                source_pane=rec["source"]["pane"],
                target_pane=rec["target"]["pane"],
                board_cursor=int(observed["cursor_seq"]),
                abandon_inflight=abandon,
                source_stopped=True,
            )
            rec["dispatch_rebind"] = rebound
            if rebound.get("ok") is not True:
                self._save(rec)
                return {
                    "ok": False, "send_allowed": False,
                    "reason": rebound.get("reason"), **rec,
                }
            rec["stage"] = "source_quiesced"
            applied = self._apply_ownership(rec, observed)
            if applied.get("ok") is not True:
                rec["stage"] = "failed"
                rec["fail_reason"] = applied.get("reason") or "ownership_apply_failed"
                self._save(rec)
                return {"ok": False, "send_allowed": False, **rec}
            self._save(rec)
            return {"ok": True, "send_allowed": False, **rec}
        self._save(rec)
        return {"ok": True, "send_allowed": False, "reason": "await_source_ack", **rec}

    def _advance_source_quiesced(self, rec: dict) -> dict:
        send = rec.get("sends", {}).get("target_register") or {}
        if send.get("status") == "uncertain":
            self._save(rec)
            return {"ok": False, "send_allowed": False, "reason": "uncertain", **rec}
        if send.get("status") == "delivered":
            rec["stage"] = "target_registered"
            self._save(rec)
            return {"ok": True, "send_allowed": False, **rec}
        result = self._send(rec, "target_register", rec["target"]["pane"],
                            _target_prompt(rec, self.switch_dir))
        rec = self.load() or rec
        if ((rec.get("sends") or {}).get("target_register") or {}).get("status") == "delivered":
            rec["stage"] = "target_registered"
            self._save(rec)
            result = {**result, **rec, "ok": True, "send_allowed": False}
        return result

    def _advance_target_registered(self, rec: dict, observed: dict) -> dict:
        if rec.get("receipts", {}).get("target"):
            rec["stage"] = "target_acknowledged"
            self._save(rec)
            return {"ok": True, "send_allowed": False, **rec}
        self._save(rec)
        return {"ok": True, "send_allowed": False, "reason": "await_target_ack", **rec}

    def _send(self, rec: dict, name: str, pane: str, prompt: str) -> dict:
        existing = (rec.get("sends") or {}).get(name) or {}
        if existing.get("status") in ("delivered", "uncertain"):
            return {"ok": False, "send_allowed": False, "reason": existing.get("status"), **rec}
        prompt_path = self.switch_dir / "prompts" / ("%s.txt" % name)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(prompt_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
            handle.flush()
            os.fsync(handle.fileno())
        rec.setdefault("sends", {})[name] = {
            "status": "prepared",
            "pane": pane,
            "prompt_path": str(prompt_path),
        }
        self._save(rec)
        result = self.transport.send(pane, prompt_path)
        rec["sends"][name]["status"] = result.get("status")
        rec["sends"][name]["exit"] = result.get("exit")
        rec["sends"][name]["reason"] = result.get("reason")
        if result.get("status") == "blocked":
            rec["stage"] = "failed"
            rec["fail_reason"] = "send_blocked_%s" % name
        self._save(rec)
        ok = result.get("status") == "delivered"
        return {
            "ok": ok,
            "send_allowed": False,
            "reason": None if ok else result.get("status"),
            **rec,
        }

    def _apply_ownership(self, rec: dict, observed: dict) -> dict:
        default_executor = rec["preserve"].get("default_executor")
        if observed.get("default_executor") != default_executor:
            return {"ok": False, "reason": "default_executor_changed"}
        patch = {
            "coordinator_owner": {
                "provider": rec["target"]["provider"],
                "pane": rec["target"]["pane"],
                "switch_id": rec["id"],
            }
        }
        fn = getattr(self.board, "put_settings", None) or getattr(self.board, "apply_owner", None)
        if callable(fn):
            try:
                fn(patch)
            except Exception:
                return {"ok": False, "reason": "ownership_apply_failed"}
        rec["ownership"]["applied"] = True
        rec["ownership"]["current_provider"] = rec["target"]["provider"]
        rec["ownership"]["patch"] = patch
        return {"ok": True}

    def ack(self, *, role: str, switch_id: str, nonce: str,
            cursor_seq: int | None = None, abandon_inflight: bool = False) -> dict:
        rec = self.load()
        if rec is None:
            return {"ok": False, "reason": "no_switch"}
        if rec.get("id") != switch_id:
            return {"ok": False, "reason": "switch_id_mismatch"}
        if rec.get("nonce") != nonce:
            return {"ok": False, "reason": "nonce_mismatch"}
        if role not in ("source", "target"):
            return {"ok": False, "reason": "invalid_role"}
        if role == "source":
            send = (rec.get("sends") or {}).get("source_quiesce") or {}
            if send.get("status") != "delivered":
                return {"ok": False, "reason": "source_not_delivered"}
            rec.setdefault("receipts", {})["source"] = {
                "switch_id": switch_id, "role": "source", "at": self._now(),
                "abandon_inflight": abandon_inflight is True,
            }
            _write_private(self.switch_dir / "receipts" / "source.json",
                           rec["receipts"]["source"] | {"nonce": nonce})
            self._save(rec)
            return {"ok": True, **rec}
        send = (rec.get("sends") or {}).get("target_register") or {}
        if send.get("status") != "delivered" and rec.get("stage") != "target_registered":
            return {"ok": False, "reason": "target_not_delivered"}
        observed = observe(self.board)
        planned = int(rec["cursor"]["seq"])
        head = int(observed["head_seq"])
        claimed = planned if cursor_seq is None else int(cursor_seq)
        if claimed == head and head > planned:
            return {"ok": False, "reason": "skip_pending_events"}
        if claimed != planned:
            return {"ok": False, "reason": "cursor_mismatch"}
        rec.setdefault("receipts", {})["target"] = {
            "switch_id": switch_id, "role": "target", "at": self._now(),
            "cursor_seq": claimed,
        }
        _write_private(self.switch_dir / "receipts" / "target.json",
                       rec["receipts"]["target"] | {"nonce": nonce})
        self._save(rec)
        return {"ok": True, **rec}

    def _ingest_receipts(self, rec: dict) -> None:
        for role in ("source", "target"):
            path = self.switch_dir / "receipts" / ("%s.json" % role)
            payload = read_json(path, default=None)
            if not isinstance(payload, dict):
                continue
            if payload.get("switch_id") != rec.get("id"):
                continue
            if payload.get("nonce") != rec.get("nonce"):
                continue
            if payload.get("role") != role:
                continue
            rec.setdefault("receipts", {}).setdefault(role, {
                "switch_id": rec["id"], "role": role,
                "cursor_seq": payload.get("cursor_seq"),
                "abandon_inflight": payload.get("abandon_inflight") is True,
            })

    def rollback(self) -> dict:
        rec = self.load()
        if rec is None:
            return {"ok": False, "reason": "no_switch"}
        stage = rec.get("stage")
        if stage in ("target_acknowledged", "complete"):
            return {"ok": False, "reason": "target_active", **rec}
        observed = observe(self.board)
        observed_owner = (observed.get("owner") or {}).get("provider")
        current = rec.get("ownership", {}).get("current_provider")
        if observed_owner and observed_owner != current:
            return {"ok": False, "reason": "ownership_mismatch", **rec}
        if rec.get("ownership", {}).get("applied") is True:
            restore = {
                "coordinator_owner": {
                    "provider": rec["source"]["provider"],
                    "pane": rec["source"]["pane"],
                    "switch_id": rec["id"],
                    "rolled_back": True,
                }
            }
            fn = getattr(self.board, "put_settings", None) or getattr(self.board, "apply_owner", None)
            if callable(fn):
                try:
                    fn(restore)
                except Exception:
                    return {"ok": False, "reason": "rollback_apply_failed", **rec}
            rec["ownership"]["applied"] = False
            rec["ownership"]["current_provider"] = rec["source"]["provider"]
        rec["stage"] = "failed"
        rec["fail_reason"] = "rolled_back"
        if rec.get("dispatch_rebind", {}).get("ok") is True:
            restore = rebind_dispatch_target(
                self.board_data_dir,
                source_pane=rec["target"]["pane"],
                target_pane=rec["source"]["pane"],
                board_cursor=int((rec.get("cursor") or {}).get("seq") or 0),
                abandon_inflight=True,
                source_stopped=True,
            )
            rec["dispatch_rebind_rollback"] = restore
        self._save(rec)
        return {"ok": True, **rec}


def next_action(rec: dict) -> str:
    stage = rec.get("stage")
    src = rec.get("source") or {}
    dst = rec.get("target") or {}
    cursor = (rec.get("cursor") or {}).get("seq")
    executor = (rec.get("preserve") or {}).get("default_executor")
    if stage == "failed":
        return "Switch failed (%s). Inspect panes; do not kill workers." % (
            rec.get("fail_reason") or "failed",)
    if stage == "complete":
        return (
            "Target %s on pane %s owns the board at cursor %s. "
            "Default executor stays %s. dispatch.json target rebound; pending seqs kept. "
            "Root may start the persistent watcher. Module tests are not a live session test. "
            "Source task workers may keep running. No process kill required."
            % (dst.get("provider"), dst.get("pane"), cursor, executor)
        )
    send_src = (rec.get("sends") or {}).get("source_quiesce") or {}
    send_dst = (rec.get("sends") or {}).get("target_register") or {}
    if stage == "prepared":
        if send_src.get("status") == "uncertain":
            return ("Do not resend. Inspect source pane %s for switch %s."
                    % (src.get("pane"), rec.get("id")))
        if send_src.get("status") != "delivered":
            return (
                "Run sprint-handoff advance to tmux-send source quiesce to %s. "
                "Stop only the idle monitor. Do not kill task workers or panes."
                % src.get("pane")
            )
        return (
            "Wait for source ack: sprint-handoff ack --role source --switch-id %s "
            "--nonce-file nonce. Then advance."
            % rec.get("id")
        )
    if stage == "source_quiesced":
        if send_dst.get("status") == "uncertain":
            return ("Do not resend. Inspect target pane %s for switch %s."
                    % (dst.get("pane"), rec.get("id")))
        return (
            "Run sprint-handoff advance to tmux-send target registration to %s. "
            "Default executor stays %s. Do not kill source task workers."
            % (dst.get("pane"), executor)
        )
    if stage == "target_registered":
        return (
            "Wait for target ack bound to switch %s at cursor %s "
            "(never skip to head). Then advance."
            % (rec.get("id"), cursor)
        )
    if stage == "target_acknowledged":
        return (
            "Start the %s coordinator on pane %s at cursor %s. "
            "Stop only the old idle monitor. No automatic process kill."
            % (dst.get("provider"), dst.get("pane"), cursor)
        )
    return "Inspect sprint-handoff status."


def _source_prompt(rec: dict, switch_dir: Path) -> str:
    receipt = switch_dir / "receipts" / "source.json"
    nonce_path = switch_dir / "nonce"
    return "\n".join([
        "Coordinator owner switch %s." % rec["id"],
        "Role: source quiesce. Provider label: %s." % rec["source"]["provider"],
        "Stop only your idle board monitor and new dispatches.",
        "Keep running task workers, worktrees, and panes.",
        "Do not kill processes. Do not change worker.default_executor.",
        "Write receipt JSON to %s with switch_id, nonce from %s, role source."
        % (receipt, nonce_path),
        "If dispatch inflight is uncertain, either wait until the board cursor "
        "covers inflight.through or set abandon_inflight true on the receipt.",
        "No account secrets belong in this message or the receipt.",
        "",
    ])


def _target_prompt(rec: dict, switch_dir: Path) -> str:
    receipt = switch_dir / "receipts" / "target.json"
    nonce_path = switch_dir / "nonce"
    return "\n".join([
        "Coordinator owner switch %s." % rec["id"],
        "Role: target take ownership. Provider label: %s." % rec["target"]["provider"],
        "Read the orchestrator event cursor from the board. Planned seq is %s."
        % rec["cursor"]["seq"],
        "Never set the cursor to head to skip pending events.",
        "Preserve card assignments, worktrees, panes, and worker.default_executor=%s."
        % rec["preserve"].get("default_executor"),
        "Same tmux-send protocol for claude, codex, and grok.",
        "Write receipt JSON to %s with switch_id, nonce from %s, role target, cursor_seq."
        % (receipt, nonce_path),
        "No account secrets belong in this message or the receipt.",
        "",
    ])


def _parse_argv(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Portable coordinator owner switch. tmux-send only.")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--board-data-dir", required=True, type=Path)
    plan.add_argument("--project-root", type=Path, default=Path("."))
    plan.add_argument("--source-pane", required=True)
    plan.add_argument("--source-provider", required=True)
    plan.add_argument("--target-pane", required=True)
    plan.add_argument("--target-provider", required=True)
    plan.add_argument("--tmux-send", type=Path, default=DEFAULT_TMUX_SEND)
    plan.add_argument("--switch-dir", type=Path)
    for name in ("status", "advance", "rollback"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--board-data-dir", required=True, type=Path)
        cmd.add_argument("--tmux-send", type=Path, default=DEFAULT_TMUX_SEND)
        cmd.add_argument("--switch-dir", type=Path)
        cmd.add_argument("--project-root", type=Path, default=Path("."))
    ack = sub.add_parser("ack")
    ack.add_argument("--board-data-dir", required=True, type=Path)
    ack.add_argument("--switch-dir", type=Path)
    ack.add_argument("--role", required=True, choices=("source", "target"))
    ack.add_argument("--switch-id", required=True)
    ack.add_argument("--nonce")
    ack.add_argument("--nonce-file", type=Path)
    ack.add_argument("--cursor-seq", type=int)
    ack.add_argument("--abandon-inflight", action="store_true")
    ack.add_argument("--tmux-send", type=Path, default=DEFAULT_TMUX_SEND)
    ack.add_argument("--project-root", type=Path, default=Path("."))
    return parser.parse_args(argv)


def _switch_from_args(args, board, transport) -> OwnerSwitch:
    return OwnerSwitch(
        args.board_data_dir, board=board, transport=transport,
        project_root=getattr(args, "project_root", None),
        switch_dir=getattr(args, "switch_dir", None),
    )


def _load_board(board_data_dir: Path):
    from sprint_coordinator.board import BoardClient
    return BoardClient(board_data_dir)


def main(argv: list[str] | None = None, *, board=None, transport=None) -> int:
    args = _parse_argv(argv)
    if board is None:
        board = _load_board(args.board_data_dir)
    if transport is None:
        transport = TmuxSendTransport(getattr(args, "tmux_send", DEFAULT_TMUX_SEND))
    switch = _switch_from_args(args, board, transport)
    if not switch.acquire():
        print(json.dumps({"ok": False, "reason": "switch_lock_held"}, sort_keys=True))
        return 1
    try:
        if args.command == "plan":
            result = switch.plan(
                source_pane=args.source_pane, source_provider=args.source_provider,
                target_pane=args.target_pane, target_provider=args.target_provider)
        elif args.command == "status":
            result = switch.status()
        elif args.command == "advance":
            result = switch.advance()
        elif args.command == "rollback":
            result = switch.rollback()
        elif args.command == "ack":
            nonce = args.nonce
            if nonce is None and args.nonce_file:
                payload = read_json(args.nonce_file, default={}) or {}
                nonce = payload.get("nonce")
            if not nonce:
                result = {"ok": False, "reason": "nonce_missing"}
            else:
                result = switch.ack(
                    role=args.role, switch_id=args.switch_id, nonce=nonce,
                    cursor_seq=args.cursor_seq,
                    abandon_inflight=bool(getattr(args, "abandon_inflight", False)))
        else:
            result = {"ok": False, "reason": "unknown_command"}
    finally:
        switch.release()
    public = dict(result)
    if "nonce" in public:
        public["nonce"] = "<redacted>"
    print(json.dumps(public, sort_keys=True, default=str))
    return 0 if public.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
