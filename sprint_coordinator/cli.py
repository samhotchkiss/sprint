from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from sprint_coordinator.config import (
    CoordinatorConfig, default_config_path, example_config, init_config, load_config,
)
from sprint_coordinator.driver import Coordinator
from sprint_coordinator.lock import held
from sprint_coordinator.store import Store
from sprint_coordinator.util import Clock


def _mode(args) -> str:
    if args.command in ("run", "run-once"):
        if bool(args.shadow) == bool(args.active):
            raise SystemExit("run and run-once require exactly one of --shadow or --active")
        return "shadow" if args.shadow else "active"
    return "shadow"


def _config(args) -> CoordinatorConfig:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.command == "init-config":
        raise AssertionError("init-config does not load")
    cfg = load_config(path)
    if args.project_root:
        cfg.project_root = Path(args.project_root).expanduser().resolve()
        if not args.data_dir and not cfg.raw.get("data_dir"):
            board = Path(args.board_data_dir).expanduser().resolve() if args.board_data_dir else (
                cfg.project_root / ".sprint")
            cfg.board_data_dir = board
            cfg.data_dir = board / "coordinator"
    if args.data_dir:
        cfg.data_dir = Path(args.data_dir).expanduser().resolve()
    if args.board_data_dir:
        cfg.board_data_dir = Path(args.board_data_dir).expanduser().resolve()
    return cfg


def cmd_init_config(args) -> int:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    project = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    if path.exists() and not args.force:
        print("config exists: %s" % path, file=sys.stderr)
        return 1
    if args.force and path.exists():
        path.unlink()
    init_config(path, project)
    print(json.dumps({
        "config": str(path),
        "project_root": str(project),
        "key_file": example_config()["jev"]["key_file"],
        "note": "put TYPESAFE_API_KEY in the key file (mode 0600); never commit it",
    }))
    return 0


def cmd_status(args) -> int:
    cfg = _config(args)
    db = cfg.data_dir / "coordinator.sqlite"
    out = {
        "config": str(cfg.path) if cfg.path else None,
        "data_dir": str(cfg.data_dir),
        "lock_held": held(cfg.data_dir),
        "db": db.exists(),
    }
    if db.exists():
        store = Store(db, Clock.live())
        try:
            out.update(store.snapshot())
        finally:
            store.close()
    print(json.dumps(out, default=str))
    return 0


def _build(args) -> Coordinator:
    cfg = _config(args)
    mode = _mode(args)
    if mode == "active":
        cfg.validate_active()
    coord = Coordinator(
        cfg, mode=mode, clock=Clock.live(),
        acknowledge_autoheal_gap=bool(args.acknowledge_autoheal_gap))
    return coord


def cmd_run_once(args) -> int:
    coord = _build(args)
    opened = coord.open()
    try:
        if coord.mode == "active":
            report = coord.check_takeover()
            if not report["ok"]:
                print(json.dumps({"takeover": report, "opened": opened}, default=str))
                return 2
        result = coord.tick(wait=True)
        result["opened"] = opened
        print(json.dumps(result, default=str))
        return 0
    finally:
        coord.close()


def cmd_run(args) -> int:
    coord = _build(args)
    opened = coord.open()
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if coord.mode == "active":
            report = coord.check_takeover()
            if not report["ok"]:
                print(json.dumps({"takeover": report, "opened": opened}, default=str))
                return 2
        while not stopping:
            coord.tick(wait=False)
            time.sleep(coord.config.poll_seconds)
        print(json.dumps({"stopped": True, "status": coord.status()}, default=str))
        return 0
    finally:
        coord.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Sprint coordinator. No model runs because a timer ticked.")
    parser.add_argument("command", choices=("init-config", "status", "run", "run-once"))
    parser.add_argument("--config", help="local coordinator.json (outside the repo is fine)")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--board-data-dir", type=Path)
    parser.add_argument("--shadow", action="store_true",
                        help="ingest and route only; do not publish to the board")
    parser.add_argument("--active", action="store_true",
                        help="publish replies after explicit takeover checks")
    parser.add_argument("--acknowledge-autoheal-gap", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init-config":
            return cmd_init_config(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "run-once":
            return cmd_run_once(args)
        return cmd_run(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print("sprint-coordinate: %s" % exc, file=sys.stderr)
        return 1
