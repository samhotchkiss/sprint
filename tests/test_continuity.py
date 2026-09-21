"""Continuity inbox tests. No model, tmux, or live board."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sprint_coordinator.continuity import ContinuityInbox, actionable
from sprint_coordinator.util import Clock


def event(seq, actor, kind, card_num, **payload):
    payload = dict(payload)
    payload.setdefault("id", "evt-%d" % seq)
    payload.setdefault("reply_to", "card:%d" % card_num)
    return {
        "seq": seq, "actor": actor, "kind": kind, "card_num": card_num, "ts": seq,
        "payload": payload,
    }


def snapshot_cards():
    return [
        {"num": 10, "state": "queued", "agent_name": "sprint-card-10",
         "pane": "%10", "worktree": "/wt/10", "branch": "sprint/card-10",
         "last_event": {"seq": 40}},
        {"num": 11, "state": "in_progress", "agent_name": "sprint-card-11",
         "pane": "%11", "worktree": "/wt/11", "last_event_seq": 41},
        {"num": 12, "state": "needs_you", "agent_name": "sprint-card-12",
         "pane": "%12", "worktree": "/wt/12", "last_event_seq": 42},
        {"num": 13, "state": "held", "held": True, "agent_name": "sprint-card-13",
         "pane": "%13", "worktree": "/wt/13", "last_event_seq": 43},
        {"num": 14, "state": "ready", "pane": "%14", "last_event_seq": 44},
        {"num": 15, "state": "integrating", "pane": "%15", "last_event_seq": 45},
    ]


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "c.sqlite"
        self.clock = Clock(1000)
        self.conn = sqlite3.connect(str(self.db), isolation_level=None)
        self.addCleanup(self.conn.close)
        self.inbox = ContinuityInbox(self.conn, clock=self.clock)

    def reopen(self):
        self.conn.close()
        self.conn = sqlite3.connect(str(self.db), isolation_level=None)
        self.addCleanup(self.conn.close)
        self.inbox = ContinuityInbox(self.conn, clock=self.clock)
        return self.inbox

    def test_snapshot_maps_waiting_occupied_excluded_and_panes(self):
        snap = self.inbox.register_snapshot(snapshot_cards(), cutoff_seq=50)
        self.assertEqual(snap["cutoff_seq"], 50)
        by_num = {c["num"]: c for c in snap["cards"]}
        self.assertEqual(by_num[10]["dispatch"], "waiting")
        self.assertEqual(by_num[11]["dispatch"], "occupied")
        self.assertEqual(by_num[12]["dispatch"], "waiting")
        self.assertTrue(by_num[12]["state"] == "needs_you")
        self.assertEqual(by_num[13]["dispatch"], "excluded")
        self.assertTrue(by_num[13]["held"])
        self.assertEqual(by_num[14]["dispatch"], "waiting")
        self.assertEqual(by_num[15]["dispatch"], "occupied")
        self.assertEqual(by_num[11]["worktree"], "/wt/11")
        self.assertEqual(sorted(self.inbox.excluded_panes()),
                         ["%10", "%11", "%12", "%13", "%14", "%15"])
        again = self.inbox.register_snapshot([{"num": 99, "state": "queued"}], 99)
        self.assertEqual(again["cutoff_seq"], 50)
        self.assertIsNone(self.inbox.card(99))
        self.assertIsNone(self.inbox.prepare_delivery(10))
        blocked = self.inbox.prepare_delivery(13)
        self.assertEqual(blocked["reason"], "excluded")

    def test_noise_and_pre_cutoff_and_chat_are_ignored(self):
        self.inbox.register_snapshot(snapshot_cards(), cutoff_seq=50)
        inserted = self.inbox.record_events([
            event(49, "user", "answer", 12, text="too old"),
            event(51, "session", "chat", 11, text="echo"),
            event(52, "worker", "progress", 11, text="compiling"),
            event(53, "worker", "phase", 11, text="build"),
            event(54, "user", "heartbeat", 11),
            event(55, "user", "chat", 11, text="ordinary chat is routing"),
            event(56, "user", "answer", 12, text="here is the path"),
        ])
        self.assertEqual(inserted, [56])
        self.assertFalse(actionable(event(52, "worker", "progress", 11)))

    def test_duplicates_and_restart_do_not_double_insert(self):
        self.inbox.register_snapshot(snapshot_cards(), cutoff_seq=50)
        first = [
            event(60, "user", "answer", 12, text="a"),
            event(61, "worker", "error", 11, text="boom"),
        ]
        self.assertEqual(self.inbox.record_events(first), [60, 61])
        self.assertEqual(self.inbox.record_events(first), [])
        inbox = self.reopen()
        self.assertEqual(inbox.record_events(first), [])
        self.assertEqual([i["seq"] for i in inbox.pending()], [60, 61])

    def test_verdict_ownership_and_error_do_not_cross_cards(self):
        self.inbox.register_snapshot(snapshot_cards(), cutoff_seq=50)
        self.inbox.record_events([
            event(70, "user", "verdict", 14, verdict="approve"),
            event(71, "user", "verdict", 11, verdict="bounce", text="try again"),
            event(72, "user", "action", 13, action="cancel"),
            event(73, "worker", "error", 11, text="typecheck failed"),
            event(74, "worker", "evidence", 15, text="diff ready"),
            event(75, "worker", "question", 12, text="which fixture?"),
            event(76, "user", "retry", 10),
            event(77, "system", "state", 10, to="queued"),
        ])
        by_seq = {i["seq"]: i for i in self.inbox.pending()}
        self.assertEqual(by_seq[70]["intent"], "notice")
        self.assertEqual(by_seq[70]["card_num"], 14)
        self.assertEqual(by_seq[71]["intent"], "resume")
        self.assertEqual(by_seq[71]["card_num"], 11)
        self.assertEqual(by_seq[72]["intent"], "notice")
        self.assertEqual(by_seq[73]["intent"], "notice")
        self.assertEqual(by_seq[73]["card_num"], 11)
        self.assertEqual(by_seq[76]["intent"], "resume")
        eleven = [i["seq"] for i in self.inbox.pending(11)]
        self.assertEqual(eleven, [71, 73])
        self.assertNotIn(70, eleven)

    def test_inflight_batches_newer_events_until_ack(self):
        self.inbox.register_snapshot(snapshot_cards(), cutoff_seq=50)
        self.inbox.record_events([event(80, "worker", "evidence", 11, text="one")])
        first = self.inbox.prepare_delivery(11)
        self.assertTrue(first["ok"])
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(first["item_seqs"], [80])
        self.inbox.record_events([
            event(81, "worker", "error", 11, text="two"),
            event(82, "user", "verdict", 11, verdict="bounce"),
        ])
        self.assertEqual([i["seq"] for i in self.inbox.pending(11)], [81, 82])
        again = self.inbox.prepare_delivery(11)
        self.assertTrue(again["batched"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["item_seqs"], [80])
        self.inbox.mark_submitted(first["id"], status="submitted")
        still = self.inbox.prepare_delivery(11)
        self.assertEqual(still["id"], first["id"])
        ack = self.inbox.acknowledge(first["id"], through_seq=80)
        self.assertEqual(ack["acknowledged_seqs"], [80])
        self.assertEqual([i["seq"] for i in self.inbox.pending(11)], [81, 82])
        nxt = self.inbox.prepare_delivery(11)
        self.assertEqual(nxt["item_seqs"], [81, 82])

    def test_ack_cannot_eat_later_events_or_other_cards(self):
        self.inbox.register_snapshot(snapshot_cards(), cutoff_seq=50)
        self.inbox.record_events([
            event(90, "user", "answer", 12, text="path A"),
            event(91, "user", "answer", 12, text="path B"),
            event(92, "worker", "question", 11, text="other card"),
        ])
        delivery = self.inbox.prepare_delivery(12)
        self.assertEqual(delivery["item_seqs"], [90, 91])
        self.inbox.record_events([event(93, "user", "answer", 12, text="path C")])
        result = self.inbox.acknowledge(delivery["id"], through_seq=90)
        self.assertEqual(result["acknowledged_seqs"], [90])
        self.assertEqual(result["released_seqs"], [91])
        pending = {i["seq"] for i in self.inbox.pending()}
        self.assertEqual(pending, {91, 92, 93})
        stale = self.inbox.acknowledge(delivery["id"], through_seq=93)
        self.assertEqual(stale["acknowledged_seqs"], [])
        pending_after = {i["seq"] for i in self.inbox.pending()}
        self.assertEqual(pending_after, {91, 92, 93})
        self.assertIn(92, {i["seq"] for i in self.inbox.pending(11)})
        self.assertIn(93, {i["seq"] for i in self.inbox.pending(12)})

    def test_uncertain_is_visible_and_never_autoresend(self):
        self.inbox.register_snapshot(snapshot_cards(), cutoff_seq=50)
        self.inbox.record_events([event(100, "user", "answer", 12, text="go")])
        delivery = self.inbox.prepare_delivery(12)
        marked = self.inbox.mark_submitted(delivery["id"], status="uncertain",
                                           error="tmux_send_unverified")
        self.assertEqual(marked["status"], "uncertain")
        inbox = self.reopen()
        blocked = inbox.prepare_delivery(12)
        self.assertEqual(blocked["reason"], "uncertain")
        self.assertEqual(blocked["delivery"]["id"], delivery["id"])
        self.assertEqual(inbox.pending(12), [])
        row = inbox.conn.execute(
            "SELECT status FROM continuity_items WHERE seq=100").fetchone()
        self.assertEqual(row["status"], "inflight")

    def test_module_does_not_send_or_import_tmux(self):
        import sprint_coordinator.continuity as mod
        self.assertFalse(hasattr(mod, "subprocess"))
        source = Path(mod.__file__).read_text()
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("send-keys", source)
        self.assertNotIn("shell=True", source)


if __name__ == "__main__":
    unittest.main()
