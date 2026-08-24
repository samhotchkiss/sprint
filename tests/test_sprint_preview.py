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

    def tearDown(self):
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
        deadline = time.monotonic() + 5
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
                record_path = project / ".sprint" / "previews" / ("card-%d.json" % card)
                deadline = time.monotonic() + 5
                while not record_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(record_path.exists(), "preview never published its record")
                record = json.loads(record_path.read_text(encoding="utf-8"))
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
        deadline = time.monotonic() + 5
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
        deadline = time.monotonic() + 5
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
        record_path = project / ".sprint" / "previews" / "card-102.json"
        deadline = time.monotonic() + 8
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
        env["TEST_PS_SWAP_AFTER"] = "3"
        stopped = self.run_preview("stop", 105, project, check=False, env=env)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("changed during verification", stopped.stderr)
        self.assertTrue(record_path.exists())
        self.assertEqual(self.read_url(preview["live_url"]), "identity-swap")
        self.run_preview("stop", 105, project)
        self.started.remove((105, project))


if __name__ == "__main__":
    unittest.main()
