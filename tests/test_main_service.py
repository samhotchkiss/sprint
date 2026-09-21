import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sprint_coordinator import main_service as service


class Board:
    def __init__(self):
        self.target = '%12'
        self.coordinator = None

    def settings(self):
        return {'session_tmux_window': self.target}

    def autoheal(self):
        return {'tmux_window': self.target, 'event_dispatch_supported': True,
                'coordinator': self.coordinator}


class MainServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.data = self.project / '.sprint'
        self.data.mkdir()
        self.board = Board()

    def test_restart_uses_registered_target_without_resetting_delivery(self):
        saved = {'target': '%12', 'inflight': {'through': 20}, 'pending': [{'seq': 21}]}
        file = self.data / 'dispatch.json'
        file.write_text(json.dumps(saved))
        with patch.object(service, 'BoardClient', return_value=self.board), \
             patch.object(service.os, 'execv') as execute:
            service.run(self.project)
        argv = execute.call_args.args[1]
        self.assertEqual(argv[-1], '%12')
        self.assertNotIn('--reset', argv)
        self.assertEqual(json.loads(file.read_text()), saved)

    def test_refuses_unreconciled_owner_switch(self):
        (self.data / 'dispatch.json').write_text(json.dumps({'target': '%11'}))
        with self.assertRaisesRegex(RuntimeError, 'handoff'):
            service.preflight(self.project, self.board)

    def test_refuses_parallel_task_coordinator(self):
        self.board.coordinator = {'status': 'active'}
        with self.assertRaisesRegex(RuntimeError, 'already owns'):
            service.preflight(self.project, self.board)

    def test_service_identity_survives_provider_change(self):
        first = service.plist_value(self.project)
        self.board.target = '%99'
        second = service.plist_value(self.project)
        self.assertEqual(first, second)
        self.assertEqual(first['KeepAlive'], {'SuccessfulExit': False})
        self.assertNotIn('--target', first['ProgramArguments'])

    def test_main_and_task_coordinator_share_exclusion_lock(self):
        from sprint_coordinator.lease import BoardLease
        from types import SimpleNamespace
        cfg = SimpleNamespace(board_data_dir=self.data, project_root=self.project)
        held = BoardLease(cfg).open()
        self.addCleanup(held.close)
        self.assertIsNone(service.dispatcher().lock_file(self.data, 'coordinator-owner.lock'))
        held.close()
        lock = service.dispatcher().lock_file(self.data, 'coordinator-owner.lock')
        self.addCleanup(lock.close)
        with self.assertRaisesRegex(RuntimeError, 'another coordinator'):
            BoardLease(cfg).open()


if __name__ == '__main__':
    unittest.main()
