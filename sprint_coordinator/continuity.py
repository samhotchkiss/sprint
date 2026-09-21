"""Durable continuity inbox. Does not send, mutate the board, or call a model.

Root wires supervisor tmux-send and lease integration. This module only records
post-cutoff lifecycle, an explicit handoff snapshot, and durable delivery
intent. Uncertain deliveries stay visible and are never resent automatically.
"""

from __future__ import annotations

import json
import sqlite3

from sprint_coordinator.util import sha256_text


UNFINISHED_STATES = ("queued", "in_progress", "needs_you", "ready", "integrating")
HELD_STATES = ("held",)
NOISE_KINDS = ("heartbeat", "cursor", "progress", "phase")
USER_LIFECYCLE = ("answer", "verdict", "action", "retry")
WORKER_LIFECYCLE = ("evidence", "error", "question")
STATE_TO = ("queued", "ready", "integrating", "failed")
RESUME_VERDICTS = ("bounce",)
NOTICE_VERDICTS = ("approve", "reject", "cancel", "hold")
RESUME_ACTIONS = ("retry", "resume")
NOTICE_ACTIONS = ("cancel", "hold")

SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_snapshot (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cutoff_seq INTEGER NOT NULL,
  created_at REAL NOT NULL,
  cards_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS continuity_cards (
  card_num INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  agent_name TEXT,
  pane TEXT,
  worktree TEXT,
  branch TEXT,
  last_event_seq INTEGER,
  dispatch TEXT NOT NULL,
  held INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS continuity_items (
  seq INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  card_num INTEGER NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  intent TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  delivery_id TEXT,
  recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS continuity_deliveries (
  id TEXT PRIMARY KEY,
  card_num INTEGER NOT NULL,
  status TEXT NOT NULL,
  item_seqs_json TEXT NOT NULL,
  through_seq INTEGER NOT NULL,
  created_at REAL NOT NULL,
  submitted_at REAL,
  last_error TEXT
);
"""


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _load(text, default=None):
    if text is None:
        return default
    return json.loads(text)


def _card_num(event: dict) -> int | None:
    if event.get("card_num") is not None:
        try:
            return int(event["card_num"])
        except (TypeError, ValueError):
            return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    reply = event.get("reply_to") or payload.get("reply_to")
    if isinstance(reply, str) and reply.startswith("card:"):
        try:
            return int(reply.split(":", 1)[1])
        except (TypeError, ValueError, IndexError):
            return None
    return None


def _payload(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def actionable(event: dict) -> bool:
    """Lifecycle only. Chat generation, progress, and session echoes are out."""
    if not isinstance(event, dict):
        return False
    actor = event.get("actor")
    kind = event.get("kind")
    if actor == "session" or kind in NOISE_KINDS:
        return False
    if _card_num(event) is None:
        return False
    if actor == "user" and kind in USER_LIFECYCLE:
        return True
    if actor == "worker" and kind in WORKER_LIFECYCLE:
        return True
    if kind == "state":
        payload = _payload(event)
        return payload.get("to", payload.get("state")) in STATE_TO
    return False


def classify(event: dict) -> str:
    """resume = work may continue; notice = record only; wait = do not start."""
    kind = event.get("kind")
    payload = _payload(event)
    if kind == "answer":
        return "resume"
    if kind == "verdict":
        verdict = str(payload.get("verdict") or payload.get("decision") or "").lower()
        if verdict in RESUME_VERDICTS:
            return "resume"
        return "notice"
    if kind in ("action", "retry"):
        action = str(payload.get("action") or payload.get("name") or kind).lower()
        if kind == "retry" or action in RESUME_ACTIONS:
            return "resume"
        return "notice"
    if kind == "state":
        to = payload.get("to", payload.get("state"))
        if to == "needs_you":
            return "wait"
        return "notice"
    return "notice"


def _dispatch_for(state: str, held: bool) -> str:
    if held or state in HELD_STATES:
        return "excluded"
    if state == "needs_you":
        return "waiting"
    if state == "queued":
        return "waiting"
    if state == "ready":
        return "waiting"
    if state in ("in_progress", "integrating"):
        return "occupied"
    return "waiting"


class ContinuityInbox:
    def __init__(self, connection, clock=None):
        self.conn = getattr(connection, "conn", connection)
        if self.conn is None:
            raise ValueError("sqlite connection required")
        self.clock = clock
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _now(self, now) -> float:
        if now is not None:
            return float(now)
        if self.clock is not None:
            return float(self.clock.now())
        import time
        return time.time()

    def _begin(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self):
        self.conn.execute("COMMIT")

    def _rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def cutoff_seq(self) -> int | None:
        row = self.conn.execute(
            "SELECT cutoff_seq FROM continuity_snapshot WHERE id=1").fetchone()
        return None if row is None else int(row["cutoff_seq"])

    def snapshot(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM continuity_snapshot WHERE id=1").fetchone()
        if row is None:
            return None
        return {
            "cutoff_seq": row["cutoff_seq"],
            "created_at": row["created_at"],
            "cards": [_card_dict(c) for c in self.conn.execute(
                "SELECT * FROM continuity_cards ORDER BY card_num").fetchall()],
        }

    def register_snapshot(self, cards: list, cutoff_seq: int, now=None) -> dict:
        """Explicit handoff. No historical replay. Second call returns the first."""
        existing = self.snapshot()
        if existing is not None:
            return existing
        cutoff_seq = int(cutoff_seq)
        if cutoff_seq < 0:
            raise ValueError("cutoff_seq must be >= 0")
        rows = []
        for raw in cards or ():
            if not isinstance(raw, dict) or raw.get("num") is None:
                continue
            num = int(raw["num"])
            state = str(raw.get("state") or "")
            held = bool(raw.get("held")) or state in HELD_STATES
            last = raw.get("last_event_seq")
            if last is None and isinstance(raw.get("last_event"), dict):
                last = raw["last_event"].get("seq")
            pane = raw.get("pane") or raw.get("tmux_pane") or raw.get("pane_id")
            rows.append({
                "card_num": num,
                "state": state,
                "agent_name": raw.get("agent_name"),
                "pane": str(pane) if pane else None,
                "worktree": raw.get("worktree"),
                "branch": raw.get("branch"),
                "last_event_seq": int(last) if last is not None else None,
                "dispatch": _dispatch_for(state, held),
                "held": 1 if held else 0,
            })
        created = self._now(now)
        self._begin()
        try:
            self.conn.execute(
                "INSERT INTO continuity_snapshot(id, cutoff_seq, created_at, cards_json) "
                "VALUES(1,?,?,?)",
                (cutoff_seq, created, _json(cards)))
            for rec in rows:
                self.conn.execute(
                    "INSERT INTO continuity_cards(card_num, state, agent_name, pane, worktree, "
                    "branch, last_event_seq, dispatch, held) VALUES(?,?,?,?,?,?,?,?,?)",
                    (rec["card_num"], rec["state"], rec["agent_name"], rec["pane"],
                     rec["worktree"], rec["branch"], rec["last_event_seq"],
                     rec["dispatch"], rec["held"]))
            self._commit()
        except Exception:
            self._rollback()
            raise
        return self.snapshot()

    def excluded_panes(self) -> list:
        rows = self.conn.execute(
            "SELECT DISTINCT pane FROM continuity_cards WHERE pane IS NOT NULL "
            "AND pane != '' ORDER BY pane"
        ).fetchall()
        return [row["pane"] for row in rows]

    def card(self, card_num: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM continuity_cards WHERE card_num=?", (int(card_num),)).fetchone()
        return _card_dict(row) if row else None

    def record_events(self, events: list, cutoff_seq: int | None = None, now=None) -> list:
        """Insert unseen actionable events after cutoff. Does not POST to the board."""
        if cutoff_seq is None:
            cutoff_seq = self.cutoff_seq()
        if cutoff_seq is None:
            raise ValueError("register_snapshot before record_events")
        cutoff_seq = int(cutoff_seq)
        inserted = []
        recorded_at = self._now(now)
        self._begin()
        try:
            for event in events or ():
                if not actionable(event):
                    continue
                seq = int(event["seq"])
                if seq <= cutoff_seq:
                    continue
                card_num = _card_num(event)
                if card_num is None:
                    continue
                payload = _payload(event)
                event_id = str(payload.get("id") or "seq:%d" % seq)
                intent = classify(event)
                try:
                    self.conn.execute(
                        "INSERT INTO continuity_items(seq, event_id, card_num, actor, kind, "
                        "intent, payload_json, status, delivery_id, recorded_at) "
                        "VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                        (seq, event_id, card_num, event.get("actor") or "",
                         event.get("kind") or "", intent, _json(payload),
                         "pending", recorded_at))
                    inserted.append(seq)
                except sqlite3.IntegrityError:
                    continue
            self._commit()
        except Exception:
            self._rollback()
            raise
        return inserted

    def pending(self, card_num: int | None = None) -> list:
        if card_num is None:
            rows = self.conn.execute(
                "SELECT * FROM continuity_items WHERE status='pending' ORDER BY seq"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM continuity_items WHERE status='pending' AND card_num=? "
                "ORDER BY seq",
                (int(card_num),)).fetchall()
        return [_item_dict(row) for row in rows]

    def _inflight(self, card_num: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM continuity_deliveries WHERE card_num=? AND status IN "
            "('prepared','submitted','uncertain') ORDER BY created_at LIMIT 1",
            (int(card_num),)).fetchone()
        return _delivery_dict(row) if row else None

    def prepare_delivery(self, card_num: int, now=None) -> dict | None:
        """Persist a card batch before any send. Does not invoke tmux-send."""
        card_num = int(card_num)
        rec = self.card(card_num)
        if rec is not None and rec["dispatch"] == "excluded":
            return {"ok": False, "reason": "excluded", "card_num": card_num}
        existing = self._inflight(card_num)
        if existing is not None:
            if existing["status"] == "uncertain":
                return {
                    "ok": False,
                    "reason": "uncertain",
                    "card_num": card_num,
                    "delivery": existing,
                }
            existing["ok"] = True
            existing["batched"] = True
            return existing
        items = self.pending(card_num)
        if not items:
            return None
        seqs = [item["seq"] for item in items]
        through_seq = max(seqs)
        created = self._now(now)
        delivery_id = "cdel-" + sha256_text("%s|%s|%s" % (card_num, seqs, created))[:16]
        self._begin()
        try:
            self.conn.execute(
                "INSERT INTO continuity_deliveries(id, card_num, status, item_seqs_json, "
                "through_seq, created_at) VALUES(?,?,?,?,?,?)",
                (delivery_id, card_num, "prepared", _json(seqs), through_seq, created))
            self.conn.execute(
                "UPDATE continuity_items SET status='inflight', delivery_id=? "
                "WHERE card_num=? AND status='pending' AND seq IN (%s)"
                % ",".join("?" * len(seqs)),
                [delivery_id, card_num, *seqs])
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {
            "ok": True,
            "id": delivery_id,
            "card_num": card_num,
            "status": "prepared",
            "item_seqs": seqs,
            "through_seq": through_seq,
            "created_at": created,
            "batched": False,
        }

    def mark_submitted(self, delivery_id: str, status: str = "submitted",
                       error: str | None = None, now=None) -> dict | None:
        if status not in ("submitted", "uncertain"):
            raise ValueError("status must be submitted or uncertain")
        row = self.conn.execute(
            "SELECT * FROM continuity_deliveries WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            return None
        if row["status"] in ("acknowledged", "uncertain") and status == "submitted":
            return _delivery_dict(row)
        submitted_at = self._now(now)
        self.conn.execute(
            "UPDATE continuity_deliveries SET status=?, submitted_at=?, last_error=? "
            "WHERE id=?",
            (status, submitted_at, error, delivery_id))
        return _delivery_dict(self.conn.execute(
            "SELECT * FROM continuity_deliveries WHERE id=?", (delivery_id,)).fetchone())

    def acknowledge(self, delivery_id: str, through_seq: int) -> dict:
        """Ack only this delivery's items with seq <= through_seq."""
        through_seq = int(through_seq)
        row = self.conn.execute(
            "SELECT * FROM continuity_deliveries WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "unknown_delivery"}
        inflight = [int(r["seq"]) for r in self.conn.execute(
            "SELECT seq FROM continuity_items WHERE delivery_id=? AND status='inflight' "
            "ORDER BY seq",
            (delivery_id,)).fetchall()]
        self._begin()
        try:
            self.conn.execute(
                "UPDATE continuity_items SET status='acknowledged' "
                "WHERE delivery_id=? AND seq<=? AND status='inflight'",
                (delivery_id, through_seq))
            self.conn.execute(
                "UPDATE continuity_items SET status='pending', delivery_id=NULL "
                "WHERE delivery_id=? AND seq>? AND status='inflight'",
                (delivery_id, through_seq))
            remaining = self.conn.execute(
                "SELECT COUNT(*) AS n FROM continuity_items WHERE delivery_id=? "
                "AND status='inflight'",
                (delivery_id,)).fetchone()["n"]
            if remaining == 0:
                self.conn.execute(
                    "UPDATE continuity_deliveries SET status='acknowledged' WHERE id=?",
                    (delivery_id,))
            self._commit()
        except Exception:
            self._rollback()
            raise
        acked = [s for s in inflight if s <= through_seq]
        released = [s for s in inflight if s > through_seq]
        return {
            "ok": True,
            "delivery_id": delivery_id,
            "through_seq": through_seq,
            "acknowledged_seqs": acked,
            "released_seqs": released,
        }


def _card_dict(row) -> dict:
    return {
        "num": row["card_num"],
        "state": row["state"],
        "agent_name": row["agent_name"],
        "pane": row["pane"],
        "worktree": row["worktree"],
        "branch": row["branch"],
        "last_event_seq": row["last_event_seq"],
        "dispatch": row["dispatch"],
        "held": bool(row["held"]),
    }


def _item_dict(row) -> dict:
    return {
        "seq": row["seq"],
        "event_id": row["event_id"],
        "card_num": row["card_num"],
        "actor": row["actor"],
        "kind": row["kind"],
        "intent": row["intent"],
        "payload": _load(row["payload_json"], {}),
        "status": row["status"],
        "delivery_id": row["delivery_id"],
        "recorded_at": row["recorded_at"],
    }


def _delivery_dict(row) -> dict:
    return {
        "id": row["id"],
        "card_num": row["card_num"],
        "status": row["status"],
        "item_seqs": _load(row["item_seqs_json"], []),
        "through_seq": row["through_seq"],
        "created_at": row["created_at"],
        "submitted_at": row["submitted_at"],
        "last_error": row["last_error"],
    }
