from __future__ import annotations

import fcntl
from pathlib import Path

from sprint_coordinator.util import read_json


def dispatch_lock_held(board_data_dir: Path) -> bool:
    path = Path(board_data_dir) / "dispatch.lock"
    try:
        with open(path, "r+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def live_dispatcher(autoheal: dict) -> bool:
    return bool(autoheal.get("event_dispatcher"))


def takeover_report(autoheal: dict, board_data_dir: Path, mode: str,
                    acknowledge_autoheal_gap: bool = False) -> dict:
    """Explicit checks. This task never performs live takeover."""
    blockers = []
    warnings = []
    if not autoheal.get("event_dispatch_supported"):
        blockers.append("board lacks event_dispatch_supported; update sprintd")
    if live_dispatcher(autoheal) or dispatch_lock_held(board_data_dir):
        blockers.append("sprint-dispatch is live; stop it before active mode")
    tmux = autoheal.get("tmux_window")
    if mode == "active" and tmux and not autoheal.get("coordinator_supported"):
        msg = ("sprintd autoheal still keys off dispatch.json leases, not the "
               "coordinator lock; a registered tmux window can be revived by the hub")
        blockers.append(msg + "; finish the explicit ownership handoff before activation")
    dispatch_state = read_json(Path(board_data_dir) / "dispatch.json")
    if dispatch_state and mode == "active":
        warnings.append("dispatch.json is present; inspect it before claiming exclusive ownership")
    return {
        "ok": not blockers,
        "mode": mode,
        "blockers": blockers,
        "warnings": warnings,
        "event_dispatch_supported": bool(autoheal.get("event_dispatch_supported")),
        "tmux_window": tmux,
        "event_dispatcher": autoheal.get("event_dispatcher"),
        "live_takeover": False,
    }
