from __future__ import annotations

import json
import math
import os
from pathlib import Path


CONFIG_VERSION = 1
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_KEY_FILE = "~/.config/sprint/typesafe.env"
PROMPT_VERSION = "sprint-coordinator-route-v1"


def default_config_path() -> Path:
    return Path.home() / ".config" / "sprint" / "coordinator.json"


def default_install_dir() -> Path:
    return Path.home() / ".config" / "sprint"


def example_config(project_root: str | None = None) -> dict:
    root = str(Path(project_root or ".").resolve())
    return {
        "version": CONFIG_VERSION,
        "activation_ready": False,
        "project_root": root,
        "data_dir": None,
        "board_data_dir": None,
        "poll_seconds": 2.0,
        "concurrency": 3,
        "reserved_response_slots": 1,
        "daily_max_calls": 200,
        "daily_max_spend_usd": 5.0,
        "max_escalation_retries": 1,
        "routing_deadline_seconds": 2.0,
        "simple_reply_deadline_seconds": 10.0,
        "reply_deadline_seconds": 30.0,
        "worker_timeout_seconds": 120.0,
        "require_jev_for_approval": True,
        "jev": {
            "enabled": True,
            "model": DEFAULT_MODEL,
            "key_file": DEFAULT_KEY_FILE,
            "prompt_version": PROMPT_VERSION,
            "timeout_seconds": 20.0,
        },
        "workers": {
            "low": {
                "command": ["/configure/your/low-cost-worker-adapter"],
                "role": "response",
                "cost": "low",
            },
            "high": {
                "command": ["/configure/your/high-capacity-worker-adapter"],
                "role": "response",
                "cost": "high",
            },
        },
        "allowed_checks": [],
    }


