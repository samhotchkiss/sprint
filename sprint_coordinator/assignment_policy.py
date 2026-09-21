"""Server-boundary gate for POST /api/cards/:n/assign.

Default builder is settings.worker.default_executor. A different provider or
model needs a case-specific reason and Jev evaluate_provider_override. Same
default does not call a model. Approvals are per assignment fingerprint; a
changed card/model/reason/default cannot reuse an older allow.

Root wires this at the mutation. This module does not edit sprintd.
Existing live assignments (existing=True) are not gated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any, Callable, Mapping

from sprint_coordinator.jev import (
    JevClient, JevError, VerificationOutcome, evaluate_provider_override, load_api_key,
)
from sprint_coordinator.util import canonical_json, chmod_private, sha256_text


PROVIDERS = ("grok", "claude", "codex")
CONFIG_NAME = "assignment-policy.json"
DB_NAME = "assignment-policy.sqlite"
DEFAULT_KEY_FILE = "~/.config/sprint/typesafe.env"
DEFAULT_DAILY_MAX = 200
ACTIONS = ("allow", "deny", "unavailable")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  day TEXT PRIMARY KEY,
  calls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  nonce TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  card_num INTEGER NOT NULL,
  event TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_assignment_fp ON audit (assignment_id, fingerprint, id);
"""


@dataclass(frozen=True)
class AssignmentGate:
    action: str
    reason: str
    fingerprint: str
    nonce: str
    snapshot: Mapping[str, Any] = field(default_factory=dict)
    identity: Mapping[str, Any] = field(default_factory=dict)
    judgments: Mapping[str, float] = field(default_factory=dict)
    usage_tokens: int = 0
    decision_id: int | None = None

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "allowed": self.allowed is True,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "nonce": self.nonce,
            "snapshot": dict(self.snapshot),
            "identity": dict(self.identity),
            "judgments": dict(self.judgments),
            "usage_tokens": int(self.usage_tokens),
            "decision_id": self.decision_id,
        }


def _label(value: Any) -> str:
    return str(value or "").strip().lower()


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _reason_from_payload(payload: Mapping[str, Any]) -> str:
    for key in ("override_reason", "model_reason"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def requested_identity(settings: Mapping[str, Any], payload: Mapping[str, Any]) -> dict:
    worker = (settings or {}).get("worker") or {}
    default_provider = _label(worker.get("default_executor"))
    executors = worker.get("executors") if isinstance(worker.get("executors"), dict) else {}
    requested_provider = _label(payload.get("executor") or payload.get("provider") or default_provider)
    default_spec = executors.get(default_provider) if isinstance(executors.get(default_provider), dict) else {}
    requested_spec = executors.get(requested_provider) if isinstance(executors.get(requested_provider), dict) else {}
    default_model = _label(default_spec.get("model") or worker.get("model") or "")
    requested_model = _label(payload.get("model") if payload.get("model") is not None else requested_spec.get("model") or default_model)
    return {
        "default_provider": default_provider,
        "default_model": default_model,
        "requested_provider": requested_provider,
        "requested_model": requested_model,
        "reason": _reason_from_payload(payload),
    }


def is_default_request(identity: Mapping[str, Any]) -> bool:
    return (
        _label(identity.get("default_provider")) == _label(identity.get("requested_provider"))
        and _label(identity.get("default_model")) == _label(identity.get("requested_model"))
    )


def fingerprint_for(*, card_num: int, assignment_id: str, identity: Mapping[str, Any]) -> str:
    body = {
        "card_num": int(card_num),
        "assignment_id": str(assignment_id),
        "default_provider": _label(identity.get("default_provider")),
        "default_model": _label(identity.get("default_model")),
        "requested_provider": _label(identity.get("requested_provider")),
        "requested_model": _label(identity.get("requested_model")),
        "reason": str(identity.get("reason") or ""),
    }
    return sha256_text(canonical_json(body))


def load_config(data_dir: Path) -> dict | None:
    path = Path(data_dir) / CONFIG_NAME
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    return {
        "enabled": True,
        "key_file": str(raw.get("key_file") or DEFAULT_KEY_FILE),
        "daily_max_calls": int(raw.get("daily_max_calls") or DEFAULT_DAILY_MAX),
    }


def _connect(data_dir: Path) -> sqlite3.Connection:
    path = Path(data_dir) / DB_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    chmod_private(path)
    return conn


def _write_audit(conn: sqlite3.Connection, *, nonce: str, fingerprint: str,
                 assignment_id: str, card_num: int, event: str,
                 snapshot: Mapping[str, Any], detail: Mapping[str, Any],
                 created_at: float, row_id: int | None = None) -> int:
    if row_id is not None:
        conn.execute(
            "UPDATE audit SET event=?, detail_json=? WHERE id=?",
            (event, canonical_json(detail), int(row_id)),
        )
        return int(row_id)
    cur = conn.execute(
        "INSERT INTO audit(nonce, fingerprint, assignment_id, card_num, event, "
        "snapshot_json, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (nonce, fingerprint, assignment_id, int(card_num), event,
         canonical_json(snapshot), canonical_json(detail), created_at),
    )
    return int(cur.lastrowid)


