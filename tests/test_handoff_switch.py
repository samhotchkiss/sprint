"""Owner-switch tests. Injected board/transport; no paid calls or live panes."""
from __future__ import annotations

import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from sprint_coordinator.handoff import (
    OwnerSwitch, TmuxSendTransport, main, snapshot_cards, snapshot_dispatch,
)
from sprint_coordinator.util import Clock


class FakeBoard:
    def __init__(self):
        self.cards = [
            {"num": 1, "title": "one", "state": "in_progress", "worktree": "/wt/1",
             "branch": "sprint/card-1", "executor": "grok", "model": "grok-4",
             "agent_name": "sprint-card-1", "pane": "%11"},
            {"num": 2, "title": "two", "state": "needs_you", "worktree": "/wt/2",
             "executor": "grok", "pane": "%12"},
        ]
        self.cursor = 40
        self.head = 40
        self.default_executor = "grok"
        self.owner = {"provider": "claude", "pane": "%5"}
        self.puts = []
        self.event_dispatcher = None

    def get(self, path):
        if path == "/api/settings":
            return {
                "session_tmux_window": self.owner.get("pane"),
                "settings": {
                    "worker": {"default_executor": self.default_executor},
                    "session_tmux_window": self.owner.get("pane"),
                },
            }
        if path == "/api/board":
            return {"cards": list(self.cards), "seq": self.head}
        if path.startswith("/api/events"):
            return {"events": [], "head": self.head, "cursor": self.cursor}
        if path == "/api/cursors/orchestrator":
            return {"seq": self.cursor}
        if path == "/api/autoheal":
            return {
                "event_dispatch_supported": True,
                "event_dispatcher": self.event_dispatcher,
                "tmux_window": self.owner.get("pane"),
            }
        raise AssertionError(path)

    def healthy_dispatcher(self, pane):
        self.event_dispatcher = {
            "status": "idle", "target": pane, "acknowledged": self.cursor,
            "scanned": self.cursor,
        }

    def request(self, method, path, body=None, timeout=5.0, idempotency_key=None):
        if method == "GET":
            return self.get(path)
        if method == "PUT" and path == "/api/settings":
            if body and "coordinator_owner" in body:
                raise AssertionError("coordinator_owner is unsupported")
            if body and "worker" in body:
                raise AssertionError("switch must not change worker settings")
            self.puts.append(dict(body or {}))
            pane = (body or {}).get("session_tmux_window")
            if pane:
                self.owner["pane"] = pane
            return self.get("/api/settings")
        raise AssertionError((method, path))

    def head_seq(self):
        return self.head


class FakeTransport:
    def __init__(self, status="delivered"):
        self.status = status
        self.sends = []

    def send(self, pane, prompt_path):
        text = Path(prompt_path).read_text()
        self.sends.append({"pane": pane, "path": str(prompt_path), "text": text})
        if self.status == "uncertain":
            return {"status": "uncertain", "exit": 4, "reason": "send_unverified"}
        if self.status == "blocked":
            return {"status": "blocked", "exit": 5, "reason": "dialog_refusal"}
        return {"status": "delivered", "exit": 0}


class FakePaneProbe:
    def __init__(self, panes=None, ok=True):
        self.panes = list(panes or [])
        self.ok = ok

    def existing_panes(self):
        if self.ok is not True:
            return {"ok": False, "reason": "tmux_probe_failed", "panes": []}
        return {"ok": True, "panes": list(self.panes)}


