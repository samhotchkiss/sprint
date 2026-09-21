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
OWNER_LOCK_NAME = "coordinator-owner.lock"
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


def _try_lock(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    chmod_private(path)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _unlock(handle) -> None:
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


def acquire_owner_locks(data_dir: Path):
    """Both locks, or neither. Held lock means the dispatcher/service is running."""
    data_dir = Path(data_dir)
    dispatch = _try_lock(data_dir / DISPATCH_LOCK_NAME)
    if dispatch is None:
        return None, "dispatcher_running"
    owner = _try_lock(data_dir / OWNER_LOCK_NAME)
    if owner is None:
        _unlock(dispatch)
        return None, "coordinator_running"
    return (dispatch, owner), None


def _rebind_unlocked(
    data_dir: Path, *, source_pane: str, target_pane: str, board_cursor: int,
    abandon_inflight: bool,
) -> dict:
    path = Path(data_dir) / DISPATCH_NAME
    state = read_json(path, default=None)
    if not isinstance(state, dict) or not state:
        return {
            "ok": True, "action": "no_dispatch_state", "rebind": None,
            "path": str(path), "lock": str(Path(data_dir) / DISPATCH_LOCK_NAME),
        }
    current = state.get("target")
    if current not in (source_pane, target_pane):
        return {"ok": False, "reason": "dispatch_target_mismatch", "target": current}
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
        "lock": str(Path(data_dir) / DISPATCH_LOCK_NAME),
        "before": before,
        "rebind": snapshot_dispatch(state),
    }


def rebind_dispatch_target(
    data_dir: Path, *, source_pane: str, target_pane: str, board_cursor: int,
    abandon_inflight: bool, source_stopped: bool,
) -> dict:
    """Point dispatch.json at the new pane under dispatch.lock + owner lock."""
    if source_stopped is not True:
        return {"ok": False, "reason": "source_not_stopped"}
    pair, err = acquire_owner_locks(data_dir)
    if err:
        return {"ok": False, "reason": err}
    try:
        return _rebind_unlocked(
            data_dir, source_pane=source_pane, target_pane=target_pane,
            board_cursor=board_cursor, abandon_inflight=abandon_inflight)
    finally:
        for handle in pair:
            _unlock(handle)


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
    head = _board_head(board, board_payload)
    pane = settings_raw.get("session_tmux_window") or settings.get("session_tmux_window")
    try:
        autoheal = _board_get(board, "/api/autoheal")
    except RuntimeError:
        autoheal = {}
    return {
        "settings": settings,
        "session_tmux_window": pane,
        "default_executor": worker.get("default_executor"),
        "cards": snapshot_cards(board_payload),
        "cursor_seq": cursor_seq,
        "head_seq": head,
        "owner_pane": canonical_pane(pane),
        "autoheal": autoheal if isinstance(autoheal, dict) else {},
    }


def _board_head(board, board_payload: dict) -> int:
    """GET /api/board uses `seq` for the log head, not `head`."""
    head = 0
    for key in ("seq", "head"):
        value = board_payload.get(key)
        if value is None:
            continue
        try:
            head = max(head, int(value))
        except (TypeError, ValueError):
            pass
    for event in board_payload.get("events") or ():
        try:
            head = max(head, int(event.get("seq") or 0))
        except (TypeError, ValueError):
            pass
    if callable(getattr(board, "head_seq", None)):
        try:
            head = max(head, int(board.head_seq()))
        except (TypeError, ValueError):
            pass
    if head > 0:
        return head
    try:
        page = _board_get(board, "/api/events?after=0&limit=1")
        if page.get("head") is not None:
            head = max(head, int(page["head"]))
    except (RuntimeError, TypeError, ValueError, AssertionError):
        pass
    return head


def _bind_source_cursor(rec: dict, observed: dict) -> None:
    """Accept older source receipts that omitted cursor_seq."""
    src = (rec.get("receipts") or {}).get("source")
    if not src and not rec.get("source_exit"):
        return
    live = int(observed.get("cursor_seq") or 0)
    if src is not None and src.get("cursor_seq") is None:
        src["cursor_seq"] = live
    if rec.get("cursor", {}).get("start_seq") is None:
        rec.setdefault("cursor", {})
        rec["cursor"]["source_acked_seq"] = live
        rec["cursor"]["start_seq"] = live