class CoordinatorConfig:
    def __init__(self, raw: dict, path: Path | None = None):
        if not isinstance(raw, dict):
            raise ValueError("config must be a JSON object")
        self.raw = raw
        for key in ("activation_ready", "require_jev_for_approval"):
            if key in raw and type(raw[key]) is not bool:
                raise ValueError(key + " must be a boolean")
        self.activation_ready = raw.get("activation_ready", False)
        self.path = path
        self.version = int(raw.get("version", CONFIG_VERSION))
        if self.version != CONFIG_VERSION:
            raise ValueError("unsupported config version")
        self.project_root = Path(raw.get("project_root") or ".").expanduser().resolve()
        board = raw.get("board_data_dir")
        self.board_data_dir = (
            Path(board).expanduser().resolve()
            if board else self.project_root / ".sprint"
        )
        data = raw.get("data_dir")
        self.data_dir = (
            Path(data).expanduser().resolve()
            if data else self.board_data_dir / "coordinator"
        )
        self.poll_seconds = float(raw.get("poll_seconds") or 2.0)
        self.concurrency = int(raw.get("concurrency") or 3)
        self.reserved_response_slots = int(raw.get("reserved_response_slots") or 1)
        self.daily_max_calls = int(raw.get("daily_max_calls", 200))
        self.daily_max_spend_usd = float(raw.get("daily_max_spend_usd", 5.0))
        self.max_escalation_retries = int(raw.get("max_escalation_retries", 1))
        self.routing_deadline_seconds = float(raw.get("routing_deadline_seconds") or 2.0)
        self.simple_reply_deadline_seconds = float(
            raw.get("simple_reply_deadline_seconds") or 10.0)
        self.reply_deadline_seconds = float(raw.get("reply_deadline_seconds") or 30.0)
        self.worker_timeout_seconds = float(raw.get("worker_timeout_seconds") or 120.0)
        self.require_jev_for_approval = bool(raw.get("require_jev_for_approval", True))
        jev = raw.get("jev") if isinstance(raw.get("jev"), dict) else {}
        if "enabled" in jev and type(jev["enabled"]) is not bool:
            raise ValueError("jev.enabled must be a boolean")
        self.jev_enabled = bool(jev.get("enabled", True))
        self.jev_model = str(jev.get("model") or DEFAULT_MODEL)
        if self.jev_model != DEFAULT_MODEL:
            raise ValueError("jev.model must match the evaluated model " + DEFAULT_MODEL)
        self.jev_key_file = Path(jev.get("key_file") or DEFAULT_KEY_FILE).expanduser()
        self.jev_prompt_version = str(jev.get("prompt_version") or PROMPT_VERSION)
        self.jev_timeout_seconds = float(jev.get("timeout_seconds") or 20.0)
        workers = raw.get("workers") if isinstance(raw.get("workers"), dict) else {}
        self.workers = {}
        for name, spec in workers.items():
            if not isinstance(spec, dict):
                raise ValueError("worker %s must be an object" % name)
            command = spec.get("command")
            if not isinstance(command, list) or not command or not all(
                    isinstance(part, str) for part in command):
                raise ValueError("worker %s.command must be a non-empty string array" % name)
            if any(part == "" for part in command):
                raise ValueError("worker %s.command contains an empty argument" % name)
            self.workers[name] = {
                "command": list(command),
                "role": str(spec.get("role") or "response"),
                "cost": str(spec.get("cost") or name),
            }
        if "low" not in self.workers or "high" not in self.workers:
            raise ValueError("config.workers must define low and high adapters")
        checks = raw.get("allowed_checks") if isinstance(raw.get("allowed_checks"), list) else []
        self.allowed_checks = []
        for item in checks:
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError("allowed_checks entries need an id")
            command = item.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(p, str) and p for p in command):
                raise ValueError("allowed_checks.%s.command must be a string array" % item["id"])
            if any(c["id"] == str(item["id"]) for c in self.allowed_checks):
                raise ValueError("duplicate check id")
            cwd = (self.project_root / item.get("cwd", ".")).resolve()
            if not cwd.is_relative_to(self.project_root):
                raise ValueError("check cwd must be inside project root")
            timeout = float(item.get("timeout_seconds", 60))
            if not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
                raise ValueError("check timeout must be finite and within 600 seconds")
            self.allowed_checks.append({"id": str(item["id"]), "command": list(command),
                                       "cwd": str(cwd), "timeout_seconds": timeout})
        if self.daily_max_calls < 0 or not math.isfinite(self.daily_max_spend_usd) or self.daily_max_spend_usd < 0:
            raise ValueError("budgets must be finite and nonnegative")
        for name in ("routing_deadline_seconds", "simple_reply_deadline_seconds", "reply_deadline_seconds", "worker_timeout_seconds", "jev_timeout_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(name + " must be finite and positive")
        if not 1 <= self.concurrency <= 32:
            raise ValueError("concurrency must be 1–32")
        if not 0 <= self.reserved_response_slots <= self.concurrency:
            raise ValueError("reserved_response_slots must fit inside concurrency")
        if self.reserved_response_slots < 1:
            raise ValueError("reserved_response_slots must be at least 1")
        if not 0.05 <= self.poll_seconds <= 30:
            raise ValueError("poll_seconds must be 0.05–30")
        if self.max_escalation_retries < 0 or self.max_escalation_retries > 3:
            raise ValueError("max_escalation_retries must be 0–3")

    def validate_active(self):
        if not self.activation_ready:
            raise ValueError("configure real worker adapters and set activation_ready before active mode")
        for worker in self.workers.values():
            if any("fake_worker" in arg or arg.startswith("/configure/") for arg in worker["command"]):
                raise ValueError("example or fake workers cannot run in active mode")
        if not self.require_jev_for_approval:
            raise ValueError("active mode requires verification")

    @property
    def check_catalog(self) -> dict:
        return {item["id"]: item for item in self.allowed_checks}

    def worker(self, name: str) -> dict:
        spec = self.workers.get(name)
        if spec is None:
            raise KeyError(name)
        return spec


def load_config(path: Path) -> CoordinatorConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return CoordinatorConfig(raw, path=Path(path))


def write_json_private(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def init_config(path: Path, project_root: Path | None = None) -> Path:
    path = Path(path)
    if path.exists():
        raise FileExistsError("config already exists: %s" % path)
    write_json_private(path, example_config(str(project_root) if project_root else None))
    return path

