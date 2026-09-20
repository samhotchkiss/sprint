"""Exercise the real HTTP client and activation entry point without model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sprint_coordinator.board import BoardClient
from sprint_coordinator.config import init_config
from sprint_coordinator.takeover import takeover_report

ROOT = Path(__file__).resolve().parents[1]


class DeliveryTests(unittest.TestCase):
    def test_delivery_key_and_private_auth_reach_http_server(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, dict(self.headers), body))
                payload = b'{"ok":true}'
                self.send_response(201)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as tmp:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                Path(tmp, 'server.json').write_text(json.dumps({
                    'port': server.server_port, 'token': 'synthetic-test-token',
                }))
                board = BoardClient(Path(tmp))
                for _ in range(2):
                    board.post_sidebar('reply', idempotency_key='reply:obligation:r1')
                board.post_card_chat(42, 'card reply', idempotency_key='reply:card42:r1')
                self.assertEqual([r[1]['Idempotency-Key'] for r in requests], [
                    'reply:obligation:r1', 'reply:obligation:r1', 'reply:card42:r1'])
                self.assertEqual(requests[2][0], '/api/cards/42/chat')
                self.assertTrue(all(r[1]['Authorization'] == 'Bearer synthetic-test-token'
                                    for r in requests))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_default_config_cannot_activate_or_create_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'coordinator.json'
            init_config(config, root)
            result = subprocess.run([
                sys.executable, str(ROOT / 'bin/sprint-coordinate'), 'run-once',
                '--active', '--config', str(config),
            ], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('activation_ready', result.stderr)
            self.assertFalse(list(root.rglob('*.sqlite')))

    def test_unsafe_legacy_override_cannot_allow_two_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = takeover_report({
                'event_dispatch_supported': True, 'tmux_window': '%72',
            }, Path(tmp), 'active', acknowledge_autoheal_gap=True)
            self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
