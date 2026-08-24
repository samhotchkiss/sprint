#!/usr/bin/env python3
"""Concurrency and ownership tests for bin/sprint-preview."""

import concurrent.futures
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import signal
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

TIMED_HTTP_SERVER = r"""
import http.server, os, sys, threading
host, port, marker, delay, code = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4]), int(sys.argv[5])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = marker.encode(); self.send_response(200)
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
server = http.server.ThreadingHTTPServer((host, port), Handler)
threading.Timer(delay, lambda: os._exit(code)).start()
server.serve_forever()
"""

CLOSING_HTTP_SERVER = r"""
import http.server, sys, threading, time
host, port = sys.argv[1], int(sys.argv[2])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body=b'closing'; self.send_response(200); self.send_header('Content-Length', '7')
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
server=http.server.ThreadingHTTPServer((host, port), Handler)
threading.Timer(1.5, server.shutdown).start()
server.serve_forever(); server.server_close(); time.sleep(60)
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

FORK_AND_EXIT = r"""
import subprocess, sys
child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], 'forked'])
open(sys.argv[4], 'w').write(str(child.pid))
"""

DETACHED_WRAPPER = r"""
import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], 'detached'],
                 start_new_session=True)
while True: time.sleep(60)
"""

SIBLING_GROUP_WRAPPER = r"""
import os, signal, subprocess, sys, time
if sys.argv[1] == 'leader':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child=subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3], sys.argv[4], 'sibling'],
                           preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))
    open(sys.argv[5], 'w').write('%d,%d' % (os.getpid(), child.pid))
    while True: time.sleep(60)
subprocess.Popen([sys.executable, sys.argv[0], 'leader', *sys.argv[1:]],
                 start_new_session=True)
