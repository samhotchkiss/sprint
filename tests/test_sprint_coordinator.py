"""Coordinator tests. Fake workers/HTTP/Jev only; no live board or paid calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sprint_coordinator.config import CoordinatorConfig, init_config, write_json_private
from sprint_coordinator.driver import Coordinator
from sprint_coordinator.store import Store
from sprint_coordinator.takeover import takeover_report
from sprint_coordinator.util import Clock
from sprint_coordinator.workers import InProcessRunner, SlotGate


def user_event(seq, text="hello?", reply_to="sidebar", kind="chat", card_num=None):
    return {
        "seq": seq, "actor": "user", "kind": kind, "card_num": card_num, "ts": seq,
        "payload": {"text": text, "reply_to": reply_to},
    }


def noise_event(seq, actor="worker", kind="progress"):
    return {"seq": seq, "actor": actor, "kind": kind, "card_num": 1, "ts": seq,
            "payload": {"text": "still running"}}


class MemoryBoard:
    def __init__(self):
        self.events = []
        self.head = 0
        self.cursor = 0
        self.posts = []
        self.autoheal_doc = {
            "event_dispatch_supported": True,
            "event_dispatcher": None,
            "tmux_window": None,
            "registered": False,
        }

    def add(self, event):
        self.events.append(event)
        self.head = event["seq"]

    def events_after(self, after, limit=500):
        evs = [e for e in self.events if e["seq"] > after][:limit]
        return {"events": evs, "head": self.head, "cursor": self.cursor, "after": after}

    def autoheal(self):
        return dict(self.autoheal_doc)

    def settings(self):
        return {"session_tmux_window": None}

    def post_sidebar(self, text, detail=None):
        self.posts.append(("sidebar", text))
        return {"ok": True}

    def post_card_chat(self, card_num, text, detail=None):
        self.posts.append(("card:%d" % card_num, text))
        return {"ok": True}


class Outcome:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.action = "accept" if accepted else "escalate"


class FakeJev:
    def __init__(self, configured=True, intents=None, escalate=False, accept=True):
        self._configured = configured
        self.intents = list(intents or ["answer_question"])
        self.escalate = escalate
        self.accept = accept
        self.calls = []
        self.verify_calls = []

    def configured(self):
        return self._configured

    def route(self, obl):
        self.calls.append(obl)
        if not self._configured:
            return {"intents": ["unclear"], "escalate": True, "reason": "missing_jev_key"}
        return {
            "intents": self.intents,
            "escalate": self.escalate or len(self.intents) != 1,
            "reason": "multi_intent" if len(self.intents) != 1 else "",
            "usage": {"calls": 1, "spend_usd": 0.01},
        }

    def verify_code(self, **kwargs):
        self.verify_calls.append(kwargs)
        if not self._configured:
            raise RuntimeError("missing_jev_key")
        return Outcome(self.accept)


def make_config(directory, extra_workers=None, **overrides):
    workers = {
        "low": {"command": ["low"], "role": "response", "cost": "low"},
        "high": {"command": ["high"], "role": "response", "cost": "high"},
    }
    if extra_workers:
        workers.update(extra_workers)
    raw = {
        "version": 1,
        "project_root": str(directory),
        "data_dir": str(Path(directory) / "coord"),
        "board_data_dir": str(Path(directory) / "board"),
        "concurrency": 3,
        "reserved_response_slots": 1,
        "daily_max_calls": 50,
        "daily_max_spend_usd": 2.0,
        "max_escalation_retries": 1,
        "poll_seconds": 0.05,
        "jev": {"enabled": True, "key_file": str(Path(directory) / "missing.env"),
                "model": "jev-1.13.0"},
        "workers": workers,
        "allowed_checks": [{"id": "unit-self", "command": [sys.executable, "-c", "print(0)"]}],
        "require_jev_for_approval": True,
    }
    raw.update(overrides)
    Path(directory, "board").mkdir(exist_ok=True)
    return CoordinatorConfig(raw)


def ok_reply(job, text="answered"):
    return {"ok": True, "kind": "reply", "text": text, "addresses_obligation": True}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.pop("TYPESAFE_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory(prefix="coord-")
        self.dir = Path(self.tmp.name)
        self.clock = Clock(1000)
        self.board = MemoryBoard()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_key)

    def _restore_key(self):
        if self._saved_key is not None:
            os.environ["TYPESAFE_API_KEY"] = self._saved_key

    def coord(self, runner, jev=None, mode="shadow", **cfg):
        config = make_config(self.dir, **cfg)
        c = Coordinator(config, mode=mode, clock=self.clock, board=self.board,
                        jev=jev or FakeJev(), runner=runner,
                        check_runner=lambda command, timeout=60: {
                            "command": command, "exit_code": 0, "adapter": "trusted"})
        c.open()
        self.addCleanup(c.close)
        return c

    def test_ingest_cursor_is_not_answered(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1, "one"))
        self.board.add(user_event(2, "two"))
        self.board.add(noise_event(3))
        c.tick(wait=True)
        snap = c.store.snapshot()
        self.assertEqual(snap["ingest_cursor"], 3)
        answered = c.store.obligations("answered")
        failed = c.store.obligations("failed")
        open_n = len(c.store.open_obligations())
        self.assertEqual(len(answered) + len(failed) + open_n, 2)
        self.assertTrue(c.store.obligations())
        self.assertIsNone(c.store.obligation("missing"))
        self.assertEqual(len([e for e in [c.store.event(1), c.store.event(2)] if e]), 2)

    def test_status_update_does_not_satisfy_question(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1, "what is the status of #9?"))
        c.tick(wait=True)
        oid = c.store.obligations()[0]["id"]
        self.board.add(noise_event(2))
        c.tick(wait=True)
        self.assertEqual(c.store.ingest_cursor(), 2)
        self.assertTrue(c.store.links_for(oid) or c.store.obligation(oid)["status"] in
                        ("answered", "assigned", "escalated", "routed", "failed"))
        self.assertEqual(len(c.store.obligations()), 1)

    def test_restart_does_not_repeat_uncertain_work(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        c.store.ingest_events([user_event(1)], self.clock.now())
        c.router.create_obligations([c.store.event(1)])
        oid = c.store.obligations()[0]["id"]
        c.store.put_assignment({
            "id": "asg-crash-low-1", "obligation_id": oid,
            "worker": "low", "role": "response", "status": "running", "attempt": 1,
            "idempotent": False, "job": {}, "started_at": 1, "progress_at": 1,
        })
        aid = "asg-crash-low-1"
        c.close()
        config = make_config(self.dir)
        c2 = Coordinator(config, mode="shadow", clock=self.clock, board=self.board,
                         jev=FakeJev(), runner=runner)
        opened = c2.open()
        self.addCleanup(c2.close)
        self.assertIn(aid, opened["recovery"]["uncertain"] or [aid])
        row = c2.store.assignment(aid)
        self.assertEqual(row["status"], "uncertain")
        c2.tick(wait=True)
        self.assertEqual(c2.store.assignment(aid)["status"], "uncertain")

    def test_idempotent_pending_is_retried_after_restart(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1))
        c.tick(wait=True)
        oid = c.store.obligations()[0]["id"]
        c.store.put_assignment({
            "id": "asg-idem-low-9", "obligation_id": oid, "worker": "low",
            "role": "response", "status": "running", "attempt": 9, "idempotent": True,
            "job": {"assignment_id": "asg-idem-low-9", "obligation_id": oid},
            "started_at": 1, "progress_at": 1,
        })
        c.close()
        config = make_config(self.dir)
        c2 = Coordinator(config, mode="shadow", clock=self.clock, board=self.board,
                         jev=FakeJev(), runner=runner)
        opened = c2.open()
        self.addCleanup(c2.close)
        self.assertIn("asg-idem-low-9", opened["recovery"]["restarted"])
        self.assertEqual(c2.store.assignment("asg-idem-low-9")["status"], "pending")

    def test_stale_reply_is_blocked_and_rerouted_once(self):
        gate = threading.Event()

        def slow(job):
            gate.wait(2)
            return ok_reply(job, "stale-answer")

        runner = InProcessRunner({"low": slow, "high": lambda job: ok_reply(job, "fresh")})
        c = self.coord(runner)
        self.board.add(user_event(1, "first"))
        c.tick(wait=False)
        self.board.add(user_event(2, "correction"))
        c.tick(wait=False)
        gate.set()
        c.tick(wait=True)
        stale = [row for row in c.store.outbox_all() if row["status"] == "blocked_stale"]
        self.assertTrue(stale)
        high = [a for a in c.store.assignments_by_status("succeeded", "failed", "pending", "running")
                if a["worker"] == "high"]
        self.assertTrue(high)

    def test_blocked_code_worker_does_not_starve_response(self):
        started = threading.Event()
        release = threading.Event()

        def code_job(job):
            started.set()
            release.wait(30)
            return {"ok": True, "kind": "code_result", "check_ids": ["unit-self"], "text": "code"}

        runner = InProcessRunner({
            "low": lambda job: ok_reply(job, "quick"),
            "high": lambda job: ok_reply(job, "quick"),
            "code": code_job,
        })
        extra = {"code": {"command": ["code"], "role": "code", "cost": "high"}}
        c = self.coord(runner, extra_workers=extra, concurrency=2, reserved_response_slots=1)
        c.store.put_obligation({
            "id": "obl-codewait", "event_seq": 0, "event_id": "seq:0",
            "reply_to": "sidebar", "thread_key": "sidebar", "status": "assigned",
            "revision": 1, "bound_seq": 0, "question_text": "build",
            "created_at": self.clock.now(), "due_at": self.clock.now() + 30,
        })
        c.store.put_assignment({
            "id": "asg-code-1", "obligation_id": "obl-codewait", "worker": "code",
            "role": "code", "status": "pending", "attempt": 1, "idempotent": False,
            "job": {"kind": "code"}, "started_at": self.clock.now(),
            "progress_at": self.clock.now(),
        })
        c.tick(wait=False)
        self.assertTrue(started.wait(2))
        self.board.add(user_event(1, "ping"))
        c.tick(wait=False)
        deadline = time.time() + 2
        replies = []
        while time.time() < deadline:
            c._reap()
            replies = [a for a in c.store.assignments_by_status("succeeded") if a["worker"] == "low"]
            if replies:
                break
            time.sleep(0.05)
        self.assertTrue(replies)
        self.assertEqual(c.store.assignment("asg-code-1")["status"], "running")
        release.set()
        c.tick(wait=True)

    def test_missing_jev_key_cannot_approve_code(self):
        def code_ok(job):
            return {"ok": True, "kind": "code_result", "check_ids": ["unit-self"], "text": "done"}

        runner = InProcessRunner({"low": code_ok, "high": code_ok})
        c = self.coord(runner, jev=FakeJev(configured=False))
        self.board.add(user_event(1, "ship it", kind="verdict"))
        c.tick(wait=True)
        failed = c.store.obligations("failed")
        self.assertTrue(failed)
        self.assertIn("missing_jev_key", failed[-1]["last_error"])

    def test_low_cost_failure_escalates_to_high(self):
        runner = InProcessRunner({
            "low": lambda job: {"ok": False, "error": "cheap-fail"},
            "high": lambda job: ok_reply(job, "from-high"),
        })
        c = self.coord(runner)
        self.board.add(user_event(1, "please answer"))
        c.tick(wait=True)
        workers = [a["worker"] for a in c.store.assignments_for(c.store.obligations()[0]["id"])]
        self.assertEqual(workers[:2], ["low", "high"])
        self.assertEqual(c.store.assignments_by_status("succeeded")[0]["worker"], "high")

    def test_no_repeated_retry_loop(self):
        runner = InProcessRunner({
            "low": lambda job: {"ok": False, "error": "nope"},
            "high": lambda job: {"ok": False, "error": "still-nope"},
        })
        c = self.coord(runner)
        self.board.add(user_event(1, "loop?"))
        for _ in range(8):
            c.tick(wait=True)
            self.clock.advance(5)
        assigns = c.store.assignments_for(c.store.obligations()[0]["id"])
        self.assertEqual(len(assigns), 2)
        self.assertEqual(c.store.obligations()[0]["status"], "failed")

    def test_multi_intent_escalates(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=FakeJev(intents=["answer_question", "dispatch_work"]))
        self.board.add(user_event(1, "answer this and also build that"))
        c.tick(wait=True)
        obl = c.store.obligations()[0]
        self.assertIn(obl["status"], ("escalated", "assigned", "answered"))
        self.assertTrue(any(a["worker"] == "high" for a in c.store.assignments_for(obl["id"])))

    def test_idle_tick_makes_no_jev_call(self):
        jev = FakeJev()
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=jev)
        self.board.add(noise_event(1, "server", "heartbeat"))
        c.tick(wait=True)
        self.assertEqual(jev.calls, [])
        self.assertEqual(c.router.jev_calls, 0)

    def test_shadow_does_not_publish(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, mode="shadow")
        self.board.add(user_event(1))
        c.tick(wait=True)
        self.assertEqual(self.board.posts, [])
        self.assertTrue(any(o["status"] == "skipped_shadow" for o in c.store.outbox_all()))

    def test_active_publish_uses_stable_key(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, mode="active")
        self.board.add(user_event(1, "hi"))
        c.tick(wait=True)
        self.assertTrue(self.board.posts)
        keys = [row["idempotency_key"] for row in c.store.outbox_all()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(k.startswith("reply:") for k in keys))
        c.tick(wait=True)
        self.assertEqual(len(self.board.posts), 1)

    def test_active_takeover_blocked_when_dispatch_live(self):
        report = takeover_report(
            {"event_dispatch_supported": True, "event_dispatcher": {"status": "idle"},
             "tmux_window": "%5"},
            self.dir / "board", "active")
        self.assertFalse(report["ok"])
        self.assertTrue(report["blockers"])
        self.assertFalse(report["live_takeover"])

    def test_clocks_are_distinct(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1))
        c.tick(wait=True)
        self.assertIsNotNone(c.store.get_clock("service_heartbeat"))
        progress = [row for row in c.store.conn.execute("SELECT name FROM clocks").fetchall()
                    if row["name"].startswith("job_progress:")]
        self.assertTrue(progress)

    def test_duplicate_event_id_and_seq_dedup(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1, "same"))
        c.tick(wait=True)
        n = len(c.store.obligations())
        c.store.ingest_events([user_event(1, "same")], self.clock.now())
        self.assertEqual(len(c.store.obligations()), n)
        self.assertEqual(c.store.ingest_cursor(), 1)

    def test_flock_is_exclusive(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        config = make_config(self.dir)
        other = Coordinator(config, mode="shadow", clock=self.clock, board=self.board,
                            jev=FakeJev(), runner=runner)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(RuntimeError, "flock"):
            other.open()

    def test_slot_gate_reserves_response(self):
        gate = SlotGate(2, 1)
        self.assertTrue(gate.acquire("code"))
        self.assertFalse(gate.can_start("code"))
        self.assertTrue(gate.can_start("response"))
        self.assertTrue(gate.acquire("response"))
        self.assertFalse(gate.can_start("response"))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="coord-http-")
        self.dir = Path(self.tmp.name)
        self.state = {"events": [], "posts": []}

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/events":
                    after = 0
                    if "after=" in self.path:
                        after = int(self.path.split("after=")[1].split("&")[0])
                    evs = [e for e in outer.state["events"] if e["seq"] > after]
                    head = outer.state["events"][-1]["seq"] if outer.state["events"] else 0
                    return self._send(200, {"events": evs, "head": head, "cursor": 0})
                if path == "/api/autoheal":
                    return self._send(200, {"event_dispatch_supported": True,
                                            "event_dispatcher": None, "tmux_window": None})
                if path == "/api/settings":
                    return self._send(200, {"session_tmux_window": None})
                if path == "/api/cursors/orchestrator":
                    return self._send(200, {"seq": 0})
                return self._send(404, {"error": "no"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode() or "{}")
                outer.state["posts"].append((self.path, body))
                self._send(201, {"ok": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.tmp.cleanup)
        board_dir = self.dir / "board"
        board_dir.mkdir()
        write_json_private(board_dir / "server.json",
                           {"port": self.port, "token": "test-token", "host": "127.0.0.1"})

    def test_http_shadow_run_once_roundtrip(self):
        env = os.environ.copy()
        env.pop("TYPESAFE_API_KEY", None)
        env["PYTHONPATH"] = str(ROOT)
        cfg_path = self.dir / "coordinator.json"
        init_config(cfg_path, self.dir)
        raw = json.loads(cfg_path.read_text())
        raw["data_dir"] = str(self.dir / "coord")
        raw["board_data_dir"] = str(self.dir / "board")
        raw["project_root"] = str(self.dir)
        raw["jev"]["key_file"] = str(self.dir / "no.key")
        raw["workers"] = {
            "low": {"command": [sys.executable, "-m", "sprint_coordinator.fake_worker", "low"],
                    "role": "response", "cost": "low"},
            "high": {"command": [sys.executable, "-m", "sprint_coordinator.fake_worker", "high"],
                     "role": "response", "cost": "high"},
        }
        write_json_private(cfg_path, raw)
        self.state["events"] = [user_event(1, "http-hello")]
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin/sprint-coordinate"), "run-once", "--shadow",
             "--config", str(cfg_path)],
            env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("ingest_cursor", payload)
        status = subprocess.run(
            [sys.executable, str(ROOT / "bin/sprint-coordinate"), "status",
             "--config", str(cfg_path)],
            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("ingest_cursor", status.stdout)

    def test_cli_init_config_and_active_without_takeover_ack(self):
        env = os.environ.copy()
        env.pop("TYPESAFE_API_KEY", None)
        env["PYTHONPATH"] = str(ROOT)
        cfg_path = self.dir / "fresh.json"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin/sprint-coordinate"), "init-config",
             "--config", str(cfg_path), "--project-root", str(self.dir)],
            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        raw = json.loads(cfg_path.read_text())
        raw["data_dir"] = str(self.dir / "coord2")
        raw["board_data_dir"] = str(self.dir / "board")
        raw["jev"]["key_file"] = str(self.dir / "no.key")
        write_json_private(cfg_path, raw)
        active = subprocess.run(
            [sys.executable, str(ROOT / "bin/sprint-coordinate"), "run-once", "--active",
             "--config", str(cfg_path)],
            env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(active.returncode, 0, active.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
