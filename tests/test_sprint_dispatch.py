"""No model calls or real tmux panes: state-machine and real HTTP/process tests."""
import copy
from importlib.machinery import SourceFileLoader
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_sprintd import Base, sprintd

ROOT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader('dispatch', str(ROOT / 'bin/sprint-dispatch'))
spec = importlib.util.spec_from_loader('dispatch', loader)
dispatch = importlib.util.module_from_spec(spec)
loader.exec_module(dispatch)


def event(seq, actor='user', kind='chat', payload=None):
    return dict(seq=seq, actor=actor, kind=kind, payload=payload or {}, card_num=1)


class FakeBoard:
    def __init__(self):
        self.events = []
        self.cursor = 0
        self.target = '%5'
        self.limited = False

    def get(self, path):
        if path == '/api/settings':
            return {'session_tmux_window': self.target}
        if path.startswith('/api/events'):
            after = int(path.split('after=')[1].split('&')[0])
            return dict(events=[e for e in self.events if e['seq'] > after][:500],
                        cursor=self.cursor, head=self.events[-1]['seq'] if self.events else 0)
        if path == '/api/cursors/orchestrator':
            return {'seq': self.cursor}
        if path == '/api/autoheal':
            return {'account_limited': self.limited}
        raise AssertionError(path)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard()
        self.state = dispatch.new_state('%5', 0)
        self.sent = []
        self.saved = []

    def step(self, at=100, send=None):
        return dispatch.tick(self.state, self.board, '/fixture',
                             lambda: self.saved.append(copy.deepcopy(self.state)),
                             send or (lambda target, text: self.sent.append((target, text)) or 0), at)

    def test_quiet_and_progress_do_not_wake_or_acknowledge(self):
        self.board.events = [event(1, 'session'), event(2, 'worker', 'progress'),
                             event(3, 'worker', 'phase'), event(4, 'server', 'heartbeat')]
        for n in range(100):
            self.step(n)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state['scanned'], 4)
        self.assertEqual(self.board.cursor, 0)

    def test_burst_once_partial_ack_and_later_event(self):
        self.board.events = [event(1), event(2)]
        self.step()
        self.assertEqual(len(self.sent), 1)
        self.board.events.append(event(3))
        self.board.cursor = 1
        self.step(150)
        self.assertEqual(len(self.sent), 1)
        self.step(500)
        self.assertEqual(self.state['status'], 'needs_attention')
        self.assertEqual(len(self.sent), 1)
        self.board.cursor = 2
        self.step(501)
        self.assertEqual(len(self.sent), 2)
        self.board.cursor = 3
        self.step(502)
        self.assertEqual(self.state['status'], 'idle')

    def test_pre_send_persisted_and_restart_never_replays_uncertain_delivery(self):
        self.board.events = [event(1)]
        def send(target, text):
            self.assertEqual(self.saved[-1]['inflight']['through'], 1)
            raise OSError('uncertain delivery')
        self.step(send=send)
        restored = json.loads(json.dumps(self.saved[-1]))
        self.state = restored
        self.step(500)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state['deliveries'], 1)

    def test_account_limit_defers_without_dropping_pending(self):
        self.board.events = [event(1)]
        self.board.limited = True
        self.step()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state['status'], 'account_limited')
        self.board.limited = False
        self.step()
        self.assertEqual(len(self.sent), 1)

    def test_ownership_change_never_types_into_new_owner(self):
        self.board.events = [event(1)]
        self.board.target = '%9'
        self.assertFalse(self.step())
        self.assertEqual(self.sent, [])

    def test_ack_race_before_send_skips_already_handled_event(self):
        original = self.board.get
        def get(path):
            if path == '/api/cursors/orchestrator':
                self.board.cursor = 1
            return original(path)
        self.board.get = get
        self.board.events = [event(1)]
        self.step()
        self.assertEqual(self.sent, [])

    def test_paginated_burst_has_one_bounded_wakeup(self):
        self.board.events = [event(n) for n in range(1, 1002)]
        self.step()
        self.step()
        self.assertEqual(self.sent, [])
        self.step()
        self.assertEqual(len(self.sent), 1)
        self.assertLess(len(self.sent[0][1]), 1500)
        self.assertLessEqual(len(self.state['pending']), 256)
        self.assertEqual(self.state['inflight']['through'], 1001)

    def test_filters_lifecycle_and_unblocked_dependencies(self):
        for kind in ('question', 'error', 'evidence', 'stuck', 'agent_silent', 'limit_cleared'):
            self.assertTrue(dispatch.actionable(event(1, 'worker', kind)))
        self.assertTrue(dispatch.actionable(event(1, 'server', 'note',
                        {'blocked_by_change': True, 'blocked_by': None})))
        self.assertFalse(dispatch.actionable(event(1, 'server', 'note', {'text': 'status'})))
        self.assertTrue(dispatch.actionable(event(1, 'worker', 'state', {'to': 'ready'})))
        self.assertFalse(dispatch.actionable(event(1, 'worker', 'state', {'to': 'in_progress'})))

    def test_rollback_is_not_silently_skipped(self):
        self.state['acknowledged'] = 20
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.step()

    def test_rolling_wake_cap_survives_restart_and_retains_events(self):
        for seq in range(1, 31):
            self.board.cursor = seq - 1
            self.board.events.append(event(seq))
            self.step(100 + seq)
        self.state = json.loads(json.dumps(self.saved[-1]))
        self.board.cursor = 30
        self.board.events.append(event(31))
        self.step(200)
        self.assertEqual(self.state['status'], 'budget_limited')
        self.assertEqual(len(self.sent), 30)
        self.assertEqual(self.state['pending'][-1]['seq'], 31)
        self.step(3702)
        self.assertEqual(len(self.sent), 31)

    def test_restored_history_is_not_silently_skipped(self):
        self.state['scanned'] = 20
        with self.assertRaisesRegex(ValueError, 'history moved backwards'):
            self.step()

    def test_user_settings_wake_but_coordinator_settings_do_not(self):
        self.assertTrue(dispatch.actionable(event(1, 'server', 'note',
                        {'settings': {'worker': {}}, 'by': 'user'})))
        self.assertFalse(dispatch.actionable(event(1, 'server', 'note',
                         {'settings': {'worker': {}}, 'by': 'session'})))

    def test_stop_follows_nonce_published_during_concurrent_start(self):
        states = [dict(running=True, instance='old'), dict(running=True, instance='new'),
                  dict(running=False), dict(running=False)]
        with mock.patch.object(sys, 'argv', ['sprint-dispatch', 'stop']), \
             mock.patch.object(dispatch, 'status', side_effect=states), \
             mock.patch.object(dispatch, 'write_json') as writes, \
             mock.patch.object(dispatch.time, 'sleep'), mock.patch('builtins.print'):
            self.assertEqual(dispatch.main(), 0)
        self.assertEqual([c.args[1]['instance'] for c in writes.call_args_list], ['old', 'new'])


