#!/usr/bin/env python3
"""Concurrency and ownership tests for bin/sprint-preview."""

import concurrent.futures
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "bin" / "sprint-preview"

HTTP_SERVER = r"""
import http.server, sys
host, port, marker = sys.argv[1], int(sys.argv[2]), sys.argv[3]
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = marker.encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer((host, port), Handler)
server.serve_forever()
"""

BLOCKER = r"""
import socket, sys, time
family, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
af = socket.AF_INET6 if family == 'v6' else socket.AF_INET
s = socket.socket(af, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
if af == socket.AF_INET6:
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
s.bind((host, port))
s.listen()
print('ready', flush=True)
while True:
    time.sleep(60)
"""

WRAPPER = r"""
import os, signal, subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], 'stubborn'],
    preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))
open(sys.argv[4], 'w').write(str(child.pid))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(60)
"""


def preferred_port(project_root, card):
    digest = hashlib.sha256(os.path.realpath(project_root).encode("utf-8")).digest()
    slot = int.from_bytes(digest[:4], "big") % 300
    return 20000 + slot * 100 + (card % 100)


class SprintPreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sprint-preview-test-")
        self.root = pathlib.Path(self.temp.name)
        self.registry = self.root / "preview-registry.json"
        self.server_script = self.root / "http_server.py"
        self.server_script.write_text(HTTP_SERVER, encoding="utf-8")
        self.blocker_script = self.root / "blocker.py"
        self.blocker_script.write_text(BLOCKER, encoding="utf-8")
        self.wrapper_script = self.root / "wrapper.py"
        self.wrapper_script.write_text(WRAPPER, encoding="utf-8")
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        real_lsof = shutil.which("lsof")
        if not real_lsof:
            self.skipTest("lsof is required")
        fake_lsof = self.fake_bin / "lsof"
        fake_lsof.write_text(
            "#!/bin/sh\nsleep \"${TEST_LSOF_DELAY:-0}\"\nexec %s \"$@\"\n" % real_lsof,
            encoding="utf-8")
        fake_lsof.chmod(0o755)
        self.started = []
        self.blockers = []

    def tearDown(self):
        for card, project in reversed(self.started):
            self.run_preview("stop", card, project, check=False)
        for proc in self.blockers:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        self.temp.cleanup()

    def board(self, name):
        project = self.root / name
        worktree = project / "worktree"
        worktree.mkdir(parents=True)
        (project / ".sprint").mkdir()
        return project, worktree

    def command(self, action, card, project, extra=()):
        return [
            sys.executable, str(PREVIEW), "--registry", str(self.registry),
            action, str(card), "--project-root", str(project), *map(str, extra),
        ]

    def run_preview(self, action, card, project, extra=(), check=True, env=None):
        proc = subprocess.run(self.command(action, card, project, extra), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=20, check=False, env=env)
        if check and proc.returncode:
            self.fail("%s failed:\nstdout=%s\nstderr=%s" %
                      (action, proc.stdout, proc.stderr))
        return proc

    def delayed_lsof_env(self, delay, timeout=None):
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["TEST_LSOF_DELAY"] = str(delay)
        if timeout is not None:
            env["SPRINT_PREVIEW_OWNERSHIP_TIMEOUT"] = str(timeout)
        return env

    def start_preview(self, card, project, worktree, marker):
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8", "--",
                 sys.executable, self.server_script, "{host}", "{port}", marker]
        proc = self.run_preview("start", card, project, extra)
        record = json.loads(proc.stdout)
        self.started.append((card, project))
        return record

    def read_url(self, url):
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.read().decode("utf-8")

    def start_blocker(self, family, host, port):
        proc = subprocess.Popen(
            [sys.executable, str(self.blocker_script), family, host, str(port)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = proc.stdout.readline().strip()
        if line != "ready":
            proc.wait(timeout=3)
            self.fail("blocker did not start: %s" % proc.stderr.read())
        self.blockers.append(proc)
        return proc

    def test_same_card_on_two_boards_gets_distinct_reachable_owned_ports(self):
        project_a, worktree_a = self.board("board-a")
        project_b, worktree_b = self.board("board-b")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(self.start_preview, 85, project_a, worktree_a, "board-a")
            future_b = pool.submit(self.start_preview, 85, project_b, worktree_b, "board-b")
            preview_a = future_a.result(timeout=15)
            preview_b = future_b.result(timeout=15)

        self.assertNotEqual(preview_a["port"], preview_b["port"])
        self.assertEqual(self.read_url(preview_a["live_url"]), "board-a")
        self.assertEqual(self.read_url(preview_b["live_url"]), "board-b")

        stopped = self.run_preview("stop", 85, project_a)
        self.assertTrue(json.loads(stopped.stdout)["stopped"])
        self.started.remove((85, project_a))
        with self.assertRaises(urllib.error.URLError):
            self.read_url(preview_a["live_url"])
        self.assertEqual(self.read_url(preview_b["live_url"]), "board-b")

    @unittest.skipUnless(socket.has_ipv6, "exact/wildcard coexistence needs IPv6")
    def test_exact_and_wildcard_collision_falls_back_and_cleanup_spares_both(self):
        project, worktree = self.board("collision-board")
        card = 86
        occupied = preferred_port(project, card)

        exact = self.start_blocker("v4", "127.0.0.1", occupied)
        wildcard = self.start_blocker("v6", "::", occupied)
        preview = self.start_preview(card, project, worktree, "owned-preview")

        self.assertNotEqual(preview["port"], occupied)
        self.assertEqual(self.read_url(preview["live_url"]), "owned-preview")
        verify = self.run_preview("verify", card, project,
                                  ["--url", preview["live_url"]])
        self.assertEqual(json.loads(verify.stdout)["pid"], preview["pid"])
        wrong = self.run_preview("verify", card, project,
                                 ["--url", "http://127.0.0.1:%d/" % occupied],
                                 check=False)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("does not match", wrong.stderr)

        self.run_preview("stop", card, project)
        self.started.remove((card, project))
        self.assertIsNone(exact.poll(), "cleanup killed the unrelated exact listener")
        self.assertIsNone(wildcard.poll(), "cleanup killed the unrelated wildcard listener")

    def test_slow_ownership_lookup_succeeds_within_bounded_timeout(self):
        project, worktree = self.board("slow-lsof-board")
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8", "--",
                 sys.executable, self.server_script, "{host}", "{port}", "slow-owned"]

        started = time.monotonic()
        proc = self.run_preview("start", 87, project, extra,
                                env=self.delayed_lsof_env(3.5))
        self.assertGreaterEqual(time.monotonic() - started, 3.4)
        preview = json.loads(proc.stdout)
        self.started.append((87, project))
        self.assertEqual(self.read_url(preview["live_url"]), "slow-owned")

    def test_ownership_timeout_reaps_wrapper_and_stubborn_child(self):
        project, worktree = self.board("timeout-cleanup-board")
        child_pid_file = self.root / "stubborn-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5", "--",
                 sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]

        proc = self.run_preview("start", 88, project, extra, check=False,
                                env=self.delayed_lsof_env(1, timeout=0.2))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("timed out", proc.stderr)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("ownership timeout left the wrapper's server child alive")
        self.assertFalse((project / ".sprint" / "previews" / "card-88.json").exists())


if __name__ == "__main__":
    unittest.main()
