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

from sprint_coordinator import judgments
from sprint_coordinator.config import CoordinatorConfig, init_config, write_json_private
from sprint_coordinator.driver import Coordinator
from sprint_coordinator.evidence import EvidenceError, validate_code_result
from sprint_coordinator.store import Store
from sprint_coordinator.takeover import takeover_report
from sprint_coordinator.util import Clock
from sprint_coordinator.workers import InProcessRunner, SlotGate


def user_event(seq, text="hello?", reply_to="sidebar", kind="chat", card_num=None):
    return {
        "seq": seq, "actor": "user", "kind": kind, "card_num": card_num, "ts": seq,
        "payload": {"text": text, "reply_to": reply_to, "id": "evt-%d" % seq},
    }


def noise_event(seq, actor="worker", kind="progress"):
    return {"seq": seq, "actor": actor, "kind": kind, "card_num": 1, "ts": seq,
            "payload": {"text": "still running"}}


class MemoryBoard:
    def __init__(self, page_limit=500):
        self.events = []
        self.head = 0
        self.cursor = 0
        self.posts = []
        self.page_limit = page_limit
        self.fail_posts = False
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
        cap = min(int(limit), int(self.page_limit))
        evs = [e for e in self.events if e["seq"] > after][:cap]
        return {"events": evs, "head": self.head, "cursor": self.cursor, "after": after}

    def autoheal(self):
        return dict(self.autoheal_doc)

    def settings(self):
        return {"session_tmux_window": None}

    def post_sidebar(self, text, detail=None, *, idempotency_key=None):
        if self.fail_posts:
            raise RuntimeError("post_failed")
        self.posts.append(("sidebar", text, idempotency_key))
        return {"ok": True}

    def post_card_chat(self, card_num, text, detail=None, *, idempotency_key=None):
        if self.fail_posts:
            raise RuntimeError("post_failed")
        self.posts.append(("card:%d" % card_num, text, idempotency_key))
        return {"ok": True}


class Outcome:
    def __init__(self, accepted=True, action=None):
        self.accepted = accepted
        self.action = action or ("accept" if accepted else "escalate")
        self.reason = ""
        self.usage_tokens = 1000


class FakeJev:
    def __init__(self, configured=True, intents=None, escalate=False, accept=True,
                 fail_first_verify=False, route_error=None):
        self._configured = configured
        self.intents = list(intents or ["answer_question"])
        self.escalate = escalate
        self.accept = accept
        self.fail_first_verify = fail_first_verify
        self.route_error = route_error
        self.calls = []
        self.verify_calls = []

    def configured(self):
        return self._configured

    def route(self, obl):
        self.calls.append(obl)
        if self.route_error:
            raise self.route_error
        if not self._configured:
            return {"intents": ["unclear"], "escalate": True, "reason": "missing_jev_key"}
        return {
            "intents": list(self.intents),
            "escalate": self.escalate or len(self.intents) != 1,
            "reason": "multi_intent" if len(self.intents) != 1 else "",
            "usage": {"calls": 1, "input_tokens": 1000, "spend_usd": 0.000042,
                      "billing": "input_tokens_rate"},
        }

    def verify_candidate(self, **kwargs):
        self.verify_calls.append(kwargs)
        if not self._configured:
            raise judgments.MissingCredentials("missing_jev_key")
        if self.fail_first_verify and len(self.verify_calls) == 1:
            return Outcome(False)
        return Outcome(self.accept)


class NoVerifyJev:
    def configured(self):
        return True

    def route(self, obl):
        return {"intents": ["answer_question"], "escalate": False,
                "usage": {"calls": 1, "input_tokens": 10}}


