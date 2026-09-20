"""Session delivery uncertainty must never launch a second model assignment."""
from pathlib import Path
import tempfile
import unittest

from sprint_coordinator.driver import Coordinator
from sprint_coordinator.util import Clock
from tests.test_sprint_coordinator import FakeJev, MemoryBoard, make_config, user_event, ok_reply


class SessionRunner:
    def __init__(self, outcome="pending"):
        self.outcome = outcome
        self.ready = False
        self.sent = []
        self.checked = []

    def run(self, command, job):
        if "--reconcile" in command:
            self.checked.append(job["assignment_id"])
            if self.ready:
                return dict(ok_reply(job), status="completed")
        else:
            self.sent.append(job["assignment_id"])
        return {"ok": False, "status": self.outcome, "reason": "test_delivery_pending"}


class SessionRecoveryTests(unittest.TestCase):
    def test_pending_and_uncertain_results_reconcile_without_resending(self):
        for outcome in ("pending", "uncertain", "blocked"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                runner = SessionRunner(outcome)
                board = MemoryBoard()
                clock = Clock(1000)
                adapter = str(Path(__file__).resolve().parents[1] / 'bin/sprint-session-worker')
                cfg = make_config(tmp, extra_workers={
                    'low': {'command': [adapter, '--pane', '%1'], 'role': 'response', 'cost': 'low'},
                    'high': {'command': [adapter, '--pane', '%2'], 'role': 'response', 'cost': 'high'},
                })
                c = Coordinator(cfg, mode='active', clock=clock, board=board,
                                jev=FakeJev(), runner=runner, start_cursor=0)
                c.open()
                try:
                    board.add(user_event(1, 'What is the result?'))
                    c.tick(wait=True)
                    self.assertEqual(len(runner.sent), 1)
                    self.assertEqual(len(c.store.assignments_by_status('awaiting_session')), 1)
                    clock.advance(100)
                    c.tick(wait=True)
                    self.assertEqual(len(runner.sent), 1)
                    self.assertEqual(board.posts, [])
                    self.assertEqual(len(c.store.assignments_for(c.store.obligations()[0]['id'])), 1)
                    runner.ready = True
                    clock.advance(3)
                    c.tick(wait=True)
                    self.assertEqual(len(board.posts), 1)
                    self.assertEqual(len(runner.sent), 1)
                    self.assertEqual(len(c.store.obligations('answered')), 1)
                finally:
                    c.close()


if __name__ == '__main__':
    unittest.main()
