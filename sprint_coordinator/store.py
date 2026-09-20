from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sprint_coordinator.util import chmod_private, sha256_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  card_num INTEGER,
  reply_to TEXT,
  ts REAL,
  payload_json TEXT NOT NULL,
  ingested_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS obligations (
  id TEXT PRIMARY KEY,
  event_seq INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  parent_id TEXT,
  reply_to TEXT NOT NULL,
  thread_key TEXT NOT NULL,
  status TEXT NOT NULL,
  intent TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  bound_seq INTEGER NOT NULL,
  question_text TEXT,
  created_at REAL NOT NULL,
  due_at REAL,
  routed_at REAL,
  answered_at REAL,
  last_error TEXT,
  escalations INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reply_links (
  reply_id TEXT NOT NULL,
  obligation_id TEXT NOT NULL,
  PRIMARY KEY (reply_id, obligation_id)
);
CREATE TABLE IF NOT EXISTS assignments (
  id TEXT PRIMARY KEY,
  obligation_id TEXT NOT NULL,
  worker TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  idempotent INTEGER NOT NULL DEFAULT 0,
  job_json TEXT,
  result_json TEXT,
  pid INTEGER,
  started_at REAL,
  progress_at REAL,
  finished_at REAL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  obligation_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  destination TEXT NOT NULL,
  body_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  sent_at REAL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS judgments (
  cache_key TEXT PRIMARY KEY,
  prompt_version TEXT NOT NULL,
  model TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  usage_json TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_daily (
  day TEXT PRIMARY KEY,
  calls INTEGER NOT NULL,
  spend_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clocks (
  name TEXT PRIMARY KEY,
  at REAL NOT NULL,
  detail TEXT
);
"""


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _load(text, default=None):
    if text is None:
        return default
    return json.loads(text)


class Store:
    def __init__(self, path: Path, clock):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        chmod_private(self.path)
        if self.get_meta("ingest_cursor") is None:
            self.set_meta("ingest_cursor", "0")
        if self.get_meta("generation") is None:
            self.set_meta("generation", "0")

    def close(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
            self.conn = None

    def begin(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn.execute("COMMIT")

    def rollback(self):
        self.conn.execute("ROLLBACK")

    def get_meta(self, key: str):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    def ingest_cursor(self) -> int:
        return int(self.get_meta("ingest_cursor") or 0)

    def generation(self) -> int:
        return int(self.get_meta("generation") or 0)

    def bump_generation(self) -> int:
        nxt = self.generation() + 1
        self.set_meta("generation", str(nxt))
        return nxt

    def set_clock(self, name: str, at: float, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO clocks(name, at, detail) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET at=excluded.at, detail=excluded.detail",
            (name, at, detail))

    def get_clock(self, name: str):
        row = self.conn.execute("SELECT * FROM clocks WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def ingest_events(self, events: list, ingested_at: float) -> list:
        """Insert unseen events and advance the ingest cursor. Does not answer obligations."""
        inserted = []
        if not events:
            return inserted
        self.begin()
        try:
            cursor = self.ingest_cursor()
            max_seq = cursor
            for ev in events:
                seq = int(ev["seq"])
                if seq > max_seq:
                    max_seq = seq
                event_id = str((ev.get("payload") or {}).get("id") or "seq:%d" % seq)
                payload = ev.get("payload") or {}
                reply_to = payload.get("reply_to") if isinstance(payload, dict) else None
                try:
                    self.conn.execute(
                        "INSERT INTO events(seq, event_id, actor, kind, card_num, reply_to, ts, "
                        "payload_json, ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (seq, event_id, ev.get("actor") or "", ev.get("kind") or "",
                         ev.get("card_num"), reply_to, ev.get("ts"),
                         _json(payload), ingested_at))
                    inserted.append(seq)
                except sqlite3.IntegrityError:
                    continue
            if max_seq > cursor:
                self.set_meta("ingest_cursor", str(max_seq))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return inserted

    def event(self, seq: int):
        row = self.conn.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
        return self._event_dict(row) if row else None

    def events_after(self, seq: int) -> list:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE seq>? ORDER BY seq", (seq,)).fetchall()
        return [self._event_dict(r) for r in rows]

    def thread_head(self, thread_key: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(seq) AS s FROM events WHERE COALESCE(reply_to, "
            "CASE WHEN card_num IS NULL THEN 'sidebar' ELSE 'card:' || card_num END)=?",
            (thread_key,)).fetchone()
        return int(row["s"] or 0)

    def user_thread_head(self, thread_key: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(seq) AS s FROM events WHERE actor='user' AND COALESCE(reply_to, "
            "CASE WHEN card_num IS NULL THEN 'sidebar' ELSE 'card:' || card_num END)=?",
            (thread_key,)).fetchone()
        return int(row["s"] or 0)

    def _event_dict(self, row) -> dict:
        payload = _load(row["payload_json"], {})
        return {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "actor": row["actor"],
            "kind": row["kind"],
            "card_num": row["card_num"],
            "reply_to": row["reply_to"],
            "ts": row["ts"],
            "payload": payload,
            "ingested_at": row["ingested_at"],
        }

    def put_obligation(self, rec: dict) -> None:
        self.conn.execute(
            "INSERT INTO obligations(id, event_seq, event_id, parent_id, reply_to, thread_key, "
            "status, intent, revision, bound_seq, question_text, created_at, due_at, "
            "escalations) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], rec["event_seq"], rec["event_id"], rec.get("parent_id"),
             rec["reply_to"], rec["thread_key"], rec["status"], rec.get("intent"),
             rec.get("revision", 1), rec["bound_seq"], rec.get("question_text"),
             rec["created_at"], rec.get("due_at"), rec.get("escalations", 0)))

    def obligation(self, oid: str):
        row = self.conn.execute("SELECT * FROM obligations WHERE id=?", (oid,)).fetchone()
        return dict(row) if row else None

    def obligations(self, status: str | None = None) -> list:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM obligations WHERE status=? ORDER BY created_at, id",
                (status,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM obligations ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def open_obligations(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM obligations WHERE status NOT IN ('answered','failed') "
            "ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def update_obligation(self, oid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join("%s=?" % k for k in fields)
        self.conn.execute("UPDATE obligations SET %s WHERE id=?" % cols,
                          list(fields.values()) + [oid])

    def put_assignment(self, rec: dict) -> None:
        self.conn.execute(
            "INSERT INTO assignments(id, obligation_id, worker, role, status, attempt, "
            "idempotent, job_json, started_at, progress_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], rec["obligation_id"], rec["worker"], rec["role"], rec["status"],
             rec["attempt"], 1 if rec.get("idempotent") else 0, _json(rec.get("job")),
             rec.get("started_at"), rec.get("progress_at")))

    def assignment(self, aid: str):
        row = self.conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        return self._assignment_dict(row) if row else None

    def assignments_for(self, oid: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM assignments WHERE obligation_id=? ORDER BY attempt, id",
            (oid,)).fetchall()
        return [self._assignment_dict(r) for r in rows]

    def assignments_by_status(self, *statuses) -> list:
        q = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            "SELECT * FROM assignments WHERE status IN (%s) ORDER BY started_at, id" % q,
            statuses).fetchall()
        return [self._assignment_dict(r) for r in rows]

    def update_assignment(self, aid: str, **fields) -> None:
        packed = {}
        for k, v in fields.items():
            if k in ("job", "result") or k.endswith("_json"):
                packed[k if k.endswith("_json") else k + "_json"] = _json(v)
            else:
                packed[k] = v
        cols = ", ".join("%s=?" % k for k in packed)
        self.conn.execute("UPDATE assignments SET %s WHERE id=?" % cols,
                          list(packed.values()) + [aid])

    def _assignment_dict(self, row) -> dict:
        rec = dict(row)
        rec["job"] = _load(row["job_json"], {})
        rec["result"] = _load(row["result_json"], None)
        rec["idempotent"] = bool(row["idempotent"])
        return rec

    def put_outbox(self, rec: dict) -> str:
        self.conn.execute(
            "INSERT INTO outbox(id, idempotency_key, obligation_id, revision, destination, "
            "body_json, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (rec["id"], rec["idempotency_key"], rec["obligation_id"], rec["revision"],
             rec["destination"], _json(rec["body"]), rec["status"], rec["created_at"]))
        return rec["id"]

    def outbox(self, oid: str):
        row = self.conn.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
        return self._outbox_dict(row) if row else None

    def outbox_by_key(self, key: str):
        row = self.conn.execute(
            "SELECT * FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
        return self._outbox_dict(row) if row else None

    def outbox_pending(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE status IN ('pending','sending') ORDER BY created_at"
        ).fetchall()
        return [self._outbox_dict(r) for r in rows]

    def outbox_all(self) -> list:
        rows = self.conn.execute("SELECT * FROM outbox ORDER BY created_at").fetchall()
        return [self._outbox_dict(r) for r in rows]

    def update_outbox(self, oid: str, **fields) -> None:
        packed = {}
        for k, v in fields.items():
            packed[k if k != "body" else "body_json"] = _json(v) if k == "body" else v
        cols = ", ".join("%s=?" % k for k in packed)
        self.conn.execute("UPDATE outbox SET %s WHERE id=?" % cols,
                          list(packed.values()) + [oid])

    def _outbox_dict(self, row) -> dict:
        rec = dict(row)
        rec["body"] = _load(row["body_json"], {})
        return rec

    def link_reply(self, reply_id: str, obligation_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO reply_links(reply_id, obligation_id) VALUES(?,?)",
            (reply_id, obligation_id))

    def links_for(self, obligation_id: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM reply_links WHERE obligation_id=?", (obligation_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_judgment(self, cache_key: str):
        row = self.conn.execute(
            "SELECT * FROM judgments WHERE cache_key=?", (cache_key,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["result"] = _load(row["result_json"], {})
        rec["usage"] = _load(row["usage_json"], {})
        return rec

    def put_judgment(self, cache_key: str, prompt_version: str, model: str,
                     input_hash: str, result, usage, created_at: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO judgments(cache_key, prompt_version, model, input_hash, "
            "result_json, usage_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (cache_key, prompt_version, model, input_hash, _json(result), _json(usage),
             created_at))

    def usage_day(self, day: str) -> dict:
        row = self.conn.execute("SELECT * FROM usage_daily WHERE day=?", (day,)).fetchone()
        if row:
            return dict(row)
        return {"day": day, "calls": 0, "spend_usd": 0.0}

    def add_usage(self, day: str, calls: int, spend_usd: float) -> dict:
        cur = self.usage_day(day)
        nxt = {"day": day, "calls": cur["calls"] + calls,
               "spend_usd": cur["spend_usd"] + spend_usd}
        self.conn.execute(
            "INSERT INTO usage_daily(day, calls, spend_usd) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET calls=excluded.calls, spend_usd=excluded.spend_usd",
            (nxt["day"], nxt["calls"], nxt["spend_usd"]))
        return nxt

    def recover_uncertain(self, now: float) -> dict:
        """Crash recovery: never blindly rerun non-idempotent in-flight work."""
        restarted = []
        uncertain = []
        self.begin()
        try:
            for row in self.assignments_by_status("running", "starting"):
                if row["idempotent"]:
                    self.update_assignment(row["id"], status="pending", last_error="recovered",
                                           progress_at=now)
                    restarted.append(row["id"])
                else:
                    self.update_assignment(row["id"], status="uncertain",
                                           last_error="crash_while_running", finished_at=now)
                    uncertain.append(row["id"])
                    self.update_obligation(row["obligation_id"], status="failed",
                                           last_error="uncertain_non_idempotent_work")
            for row in self.outbox_pending():
                if row["status"] == "sending":
                    self.update_outbox(row["id"], status="uncertain",
                                       last_error="crash_while_sending")
                    uncertain.append(row["id"])
            self.commit()
        except Exception:
            self.rollback()
            raise
        return {"restarted": restarted, "uncertain": uncertain}

    def snapshot(self) -> dict:
        open_obls = self.open_obligations()
        return {
            "ingest_cursor": self.ingest_cursor(),
            "generation": self.generation(),
            "obligations_open": len(open_obls),
            "obligations_answered": len(self.obligations("answered")),
            "obligations_failed": len(self.obligations("failed")),
            "assignments_running": len(self.assignments_by_status("running", "starting")),
            "assignments_uncertain": len(self.assignments_by_status("uncertain")),
            "outbox_pending": len(self.outbox_pending()),
            "service_heartbeat": self.get_clock("service_heartbeat"),
        }


def obligation_id_for(event_id: str, reply_to: str, suffix: str = "") -> str:
    raw = "%s|%s|%s" % (event_id, reply_to, suffix)
    return "obl-" + sha256_text(raw)[:16]


def assignment_id_for(oid: str, attempt: int, worker: str) -> str:
    return "asg-%s-%s-%d" % (oid[4:12] if oid.startswith("obl-") else oid[:8], worker, attempt)


def outbox_id_for(key: str) -> str:
    return "out-" + sha256_text(key)[:16]
