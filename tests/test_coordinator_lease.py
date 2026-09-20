import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from sprint_coordinator.lease import BoardLease
from tests.test_sprintd import Base

class CoordinatorLeaseTest(Base):
    def test_only_fresh_locked_lease_suppresses_wrong_session_wakeup(self):
        cfg=SimpleNamespace(board_data_dir=Path(self.app.data_dir), project_root=Path(self.app.project_root))
        lease=BoardLease(cfg).open()
        self.addCleanup(lease.close)
        lease.heartbeat(17)
        self.assertEqual(self.app.coordinator_state()['ingest_cursor'],17)
        self.assertTrue(self.app.autoheal_json()['coordinator_supported'])
        other=BoardLease(cfg)
        with self.assertRaises(RuntimeError):other.open()
        state=json.loads(lease.path.read_text()); state['heartbeat_at']-=40
        lease.path.write_text(json.dumps(state))
        self.assertIsNone(self.app.coordinator_state())
        lease.heartbeat(18)
        lease.close()
        self.assertIsNone(self.app.coordinator_state())

    def test_wrong_project_never_claims_ownership(self):
        cfg=SimpleNamespace(board_data_dir=Path(self.app.data_dir), project_root=Path('/not-this-board'))
        lease=BoardLease(cfg).open();self.addCleanup(lease.close)
        lease.heartbeat(1)
        self.assertIsNone(self.app.coordinator_state())

if __name__=='__main__':unittest.main()