def _latest_event(conn: sqlite3.Connection, assignment_id: str, fingerprint: str) -> str | None:
    row = conn.execute(
        "SELECT event FROM audit WHERE assignment_id=? AND fingerprint=? ORDER BY id DESC LIMIT 1",
        (assignment_id, fingerprint),
    ).fetchone()
    return None if row is None else str(row["event"])


def _gate(action: str, reason: str, *, nonce: str, fingerprint: str,
          snapshot: Mapping[str, Any], identity: Mapping[str, Any],
          judgments: Mapping[str, float] | None = None, usage_tokens: int = 0,
          decision_id: int | None = None) -> AssignmentGate:
    if action not in ACTIONS:
        action = "unavailable"
        reason = "unknown_action"
    return AssignmentGate(
        action=action,
        reason=reason,
        fingerprint=fingerprint,
        nonce=nonce,
        snapshot=dict(snapshot),
        identity=dict(identity),
        judgments=dict(judgments or {}),
        usage_tokens=int(usage_tokens or 0),
        decision_id=decision_id,
    )


def _reserve(conn: sqlite3.Connection, daily_max: int, ts: float) -> str | None:
    day = _day(ts)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT calls FROM usage WHERE day=?", (day,)).fetchone()
    used = int(row["calls"]) if row else 0
    if used >= int(daily_max):
        conn.rollback()
        return "assignment_policy_daily_cap"
    conn.execute(
        "INSERT INTO usage(day, calls) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET calls=calls+1",
        (day,),
    )
    conn.commit()
    return None


def _outcome_action(outcome: Any) -> tuple[str, str, dict, int]:
    if isinstance(outcome, VerificationOutcome):
        return (
            str(outcome.action),
            str(outcome.reason),
            dict(outcome.judgments or {}),
            int(outcome.usage_tokens or 0),
        )
    if isinstance(outcome, dict):
        action = str(outcome.get("action") or "")
        reason = str(outcome.get("reason") or "")
        judgments = outcome.get("judgments") if isinstance(outcome.get("judgments"), dict) else {}
        tokens = int(outcome.get("usage_tokens") or 0)
        return action, reason, {k: float(v) for k, v in judgments.items()
                                if isinstance(v, (int, float)) and not isinstance(v, bool)}, tokens
    raise TypeError("unknown_override_outcome")