def cards_fixture():
    return FakeBoard().cards


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.board_dir = self.dir / ".sprint"
        self.board_dir.mkdir()
        (self.board_dir / "server.json").write_text(json.dumps({
            "port": 8765, "token": "secret-board-token", "board_id": "yunagi",
        }))
        os.chmod(self.board_dir / "server.json", 0o600)
        self.board = FakeBoard()
        self.transport = FakeTransport()
        self.clock = Clock(1)
        self.probe = FakePaneProbe(["%5", "%8", "%11"])
        self.switch = OwnerSwitch(
            self.board_dir, board=self.board, transport=self.transport,
            project_root=self.dir, switch_dir=self.board_dir, clock=self.clock,
            pane_probe=self.probe)

    def plan(self, **kwargs):
        args = dict(source_pane="%5", source_provider="claude",
                    target_pane="%8", target_provider="codex")
        args.update(kwargs)
        return self.switch.plan(**args)

    def test_plan_preserves_board_cards_executor_and_cursor(self):
        rec = self.plan()
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["stage"], "prepared")
        self.assertEqual(rec["preserve"]["default_executor"], "grok")
        self.assertEqual(rec["preserve"]["cards"],
                         snapshot_cards({"cards": self.board.cards}))
        self.assertEqual(rec["cursor"]["seq"], 40)
        self.assertEqual(rec["head_at_plan"], 40)
        self.assertTrue(rec["cursor"]["never_skip_to_head"])
        self.assertEqual(rec["board"]["port"], 8765)
        self.assertEqual(rec["board"]["url"], "http://127.0.0.1:8765")
        blob = json.dumps(rec)
        self.assertNotIn("secret-board-token", blob)
        self.assertNotIn("secret-board-token", rec["next_action"])

    def test_providers_share_tmux_send_protocol(self):
        for src, dst in (("claude", "codex"), ("codex", "grok"), ("grok", "claude")):
            with self.subTest(src=src, dst=dst):
                self.transport.sends.clear()
                rec = self.plan(source_provider=src, target_provider=dst,
                                source_pane="%5", target_pane="%8")
                self.assertTrue(rec["ok"], rec)
                self.switch.advance()
                send = self.transport.sends[-1]
                self.assertEqual(send["pane"], "%5")
                text = send["text"]
                self.assertIn("idle board monitor", text)
                self.assertIn("Keep running task workers", text)
                self.assertIn("Project root: %s" % self.dir.resolve(), text)
                self.assertNotIn("--prompt-file", text)
                rec["stage"] = "failed"
                rec["fail_reason"] = "reset"
                self.switch._save(rec)

    def test_failed_source_ack_wrong_nonce(self):
        rec = self.plan()
        self.switch.advance()
        bad = self.switch.ack(role="source", switch_id=rec["id"], nonce="nope")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "nonce_mismatch")
        self.assertEqual(self.switch.load()["stage"], "prepared")
        self.assertNotIn("source", self.switch.load().get("receipts") or {})

    def test_failed_target_ack_cannot_skip_to_head(self):
        rec = self.plan()
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        self.switch.advance()
        self.switch.advance()
        self.board.head = 99
        bad = self.switch.ack(
            role="target", switch_id=rec["id"], nonce=rec["nonce"], cursor_seq=99)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "skip_pending_events")
        good = self.switch.ack(
            role="target", switch_id=rec["id"], nonce=rec["nonce"], cursor_seq=40)
        self.assertTrue(good["ok"])

    def test_crash_after_send_does_not_resend(self):
        rec = self.plan()
        self.switch.advance()
        self.assertEqual(len(self.transport.sends), 1)
        other = OwnerSwitch(
            self.board_dir, board=self.board, transport=self.transport,
            project_root=self.dir, switch_dir=self.board_dir, clock=self.clock)
        other.advance()
        self.assertEqual(len(self.transport.sends), 1)
        self.assertEqual(other.load()["sends"]["source_quiesce"]["status"], "delivered")

    def test_uncertain_send_is_not_replayed(self):
        self.transport.status = "uncertain"
        rec = self.plan()
        first = self.switch.advance()
        self.assertEqual(first["reason"], "uncertain")
        self.switch.advance()
        self.assertEqual(len(self.transport.sends), 1)
        self.assertEqual(self.switch.load()["sends"]["source_quiesce"]["status"],
                         "uncertain")

    def test_new_events_during_switch_keep_planned_cursor(self):
        rec = self.plan()
        self.board.head = 55
        self.board.cards.append({"num": 3, "state": "queued", "worktree": "/wt/3"})
        st = self.switch.status()
        self.assertTrue(st["pending_since_plan"])
        self.assertEqual(st["cursor"]["seq"], 40)
        self.assertNotEqual(st["cursor"]["seq"], st["observed_head"])
        self.assertFalse(st["cards_unchanged"])
        self.assertEqual(self.switch.load()["preserve"]["cards"][0]["worktree"], "/wt/1")

    def test_card_preservation_and_default_executor(self):
        rec = self.plan()
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        self.switch.advance()
        self.assertEqual(self.board.default_executor, "grok")
        self.assertEqual(self.board.owner["pane"], "%8")
        self.assertTrue(self.board.puts)
        self.assertEqual(self.board.puts[0]["session_tmux_window"], "%8")
        self.assertEqual(self.board.puts[0]["actor"], "session")
        self.assertNotIn("coordinator_owner", self.board.puts[0])
        self.assertNotIn("worker", self.board.puts[0])
        self.assertEqual(rec["preserve"]["cards"][0]["pane"], "%11")
        self.assertEqual(rec["preserve"]["cards"][0]["worktree"], "/wt/1")

    def test_switchback_after_complete(self):
        rec = self.plan()
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        self.switch.advance()
        self.switch.advance()
        self.switch.ack(role="target", switch_id=rec["id"], nonce=rec["nonce"],
                        cursor_seq=40)
        waiting = self.switch.advance()
        self.assertEqual(waiting.get("stage"), "target_acknowledged")
        blocked = self.switch.advance()
        self.assertEqual(blocked.get("reason"), "await_target_health")
        self.assertEqual(self.switch.load()["stage"], "target_acknowledged")
        self.board.healthy_dispatcher("%8")
        done = self.switch.advance()
        self.assertEqual(done.get("stage"), "complete")
        back = self.plan(source_pane="%8", source_provider="codex",
                         target_pane="%5", target_provider="claude")
        self.assertTrue(back["ok"])
        self.assertEqual(back["preserve"]["default_executor"], "grok")
        self.assertEqual(back["preserve"]["cards"][0]["worktree"], "/wt/1")

    def test_rollback_before_target_consumes_and_not_after(self):
        rec = self.plan()
        self.switch.advance()
        rolled = self.switch.rollback()
        self.assertTrue(rolled["ok"])
        self.assertEqual(self.switch.load()["stage"], "failed")
        rec = self.plan()
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        self.switch.advance()
        sent = self.switch.advance()
        self.assertTrue(sent.get("sends", {}).get("target_register", {}).get("status")
                        in ("delivered", "uncertain", "prepared") or sent.get("stage") in (
                            "target_registered", "source_quiesced"))
        blocked = self.switch.rollback()
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "target_active")
        self.assertEqual(self.board.owner["pane"], "%8")

    def test_cli_plan_advance_ack_json(self):
        board = self.board
        transport = self.transport
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([
                "plan", "--board-data-dir", str(self.board_dir),
                "--project-root", str(self.dir),
                "--source-pane", "%5", "--source-provider", "claude",
                "--target-pane", "%8", "--target-provider", "codex",
                "--switch-dir", str(self.board_dir),
            ], board=board, transport=transport)
        self.assertEqual(code, 0)
        printed = json.loads(buf.getvalue())
        self.assertEqual(printed["nonce"], "<redacted>")
        dumped = json.dumps(printed)
        rec_nonce = json.loads((self.board_dir / "nonce").read_text())["nonce"]
        self.assertNotIn(rec_nonce, dumped)
        rec = json.loads((self.board_dir / "owner-switch.json").read_text())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                "advance", "--board-data-dir", str(self.board_dir),
                "--switch-dir", str(self.board_dir),
            ], board=board, transport=transport), 0)
        with redirect_stdout(io.StringIO()):
            code = main([
                "ack", "--board-data-dir", str(self.board_dir),
                "--switch-dir", str(self.board_dir),
                "--role", "source", "--switch-id", rec["id"],
                "--nonce-file", str(self.board_dir / "nonce"),
            ], board=board, transport=transport)
        self.assertEqual(code, 0)

    def test_same_pane_rejected_and_transport_command_shape(self):
        bad = self.plan(target_pane="%5")
        self.assertEqual(bad["reason"], "competing_owner_same_pane")
        recorded = []

        class Capture:
            def send(self, pane, prompt_path):
                recorded.append([
                    "/abs/tmux-send", "--no-stash", "--wait", "0", pane,
                    "--file", str(prompt_path),
                ])
                return {"status": "delivered", "exit": 0}

        self.switch.transport = Capture()
        self.plan()
        self.switch.advance()
        self.assertEqual(recorded[0][1:5], ["--no-stash", "--wait", "0", "%5"])
        self.assertEqual(recorded[0][5], "--file")

    def _write_dispatch(self, **fields):
        state = {
            "version": 1, "target": "%5", "scanned": 20, "acknowledged": 20,
            "pending": [{"seq": 21, "card": 1, "kind": "chat"}],
            "inflight": {"through": 20, "at": 1, "result": 0},
            "wake_times": [1.0], "status": "awaiting_ack",
        }
        state.update(fields)
        path = self.board_dir / "dispatch.json"
        path.write_text(json.dumps(state))
        os.chmod(path, 0o600)
        return state

    def test_rebind_preserves_pending_and_clears_inflight_after_source_stop(self):
        before = self._write_dispatch()
        rec = self.plan()
        self.assertEqual(rec["dispatch"]["at_plan"]["pending"], before["pending"])
        self.assertEqual(rec["dispatch"]["at_plan"]["scanned"], 20)
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        advanced = self.switch.advance()
        self.assertTrue(advanced["ok"], advanced)
        self.assertEqual(advanced["stage"], "source_quiesced")
        live = json.loads((self.board_dir / "dispatch.json").read_text())
        self.assertEqual(live["target"], "%8")
        self.assertIsNone(live["inflight"])
        self.assertEqual(live["pending"], before["pending"])
        self.assertEqual(live["scanned"], 20)
        self.assertEqual(live["acknowledged"], 20)
        self.assertEqual(live["wake_times"], [1.0])
        self.assertNotEqual(live["acknowledged"], self.board.head)
        rebound = advanced["dispatch_rebind"]["rebind"]
        self.assertEqual(rebound["target"], "%8")
        self.assertEqual(snapshot_dispatch(live)["pending"], before["pending"])

    def test_uncertain_inflight_blocks_until_cursor_or_abandon(self):
        self._write_dispatch(inflight={"through": 50, "at": 1, "result": 4})
        rec = self.plan()
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        blocked = self.switch.advance()
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "uncertain_inflight")
        live = json.loads((self.board_dir / "dispatch.json").read_text())
        self.assertEqual(live["target"], "%5")
        self.assertEqual(live["inflight"]["through"], 50)
        self.assertEqual(live["pending"][0]["seq"], 21)
        self.board.cursor = 50
        cleared = self.switch.advance()
        self.assertTrue(cleared["ok"], cleared)
        live = json.loads((self.board_dir / "dispatch.json").read_text())
        self.assertEqual(live["target"], "%8")
        self.assertIsNone(live["inflight"])
        self.assertEqual(live["pending"][0]["seq"], 21)

        parked = self.switch.load()
        parked["stage"] = "failed"
        parked["fail_reason"] = "split-test"
        self.switch._save(parked)
        rec2 = self.plan(source_pane="%8", source_provider="codex",
                         target_pane="%9", target_provider="grok")
        self._write_dispatch(target="%8", inflight={"through": 80, "result": -1})
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec2["id"], nonce=rec2["nonce"],
                        abandon_inflight=True)
        abandoned = self.switch.advance()
        self.assertTrue(abandoned["ok"], abandoned)
        self.assertEqual(abandoned["dispatch_rebind"]["action"], "abandoned")
        live = json.loads((self.board_dir / "dispatch.json").read_text())
        self.assertEqual(live["target"], "%9")
        self.assertIsNone(live["inflight"])
        self.assertEqual(live["pending"][0]["seq"], 21)

    def test_dispatch_lock_blocks_rebind(self):
        self._write_dispatch()
        rec = self.plan()
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        handle = open(self.board_dir / "dispatch.lock", "a+")
        os.chmod(self.board_dir / "dispatch.lock", 0o600)
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(handle.close)
        blocked = self.switch.advance()
        self.assertEqual(blocked["reason"], "dispatcher_running")
        live = json.loads((self.board_dir / "dispatch.json").read_text())
        self.assertEqual(live["target"], "%5")
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        ok = self.switch.advance()
        self.assertTrue(ok["ok"], ok)
        live = json.loads((self.board_dir / "dispatch.json").read_text())
        self.assertEqual(live["target"], "%8")

    def test_exited_source_requires_tmux_absence_and_free_locks(self):
        rec = self.plan()
        live = self.switch.acknowledge_exited_source()
        self.assertEqual(live["reason"], "source_pane_live")
        self.assertNotIn("source", self.switch.load().get("receipts") or {})
        self.probe.ok = False
        self.probe.panes = []
        failed = self.switch.acknowledge_exited_source()
        self.assertEqual(failed["reason"], "tmux_probe_failed")
        self.probe.ok = True
        self.probe.panes = ["%8", "%11"]
        handle = open(self.board_dir / "dispatch.lock", "a+")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(handle.close)
        locked = self.switch.acknowledge_exited_source()
        self.assertEqual(locked["reason"], "dispatcher_running")
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        self.board.cursor = 52
        ok = self.switch.acknowledge_exited_source()
        self.assertTrue(ok["ok"], ok)
        stored = self.switch.load()
        self.assertTrue(stored["source_exit"]["absent"])
        self.assertEqual(stored["source_exit"]["pane"], "%5")
        self.assertEqual(stored["source_exit"]["verified_by"][:2], ["tmux", "list-panes"])
        self.assertEqual(stored["cursor"]["start_seq"], 52)
        self.assertNotIn("source", stored.get("receipts") or {})
        continued = self.switch.advance()
        self.assertEqual(continued.get("stage"), "source_quiesced")
        self.assertEqual(self.board.owner["pane"], "%8")

    def test_legacy_source_receipt_without_cursor_seq_is_accepted(self):
        self.board.cursor = 20
        self.board.head = 20
        rec = self.plan()
        self.switch.advance()
        path = self.board_dir / "receipts" / "source.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({
            "switch_id": rec["id"], "nonce": rec["nonce"], "role": "source",
            "at": 1, "abandon_inflight": False,
        }))
        os.chmod(path, 0o600)
        self.board.head = 27
        out = self.switch.advance()
        self.assertTrue(out["ok"], out)
        stored = self.switch.load()
        self.assertEqual(stored["stage"], "source_quiesced")
        self.assertEqual(stored["cursor"]["start_seq"], 20)
        self.assertEqual(out["observed_head"], 27)
        self.assertTrue(out["pending_since_plan"])

    def test_plan_rejects_unregistered_source_pane(self):
        bad = self.plan(source_pane="%9", target_pane="%8")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "source_not_registered")

    def test_crash_prepared_send_is_uncertain_and_receipt_settles(self):
        rec = self.plan()
        rec["sends"] = {"source_quiesce": {"status": "prepared", "pane": "%5"}}
        self.switch._save(rec)
        blocked = self.switch.advance()
        self.assertEqual(blocked["reason"], "uncertain")
        self.assertEqual(self.switch.load()["sends"]["source_quiesce"]["status"],
                         "uncertain")
        self.assertEqual(len(self.transport.sends), 0)
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        settled = self.switch.advance()
        self.assertTrue(settled["ok"], settled)
        self.assertEqual(settled["stage"], "source_quiesced")

    def test_file_target_receipt_enforces_cursor(self):
        rec = self.plan()
        self.switch.advance()
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        self.switch.advance()
        self.switch.advance()
        self.board.head = 99
        path = self.board_dir / "receipts" / "target.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({
            "switch_id": rec["id"], "nonce": rec["nonce"], "role": "target",
            "cursor_seq": 99,
        }))
        os.chmod(path, 0o600)
        out = self.switch.advance()
        self.assertNotEqual(out.get("stage"), "target_acknowledged")
        self.assertEqual(self.switch.load().get("receipt_error"), "skip_pending_events")

    def test_source_ack_uses_live_board_cursor(self):
        rec = self.plan()
        self.switch.advance()
        self.board.cursor = 47
        self.switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        self.assertEqual(self.switch.load()["cursor"]["start_seq"], 47)
        self.switch.advance()
        self.switch.advance()
        self.board.head = 80
        bad = self.switch.ack(role="target", switch_id=rec["id"], nonce=rec["nonce"],
                              cursor_seq=80)
        self.assertEqual(bad["reason"], "skip_pending_events")
        good = self.switch.ack(role="target", switch_id=rec["id"], nonce=rec["nonce"],
                               cursor_seq=47)
        self.assertTrue(good["ok"], good)

    def test_handoff_has_no_launchd_specifics(self):
        import sprint_coordinator.handoff as mod
        source = Path(mod.__file__).read_text()
        for banned in ("launchd", "launchctl", "LaunchAgents", "KeepAlive", "plist"):
            self.assertNotIn(banned, source)

    def test_tmux_send_transport_uses_command_array(self):
        fake = self.dir / "tmux-send"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o700)
        t = TmuxSendTransport(fake)
        prompt = self.dir / "p.txt"
        prompt.write_text("hi\n")
        result = t.send("%3", prompt)
        self.assertEqual(result["status"], "delivered")