def registered_pane(observed: dict) -> str | None:
    return canonical_pane(observed.get("session_tmux_window") or observed.get("owner_pane"))


def public_payload(value):
    """Drop secrets from CLI output. Nonce stays in the private file."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "nonce":
                out[key] = "<redacted>"
            else:
                out[key] = public_payload(item)
        return out
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    return value


def _freeze_prepared_sends(rec: dict) -> None:
    """A crash after durable 'prepared' is uncertain. Never retry the send."""
    for send in (rec.get("sends") or {}).values():
        if isinstance(send, dict) and send.get("status") == "prepared":
            send["status"] = "uncertain"
            send["reason"] = send.get("reason") or "crash_after_prepare"


def _start_seq(rec: dict) -> int:
    cursor = rec.get("cursor") or {}
    for key in ("start_seq", "source_acked_seq", "seq"):
        if cursor.get(key) is not None:
            return int(cursor[key])
    return 0


def _validate_target_cursor(rec: dict, claimed, observed: dict):
    start = _start_seq(rec)
    head = int(observed.get("head_seq") or 0)
    live = int(observed.get("cursor_seq") or 0)
    if claimed is None:
        claimed = start
    else:
        claimed = int(claimed)
    if registered_pane(observed) != rec["target"]["pane"]:
        return None, "owner_changed"
    if live != start:
        return None, "live_cursor_mismatch"
    if claimed == head and head > start:
        return None, "skip_pending_events"
    if claimed != start:
        return None, "cursor_mismatch"
    return claimed, None


def inspect_target_health(rec: dict, observed: dict, data_dir: Path, now: float,
                          project_root: Path) -> tuple[bool, str | None]:
    """Complete only when the live dispatcher is on the target pane."""
    receipt = (rec.get("receipts") or {}).get("target")
    if not receipt:
        return False, "target_receipt_missing"
    # The receipt was validated before ingress started. Progress after that is
    # expected; do not reject a working target for advancing the cursor.
    if registered_pane(observed) != rec["target"]["pane"]:
        return False, "owner_changed"
    if receipt.get("cursor_seq") != _start_seq(rec):
        return False, "cursor_mismatch"
    if int(observed.get("cursor_seq") or 0) < _start_seq(rec):
        return False, "live_cursor_mismatch"
    dispatcher = (observed.get("autoheal") or {}).get("event_dispatcher")
    if not isinstance(dispatcher, dict):
        return False, "await_target_health"
    if dispatcher.get("target") != rec["target"]["pane"]:
        return False, "dispatcher_target_mismatch"
    live = snapshot_dispatch(read_json(Path(data_dir) / DISPATCH_NAME, default=None))
    if live:
        if live.get("target") != rec["target"]["pane"]:
            return False, "dispatch_json_target_mismatch"
        try:
            age = now - float(live.get("heartbeat_at"))
        except (TypeError, ValueError):
            age = None
        if age is not None and not (0 <= age <= 30):
            return False, "dispatcher_stale"
        if live.get("project_root") not in (None, str(project_root)):
            return False, "dispatcher_project_mismatch"
    return True, None


def _target_send_started(rec: dict) -> bool:
    send = (rec.get("sends") or {}).get("target_register") or {}
    return send.get("status") in ("prepared", "uncertain", "delivered")


def _board_request(board, method: str, path: str, body=None):
    fn = getattr(board, "request", None)
    if not callable(fn):
        raise RuntimeError("board_request_missing")
    return fn(method, path, body)


TMUX_LIST_PANES = ["tmux", "list-panes", "-a", "-F", "#{pane_id}"]


class TmuxListPanesProbe:
    """Observe pane existence. Never kills or sends."""

    def existing_panes(self) -> dict:
        try:
            completed = subprocess.run(
                TMUX_LIST_PANES, capture_output=True, text=True, shell=False,
                timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "reason": "tmux_probe_failed", "panes": []}
        if completed.returncode != 0:
            return {"ok": False, "reason": "tmux_probe_failed", "panes": []}
        panes = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return {"ok": True, "panes": panes}


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
                 clock=None, pane_probe=None):
        self.board_data_dir = Path(board_data_dir)
        self.board = board
        self.transport = transport
        self.project_root = Path(project_root or ".").resolve()
        self.switch_dir = Path(switch_dir) if switch_dir else self.board_data_dir
        self.clock = clock
        self.pane_probe = pane_probe or TmuxListPanesProbe()
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
        observed = observe(self.board)
        if registered_pane(observed) != src_pane:
            return {"ok": False, "reason": "source_not_registered",
                    "registered": registered_pane(observed)}
        existing = self.load()
        if existing and existing.get("stage") not in ("complete", "failed"):
            return {"ok": False, "reason": "switch_in_progress", "id": existing.get("id")}
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
                "current_pane": src_pane,
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
        rec["observed_owner_pane"] = registered_pane(observed)
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
        rec["observed_cursor"] = observed["cursor_seq"]
        _freeze_prepared_sends(rec)
        owner_err = self._owner_mismatch(rec, observed)
        if owner_err:
            rec["fail_reason"] = owner_err
            self._save(rec)
            return {"ok": False, "send_allowed": False, "reason": owner_err, **rec}
        self._ingest_receipts(rec, observed)
        _bind_source_cursor(rec, observed)
        if stage == "prepared":
            return self._advance_prepared(rec, observed)
        if stage == "source_quiesced":
            return self._advance_source_quiesced(rec)
        if stage == "target_registered":
            return self._advance_target_registered(rec, observed)
        if stage == "target_acknowledged":
            ready, err = inspect_target_health(
                rec, observed, self.board_data_dir, self._now(), self.project_root)
            rec["health_error"] = err
            if ready is not True:
                self._save(rec)
                return {
                    "ok": False, "send_allowed": False,
                    "reason": err or "await_target_health", **rec,
                }
            rec["stage"] = "complete"
            self._save(rec)
            return {"ok": True, "send_allowed": False, **rec}
        return {"ok": False, "reason": "unknown_stage", **rec}

    def _owner_mismatch(self, rec: dict, observed: dict) -> str | None:
        reg = registered_pane(observed)
        expected = rec["target"]["pane"] if rec.get("ownership", {}).get("applied") is True else rec["source"]["pane"]
        if reg != expected:
            return "owner_changed"
        return None

    def _advance_prepared(self, rec: dict, observed: dict) -> dict:
        send = rec.get("sends", {}).get("source_quiesce") or {}
        if rec.get("receipts", {}).get("source") or rec.get("source_exit"):
            transferred = self._transfer_after_source_receipt(rec, observed)
            if transferred.get("ok") is not True:
                rec["fail_reason"] = transferred.get("reason")
                self._save(rec)
                return {
                    "ok": False, "send_allowed": False,
                    "reason": transferred.get("reason"), **rec,
                }
            rec["stage"] = "source_quiesced"
            self._save(rec)
            return {"ok": True, "send_allowed": False, **rec}
        if send.get("status") == "uncertain":
            self._save(rec)
            return {"ok": False, "send_allowed": False, "reason": "uncertain", **rec}
        if send.get("status") != "delivered":
            return self._send(rec, "source_quiesce", rec["source"]["pane"],
                              _source_prompt(rec, self.switch_dir))
        self._save(rec)
        return {"ok": True, "send_allowed": False, "reason": "await_source_ack", **rec}

    def _transfer_after_source_receipt(self, rec: dict, observed: dict) -> dict:
        pair, err = acquire_owner_locks(self.board_data_dir)
        if err:
            return {"ok": False, "reason": err}
        try:
            live = observe(self.board)
            if registered_pane(live) != rec["source"]["pane"]:
                return {"ok": False, "reason": "owner_changed"}
            src_receipt = (rec.get("receipts") or {}).get("source") or {}
            abandon = src_receipt.get("abandon_inflight") is True or bool(rec.get("source_exit"))
            rebound = _rebind_unlocked(
                self.board_data_dir,
                source_pane=rec["source"]["pane"],
                target_pane=rec["target"]["pane"],
                board_cursor=int(live["cursor_seq"]),
                abandon_inflight=abandon,
            )
            rec["dispatch_rebind"] = rebound
            if rebound.get("ok") is not True:
                return rebound
            return self._apply_ownership(rec, live)
        finally:
            for handle in pair:
                _unlock(handle)

    def _advance_source_quiesced(self, rec: dict) -> dict:
        send = rec.get("sends", {}).get("target_register") or {}
        if rec.get("receipts", {}).get("target") and send.get("status") in (
                "delivered", "uncertain"):
            rec["stage"] = "target_registered"
            self._save(rec)
            return {"ok": True, "send_allowed": False, **rec}
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
        # Crash after this save: status stays prepared, later frozen to uncertain.
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
        pane = rec["target"]["pane"]
        body = {"session_tmux_window": pane, "actor": "session"}
        try:
            _board_request(self.board, "PUT", "/api/settings", body)
        except Exception:
            return {"ok": False, "reason": "ownership_apply_failed"}
        back = observe(self.board)
        if registered_pane(back) != pane:
            return {"ok": False, "reason": "ownership_readback_mismatch",
                    "registered": registered_pane(back)}
        rec["ownership"]["applied"] = True
        rec["ownership"]["current_pane"] = pane
        rec["ownership"]["patch"] = body
        return {"ok": True}

    def acknowledge_exited_source(self) -> dict:
        """Record that the recorded source pane is gone. No receipt is invented."""
        rec = self.load()
        if rec is None:
            return {"ok": False, "reason": "no_switch"}
        if rec.get("stage") not in ("prepared", "source_quiesced"):
            return {"ok": False, "reason": "wrong_stage", **rec}
        pane = rec["source"]["pane"]
        probe = self.pane_probe.existing_panes()
        if probe.get("ok") is not True:
            return {"ok": False, "reason": probe.get("reason") or "tmux_probe_failed"}
        if pane in (probe.get("panes") or []):
            return {"ok": False, "reason": "source_pane_live", "pane": pane}
        pair, err = acquire_owner_locks(self.board_data_dir)
        if err:
            return {"ok": False, "reason": err}
        for handle in pair:
            _unlock(handle)
        observed = observe(self.board)
        rec["source_exit"] = {
            "pane": pane,
            "absent": True,
            "verified_by": list(TMUX_LIST_PANES),
            "at": self._now(),
            "cursor_seq": int(observed["cursor_seq"]),
        }
        rec["cursor"]["source_acked_seq"] = int(observed["cursor_seq"])
        rec["cursor"]["start_seq"] = int(observed["cursor_seq"])
        self._save(rec)
        return {"ok": True, **rec}

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
        _freeze_prepared_sends(rec)
        observed = observe(self.board)
        if role == "source":
            send = (rec.get("sends") or {}).get("source_quiesce") or {}
            if send.get("status") not in ("delivered", "uncertain"):
                return {"ok": False, "reason": "source_not_delivered"}
            rec.setdefault("receipts", {})["source"] = {
                "switch_id": switch_id, "role": "source", "at": self._now(),
                "abandon_inflight": abandon_inflight is True,
                "cursor_seq": int(observed["cursor_seq"]),
            }
            rec["cursor"]["source_acked_seq"] = int(observed["cursor_seq"])
            rec["cursor"]["start_seq"] = int(observed["cursor_seq"])
            _write_private(self.switch_dir / "receipts" / "source.json",
                           rec["receipts"]["source"] | {"nonce": nonce})
            self._save(rec)
            return {"ok": True, **rec}
        send = (rec.get("sends") or {}).get("target_register") or {}
        if send.get("status") not in ("delivered", "uncertain") and rec.get("stage") != "target_registered":
            return {"ok": False, "reason": "target_not_delivered"}
        claimed, err = _validate_target_cursor(rec, cursor_seq, observed)
        if err:
            return {"ok": False, "reason": err}
        rec.setdefault("receipts", {})["target"] = {
            "switch_id": switch_id, "role": "target", "at": self._now(),
            "cursor_seq": claimed,
        }
        _write_private(self.switch_dir / "receipts" / "target.json",
                       rec["receipts"]["target"] | {"nonce": nonce})
        self._save(rec)
        return {"ok": True, **rec}

    def _ingest_receipts(self, rec: dict, observed: dict) -> None:
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
            if role == "target":
                if (rec.get("stage") in ("target_acknowledged", "complete")
                        and rec.get("receipts", {}).get("target")):
                    continue
                claimed, err = _validate_target_cursor(
                    rec, payload.get("cursor_seq"), observed)
                if err:
                    rec["receipt_error"] = err
                    continue
                rec.setdefault("receipts", {})[role] = {
                    "switch_id": rec["id"], "role": role,
                    "cursor_seq": claimed,
                    "abandon_inflight": payload.get("abandon_inflight") is True,
                }
                continue
            rec.setdefault("receipts", {}).setdefault(role, {
                "switch_id": rec["id"], "role": role,
                "cursor_seq": payload.get("cursor_seq"),
                "abandon_inflight": payload.get("abandon_inflight") is True,
                "at": payload.get("at"),
            })
            _bind_source_cursor(rec, observed)

    def rollback(self) -> dict:
        rec = self.load()
        if rec is None:
            return {"ok": False, "reason": "no_switch"}
        stage = rec.get("stage")
        if stage in ("target_acknowledged", "complete") or _target_send_started(rec):
            return {"ok": False, "reason": "target_active", **rec}
        observed = observe(self.board)
        expected = rec["target"]["pane"] if rec.get("ownership", {}).get("applied") is True else rec["source"]["pane"]
        if registered_pane(observed) != expected:
            return {"ok": False, "reason": "ownership_mismatch", **rec}
        if rec.get("ownership", {}).get("applied") is True:
            body = {"session_tmux_window": rec["source"]["pane"], "actor": "session"}
            try:
                _board_request(self.board, "PUT", "/api/settings", body)
            except Exception:
                return {"ok": False, "reason": "rollback_apply_failed", **rec}
            back = observe(self.board)
            if registered_pane(back) != rec["source"]["pane"]:
                return {"ok": False, "reason": "rollback_readback_mismatch", **rec}
            rec["ownership"]["applied"] = False
            rec["ownership"]["current_pane"] = rec["source"]["pane"]
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
    cursor = _start_seq(rec)
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
            "--nonce-file nonce. If the source pane is gone after shutdown, "
            "sprint-handoff acknowledge-exited-source (tmux list-panes only; no kill). "
            "Then advance."
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
            "Root: install/start the target ingress watcher on pane %s. "
            "Then sprint-handoff advance to verify /api/autoheal event_dispatcher "
            "target, held fresh lock, and target receipt at cursor %s. "
            "Do not treat module tests as a live demo."
            % (dst.get("pane"), cursor)
        )
    return "Inspect sprint-handoff status."


def _source_prompt(rec: dict, switch_dir: Path) -> str:
    receipt = switch_dir / "receipts" / "source.json"
    nonce_path = switch_dir / "nonce"
    return "\n".join([
        "Coordinator owner switch %s." % rec["id"],
        "Project root: %s" % rec.get("project_root"),
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
        "Project root: %s" % rec.get("project_root"),
        "Role: target take ownership. Provider label: %s." % rec["target"]["provider"],
        "First acknowledge this handoff at the source cursor, before handling pending events.",
        "Then end your turn. The operator starts sprint-dispatch-service to wake you for pending work.",
        "Do not start a Monitor, polling loop, or a second watcher.",
        "Read the orchestrator event cursor from the board. Start at seq %s "
        "(latest fully handled source cursor). Do not skip to head."
        % _start_seq(rec),
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
    exited = sub.add_parser("acknowledge-exited-source")
    exited.add_argument("--board-data-dir", required=True, type=Path)
    exited.add_argument("--switch-dir", type=Path)
    exited.add_argument("--project-root", type=Path, default=Path("."))
    exited.add_argument("--tmux-send", type=Path, default=DEFAULT_TMUX_SEND)
    return parser.parse_args(argv)


def _switch_from_args(args, board, transport, pane_probe=None) -> OwnerSwitch:
    return OwnerSwitch(
        args.board_data_dir, board=board, transport=transport,
        project_root=getattr(args, "project_root", None),
        switch_dir=getattr(args, "switch_dir", None),
        pane_probe=pane_probe,
    )


def _load_board(board_data_dir: Path):
    from sprint_coordinator.board import BoardClient
    return BoardClient(board_data_dir)


def main(argv: list[str] | None = None, *, board=None, transport=None,
         pane_probe=None) -> int:
    args = _parse_argv(argv)
    if board is None:
        board = _load_board(args.board_data_dir)
    if transport is None:
        transport = TmuxSendTransport(getattr(args, "tmux_send", DEFAULT_TMUX_SEND))
    switch = _switch_from_args(args, board, transport, pane_probe=pane_probe)
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
        elif args.command == "acknowledge-exited-source":
            result = switch.acknowledge_exited_source()
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
    public = public_payload(result)
    print(json.dumps(public, sort_keys=True, default=str))
    return 0 if public.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