while True: time.sleep(60)
"""


def preferred_port(project_root, card):
    digest = hashlib.sha256(os.path.realpath(project_root).encode("utf-8")).digest()
    slot = int.from_bytes(digest[:4], "big") % 300
    return 20000 + slot * 100 + (card % 100)


def pid_exists_for_test(pid):
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL).stdout.strip()
    return bool(status and not status.startswith("Z"))


class SprintPreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sprint-preview-test-")
        self.root = pathlib.Path(self.temp.name)
        self.registry = self.root / "preview-registry.json"
        self.server_script = self.root / "http_server.py"
        self.server_script.write_text(HTTP_SERVER, encoding="utf-8")
        self.timed_server_script = self.root / "timed_http_server.py"
        self.timed_server_script.write_text(TIMED_HTTP_SERVER, encoding="utf-8")
        self.closing_server_script = self.root / "closing_http_server.py"
        self.closing_server_script.write_text(CLOSING_HTTP_SERVER, encoding="utf-8")
        self.blocker_script = self.root / "blocker.py"
        self.blocker_script.write_text(BLOCKER, encoding="utf-8")
        self.wrapper_script = self.root / "wrapper.py"
        self.wrapper_script.write_text(WRAPPER, encoding="utf-8")
        self.fork_exit_script = self.root / "fork_and_exit.py"
        self.fork_exit_script.write_text(FORK_AND_EXIT, encoding="utf-8")
        self.detached_wrapper_script = self.root / "detached_wrapper.py"
        self.detached_wrapper_script.write_text(DETACHED_WRAPPER, encoding="utf-8")
        self.sibling_group_script = self.root / "sibling_group.py"
        self.sibling_group_script.write_text(SIBLING_GROUP_WRAPPER, encoding="utf-8")
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        real_lsof = shutil.which("lsof")
        if not real_lsof:
            self.skipTest("lsof is required")
        fake_lsof = self.fake_bin / "lsof"
        fake_lsof.write_text(
            "#!/bin/sh\nsleep \"${TEST_LSOF_DELAY:-0}\"\n"
            "if [ \"${TEST_LSOF_MODE:-real}\" = fail ]; then exit 7; fi\n"
            "if [ \"${TEST_LSOF_MODE:-real}\" = empty ]; then exit 1; fi\n"
            "if [ \"${TEST_LSOF_MODE:-real}\" = nonzero_stdout ]; then "
            "echo 'p99999'; echo 'n127.0.0.1:1'; exit 7; fi\n"
            "exec %s \"$@\"\n" % real_lsof,
            encoding="utf-8")
        fake_lsof.chmod(0o755)
        real_ps = shutil.which("ps")
        fake_ps = self.fake_bin / "ps"
        fake_ps.write_text(
            "#!/bin/sh\n"
            "count_file=\"${TEST_PS_COUNT_FILE:-/dev/null}\"\n"
            "count=0\n"
            "[ ! -f \"$count_file\" ] || count=$(cat \"$count_file\")\n"
            "count=$((count + 1))\n"
            "[ \"$count_file\" = /dev/null ] || echo \"$count\" > \"$count_file\"\n"
            "sleep \"${TEST_PS_DELAY:-0}\"\n"
            "if [ \"${TEST_PS_MODE:-real}\" = fail ]; then exit 127; fi\n"
            "if [ \"${TEST_PS_MODE:-real}\" = nonzero_stdout ]; then "
            "echo 'Sun Jan  1 00:00:00 2000'; exit 7; fi\n"
            "if [ -n \"${TEST_PS_SWAP_AFTER:-}\" ] && "
            "[ \"$count\" -gt \"$TEST_PS_SWAP_AFTER\" ]; then "
            "echo 'Mon Jan  2 00:00:00 2000'; exit 0; fi\n"
            "if [ -n \"${TEST_PS_FAIL_AFTER:-}\" ] && "
            "[ \"$count\" -gt \"$TEST_PS_FAIL_AFTER\" ]; then exit 127; fi\n"
            "exec %s \"$@\"\n" % real_ps,
            encoding="utf-8")
        fake_ps.chmod(0o755)
        self.started = []
        self.blockers = []
        self.helpers = []
        self.test_group_pid_files = []

    def tearDown(self):
        self.cleanup_tracked_helpers()
        for pid_file in self.test_group_pid_files:
            if not pid_file.exists():
                continue
            pids = [int(value) for value in
                    pid_file.read_text(encoding="utf-8").split(",") if value]
            for pid in pids:
                command = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(pid)], text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
                if str(self.root) not in command:
                    continue
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
        for card, project in reversed(self.started):
            record_path = project / ".sprint" / "previews" / ("card-%d.json" % card)
            record = None
            if record_path.exists():
                with record_path.open(encoding="utf-8") as src:
                    record = json.load(src)
            stopped = self.run_preview("stop", card, project, check=False)
            if stopped.returncode and record:
                # Test-only last resort: this record was created by this test,
                # and the exact launch PGID was captured before assertions.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(int(record.get("pgid") or record["pid"]), signal.SIGKILL)
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

    def cleanup_tracked_helpers(self):
        for helper, child_pid_file in reversed(self.helpers):
            if helper.poll() is None:
                helper.terminate()
                try:
                    # Startup deliberately defers signals across its bounded
                    # ownership lookup and exact-group cleanup (15s + reap).
                    helper.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    helper.kill()
                    helper.wait(timeout=2)
            if child_pid_file and child_pid_file.exists():
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                command = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(child_pid)], text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
                if str(self.root) in command:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(child_pid), signal.SIGKILL)
            if helper.stdout:
                helper.stdout.close()
            if helper.stderr:
                helper.stderr.close()
        self.helpers.clear()

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
                              timeout=45, check=False, env=env)
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

    def assert_pid_gone(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                    text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL).stdout.strip()
            if not status or status.startswith("Z"):
                return
            time.sleep(0.05)
        self.fail("preview process %d survived cleanup" % pid)

    def start_preview(self, card, project, worktree, marker):
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8", "--",
                 sys.executable, self.server_script, "{host}", "{port}", marker]
        proc = self.run_preview("start", card, project, extra)
        record = json.loads(proc.stdout)
        self.started.append((card, project))
        return record

    def register_started(self, card, project, record):
        """Register exact cleanup before a test makes any assertions."""
        self.started.append((card, project))
        return record

    def track_helper(self, helper, child_pid_file=None):
        """Install teardown before any wait/assertion can fail."""
        self.helpers.append((helper, child_pid_file))
        return helper

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
        preview = json.loads(proc.stdout)
        self.started.append((87, project))
        self.assertGreaterEqual(time.monotonic() - started, 3.4)
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
        self.assert_pid_gone(child_pid)
        self.assertFalse((project / ".sprint" / "previews" / "card-88.json").exists())

    def test_sigterm_during_slow_lookup_reaps_exact_process_group(self):
        project, worktree = self.board("signal-cleanup-board")
        child_pid_file = self.root / "signal-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "20", "--",
                 sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = self.delayed_lsof_env(10)
        helper = subprocess.Popen(self.command("start", 89, project, extra), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(helper, child_pid_file)
        deadline = time.monotonic() + 20
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_pid_file.exists(), "wrapper child never launched")
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        time.sleep(0.2)
        helper.terminate()
        # The real ownership query can take ~9 seconds; signals stay blocked
        # until that bounded lookup returns and exact cleanup completes.
        stdout, stderr = helper.communicate(timeout=20)
        self.assertNotEqual(helper.returncode, 0, stdout)
        self.assertIn("interrupted by signal", stderr)
        self.assert_pid_gone(child_pid)
        self.assertFalse((project / ".sprint" / "previews" / "card-89.json").exists())

    def test_missing_ps_identity_still_reaps_launched_group(self):
        project, worktree = self.board("missing-ps-board")
        child_pid_file = self.root / "missing-ps-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5", "--",
                 sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = self.delayed_lsof_env(0)
        env["TEST_PS_MODE"] = "fail"
        env["TEST_PS_DELAY"] = "0.3"
        proc = self.run_preview("start", 90, project, extra, check=False, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("could not record", proc.stderr)
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            self.assert_pid_gone(child_pid)
        self.assertFalse((project / ".sprint" / "previews" / "card-90.json").exists())

    def test_cleanup_fails_closed_when_ps_breaks_after_identity(self):
        project, worktree = self.board("cleanup-ps-failure-board")
        child_pid_file = self.root / "cleanup-ps-child.pid"
        count_file = self.root / "ps-count"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5", "--",
                 sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = self.delayed_lsof_env(1, timeout=0.2)
        env["TEST_PS_COUNT_FILE"] = str(count_file)
        env["TEST_PS_FAIL_AFTER"] = "2"
        proc = self.run_preview("start", 91, project, extra, check=False, env=env)
        self.assertNotEqual(proc.returncode, 0)
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            self.assert_pid_gone(child_pid)
        self.assertFalse((project / ".sprint" / "previews" / "card-91.json").exists())

    def test_ps_timeout_during_identity_still_reaps_launched_group(self):
        project, worktree = self.board("ps-timeout-board")
        child_pid_file = self.root / "ps-timeout-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5", "--",
                 sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = self.delayed_lsof_env(0)
        env["TEST_PS_DELAY"] = "3"
        proc = self.run_preview("start", 92, project, extra, check=False, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("could not record", proc.stderr)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.assert_pid_gone(child_pid)
        self.assertFalse((project / ".sprint" / "previews" / "card-92.json").exists())

    def test_launch_exception_releases_starting_lease(self):
        project, worktree = self.board("launch-exception-board")
        missing = self.root / "command-that-does-not-exist"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5", "--",
                 missing, "{host}", "{port}"]
        proc = self.run_preview("start", 93, project, extra, check=False)
        self.assertNotEqual(proc.returncode, 0)
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        key = os.path.realpath(project) + "\0card-93"
        self.assertNotIn(key, registry)

    def test_signals_between_publish_and_return_preserve_success(self):
        for offset, signum in enumerate((signal.SIGINT, signal.SIGTERM, signal.SIGHUP)):
            with self.subTest(signal=signum):
                card = 94 + offset
                project, worktree = self.board("published-signal-%d" % signum)
                extra = ["--worktree", worktree, "--host", "127.0.0.1",
                         "--timeout", "8", "--", sys.executable,
                         self.server_script, "{host}", "{port}", "published"]
                env = os.environ.copy()
                env["SPRINT_PREVIEW_TEST_POST_PUBLISH_DELAY"] = "5"
                helper = subprocess.Popen(self.command("start", card, project, extra),
                                          text=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, env=env)
                self.track_helper(helper)
                record_path = project / ".sprint" / "previews" / ("card-%d.json" % card)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if record_path.exists():
                        candidate = json.loads(record_path.read_text(encoding="utf-8"))
                        if candidate.get("published"):
                            break
                    time.sleep(0.02)
                self.assertTrue(record_path.exists(), "preview never published its record")
                record = json.loads(record_path.read_text(encoding="utf-8"))
                self.assertTrue(record.get("published"), "record remained provisional")
                os.kill(helper.pid, signum)
                stdout, stderr = helper.communicate(timeout=8)
                self.assertEqual(helper.returncode, 0, stderr)
                response = json.loads(stdout)
                self.assertEqual(response["pid"], record["pid"])
                self.assertEqual(self.read_url(response["live_url"]), "published")
                cleanup = self.run_preview("stop", card, project)
                self.assertTrue(json.loads(cleanup.stdout)["stopped"])
                self.assert_pid_gone(record["pid"])
                self.assertFalse(record_path.exists())

    def test_signal_between_popen_and_pgid_still_reaps_group(self):
        project, worktree = self.board("popen-pgid-signal")
        child_pid_file = self.root / "popen-pgid-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8", "--",
                 sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = os.environ.copy()
        env["SPRINT_PREVIEW_TEST_POST_POPEN_DELAY"] = "2"
        helper = subprocess.Popen(self.command("start", 100, project, extra), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(helper, child_pid_file)
        deadline = time.monotonic() + 20
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_pid_file.exists())
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        helper.terminate()
        stdout, stderr = helper.communicate(timeout=10)
        self.assertNotEqual(helper.returncode, 0, stdout)
        self.assertIn("interrupted by signal", stderr)
        self.assert_pid_gone(child_pid)

    def test_repeated_signal_cannot_interrupt_stubborn_cleanup(self):
        project, worktree = self.board("repeated-signal-cleanup")
        child_pid_file = self.root / "repeated-signal-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "20", "--",
                 sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = self.delayed_lsof_env(10)
        helper = subprocess.Popen(self.command("start", 101, project, extra), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(helper, child_pid_file)
        deadline = time.monotonic() + 20
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_pid_file.exists())
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        helper.terminate()
        time.sleep(0.3)
        helper.terminate()
        stdout, stderr = helper.communicate(timeout=20)
        self.assertNotEqual(helper.returncode, 0, stdout)
        self.assert_pid_gone(child_pid)

    def test_signal_after_record_replace_compensates_disk_and_registry(self):
        project, worktree = self.board("post-replace-signal")
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8", "--",
                 sys.executable, self.server_script, "{host}", "{port}", "replace"]
        env = os.environ.copy()
        env["SPRINT_PREVIEW_TEST_POST_REPLACE_DELAY"] = "5"
        helper = subprocess.Popen(self.command("start", 102, project, extra), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(helper)
        record_path = project / ".sprint" / "previews" / "card-102.json"
        deadline = time.monotonic() + 20
        while not record_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(record_path.exists())
        record = json.loads(record_path.read_text(encoding="utf-8"))
        helper.terminate()
        helper.communicate(timeout=10)
        self.assertNotEqual(helper.returncode, 0)
        self.assert_pid_gone(record["pid"])
        self.assertFalse(record_path.exists())
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertNotIn(os.path.realpath(project) + "\0card-102", registry)

    def test_reused_preview_signal_returns_durable_success(self):
        project, worktree = self.board("reused-signal")
        preview = self.start_preview(103, project, worktree, "reused")
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8", "--",
                 sys.executable, self.server_script, "{host}", "{port}", "unused"]
        env = self.delayed_lsof_env(5)
        helper = subprocess.Popen(self.command("start", 103, project, extra), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(helper)
        time.sleep(0.3)
        helper.terminate()
        stdout, stderr = helper.communicate(timeout=15)
        self.assertEqual(helper.returncode, 0, stderr)
        reused = json.loads(stdout)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["pid"], preview["pid"])
        self.assertEqual(self.read_url(reused["live_url"]), "reused")
        self.run_preview("stop", 103, project)
        self.assert_pid_gone(preview["pid"])
        self.started.remove((103, project))

    def test_stop_preserves_state_when_ps_cannot_prove_identity(self):
        modes = ("missing", "timeout", "nonzero")
        for offset, mode in enumerate(modes):
            with self.subTest(mode=mode):
                card = 97 + offset
                project, worktree = self.board("stop-ps-%s" % mode)
                preview = self.start_preview(card, project, worktree, mode)
                record_path = project / ".sprint" / "previews" / ("card-%d.json" % card)
                env = os.environ.copy()
                if mode == "missing":
                    empty_bin = self.root / ("empty-bin-%d" % card)
                    empty_bin.mkdir()
                    env["PATH"] = str(empty_bin)
                else:
                    env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
                    if mode == "timeout":
                        env["TEST_PS_DELAY"] = "3"
                    else:
                        env["TEST_PS_MODE"] = "fail"
                stopped = self.run_preview("stop", card, project, check=False, env=env)
                self.assertNotEqual(stopped.returncode, 0)
                self.assertIn("could not prove", stopped.stderr)
                self.assertTrue(record_path.exists(), "failed stop discarded ownership record")
                self.assertEqual(self.read_url(preview["live_url"]), mode)
                cleanup = self.run_preview("stop", card, project)
                self.assertTrue(json.loads(cleanup.stdout)["stopped"])
                self.started.remove((card, project))
                self.assertFalse(record_path.exists())

    def test_stop_rejects_ps_nonzero_even_with_identity_stdout(self):
        project, worktree = self.board("stop-ps-nonzero-stdout")
        preview = self.start_preview(104, project, worktree, "nonzero-stdout")
        record_path = project / ".sprint" / "previews" / "card-104.json"
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["TEST_PS_MODE"] = "nonzero_stdout"
        stopped = self.run_preview("stop", 104, project, check=False, env=env)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertTrue(record_path.exists())
        self.assertEqual(self.read_url(preview["live_url"]), "nonzero-stdout")
        self.run_preview("stop", 104, project)
        self.started.remove((104, project))

    def test_identity_swap_before_kill_preserves_unrelated_process(self):
        project, worktree = self.board("stop-identity-swap")
        preview = self.start_preview(105, project, worktree, "identity-swap")
        record_path = project / ".sprint" / "previews" / "card-105.json"
        count_file = self.root / "identity-swap-count"
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["TEST_PS_COUNT_FILE"] = str(count_file)
        env["TEST_PS_SWAP_AFTER"] = "1"
        stopped = self.run_preview("stop", 105, project, check=False, env=env)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("changed during verification", stopped.stderr)
        self.assertTrue(record_path.exists())
        self.assertEqual(self.read_url(preview["live_url"]), "identity-swap")
        self.run_preview("stop", 105, project)
        self.started.remove((105, project))

    def test_reused_start_preserves_lease_when_ownership_tools_fail(self):
        modes = ("ps-missing", "ps-timeout", "ps-nonzero",
                 "lsof-missing", "lsof-timeout", "lsof-nonzero")
        for offset, mode in enumerate(modes):
            with self.subTest(mode=mode):
                card = 106 + offset
                project, worktree = self.board("reuse-tool-%s" % mode)
                preview = self.start_preview(card, project, worktree, mode)
                record_path = project / ".sprint" / "previews" / ("card-%d.json" % card)
                env = os.environ.copy()
                if mode.endswith("missing"):
                    tool_dir = self.root / ("tool-dir-%d" % card)
                    tool_dir.mkdir()
                    keep = "lsof" if mode.startswith("ps") else "ps"
                    os.symlink(shutil.which(keep), tool_dir / keep)
                    env["PATH"] = str(tool_dir)
                else:
                    env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
                    if mode == "ps-timeout":
                        env["TEST_PS_DELAY"] = "3"
                    elif mode == "ps-nonzero":
                        env["TEST_PS_MODE"] = "nonzero_stdout"
                    elif mode == "lsof-timeout":
                        env["TEST_LSOF_DELAY"] = "1"
                        env["SPRINT_PREVIEW_OWNERSHIP_TIMEOUT"] = "0.2"
                    else:
                        env["TEST_LSOF_MODE"] = "nonzero_stdout"
                extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5",
                         "--", sys.executable, self.server_script,
                         "{host}", "{port}", "unused"]
                retried = self.run_preview("start", card, project, extra,
                                           check=False, env=env)
                self.assertNotEqual(retried.returncode, 0)
                self.assertTrue(record_path.exists())
                registry = json.loads(self.registry.read_text(encoding="utf-8"))
                key = os.path.realpath(project) + "\0card-%d" % card
                self.assertEqual(registry[key]["lease"],
                                 json.loads(record_path.read_text(encoding="utf-8"))["lease"])
                self.assertEqual(self.read_url(preview["live_url"]), mode)
                verified = self.run_preview("verify", card, project,
                                            ["--url", preview["live_url"]])
                self.assertEqual(json.loads(verified.stdout)["pid"], preview["pid"])
                self.run_preview("stop", card, project)
                self.started.remove((card, project))

    def test_stop_signals_cannot_escape_stubborn_group_cleanup(self):
        for offset, signum in enumerate((signal.SIGINT, signal.SIGTERM, signal.SIGHUP)):
            with self.subTest(signal=signum):
                card = 112 + offset
                project, worktree = self.board("stop-signal-%d" % signum)
                child_pid_file = self.root / ("stop-signal-%d.pid" % signum)
                extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                         "--", sys.executable, self.wrapper_script, self.server_script,
                         "{host}", "{port}", child_pid_file]
                started = self.run_preview("start", card, project, extra)
                record = json.loads(started.stdout)
                self.started.append((card, project))
                env = os.environ.copy()
                env["SPRINT_PREVIEW_TEST_POST_STOP_TERM_DELAY"] = "2"
                stopper = subprocess.Popen(self.command("stop", card, project), text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                self.track_helper(stopper, child_pid_file)
                deadline = time.monotonic() + 20
                while pid_exists_for_test(record["pid"]) and time.monotonic() < deadline:
                    time.sleep(0.02)
                os.kill(stopper.pid, signum)
                os.kill(stopper.pid, signum)
                stdout, stderr = stopper.communicate(timeout=10)
                self.assertEqual(stopper.returncode, 0, stderr)
                self.assertTrue(json.loads(stdout)["stopped"])
                self.assert_pid_gone(int(child_pid_file.read_text(encoding="utf-8")))
                self.started.remove((card, project))

    def test_retry_stop_recovers_dead_leader_with_proven_listener(self):
        project, worktree = self.board("dead-leader-recovery")
        child_pid_file = self.root / "dead-leader-child.pid"
        card = 115
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        started = self.run_preview("start", card, project, extra)
        preview = json.loads(started.stdout)
        self.started.append((card, project))
        env = os.environ.copy()
        env["SPRINT_PREVIEW_TEST_POST_STOP_TERM_DELAY"] = "10"
        stopper = subprocess.Popen(self.command("stop", card, project), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(stopper, child_pid_file)
        time.sleep(0.3)
        self.assertTrue(pid_exists_for_test(preview["pid"]),
                        "persistent supervisor exited before its stubborn descendant")
        stopper.kill()
        stopper.communicate(timeout=5)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.assertTrue(pid_exists_for_test(child_pid))
        record_path = project / ".sprint" / "previews" / "card-115.json"
        self.assertTrue(record_path.exists())
        recovered = self.run_preview("stop", card, project)
        self.assertTrue(json.loads(recovered.stdout)["stopped"])
        self.assert_pid_gone(child_pid)
        self.assertFalse(record_path.exists())
        self.started.remove((card, project))

    def test_dead_leader_live_listener_survives_unrelated_reserve(self):
        project, worktree = self.board("dead-leader-reserve")
        child_pid_file = self.root / "dead-leader-reserve-child.pid"
        card = 116
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        started = self.run_preview("start", card, project, extra)
        preview = self.register_started(card, project, json.loads(started.stdout))
        env = os.environ.copy()
        env["SPRINT_PREVIEW_TEST_POST_STOP_TERM_DELAY"] = "10"
        stopper = subprocess.Popen(self.command("stop", card, project), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(stopper, child_pid_file)
        time.sleep(0.3)
        self.assertTrue(pid_exists_for_test(preview["pid"]),
                        "persistent supervisor abandoned its owned group")
        stopper.kill()
        stopper.communicate(timeout=5)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        other_project, other_worktree = self.board("unrelated-reserve")
        other = self.start_preview(117, other_project, other_worktree, "unrelated")
        self.assertTrue(pid_exists_for_test(child_pid))
        self.assertEqual(self.read_url(preview["live_url"]), "stubborn")
        recovered = self.run_preview("stop", card, project)
        self.assertTrue(json.loads(recovered.stdout)["stopped"])
        self.assert_pid_gone(child_pid)
        self.started.remove((card, project))
        self.assertEqual(self.read_url(other["live_url"]), "unrelated")

    def test_rc1_empty_lsof_preserves_healthy_reused_preview(self):
        project, worktree = self.board("reuse-empty-lsof")
        card = 118
        preview = self.start_preview(card, project, worktree, "empty-lsof")
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["TEST_LSOF_MODE"] = "empty"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5",
                 "--", sys.executable, self.server_script, "{host}", "{port}", "unused"]
        retried = self.run_preview("start", card, project, extra, check=False, env=env)
        self.assertNotEqual(retried.returncode, 0)
        record_path = project / ".sprint" / "previews" / "card-118.json"
        self.assertTrue(record_path.exists())
        self.assertEqual(self.read_url(preview["live_url"]), "empty-lsof")
        verified = self.run_preview("verify", card, project, ["--url", preview["live_url"]])
        self.assertEqual(json.loads(verified.stdout)["pid"], preview["pid"])
        self.run_preview("stop", card, project)
        self.started.remove((card, project))

    def test_fork_exit_before_getpgid_reaps_candidate_session(self):
        project, worktree = self.board("fork-exit-before-getpgid")
        child_pid_file = self.root / "fork-exit-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5",
                 "--", sys.executable, self.fork_exit_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = os.environ.copy()
        env["SPRINT_PREVIEW_TEST_POST_POPEN_DELAY"] = "1"
        proc = self.run_preview("start", 119, project, extra, check=False, env=env)
        self.assertNotEqual(proc.returncode, 0)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.assert_pid_gone(child_pid)
        self.assertFalse((project / ".sprint" / "previews" / "card-119.json").exists())

    def test_forced_assertion_path_runs_exact_test_teardown(self):
        project, worktree = self.board("forced-failure-teardown")
        child_pid_file = self.root / "forced-failure-child.pid"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "20",
                 "--", sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        env = self.delayed_lsof_env(10)
        helper = subprocess.Popen(self.command("start", 120, project, extra), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(helper, child_pid_file)
        deadline = time.monotonic() + 20
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_pid_file.exists())
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        try:
            self.assertTrue(False, "synthetic assertion failure")
        except AssertionError:
            self.cleanup_tracked_helpers()
        self.assert_pid_gone(child_pid)

    def test_sigkill_before_publication_self_cleans_and_retry_is_unique(self):
        for offset, ps_mode in enumerate(("unavailable_after",)):
            with self.subTest(ps_mode=ps_mode):
                card = 121 + offset
                project, worktree = self.board("sigkill-launch-%s" % ps_mode)
                child_pid_file = self.root / ("sigkill-%s.pid" % ps_mode)
                extra = ["--worktree", worktree, "--host", "127.0.0.1",
                         "--timeout", "20", "--", sys.executable,
                         self.wrapper_script, self.server_script, "{host}", "{port}",
                         child_pid_file]
                env = self.delayed_lsof_env(10)
                helper = subprocess.Popen(self.command("start", card, project, extra),
                                          text=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, env=env)
                self.track_helper(helper, child_pid_file)
                deadline = time.monotonic() + 20
                while not child_pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(child_pid_file.exists())
                first_child = int(child_pid_file.read_text(encoding="utf-8"))
                if ps_mode == "unavailable_after":
                    self.fake_bin.joinpath("ps").unlink()
                helper.kill()
                helper.communicate(timeout=5)
                self.assert_pid_gone(first_child)
                key = os.path.realpath(project) + "\0card-%d" % card
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    registry = json.loads(self.registry.read_text(encoding="utf-8"))
                    if key not in registry:
                        break
                    time.sleep(0.05)
                self.assertNotIn(key, registry)
                self.assertEqual(list((project / ".sprint" / "previews").glob(
                    "card-%d.json.*.supervisor*" % card)), [])
                retry = self.start_preview(card, project, worktree, "retry")
                self.assertEqual(self.read_url(retry["live_url"]), "retry")

    def test_supervisor_artifacts_never_persist_environment_secrets(self):
        project, worktree = self.board("supervisor-secret")
        card = 123
        sentinel = "SENTINEL_SECRET_DO_NOT_PERSIST_7a9c"
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.server_script, "{host}", "{port}", "secret"]
        env = os.environ.copy()
        env["UNRELATED_SECRET_TOKEN"] = sentinel
        env["SPRINT_PREVIEW_TEST_POST_PUBLISH_DELAY"] = "2"
        helper = subprocess.Popen(self.command("start", card, project, extra), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.track_helper(helper)
        record_path = project / ".sprint" / "previews" / "card-123.json"
        deadline = time.monotonic() + 20
        while not record_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(record_path.exists())
        record = self.register_started(
            card, project, json.loads(record_path.read_text(encoding="utf-8")))
        artifacts = list((project / ".sprint" / "previews").glob("card-123*"))
        self.assertTrue(artifacts)
        for artifact in artifacts:
            self.assertNotIn(sentinel, artifact.read_text(encoding="utf-8", errors="replace"))
        stdout, stderr = helper.communicate(timeout=8)
        self.assertEqual(helper.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["pid"], record["pid"])

    def test_reaped_supervisor_never_signals_recycled_numeric_pgid(self):
        code = """