try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_sprintd import Base as SprintdBase
except ImportError:  # pragma: no cover
    SprintdBase = None


@unittest.skipIf(SprintdBase is None, "sprintd test base unavailable")
class HandoffHttpTests(SprintdBase):
    def test_observe_uses_board_seq_as_head(self):
        from sprint_coordinator.board import BoardClient
        from sprint_coordinator.handoff import observe
        data = Path(self.app.data_dir)
        (data / "server.json").write_text(json.dumps({
            "port": self.port, "token": "test-token",
        }))
        os.chmod(data / "server.json", 0o600)
        self.new_card("during-switch message")
        status, board = self.get("/api/board")
        self.assertEqual(status, 200)
        self.assertIn("seq", board)
        self.assertNotIn("head", board)
        self.assertGreater(int(board["seq"]), 0)
        client = BoardClient(data)
        seen = observe(client)
        self.assertEqual(seen["head_seq"], int(board["seq"]))
        status, events = self.get("/api/events?after=0&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(int(events["head"]), seen["head_seq"])

    def test_put_session_window_roundtrip(self):
        from sprint_coordinator.board import BoardClient
        data = Path(self.app.data_dir)
        (data / "server.json").write_text(json.dumps({
            "port": self.port, "token": "test-token",
        }))
        os.chmod(data / "server.json", 0o600)
        status, body = self.req("PUT", "/api/settings", {
            "session_tmux_window": "%5", "actor": "session",
        })
        self.assertEqual(status, 200, body)
        client = BoardClient(data)
        transport = FakeTransport()
        switch = OwnerSwitch(
            data, board=client, transport=transport,
            project_root=Path(self.project_root), switch_dir=data)
        rec = switch.plan(
            source_pane="%5", source_provider="claude",
            target_pane="%8", target_provider="codex")
        self.assertTrue(rec["ok"], rec)
        switch.advance()
        switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        out = switch.advance()
        self.assertTrue(out["ok"], out)
        status, settings = self.get("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(settings.get("session_tmux_window"), "%8")
        worker = (settings.get("settings") or {}).get("worker") or {}
        self.assertEqual(worker.get("default_executor"),
                         rec["preserve"]["default_executor"])

    def test_complete_requires_live_event_dispatcher(self):
        from sprint_coordinator.board import BoardClient
        data = Path(self.app.data_dir)
        (data / "server.json").write_text(json.dumps({
            "port": self.port, "token": "test-token",
        }))
        os.chmod(data / "server.json", 0o600)
        self.req("PUT", "/api/settings", {
            "session_tmux_window": "%5", "actor": "session",
        })
        client = BoardClient(data)
        switch = OwnerSwitch(
            data, board=client, transport=FakeTransport(),
            project_root=Path(self.project_root), switch_dir=data)
        rec = switch.plan(
            source_pane="%5", source_provider="claude",
            target_pane="%8", target_provider="codex")
        self.assertTrue(rec["ok"], rec)
        switch.advance()
        switch.ack(role="source", switch_id=rec["id"], nonce=rec["nonce"])
        switch.advance()
        switch.advance()
        status, cur = self.get("/api/cursors/orchestrator")
        self.assertEqual(status, 200)
        seq = int(cur["seq"])
        switch.ack(role="target", switch_id=rec["id"], nonce=rec["nonce"],
                   cursor_seq=seq)
        waiting = switch.advance()
        self.assertEqual(waiting.get("stage"), "target_acknowledged")
        blocked = switch.advance()
        self.assertEqual(blocked.get("reason"), "await_target_health")
        (data / "dispatch.json").write_text(json.dumps({
            "version": 1, "target": "%8", "heartbeat_at": time.time(),
            "project_root": self.app.project_root, "status": "idle",
            "scanned": seq, "acknowledged": seq, "pending": [],
        }))
        lock = open(data / "dispatch.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lock.close)
        status, heal = self.get("/api/autoheal")
        self.assertEqual(status, 200, heal)
        self.assertEqual((heal.get("event_dispatcher") or {}).get("target"), "%8")
        done = switch.advance()
        self.assertEqual(done.get("stage"), "complete", done)


if __name__ == "__main__":
    unittest.main()