def make_config(directory, extra_workers=None, extra_checks=None, **overrides):
    workers = {
        "low": {"command": ["low"], "role": "response", "cost": "low",
                "estimated_calls": 1, "estimated_spend_usd": 0.01},
        "high": {"command": ["high"], "role": "response", "cost": "high",
                 "estimated_calls": 1, "estimated_spend_usd": 0.02},
    }
    if extra_workers:
        workers.update(extra_workers)
    checks = [{"id": "unit-self", "command": [sys.executable, "-c", "raise SystemExit(0)"]}]
    if extra_checks is not None:
        checks = extra_checks
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
        "allowed_checks": checks,
        "require_jev_for_approval": True,
        "start_cursor": 0,
    }
    raw.update(overrides)
    Path(directory, "board").mkdir(exist_ok=True)
    return CoordinatorConfig(raw)


def ok_reply(job, text="answered"):
    return {"ok": True, "kind": "reply", "text": text, "addresses_obligation": True}


_PASS_CHECK = object()


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

    def coord(self, runner, jev=None, mode="active", start_cursor=0, check_runner=_PASS_CHECK,
              **cfg):
        config = make_config(self.dir, **cfg)
        if check_runner is _PASS_CHECK:
            check_runner = lambda command, timeout=60, cwd=None: {
                "command": command, "exit_code": 0, "adapter": "trusted",
                "cwd": cwd, "timeout_seconds": timeout}
        c = Coordinator(config, mode=mode, clock=self.clock, board=self.board,
                        jev=jev or FakeJev(), runner=runner,
                        check_runner=check_runner, start_cursor=start_cursor)
        c.open()
        self.addCleanup(c.close)
        return c

    def test_shadow_is_ingest_only(self):
        jev = FakeJev()
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=jev, mode="shadow")
        self.board.add(user_event(1, "please answer"))
        snap = c.tick(wait=True)
        self.assertEqual(snap["ingest_cursor"], 1)
        self.assertEqual(jev.calls, [])
        self.assertEqual(jev.verify_calls, [])
        self.assertEqual(c.store.assignments_by_status("pending", "running", "succeeded"), [])
        self.assertEqual(c.store.obligations("answered"), [])
        self.assertEqual(c.store.outbox_all(), [])
        self.assertEqual(self.board.posts, [])
        self.assertEqual(c.store.obligations()[0]["status"], "received")

    def test_ingest_cursor_is_not_answered(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, mode="shadow")
        self.board.add(user_event(1, "one"))
        self.board.add(user_event(2, "two"))
        self.board.add(noise_event(3))
        c.tick(wait=True)
        snap = c.store.snapshot()
        self.assertEqual(snap["ingest_cursor"], 3)
        self.assertEqual(c.store.obligations("answered"), [])
        self.assertEqual(len(c.store.obligations("received")), 2)

    def test_status_only_does_not_launch_worker(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=FakeJev(intents=["status_only"]))
        self.board.add(user_event(1, "ok thanks"))
        c.tick(wait=True)
        self.assertEqual(c.store.obligations()[0]["status"], "recorded")
        self.assertEqual(c.store.assignments_by_status("pending", "running", "succeeded"), [])
        self.assertEqual(self.board.posts, [])

    def test_deterministic_ui_action_is_not_reexecuted(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1, "approve", kind="verdict", reply_to="card:9", card_num=9))
        c.tick(wait=True)
        self.assertEqual(c.store.obligations()[0]["status"], "recorded")
        self.assertEqual(c.store.assignments_by_status("succeeded", "pending", "running"), [])

    def test_approval_cannot_dispatch_code(self):
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply, "code": ok_reply})
        c = self.coord(runner, jev=FakeJev(intents=["approval"]), extra_workers=extra)
        self.board.add(user_event(1, "lgtm, merge it"))
        c.tick(wait=True)
        obl = c.store.obligations()[0]
        self.assertEqual(obl["status"], "held")
        self.assertFalse(any(a["role"] == "code" for a in c.store.assignments_for(obl["id"])))

    def test_restart_does_not_repeat_uncertain_work(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        c.store.ingest_events([user_event(1)], self.clock.now())
        c.router.reconcile_obligations()
        oid = c.store.obligations()[0]["id"]
        c.store.put_assignment({
            "id": "asg-crash-low-1", "obligation_id": oid,
            "worker": "low", "role": "response", "status": "running", "attempt": 1,
            "idempotent": False, "job": {}, "started_at": 1, "progress_at": 1,
        })
        aid = "asg-crash-low-1"
        c.close()
        config = make_config(self.dir)
        c2 = Coordinator(config, mode="active", clock=self.clock, board=self.board,
                         jev=FakeJev(), runner=runner, start_cursor=0)
        c2.open()
        self.addCleanup(c2.close)
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
        c2 = Coordinator(config, mode="active", clock=self.clock, board=self.board,
                         jev=FakeJev(), runner=runner, start_cursor=0)
        opened = c2.open()
        self.addCleanup(c2.close)
        self.assertEqual(c2.store.assignment("asg-idem-low-9")["status"], "pending")
        self.assertTrue(
            "asg-idem-low-9" in opened["recovery"]["restarted"]
            or c2.store.assignment("asg-idem-low-9")["status"] == "pending")

    def test_unanswered_survive_restart(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, mode="shadow")
        self.board.add(user_event(1, "still open"))
        c.tick(wait=True)
        oid = c.store.obligations()[0]["id"]
        self.assertEqual(c.store.obligation(oid)["status"], "received")
        c.close()
        config = make_config(self.dir)
        c2 = Coordinator(config, mode="shadow", clock=self.clock, board=self.board,
                         jev=FakeJev(), runner=runner, start_cursor=0)
        c2.open()
        self.addCleanup(c2.close)
        self.assertEqual(c2.store.obligation(oid)["status"], "received")

    def test_ingest_without_obligation_is_recovered(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, mode="shadow")
        c.store.ingest_events([user_event(4, "lost-create")], self.clock.now())
        self.assertEqual(c.store.obligations(), [])
        self.assertEqual(c.store.ingest_cursor(), 4)
        c.close()
        config = make_config(self.dir)
        c2 = Coordinator(config, mode="shadow", clock=self.clock, board=self.board,
                         jev=FakeJev(), runner=runner, start_cursor=0)
        c2.open()
        self.addCleanup(c2.close)
        self.assertEqual(len(c2.store.obligations()), 1)
        self.assertEqual(c2.store.obligations()[0]["question_text"], "lost-create")

    def test_active_refuses_uninitialized_history(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        config = make_config(self.dir)
        config.raw.pop("start_cursor", None)
        c = Coordinator(config, mode="active", clock=self.clock, board=self.board,
                        jev=FakeJev(), runner=runner, start_cursor=None)
        with self.assertRaisesRegex(RuntimeError, "uninitialized history"):
            c.open()
        if c.lock_handle is not None:
            c.lock_handle.close()
        c.store.close()

    def test_stale_reply_is_blocked_and_rerouted_once(self):
        gate = threading.Event()

        def slow(job):
            gate.wait(2)
            return ok_reply(job, "stale-answer")

        runner = InProcessRunner({"low": slow, "high": lambda job: ok_reply(job, "fresh")})
        c = self.coord(runner)
        self.board.add(user_event(1, "first"))
        c.tick(wait=False)
        deadline = time.time() + 2
        while time.time() < deadline and not c.store.assignments_by_status("running"):
            c._drain()
            c._launch()
            time.sleep(0.02)
        self.board.add(user_event(2, "correction"))
        c.tick(wait=False)
        gate.set()
        c.tick(wait=True)
        stale = [row for row in c.store.outbox_all() if row["status"] == "blocked_stale"]
        self.assertTrue(stale)
        high = [a for a in c.store.assignments_by_status("succeeded", "failed", "pending", "running")
                if a["worker"] == "high" or (a.get("job") or {}).get("tier") == "high"]
        self.assertTrue(high)
        job = high[0]["job"]
        self.assertEqual(job.get("thread_revision"), 2)
        self.assertTrue(job.get("previous_attempt"))
        texts = [e.get("text") for e in job.get("thread_context") or []]
        self.assertIn("correction", texts)

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
            "job": {"kind": "code", "role": "code"}, "started_at": self.clock.now(),
            "progress_at": self.clock.now(),
        })
        c.tick(wait=False)
        self.assertTrue(started.wait(2))
        self.board.add(user_event(1, "ping"))
        c.tick(wait=False)
        deadline = time.time() + 2
        replies = []
        while time.time() < deadline:
            c._drain()
            c._launch()
            replies = [a for a in c.store.assignments_by_status("succeeded") if a["worker"] == "low"]
            if replies:
                break
            time.sleep(0.05)
        self.assertTrue(replies)
        self.assertEqual(c.store.assignment("asg-code-1")["status"], "running")
        release.set()
        c.tick(wait=True)

    def test_blocked_check_does_not_block_ingest(self):
        started = threading.Event()
        release = threading.Event()

        def slow_check(command, timeout=60, cwd=None):
            started.set()
            release.wait(30)
            return {"command": command, "exit_code": 0, "adapter": "trusted"}

        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "low": ok_reply,
            "high": ok_reply,
            "code": lambda job: {"ok": True, "kind": "code_result", "text": "patch",
                                 "check_ids": []},
        })
        c = self.coord(runner, jev=FakeJev(intents=["dispatch_work"]), extra_workers=extra,
                       check_runner=slow_check)
        self.board.add(user_event(1, "build the thing"))
        c.tick(wait=False)
        deadline = time.time() + 2
        while time.time() < deadline and not started.is_set():
            c._drain()
            c._launch()
            time.sleep(0.02)
        self.assertTrue(started.is_set())
        self.board.add(noise_event(2, "server", "heartbeat"))
        self.board.add(user_event(3, "are you there?"))
        t0 = time.time()
        c.tick(wait=False)
        self.assertLess(time.time() - t0, 1.0)
        self.assertGreaterEqual(c.store.ingest_cursor(), 3)
        self.assertTrue(c.store.event(2))
        self.assertTrue(c.store.event(3))
        release.set()
        c.tick(wait=True)

    def test_missing_jev_key_cannot_approve_code(self):
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "low": ok_reply,
            "high": ok_reply,
            "code": lambda job: {"ok": True, "kind": "code_result", "text": "done"},
        })
        c = self.coord(runner, jev=FakeJev(configured=False, intents=["dispatch_work"]),
                       extra_workers=extra)
        self.board.add(user_event(1, "ship it"))
        c.tick(wait=True)
        obl = c.store.obligations()[0]
        self.assertEqual(obl["status"], "held")
        self.assertIn("missing_jev_key", obl["last_error"])
        self.assertEqual(self.board.posts, [])

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
        high_job = [a for a in c.store.assignments_for(c.store.obligations()[0]["id"])
                    if a["worker"] == "high"][0]["job"]
        self.assertEqual(high_job["previous_attempt"]["rejection"], "cheap-fail")

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

    def test_max_escalation_counts_all_high_assignments(self):
        runner = InProcessRunner({
            "low": lambda job: {"ok": False, "error": "nope"},
            "high": lambda job: {"ok": False, "error": "high-nope"},
        })
        c = self.coord(runner, max_escalation_retries=1)
        self.board.add(user_event(1, "once"))
        c.tick(wait=True)
        oid = c.store.obligations()[0]["id"]
        self.assertEqual(c.router.high_used(oid), 1)
        again = c.router._assign(oid, "high", role="response", tier="high",
                                 previous={"rejection": "again", "thread_revision": 1})
        self.assertIsNone(again)
        self.assertEqual(c.store.obligation(oid)["last_error"], "escalation_exhausted")

    def test_ambiguous_does_not_execute_privileged_work(self):
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply, "code": ok_reply})
        c = self.coord(runner, jev=FakeJev(intents=["answer_question", "dispatch_work"]),
                       extra_workers=extra)
        self.board.add(user_event(1, "answer this and also build that"))
        c.tick(wait=True)
        obl = c.store.obligations()[0]
        self.assertEqual(obl["status"], "held")
        self.assertFalse(c.store.assignments_for(obl["id"]))

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
        self.assertEqual(c.store.outbox_all(), [])

    def test_active_publish_uses_stable_key(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, mode="active")
        self.board.add(user_event(1, "hi"))
        c.tick(wait=True)
        self.assertTrue(self.board.posts)
        self.assertIsNotNone(self.board.posts[0][2])
        keys = [row["idempotency_key"] for row in c.store.outbox_all()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(k.startswith("reply:") for k in keys))
        self.assertEqual(keys[0], self.board.posts[0][2])
        c.tick(wait=True)
        self.assertEqual(len(self.board.posts), 1)
        answered = c.store.obligations("answered")
        self.assertEqual(len(answered), 1)

    def test_publish_skipped_when_not_caught_up(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1, "one"))
        self.board.head = 99
        c.tick(wait=True)
        self.assertFalse(c.caught_up)
        self.assertEqual(self.board.posts, [])
        self.assertTrue(c.store.outbox_all() or c.store.assignments_by_status("succeeded"))

    def test_uncertain_send_is_not_duplicated(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1, "hi"))
        self.board.fail_posts = True
        c.tick(wait=True)
        uncertain = [r for r in c.store.outbox_all() if r["status"] == "uncertain"]
        self.assertTrue(uncertain)
        self.assertEqual(c.store.obligations()[0]["status"] != "answered", True)
        self.board.fail_posts = False
        c.tick(wait=True)
        self.assertEqual(len(c.store.outbox_all()), 1)
        self.assertEqual(c.store.outbox_all()[0]["status"], "uncertain")

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
        other = Coordinator(config, mode="active", clock=self.clock, board=self.board,
                            jev=FakeJev(), runner=runner, start_cursor=0)
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

    def test_cache_key_includes_full_request(self):
        a = judgments.routing_request("alpha question", "sidebar")
        b = judgments.routing_request("beta question", "sidebar")
        ka = judgments.cache_key("pv", "jev-1.13.0", a, a["state"])
        kb = judgments.cache_key("pv", "jev-1.13.0", b, b["state"])
        self.assertNotEqual(ka, kb)
        self.assertEqual(ka, judgments.cache_key("pv", "jev-1.13.0", a, a["state"]))

    def test_classification_cache_reuses_identical_inputs(self):
        jev = FakeJev()
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=jev)
        self.board.add(user_event(1, "same-question"))
        c.tick(wait=True)
        self.board.add(user_event(2, "other-question"))
        c.tick(wait=True)
        self.board.add(user_event(3, "same-question"))
        c.tick(wait=True)
        self.assertEqual(len(jev.calls), 2)
        self.assertEqual(jev.calls[0]["question_text"], "same-question")
        self.assertEqual(jev.calls[1]["question_text"], "other-question")

    def test_verify_candidate_sees_actual_reply_text(self):
        jev = FakeJev()
        runner = InProcessRunner({"low": lambda job: ok_reply(job, "the-real-answer"),
                                  "high": ok_reply})
        c = self.coord(runner, jev=jev)
        self.board.add(user_event(1, "what is 2+2?"))
        c.tick(wait=True)
        self.assertTrue(jev.verify_calls)
        self.assertEqual(jev.verify_calls[0]["candidate"], "the-real-answer")
        self.assertEqual(jev.verify_calls[0]["task"], "what is 2+2?")
        self.assertFalse(jev.verify_calls[0].get("code_change"))

    def test_worker_claim_does_not_skip_verify(self):
        jev = FakeJev(accept=False)
        runner = InProcessRunner({
            "low": lambda job: {"ok": True, "kind": "reply", "text": "nope",
                                "addresses_obligation": True},
            "high": lambda job: {"ok": True, "kind": "reply", "text": "nope",
                                 "addresses_obligation": True},
        })
        c = self.coord(runner, jev=jev)
        self.board.add(user_event(1, "real question"))
        c.tick(wait=True)
        self.assertEqual(self.board.posts, [])
        self.assertTrue(jev.verify_calls)
        self.assertNotEqual(c.store.obligations()[0]["status"], "answered")

    def test_empty_text_and_unknown_kind_fail(self):
        runner = InProcessRunner({
            "low": lambda job: {"ok": True, "kind": "mystery", "text": "x",
                                "addresses_obligation": True},
            "high": lambda job: {"ok": True, "kind": "reply", "text": "",
                                 "addresses_obligation": True},
        })
        c = self.coord(runner)
        self.board.add(user_event(1, "hi"))
        c.tick(wait=True)
        errors = [a["last_error"] for a in c.store.assignments_for(c.store.obligations()[0]["id"])]
        self.assertTrue(any("unknown_result_kind" in (e or "") for e in errors))
        self.assertTrue(any("empty_text" in (e or "") for e in errors))
        self.assertEqual(self.board.posts, [])

    def test_missing_verify_method_prevents_publish(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=NoVerifyJev())
        self.board.add(user_event(1, "hi"))
        c.tick(wait=True)
        self.assertEqual(self.board.posts, [])
        self.assertTrue(any("missing_verify_candidate" in (a["last_error"] or "")
                            for a in c.store.assignments_for(c.store.obligations()[0]["id"])))

    def test_code_role_ignores_worker_kind_reply(self):
        observed = []

        def recording_check(command, timeout=60, cwd=None):
            observed.append({"command": command, "timeout": timeout, "cwd": cwd})
            return {"command": command, "exit_code": 0, "adapter": "trusted"}

        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "low": ok_reply,
            "high": ok_reply,
            "code": lambda job: {"ok": True, "kind": "reply", "text": "diff here",
                                 "addresses_obligation": True, "check_ids": []},
        })
        jev = FakeJev(intents=["dispatch_work"])
        c = self.coord(runner, jev=jev, extra_workers=extra, check_runner=recording_check)
        self.board.add(user_event(1, "please implement"))
        c.tick(wait=True)
        self.assertTrue(observed)
        self.assertTrue(jev.verify_calls)
        self.assertTrue(jev.verify_calls[0].get("code_change"))

    def test_worker_cannot_omit_failing_catalog_check(self):
        checks = [
            {"id": "pass-check", "command": [sys.executable, "-c", "raise SystemExit(0)"]},
            {"id": "fail-check", "command": [sys.executable, "-c", "raise SystemExit(1)"]},
        ]
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "low": ok_reply,
            "high": ok_reply,
            "code": lambda job: {"ok": True, "kind": "code_result", "text": "patch",
                                 "check_ids": ["pass-check"]},
        })
        c = self.coord(runner, jev=FakeJev(intents=["dispatch_work"]), extra_workers=extra,
                       extra_checks=checks, check_runner=None)
        self.board.add(user_event(1, "fix the bug"))
        c.tick(wait=True)
        errors = [a["last_error"] or "" for a in c.store.assignments_for(c.store.obligations()[0]["id"])]
        self.assertTrue(any("fail-check" in e for e in errors))
        self.assertEqual(self.board.posts, [])

    def test_code_task_without_catalog_is_rejected(self):
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "code": lambda job: {"ok": True, "kind": "code_result", "text": "patch"},
            "low": ok_reply, "high": ok_reply,
        })
        c = self.coord(runner, jev=FakeJev(intents=["dispatch_work"]), extra_workers=extra,
                       extra_checks=[], check_runner=None)
        self.board.add(user_event(1, "implement"))
        c.tick(wait=True)
        errors = [a["last_error"] or "" for a in c.store.assignments_for(c.store.obligations()[0]["id"])]
        self.assertTrue(any("no required catalog checks" in e for e in errors))

    def test_model_generated_commands_are_rejected(self):
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "code": lambda job: {"ok": True, "kind": "code_result", "text": "patch",
                                 "model_command": ["rm", "-rf", "/"]},
            "low": ok_reply, "high": ok_reply,
        })
        c = self.coord(runner, jev=FakeJev(intents=["dispatch_work"]), extra_workers=extra)
        self.board.add(user_event(1, "implement"))
        c.tick(wait=True)
        errors = [a["last_error"] or "" for a in c.store.assignments_for(c.store.obligations()[0]["id"])]
        self.assertTrue(any("model-generated" in e for e in errors))

    def test_catalog_cwd_and_timeout_are_honored(self):
        observed = []
        nested = self.dir / "nested"
        nested.mkdir()
        (nested / "marker").write_text("ok", encoding="utf-8")

        def recording_check(command, timeout=60, cwd=None):
            observed.append({"command": list(command), "timeout": timeout, "cwd": cwd})
            return {"command": command, "exit_code": 0, "adapter": "trusted"}

        checks = [{
            "id": "cwd-check",
            "command": [sys.executable, "-c", "print(open('marker').read())"],
            "cwd": "nested",
            "timeout_seconds": 12,
        }]
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "code": lambda job: {"ok": True, "kind": "code_result", "text": "patch"},
            "low": ok_reply, "high": ok_reply,
        })
        c = self.coord(runner, jev=FakeJev(intents=["dispatch_work"]), extra_workers=extra,
                       extra_checks=checks, check_runner=recording_check)
        self.board.add(user_event(1, "implement"))
        c.tick(wait=True)
        self.assertTrue(observed)
        self.assertEqual(observed[0]["timeout"], 12)
        self.assertTrue(str(observed[0]["cwd"]).endswith("nested"))

    def test_validate_code_result_ignores_worker_check_ids(self):
        catalog = {
            "must-run": {"id": "must-run", "command": [sys.executable, "-c", "raise SystemExit(1)"],
                         "cwd": str(self.dir), "timeout_seconds": 5},
        }
        with self.assertRaisesRegex(EvidenceError, "must-run"):
            validate_code_result(
                {"ok": True, "check_ids": []},
                catalog,
                runner=lambda command, timeout=60, cwd=None: {
                    "command": command, "exit_code": 1, "adapter": "trusted"},
            )

    def test_zero_budget_means_zero_calls(self):
        jev = FakeJev()
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=jev, daily_max_calls=0, daily_max_spend_usd=0)
        self.board.add(user_event(1, "hello"))
        c.tick(wait=True)
        self.assertEqual(jev.calls, [])
        self.assertEqual(jev.verify_calls, [])
        self.assertEqual(c.store.assignments_for(c.store.obligations()[0]["id"]), [])
        self.assertEqual(c.store.obligations()[0]["status"], "held")
        from sprint_coordinator.routing import budget_day
        self.assertEqual(c.store.usage_day(budget_day(self.clock.now()))["calls"], 0)

    def test_failed_jev_attempt_is_counted_and_does_not_escalate(self):
        jev = FakeJev(route_error=judgments.JevUnavailable("down"))
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner, jev=jev)
        self.board.add(user_event(1, "hello"))
        c.tick(wait=True)
        from sprint_coordinator.routing import budget_day
        usage = c.store.usage_day(budget_day(self.clock.now()))
        self.assertGreaterEqual(usage["calls"], 1)
        self.assertEqual(c.store.obligations()[0]["status"], "held")
        self.assertFalse(any(a["worker"] == "high" for a in c.store.assignments_for(
            c.store.obligations()[0]["id"])))

    def test_budget_exhaustion_does_not_launch_high_worker(self):
        runner = InProcessRunner({
            "low": lambda job: {"ok": False, "error": "cheap-fail"},
            "high": lambda job: ok_reply(job, "should-not-run"),
        })
        c = self.coord(runner, daily_max_calls=2)
        self.board.add(user_event(1, "hello"))
        c.tick(wait=True)
        workers = [a["worker"] for a in c.store.assignments_for(c.store.obligations()[0]["id"])]
        self.assertNotIn("high", workers)
        self.assertIn(c.store.obligations()[0]["status"], ("held", "failed", "assigned"))

    def test_failed_candidate_is_preserved_on_escalation(self):
        jev = FakeJev(fail_first_verify=True)
        runner = InProcessRunner({
            "low": lambda job: ok_reply(job, "low-candidate"),
            "high": lambda job: ok_reply(job, "high-candidate"),
        })
        c = self.coord(runner, jev=jev)
        self.board.add(user_event(1, "question"))
        c.tick(wait=True)
        high = [a for a in c.store.assignments_for(c.store.obligations()[0]["id"])
                if a["worker"] == "high"]
        self.assertTrue(high)
        prev = high[0]["job"]["previous_attempt"]
        self.assertEqual(prev["candidate"], "low-candidate")
        self.assertIn("jev_did_not_accept", prev["rejection"])

    def test_thread_revision_matches_inputs(self):
        runner = InProcessRunner({"low": ok_reply, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(7, "rev-check"))
        c.tick(wait=True)
        job = c.store.assignments_for(c.store.obligations()[0]["id"])[0]["job"]
        self.assertEqual(job["thread_revision"], 7)
        self.assertEqual(job["thread_context"][-1]["seq"], 7)
        self.assertEqual(job["thread_context"][-1]["text"], "rev-check")

    def test_semantic_approval_does_not_authorize_merge(self):
        extra = {"code": {"command": ["code"], "role": "code", "cost": "low"}}
        runner = InProcessRunner({
            "code": lambda job: {"ok": True, "kind": "code_result", "text": "patch",
                                 "merge": True},
            "low": ok_reply, "high": ok_reply,
        })
        c = self.coord(runner, jev=FakeJev(intents=["dispatch_work"]), extra_workers=extra)
        self.board.add(user_event(1, "implement"))
        c.tick(wait=True)
        errors = [a["last_error"] or "" for a in c.store.assignments_for(c.store.obligations()[0]["id"])]
        self.assertTrue(any("not_authorization" in e or "authorization" in e for e in errors))
        self.assertEqual(self.board.posts, [])

    def test_close_records_unfinished_workers(self):
        hang = threading.Event()

        def hanging(job):
            hang.wait(10)
            return ok_reply(job, "late")

        runner = InProcessRunner({"low": hanging, "high": ok_reply})
        c = self.coord(runner)
        self.board.add(user_event(1, "hang"))
        c.tick(wait=False)
        deadline = time.time() + 2
        while time.time() < deadline and not c.store.assignments_by_status("running"):
            c._drain()
            c._launch()
            time.sleep(0.02)
        self.assertTrue(c.store.assignments_by_status("running"))
        aid = c.store.assignments_by_status("running")[0]["id"]
        c.close()
        config = make_config(self.dir)
        c2 = Coordinator(config, mode="active", clock=self.clock, board=self.board,
                         jev=FakeJev(), runner=InProcessRunner({"low": ok_reply, "high": ok_reply}),
                         start_cursor=0)
        c2.open()
        self.addCleanup(c2.close)
        self.assertEqual(c2.store.assignment(aid)["status"], "uncertain")
        hang.set()


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
                outer.state["posts"].append((self.path, body, self.headers.get("Idempotency-Key")))
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
        self.assertEqual(self.state["posts"], [])
        store = Store(Path(raw["data_dir"]) / "coordinator.sqlite", Clock(0))
        try:
            self.assertEqual(store.obligations("answered"), [])
            self.assertTrue(store.obligations())
        finally:
            store.close()
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
        self.assertNotEqual(active.returncode, 0)
        self.assertTrue(active.stderr or active.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