import importlib.machinery, importlib.util, subprocess
loader = importlib.machinery.SourceFileLoader('preview', %r)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec); loader.exec_module(module)
child = subprocess.Popen(['/usr/bin/true'], start_new_session=True)
pgid = child.pid; child.wait()
called = []
module.os.killpg = lambda *args: called.append(args)
module.terminate_launched_group(child, pgid)
assert called == [], called
""" % str(PREVIEW)
        proc = subprocess.run([sys.executable, "-c", code], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def assert_preview_state_removed(self, card, project, timeout=8):
        key = os.path.realpath(project) + "\0card-%d" % card
        record_path = project / ".sprint" / "previews" / ("card-%d.json" % card)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            registry = json.loads(self.registry.read_text(encoding="utf-8"))
            artifacts = list(record_path.parent.glob(record_path.name + ".*supervisor*"))
            if key not in registry and not record_path.exists() and not artifacts:
                return
            time.sleep(0.05)
        self.fail("natural exit left preview ownership state: key=%s record=%s artifacts=%s" %
                  (key in registry, record_path.exists(), [str(p) for p in artifacts]))

    def test_natural_exit_before_publication_cleans_and_allows_retry(self):
        project, worktree = self.board("natural-exit-before-publish")
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "5",
                 "--", sys.executable, "-c", "raise SystemExit(0)"]
        proc = self.run_preview("start", 124, project, extra, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assert_preview_state_removed(124, project)
        retry = self.start_preview(124, project, worktree, "retry-before")
        self.assertEqual(self.read_url(retry["live_url"]), "retry-before")

    def test_natural_and_nonzero_exit_after_publication_clean_state(self):
        for offset, code in enumerate((0, 7)):
            with self.subTest(code=code):
                card = 125 + offset
                project, worktree = self.board("natural-exit-%d" % code)
                extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                         "--", sys.executable, self.timed_server_script, "{host}", "{port}",
                         "timed", "1.5", str(code)]
                started = self.run_preview("start", card, project, extra)
                preview = self.register_started(card, project, json.loads(started.stdout))
                self.assertEqual(self.read_url(preview["live_url"]), "timed")
                self.assert_preview_state_removed(card, project)
                self.started.remove((card, project))
                retry = self.start_preview(card, project, worktree, "retry-after")
                self.assertEqual(self.read_url(retry["live_url"]), "retry-after")

    def test_inspection_failure_is_bounded_and_later_lifecycle_recovers(self):
        for offset, mode in enumerate(("fail", "timeout")):
            with self.subTest(mode=mode):
                card = 127 + offset
                project, worktree = self.board("inspection-%s" % mode)
                extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                         "--", sys.executable, self.timed_server_script, "{host}", "{port}",
                         "inspect", "5", "0"]
                env = os.environ.copy()
                env["SPRINT_PREVIEW_TEST_GROUP_INSPECTION"] = mode
                started = self.run_preview("start", card, project, extra, env=env)
                preview = json.loads(started.stdout)
                self.assertEqual(self.read_url(preview["live_url"]), "inspect")
                self.assert_preview_state_removed(card, project, timeout=10)
                retry = self.start_preview(card, project, worktree, "inspection-retry")
                verified = self.run_preview("verify", card, project,
                                            ["--url", retry["live_url"]])
                self.assertEqual(json.loads(verified.stdout)["pid"], retry["pid"])
                self.run_preview("stop", card, project)
                self.started.remove((card, project))

    def test_supervisor_sigkill_boundaries_leave_no_unowned_command(self):
        modes = ("PRE_COMMAND", "PRE_READY", "POST_COMMAND")
        for offset, mode in enumerate(modes):
            with self.subTest(mode=mode):
                card = 129 + offset
                project, worktree = self.board("supervisor-kill-%s" % mode.lower())
                extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "12",
                         "--", sys.executable, self.server_script, "{host}", "{port}", mode]
                env = os.environ.copy()
                env["SPRINT_PREVIEW_TEST_SUPERVISOR_%s_DELAY" % mode] = "5"
                helper = subprocess.Popen(self.command("start", card, project, extra),
                                          text=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, env=env)
                self.track_helper(helper)
                preview_dir = project / ".sprint" / "previews"
                deadline = time.monotonic() + 20
                control = None
                proof = None
                while time.monotonic() < deadline:
                    matches = list(preview_dir.glob("card-%d.json.*.supervisor" % card))
                    if matches:
                        candidate = json.loads(matches[0].read_text(encoding="utf-8"))
                        needed = "supervisor_pid"
                        if mode != "PRE_COMMAND":
                            needed = "command_pid"
                        if candidate.get(needed):
                            control, proof = matches[0], candidate
                            break
                    time.sleep(0.02)
                self.assertIsNotNone(control)
                os.kill(int(proof["supervisor_pid"]), signal.SIGKILL)
                _stdout, helper_stderr = helper.communicate(timeout=12)
                self.assertNotEqual(helper.returncode, 0)
                command_pid = int(proof.get("command_pid") or 0)
                if command_pid:
                    self.assert_pid_gone(command_pid)
                try:
                    self.assert_preview_state_removed(card, project)
                except AssertionError as exc:
                    self.fail("%s; helper stderr=%s" % (exc, helper_stderr))
                retry = self.start_preview(card, project, worktree, "unique-retry")
                self.assertEqual(self.read_url(retry["live_url"]), "unique-retry")

    def test_detached_listener_is_rejected_and_exactly_cleaned(self):
        project, worktree = self.board("detached-listener")
        card = 132
        unrelated_port = preferred_port(project, card) + 1
        unrelated = self.start_blocker("v4", "127.0.0.1", unrelated_port)
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.detached_wrapper_script,
                 self.server_script, "{host}", "{port}"]
        proc = self.run_preview("start", card, project, extra, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("escaped the owned process group", proc.stderr)
        self.assert_preview_state_removed(card, project)
        self.assertIsNone(unrelated.poll(), "detached cleanup touched unrelated listener")
        retry = self.start_preview(card, project, worktree, "detached-retry")
        verified = self.run_preview("verify", card, project, ["--url", retry["live_url"]])
        self.assertEqual(json.loads(verified.stdout)["pid"], retry["pid"])

    def test_unrelated_detached_listener_is_never_signalled(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        spawn = """