class HTTPTests(Base):
    def setUp(self):
        super().setUp()
        self.app.save_settings({'session_tmux_window': '%5'}, actor='session')
        self.data = Path(self.app.data_dir)
        dispatch.write_json(self.data / 'server.json', {'port': self.port, 'token': 'test-token'})
        self.app.set_cursor('orchestrator', self.app.max_seq())

    def lease(self, mode='idle'):
        handle = dispatch.lock_file(self.data)
        self.addCleanup(handle.close)
        state = dispatch.new_state('%5', self.app.cursor_seq())
        state.update(status=mode, project_root=self.app.project_root, heartbeat_at=time.time())
        dispatch.write_json(self.data / 'dispatch.json', state)
        return handle, state

    def test_live_lease_suppresses_autoheal_without_claiming_consumption(self):
        before = self.app.cursor_seq()
        handle, state = self.lease()
        self.assertEqual(self.app.session_dead_state()['reason'], 'event_dispatcher')
        self.assertEqual(self.app.session_liveness()['note'],
                         'watching for actionable events — session resting')
        self.assertEqual(self.app.cursor_seq(), before)
        handle.close()
        self.assertIsNone(self.app.event_dispatch_state())

    def test_stale_mismatched_or_future_lease_does_not_suppress_recovery(self):
        handle, state = self.lease()
        for patch in ({'heartbeat_at': time.time() - 31}, {'target': '%8'},
                      {'heartbeat_at': time.time() + 120}, {'status': 'unavailable'}):
            altered = dict(state, **patch)
            dispatch.write_json(self.data / 'dispatch.json', altered)
            self.assertIsNone(self.app.event_dispatch_state())

    def test_unacknowledged_wake_visible_without_duplicate_autoheal(self):
        self.lease('needs_attention')
        live = self.app.session_liveness()
        self.assertFalse(live['online'])
        self.assertIn('not acknowledged', live['note'])
        self.assertEqual(self.app.session_dead_state()['reason'], 'event_dispatcher')

    def test_startup_lease_excludes_legacy_autoheal(self):
        self.lease('starting')
        self.assertEqual(self.app.session_dead_state()['reason'], 'event_dispatcher')
        self.assertFalse(self.app.autoheal_json()['revive_wanted'])

    def test_real_http_cursor_and_event_read_do_not_mark_waiter_alive(self):
        before = self.app.waiter_seen_at()
        b = dispatch.Board(self.data)
        self.assertTrue(b.get('/api/autoheal')['event_dispatch_supported'])
        self.assertEqual(b.get('/api/events?after=0')['cursor'], self.app.cursor_seq())
        self.assertEqual(self.app.waiter_seen_at(), before)

    def test_daemon_singleton_delivery_restart_and_safe_stop(self):
        sender = Path(self.tmp) / 'sender'
        sent = Path(self.tmp) / 'sent'
        sender.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\n'
                          f'with Path({str(sent)!r}).open("a") as f: f.write("sent\\n")\n')
        sender.chmod(0o700)
        command = [str(ROOT / 'bin/sprint-dispatch')]
        opts = ['--project-root', self.project_root, '--target', '%5', '--poll', '.1',
                '--tmux-send', str(sender)]
        def cli(cmd, extra=()):
            return subprocess.run(command + [cmd] + opts + list(extra),
                                  capture_output=True, text=True, timeout=35)
        self.addCleanup(lambda: cli('stop'))
        first = cli('start')
        self.assertEqual(first.returncode, 0, first.stderr)
        pid = json.loads(first.stdout)['pid']
        second = cli('start')
        self.assertEqual(json.loads(second.stdout)['pid'], pid)
        with self.app.lock:
            seq = self.app._append_event(None, 'user', 'chat', {'text': 'route this'})
        deadline = time.monotonic() + 4
        while not sent.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(sent.exists())
        self.assertEqual(sent.read_text().splitlines(), ['sent'])
        self.assertLess(self.app.cursor_seq(), seq)
        self.assertEqual(cli('stop').returncode, 0)
        self.assertEqual(cli('start').returncode, 0)
        time.sleep(.3)
        self.assertEqual(sent.read_text().splitlines(), ['sent'])
        self.app.set_cursor('orchestrator', seq)
        with self.app.lock:
            self.app._append_event(None, 'worker', 'evidence', {'text': 'ready'})
        deadline = time.monotonic() + 4
        while len(sent.read_text().splitlines()) < 2 and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(sent.read_text().splitlines(), ['sent', 'sent'])
        self.assertEqual(cli('stop').returncode, 0)
        self.assertFalse(dispatch.status(self.data)['running'])


if __name__ == '__main__':
    unittest.main()
