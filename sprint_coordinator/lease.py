"""Board-wide ownership and a truthful service lease, separate from model activity."""
import fcntl
import os
import time

from sprint_coordinator.config import write_json_private


class BoardLease:
    def __init__(self, config):
        self.config = config
        self.handle = None
        self.path = config.board_data_dir / 'coordinator-owner.json'

    def open(self):
        self.config.board_data_dir.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.config.board_data_dir / 'coordinator-owner.lock', 'a+')
        os.chmod(self.handle.name, 0o600)
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise RuntimeError('another coordinator owns this board') from None
        return self

    def heartbeat(self, cursor, status='active'):
        if self.handle is None:
            raise RuntimeError('board ownership is not held')
        write_json_private(self.path, {
            'version': 1, 'project_root': str(self.config.project_root),
            'pid': os.getpid(), 'heartbeat_at': time.time(),
            'status': status, 'ingest_cursor': cursor,
        })

    def close(self):
        if self.handle is not None:
            # Leave evidence for diagnostics; the released flock makes it inactive.
            self.handle.close()
            self.handle = None