import subprocess, sys
p=subprocess.Popen([sys.executable, %r, 'v4', '127.0.0.1', %r],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   start_new_session=True)
print(p.pid)
""" % (str(self.blocker_script), str(port))
        blocker_pid = int(subprocess.check_output(
            [sys.executable, "-c", spawn], text=True).strip())
        self.addCleanup(lambda: os.kill(blocker_pid, signal.SIGKILL)
                        if pid_exists_for_test(blocker_pid) else None)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=.1)
                connection.close()
                break
            except OSError:
                time.sleep(.05)
        code = """
import importlib.machinery, importlib.util, os
loader=importlib.machinery.SourceFileLoader('preview', %r)
spec=importlib.util.spec_from_loader(loader.name, loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
m.persist_escaped_cleanup_state=lambda record,escaped:None
r={'pid':os.getpid(),'pgid':os.getpgrp(),'host':'127.0.0.1','port':%d}
try: m.capture_listener_proofs(r)
except m.PreviewError as e:
 assert 'unrelated detached listener' in str(e), e
else: raise AssertionError('unrelated listener accepted')
""" % (str(PREVIEW), port)
        checked = subprocess.run([sys.executable, "-c", code], text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertTrue(pid_exists_for_test(blocker_pid),
                        "ownership check killed unrelated listener")

    def test_stop_live_supervisor_after_listener_closes(self):
        project, worktree = self.board("listener-closes")
        card = 133
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.closing_server_script, "{host}", "{port}"]
        started = self.run_preview("start", card, project, extra)
        preview = self.register_started(card, project, json.loads(started.stdout))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.read_url(preview["live_url"])
            except (urllib.error.URLError, OSError):
                break
            time.sleep(.1)
        self.assertTrue(pid_exists_for_test(preview["pid"]))
        stopped = self.run_preview("stop", card, project)
        self.assertTrue(json.loads(stopped.stdout)["stopped"])
        self.assert_pid_gone(preview["pid"])
        self.started.remove((card, project))

    def test_stop_identity_flip_between_term_and_kill_never_escalates(self):
        project, worktree = self.board("stop-escalation-swap")
        child_pid_file = self.root / "stop-escalation-child.pid"
        card = 134
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        started = self.run_preview("start", card, project, extra)
        preview = self.register_started(card, project, json.loads(started.stdout))
        unrelated = self.start_blocker("v4", "127.0.0.1", preview["port"] + 1)
        count_file = self.root / "stop-escalation-count"
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["TEST_PS_COUNT_FILE"] = str(count_file)
        env["TEST_PS_SWAP_AFTER"] = "2"
        env["SPRINT_PREVIEW_TEST_FORCE_GROUP_ALIVE"] = "1"
        stopped = self.run_preview("stop", card, project, check=False, env=env)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("refusing escalation", stopped.stderr)
        self.assertIsNone(unrelated.poll(), "identity flip escalation killed unrelated process")
        self.assertTrue((project / ".sprint" / "previews" / "card-134.json").exists())
        recovered = self.run_preview("stop", card, project)
        self.assertTrue(json.loads(recovered.stdout)["stopped"])
        self.started.remove((card, project))

    def test_launched_and_detached_identity_flip_never_send_kill(self):
        code = """
import importlib.machinery, importlib.util, signal
loader=importlib.machinery.SourceFileLoader('preview', %r)
spec=importlib.util.spec_from_loader(loader.name, loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
class Child:
 pid=42420
 def poll(self): return None
 def wait(self, timeout=None): return None
signals=[]
m.os.getpgid=lambda pid: 42420
m.os.killpg=lambda pgid, sig: signals.append(sig)
m.process_group_alive=lambda pgid: True
m.read_record=lambda path: {'supervisor_pid':42420,'supervisor_pgid':42420,
 'supervisor_identity':'original','supervisor_executable':'/exact/python'}
m._system_pid_identity=lambda pid: 'flipped'
m.pid_executable=lambda pid: '/exact/python'
try: m.terminate_launched_group(Child(), 42420, timeout=0, control='proof')
except m.PreviewError: pass
assert signals == [signal.SIGTERM], signals
signals.clear(); identities=iter(['original','original','original','flipped'])
m.os.getpgid=lambda pid: pid
m.pid_exists=lambda pid: True
m.pid_identity=lambda pid: next(identities)
m.pid_executable=lambda pid: '/exact/listener'
m.process_group_member_pids=lambda pgid: {42421}
m.matching_listener_pids=lambda record: {42421}
m.descendants=lambda root:{42421}
try: m.cleanup_detached_listener({'pid':1,'host':'127.0.0.1','port':1},
                                 42421, 42421, 'original', {42421})
except m.PreviewError: pass
assert signals == [signal.SIGTERM], signals
""" % str(PREVIEW)
        proc = subprocess.run([sys.executable, "-c", code], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dead_supervisor_stubborn_listener_escalates_from_exact_proof(self):
        project, worktree = self.board("dead-supervisor-stubborn")
        child_pid_file = self.root / "dead-supervisor-stubborn.pid"
        card = 135
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.wrapper_script, self.server_script,
                 "{host}", "{port}", child_pid_file]
        started = self.run_preview("start", card, project, extra)
        preview = self.register_started(card, project, json.loads(started.stdout))
        listener_pid = int(child_pid_file.read_text(encoding="utf-8"))
        os.kill(preview["pid"], signal.SIGKILL)
        self.assert_pid_gone(preview["pid"])
        self.assertTrue(pid_exists_for_test(listener_pid))
        stopped = self.run_preview("stop", card, project, ["--timeout", "0.2"])
        self.assertTrue(json.loads(stopped.stdout)["stopped"])
        self.assert_pid_gone(listener_pid)
        self.assertFalse((project / ".sprint" / "previews" / "card-135.json").exists())
        self.started.remove((card, project))

    def test_dead_supervisor_listener_flip_preserves_state_without_kill(self):
        code = """
import importlib.machinery, importlib.util, signal
loader=importlib.machinery.SourceFileLoader('preview', %r)
spec=importlib.util.spec_from_loader(loader.name, loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
record={'pid':50000,'pgid':50000,'pid_identity':'supervisor','pid_executable':'/supervisor',
 'port':25000,'host':'127.0.0.1','listener_proofs':[{'pid':50001,
 'pid_identity':'listener','pid_executable':'/listener','pgid':50000}]}
identities=iter(['listener','flipped'])
m.pid_exists=lambda pid: pid == 50001
m.pid_identity=lambda pid: next(identities)
m.pid_executable=lambda pid: '/listener'
m.os.getpgid=lambda pid: 50000
m.matching_listener_pids=lambda record: {50001}
m.process_group_alive=lambda pgid: True
signals=[]; m.os.killpg=lambda pgid,sig: signals.append(sig)
try: m.terminate_record(record, timeout=0)
except m.PreviewError: pass
else: raise AssertionError('identity flip authorized escalation')
assert signals == [signal.SIGTERM], signals
""" % str(PREVIEW)
        proc = subprocess.run([sys.executable, "-c", code], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_sibling_led_escaped_listener_group_is_exactly_cleaned(self):
        project, worktree = self.board("sibling-led-escape")
        pid_file = self.root / "sibling-led.pid"
        self.test_group_pid_files.append(pid_file)
        card = 136
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.sibling_group_script,
                 self.server_script, "{host}", "{port}", pid_file]
        proc = self.run_preview("start", card, project, extra, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("escaped the owned process group", proc.stderr)
        leader_pid, listener_pid = map(
            int, pid_file.read_text(encoding="utf-8").split(","))
        self.assertNotEqual(leader_pid, listener_pid)
        self.assert_pid_gone(leader_pid)
        self.assert_pid_gone(listener_pid)
        self.assert_preview_state_removed(card, project)

    def test_detached_group_member_flip_never_escalates(self):
        code = """
import importlib.machinery, importlib.util, signal
loader=importlib.machinery.SourceFileLoader('preview', %r)
spec=importlib.util.spec_from_loader(loader.name, loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
record={'pid':1,'host':'127.0.0.1','port':25000}; calls=[0]
def members(_pgid):
 calls[0]+=1
 return {51000,51001} if calls[0] < 4 else {51000,51001,59999}
m.process_group_member_pids=members
m.descendants=lambda root:{51000,51001}
m.matching_listener_pids=lambda record: {51001}
m.pid_exists=lambda pid: True
m.pid_identity=lambda pid: 'id-'+str(pid)
m.pid_executable=lambda pid: '/exe-'+str(pid)
m.os.getpgid=lambda pid: 51000
m.process_group_alive=lambda pgid: True
signals=[]; m.os.killpg=lambda pgid,sig: signals.append(sig)
try: m.cleanup_detached_listener(record,51001,51000,'id-51001',{51000,51001})
except m.PreviewError: pass
else: raise AssertionError('new group member authorized escalation')
assert signals == [signal.SIGTERM], signals
""" % str(PREVIEW)
        proc = subprocess.run([sys.executable, "-c", code], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_sibling_group_flip_preserves_actionable_state_for_retry_stop(self):
        project, worktree = self.board("sibling-flip-state")
        pid_file = self.root / "sibling-flip.pid"
        self.test_group_pid_files.append(pid_file)
        count_file = self.root / "sibling-member-count"
        card = 137
        extra = ["--worktree", worktree, "--host", "127.0.0.1", "--timeout", "8",
                 "--", sys.executable, self.sibling_group_script,
                 self.server_script, "{host}", "{port}", pid_file]
        env = os.environ.copy()
        env["SPRINT_PREVIEW_TEST_GROUP_MEMBER_COUNT"] = str(count_file)
        env["SPRINT_PREVIEW_TEST_GROUP_MEMBER_FLIP_AFTER"] = "3"
        proc = self.run_preview("start", card, project, extra, check=False, env=env)
        self.assertNotEqual(proc.returncode, 0)
        record_path = project / ".sprint" / "previews" / "card-137.json"
        self.assertTrue(record_path.exists(), "cleanup mismatch discarded ownership state")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "cleanup_required")
        self.assertIn("escaped_group", record)
        leader_pid, listener_pid = map(int, pid_file.read_text(encoding="utf-8").split(","))
        self.assertTrue(pid_exists_for_test(leader_pid))
        self.assertTrue(pid_exists_for_test(listener_pid))
        stopped = self.run_preview("stop", card, project)
        self.assertTrue(json.loads(stopped.stdout)["stopped"])
        self.assert_pid_gone(leader_pid)
        self.assert_pid_gone(listener_pid)
        self.assertFalse(record_path.exists())

    def test_detached_reparent_rechecked_before_each_signal(self):
        code = """
import importlib.machinery, importlib.util, signal
loader=importlib.machinery.SourceFileLoader('preview', %r)
spec=importlib.util.spec_from_loader(loader.name, loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
record={'pid':1,'host':'127.0.0.1','port':25000}
m.process_group_member_pids=lambda pgid:{52000,52001}
m.matching_listener_pids=lambda record:{52001}
m.pid_exists=lambda pid:True
m.pid_identity=lambda pid:'id-'+str(pid)
m.pid_executable=lambda pid:'/exe-'+str(pid)
m.os.getpgid=lambda pid:52000
m.process_group_alive=lambda pgid:True
for ancestry, expected in [([set()], []),
                           ([{52000,52001},{52000,52001},set()], [signal.SIGTERM])]:
 calls=list(ancestry); m.descendants=lambda root: calls.pop(0) if calls else set()
 signals=[]; m.os.killpg=lambda pgid,sig:signals.append(sig)
 try: m.cleanup_detached_listener(record,52001,52000,'id-52001',{52000,52001})
 except m.PreviewError: pass
 else: raise AssertionError('reparented group authorized')
 assert signals == expected, (signals, expected)
""" % str(PREVIEW)
        proc = subprocess.run([sys.executable, "-c", code], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_first_escaped_observation_persists_all_tool_failures(self):
        code = """
import importlib.machinery, importlib.util
loader=importlib.machinery.SourceFileLoader('preview', %r)
spec=importlib.util.spec_from_loader(loader.name, loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
for failure in ('ps missing','ps timeout','ps nonzero','ps error',
                'lsof missing','lsof timeout','lsof nonzero'):
 persisted=[]
 m.descendants=lambda root:{53001}
 m.matching_listener_pids=lambda record:{53001}
 m.persist_escaped_cleanup_state=lambda record,escaped:persisted.append(dict(escaped))
 if failure.startswith('ps'):
  m.pid_identity=lambda pid: (_ for _ in ()).throw(OSError(failure)) if failure=='ps error' else ''
  m.os.getpgid=lambda pid:53000
 else:
  m.pid_identity=lambda pid:'listener-id'
  m.os.getpgid=lambda pid:1
  m.pid_executable=lambda pid:''
 try: m.capture_listener_proofs({'pid':1,'pgid':1,'host':'127.0.0.1','port':1})
 except m.DetachedCleanupError as exc:
  assert exc.escaped['listener_pid']==53001
 else: raise AssertionError('tool failure lost escaped authority')
 assert persisted and persisted[0]['listener_pid']==53001
 assert persisted[0].get('pgid') is None
""" % str(PREVIEW)
        proc = subprocess.run([sys.executable, "-c", code], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