def gate_assignment(
    data_dir: str | Path,
    *,
    settings: Mapping[str, Any],
    card_num: int,
    payload: Mapping[str, Any],
    assignment_id: str | None = None,
    task: str = "",
    evidence: Mapping[str, Any] | None = None,
    existing: bool = False,
    client: Any = None,
    evaluate: Callable[..., Any] | None = None,
    now: float | None = None,
) -> AssignmentGate:
    """Return allow/deny/unavailable. Only action==allow may mutate assign.

    Root calls this at POST /api/cards/:n/assign (new assignments) and may
    revalidate the same assignment_id + fingerprint before write.
    """
    ts = _now(now)
    nonce = secrets.token_hex(16)
    identity = requested_identity(settings, payload or {})
    aid = str(assignment_id or payload.get("assignment_id") or ("pending:" + nonce))
    identity = dict(identity, card_num=int(card_num), assignment_id=aid)
    fp = fingerprint_for(card_num=int(card_num), assignment_id=aid, identity=identity)
    snapshot = {
        "card_num": int(card_num),
        "assignment_id": aid,
        "default_provider": identity["default_provider"],
        "default_model": identity["default_model"],
        "requested_provider": identity["requested_provider"],
        "requested_model": identity["requested_model"],
        "reason": identity["reason"],
        "task": str(task or payload.get("task") or payload.get("title") or ""),
        "existing": bool(existing),
    }
    try:
        config = load_config(Path(data_dir))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _gate("unavailable", "assignment_policy_config_invalid", nonce=nonce,
                     fingerprint=fp, snapshot=snapshot, identity=identity)
    if config is None:
        return _gate("allow", "assignment_policy_disabled", nonce=nonce,
                     fingerprint=fp, snapshot=snapshot, identity=identity)
    conn = _connect(Path(data_dir))
    try:
        if existing is True:
            decision_id = _write_audit(
                conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                card_num=int(card_num), event="preserved", snapshot=snapshot,
                detail={"reason": "existing_assignment"}, created_at=ts)
            conn.commit()
            return _gate("allow", "existing_assignment_preserved", nonce=nonce,
                         fingerprint=fp, snapshot=snapshot, identity=identity,
                         decision_id=decision_id)
        if is_default_request(identity):
            decision_id = _write_audit(
                conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                card_num=int(card_num), event="passthrough", snapshot=snapshot,
                detail={"reason": "default_executor"}, created_at=ts)
            conn.commit()
            return _gate("allow", "default_executor", nonce=nonce, fingerprint=fp,
                         snapshot=snapshot, identity=identity, decision_id=decision_id)
        if not identity["reason"]:
            decision_id = _write_audit(
                conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                card_num=int(card_num), event="denied", snapshot=snapshot,
                detail={"reason": "override_reason_required"}, created_at=ts)
            conn.commit()
            return _gate("deny", "override_reason_required", nonce=nonce,
                         fingerprint=fp, snapshot=snapshot, identity=identity,
                         decision_id=decision_id)
        prior = _latest_event(conn, aid, fp)
        if prior == "allowed":
            decision_id = _write_audit(
                conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                card_num=int(card_num), event="revalidated", snapshot=snapshot,
                detail={"reason": "same_assignment_fingerprint"}, created_at=ts)
            conn.commit()
            return _gate("allow", "same_assignment_fingerprint", nonce=nonce,
                         fingerprint=fp, snapshot=snapshot, identity=identity,
                         decision_id=decision_id)
        cap = _reserve(conn, config["daily_max_calls"], ts)
        if cap:
            decision_id = _write_audit(
                conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                card_num=int(card_num), event="unavailable", snapshot=snapshot,
                detail={"reason": cap}, created_at=ts)
            conn.commit()
            return _gate("unavailable", cap, nonce=nonce, fingerprint=fp,
                         snapshot=snapshot, identity=identity, decision_id=decision_id)
        reserved_id = _write_audit(
            conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
            card_num=int(card_num), event="reserved", snapshot=snapshot,
            detail={"reason": "jev_reserved"}, created_at=ts)
        conn.commit()
        fn = evaluate
        if fn is None:
            fn = lambda c, **kw: evaluate_provider_override(c, **kw)
        jev = client
        if jev is None and evaluate is None:
            try:
                key = load_api_key(secret_file=Path(config["key_file"]).expanduser())
                jev = JevClient(key, timeout=5, max_attempts=1)
            except JevError:
                decision_id = _write_audit(
                    conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                    card_num=int(card_num), event="unavailable", snapshot=snapshot,
                    detail={"reason": "verifier_unavailable"},
                    created_at=ts, row_id=reserved_id)
                conn.commit()
                return _gate("unavailable", "verifier_unavailable", nonce=nonce,
                             fingerprint=fp, snapshot=snapshot, identity=identity,
                             decision_id=decision_id)
        try:
            outcome = fn(
                jev,
                task=snapshot["task"] or ("card %s" % card_num),
                default_provider=identity["default_provider"] or "grok",
                requested_provider=identity["requested_provider"],
                default_model=identity["default_model"] or identity["requested_model"],
                requested_model=identity["requested_model"],
                reason=identity["reason"],
                evidence=dict(evidence or payload.get("evidence") or {}),
            )
        except Exception:
            decision_id = _write_audit(
                conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                card_num=int(card_num), event="unavailable", snapshot=snapshot,
                detail={"reason": "verifier_unavailable"},
                created_at=ts, row_id=reserved_id)
            conn.commit()
            return _gate("unavailable", "verifier_unavailable", nonce=nonce,
                         fingerprint=fp, snapshot=snapshot, identity=identity,
                         decision_id=decision_id)
        try:
            action, jev_reason, judgments, tokens = _outcome_action(outcome)
        except (TypeError, ValueError):
            decision_id = _write_audit(
                conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
                card_num=int(card_num), event="unavailable", snapshot=snapshot,
                detail={"reason": "malformed_verifier_response"},
                created_at=ts, row_id=reserved_id)
            conn.commit()
            return _gate("unavailable", "malformed_verifier_response", nonce=nonce,
                         fingerprint=fp, snapshot=snapshot, identity=identity,
                         decision_id=decision_id)
        if action == "accept":
            event, gate_action, gate_reason = "allowed", "allow", "override_accepted"
        elif action == "deny":
            event, gate_action, gate_reason = "denied", "deny", jev_reason or "override_denied"
        else:
            event, gate_action, gate_reason = "unavailable", "unavailable", jev_reason or "override_uncertain"
        detail = {
            "jev_action": action,
            "jev_reason": jev_reason,
            "judgments": judgments,
            "usage_tokens": tokens,
        }
        decision_id = _write_audit(
            conn, nonce=nonce, fingerprint=fp, assignment_id=aid,
            card_num=int(card_num), event=event, snapshot=snapshot,
            detail=detail, created_at=ts, row_id=reserved_id)
        conn.commit()
        return _gate(gate_action, gate_reason, nonce=nonce, fingerprint=fp,
                     snapshot=snapshot, identity=identity, judgments=judgments,
                     usage_tokens=tokens, decision_id=decision_id)
    finally:
        conn.close()
