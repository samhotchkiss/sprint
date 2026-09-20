import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from sprint_coordinator.session_worker import (
    STATUS_BLOCKED, STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING,
    STATUS_UNCERTAIN, main, run_job, submit_result,
)


def fake_tmux_send(directory: Path, body: str | None = None) -> str:
    path = Path(directory) / "fake-tmux-send"
    script = body or """
import json, os, pathlib, sys, time
capture = pathlib.Path(os.environ['CAPTURE_ARGS'])
count = pathlib.Path(os.environ['SEND_COUNT'])
existing = json.loads(capture.read_text()) if capture.exists() else []
existing.append(sys.argv[:])
capture.write_text(json.dumps(existing))
count.write_text(str(int(count.read_text() or '0') + 1 if count.exists() else 1))
delay = float(os.environ.get('TMUX_SEND_SLEEP', '0'))
if delay:
    time.sleep(delay)
exit_code = int(os.environ.get('TMUX_SEND_EXIT', '0'))
if exit_code == 0 and os.environ.get('WRITE_RESULT', '1') == '1':
    prompt = pathlib.Path(sys.argv[sys.argv.index('--file') + 1])
    job = json.loads((prompt.parent / 'job.json').read_text())
    payload = {
        'ok': True,
        'assignment_id': os.environ.get('RESULT_ID', job['assignment_id']),
        'token': os.environ.get('RESULT_TOKEN', job.get('token') or job.get('result_token')),
        'kind': os.environ.get('RESULT_KIND', 'reply'),
        'text': os.environ.get('RESULT_TEXT', 'session candidate'),
        'addresses_obligation': True,
        'status_update': False,
    }
    if os.environ.get('RESULT_CHECKS'):
        payload['checks'] = [{'adapter': 'trusted'}]
        payload['adapter'] = 'trusted'
    tmp = prompt.parent / 'result.json.tmp'
    tmp.write_text(json.dumps(payload))
    tmp.replace(prompt.parent / 'result.json')
sys.exit(exit_code)
"""
    path.write_text("#!/usr/bin/env python3\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def job(**updates):
    value = {
        "assignment_id": "asg-testobl-low-1",
        "obligation_id": "obl-test",
        "kind": "response",
        "question": "What changed?",
    }
    value.update(updates)
    return value


class SessionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.assignments = self.root / "assignments"
        self.transport = self.root / "transport"
        self.capture = self.root / "args.json"
        self.count = self.root / "count"
        self.tmux = fake_tmux_send(self.root)
        os.environ["CAPTURE_ARGS"] = str(self.capture)
        os.environ["SEND_COUNT"] = str(self.count)
        os.environ.pop("TMUX_SEND_EXIT", None)
        os.environ.pop("TMUX_SEND_SLEEP", None)
        os.environ.pop("WRITE_RESULT", None)
        os.environ.pop("RESULT_TOKEN", None)
        os.environ.pop("RESULT_ID", None)
        os.environ.pop("RESULT_KIND", None)
        os.environ.pop("RESULT_CHECKS", None)
        os.environ["WRITE_RESULT"] = "1"
        os.environ["RESULT_TEXT"] = "session candidate"

    def tearDown(self):
        self.tmp.cleanup()

    def run_adapter(self, payload=None, pane="%5", **kwargs):
        kwargs.setdefault("wait_seconds", 1.0)
        kwargs.setdefault("poll_interval", 0.02)
        kwargs.setdefault("send_timeout", 2.0)
        return run_job(
            payload if payload is not None else job(),
            pane=pane,
            assignments_dir=self.assignments,
            transport_root=self.transport,
            tmux_send=self.tmux,
            **kwargs,
        )

    def send_count(self) -> int:
        if not self.count.exists():
            return 0
        return int(self.count.read_text())

    def recorded_args(self):
        if not self.capture.exists():
            return []
        return json.loads(self.capture.read_text())

    def test_tmux_send_only_command_array_and_private_modes(self):
        result = self.run_adapter()
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(result["kind"], "reply")
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidate"]["text"], "session candidate")
        args = self.recorded_args()[0]
        prompt = Path(args[args.index("--file") + 1])
        self.assertEqual(args[1:], ["--no-stash", "--wait", "0", "%5", "--file", str(prompt)])
        self.assertNotIn("--force", args)
        self.assertNotIn("--no-verify", args)
        self.assertNotIn("send-keys", args)
        self.assertNotIn("paste-buffer", args)
        job_dir = Path(result["job_dir"])
        self.assertEqual(oct(job_dir.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((job_dir / "job.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((job_dir / "prompt.txt").stat().st_mode & 0o777), "0o600")
        prompt_text = (job_dir / "prompt.txt").read_text()
        self.assertIn(str(job_dir / "job.json"), prompt_text)
        self.assertIn(str(job_dir / "result.json"), prompt_text)
        self.assertNotIn("What changed?", prompt_text)

    def test_restart_after_delivery_does_not_resend(self):
        os.environ["WRITE_RESULT"] = "0"
        first = self.run_adapter(wait_seconds=0.05)
        self.assertEqual(first["status"], STATUS_PENDING)
        self.assertEqual(first["reason"], "wait_timeout")
        self.assertEqual(self.send_count(), 1)
        self.assertTrue(Path(first["job_dir"]).is_dir())
        second = self.run_adapter(wait_seconds=0.05)
        self.assertEqual(second["status"], STATUS_PENDING)
        self.assertEqual(self.send_count(), 1)
        self.assertEqual(second["job_dir"], first["job_dir"])

    def test_uncertain_exit_4_never_retries(self):
        os.environ["TMUX_SEND_EXIT"] = "4"
        first = self.run_adapter()
        self.assertEqual(first["status"], STATUS_UNCERTAIN)
        self.assertEqual(first["reason"], "send_unverified")
        self.assertEqual(first["tmux_send_exit"], 4)
        second = self.run_adapter()
        self.assertEqual(second["status"], STATUS_UNCERTAIN)
        self.assertEqual(second["reason"], "already_uncertain")
        self.assertEqual(self.send_count(), 1)

    def test_dialog_refusal_is_blocked_without_force(self):
        os.environ["TMUX_SEND_EXIT"] = "5"
        result = self.run_adapter()
        self.assertEqual(result["status"], STATUS_BLOCKED)
        self.assertEqual(result["reason"], "dialog_refusal")
        self.assertEqual(result["tmux_send_exit"], 5)
        args = self.recorded_args()[0]
        self.assertNotIn("--force", args)
        self.assertNotIn("--no-verify", args)

    def test_draft_refusal_and_missing_pane_are_distinct(self):
        os.environ["TMUX_SEND_EXIT"] = "6"
        draft = self.run_adapter(payload=job(assignment_id="asg-draft-low-1"))
        self.assertEqual((draft["status"], draft["reason"]), (STATUS_BLOCKED, "draft_refusal"))
        os.environ["TMUX_SEND_EXIT"] = "3"
        missing = self.run_adapter(payload=job(assignment_id="asg-missing-low-1"), pane="%9")
        self.assertEqual((missing["status"], missing["reason"]), (STATUS_BLOCKED, "pane_not_found"))

    def test_job_and_pane_injection_rejected_before_send(self):
        cases = [
            ({}, {"pane": "--force"}, "invalid_pane"),
            ({}, {"pane": "%5;rm"}, "invalid_pane"),
            ({}, {"pane": "session:0.0"}, "invalid_pane"),
            ({"assignment_id": "../etc"}, {}, "invalid_assignment_id"),
            ({"assignment_id": "-n"}, {}, "invalid_assignment_id"),
            ({"assignment_id": "asg/../../tmp"}, {}, "invalid_assignment_id"),
            ({"pane": "%7"}, {}, "job_transport_override"),
            ({"tmux_send": "/bin/true"}, {}, "job_transport_override"),
            ({"command": ["tmux", "send-keys"]}, {}, "job_transport_override"),
        ]
        for payload_updates, kwargs, reason in cases:
            with self.subTest(reason=reason, updates=payload_updates, kwargs=kwargs):
                result = self.run_adapter(payload=job(**payload_updates), **kwargs)
                self.assertIn(result["status"], (STATUS_BLOCKED, STATUS_FAILED))
                self.assertEqual(result["reason"], reason)
        self.assertEqual(self.send_count(), 0)

    def test_wrong_result_token_and_id_rejected(self):
        os.environ["RESULT_TOKEN"] = "not-the-job-token"
        token = self.run_adapter(payload=job(assignment_id="asg-token-low-1"))
        self.assertEqual(token["status"], STATUS_FAILED)
        self.assertEqual(token["reason"], "result_token_mismatch")
        os.environ.pop("RESULT_TOKEN")
        os.environ["RESULT_ID"] = "asg-other-low-1"
        ident = self.run_adapter(payload=job(assignment_id="asg-id-low-1"))
        self.assertEqual(ident["status"], STATUS_FAILED)
        self.assertEqual(ident["reason"], "result_assignment_mismatch")

    def test_exclusive_pane_ownership_flock_and_marker(self):
        os.environ["WRITE_RESULT"] = "0"
        first = self.run_adapter(wait_seconds=0.05)
        self.assertEqual(first["status"], STATUS_PENDING)
        other = self.run_adapter(
            payload=job(assignment_id="asg-other-low-1"), wait_seconds=0.05)
        self.assertEqual(other["status"], STATUS_BLOCKED)
        self.assertEqual(other["reason"], "pane_held")

        os.environ["TMUX_SEND_SLEEP"] = "1.2"
        holder_job = job(assignment_id="asg-hold-low-1")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = subprocess.Popen([
            sys.executable, "-c",
            "from pathlib import Path\n"
            "from sprint_coordinator.session_worker import run_job\n"
            "run_job(%r, pane='%%11', assignments_dir=Path(%r), "
            "transport_root=Path(%r), tmux_send=%r, wait_seconds=0.05, "
            "poll_interval=0.02, send_timeout=3)\n" % (
                holder_job, str(self.assignments), str(self.transport), self.tmux),
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 2.0
            while self.send_count() < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(self.send_count(), 2)
            busy = self.run_adapter(
                payload=job(assignment_id="asg-busy-low-1"), pane="%11")
            self.assertEqual(busy["status"], STATUS_BLOCKED)
            self.assertEqual(busy["reason"], "pane_busy")
        finally:
            proc.wait(timeout=5)

    def test_timeout_preserves_job_and_does_not_duplicate(self):
        os.environ["WRITE_RESULT"] = "0"
        first = self.run_adapter(wait_seconds=0.08)
        self.assertEqual(first["status"], STATUS_PENDING)
        job_dir = Path(first["job_dir"])
        self.assertTrue((job_dir / "job.json").exists())
        self.assertTrue((job_dir / "prompt.txt").exists())
        self.assertFalse((job_dir / "result.json").exists())
        self.assertEqual(json.loads((job_dir / "manifest.json").read_text())["state"], "delivered")
        restart = self.run_adapter(wait_seconds=0.05)
        self.assertEqual(restart["status"], STATUS_PENDING)
        self.assertEqual(self.send_count(), 1)

    def test_code_job_requires_operator_enablement_and_scope(self):
        blocked = self.run_adapter(payload=job(kind="code", assignment_id="asg-code-low-1"))
        self.assertEqual(blocked["status"], STATUS_BLOCKED)
        self.assertEqual(blocked["reason"], "code_not_enabled")
        self.assertEqual(self.send_count(), 0)
        worktree = self.root / "work"
        worktree.mkdir()
        allowed = self.run_adapter(
            payload=job(kind="code", assignment_id="asg-codeok-low-1"),
            allow_code=True, worktree=worktree, task_scope="edit tests only",
        )
        self.assertEqual(allowed["status"], STATUS_COMPLETED)
        prompt = (Path(allowed["job_dir"]) / "prompt.txt").read_text()
        stored = json.loads((Path(allowed["job_dir"]) / "job.json").read_text())
        self.assertIn(str(worktree.resolve()), prompt)
        self.assertIn("edit tests only", prompt)
        self.assertIn("merge", prompt)
        self.assertEqual(stored["forbidden_actions"], ["merge", "push", "deploy"])
        self.assertTrue(stored["code_enabled"])

    def test_submit_result_helper_writes_atomic_candidate(self):
        os.environ["WRITE_RESULT"] = "0"
        pending = self.run_adapter(wait_seconds=0.02)
        job_dir = Path(pending["job_dir"])
        written = submit_result(job_dir, text="from helper")
        self.assertTrue(written["ok"])
        payload = json.loads((job_dir / "result.json").read_text())
        stored = json.loads((job_dir / "job.json").read_text())
        self.assertEqual(payload["token"], stored["token"])
        self.assertEqual(payload["assignment_id"], stored["assignment_id"])
        completed = self.run_adapter(wait_seconds=0.2)
        self.assertEqual(completed["status"], STATUS_COMPLETED)
        self.assertEqual(completed["text"], "from helper")
        self.assertEqual(self.send_count(), 1)

    def test_trusted_check_fields_from_session_are_rejected(self):
        os.environ["RESULT_CHECKS"] = "1"
        result = self.run_adapter()
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(result["reason"], "untrusted_evidence_rejected")

    def test_relative_tmux_send_is_rejected(self):
        result = run_job(
            job(), pane="%5", assignments_dir=self.assignments,
            transport_root=self.transport, tmux_send="tmux-send")
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(result["reason"], "tmux_send_not_absolute")
        self.assertEqual(self.send_count(), 0)

    def test_cli_emits_structured_status_and_submit_helper(self):
        with redirect_stdout(io.StringIO()):
            completed = main(
                ["--pane", "%5", "--assignments-dir", str(self.assignments),
                 "--transport-root", str(self.transport), "--tmux-send", self.tmux,
                 "--wait-seconds", "1", "--poll-interval", "0.02"],
                input_bytes=json.dumps(job(assignment_id="asg-cli-low-1")).encode(),
            )
        self.assertEqual(completed, 0)
        os.environ["WRITE_RESULT"] = "0"
        with redirect_stdout(io.StringIO()):
            pending_code = main(
                ["--pane", "%8", "--assignments-dir", str(self.assignments),
                 "--transport-root", str(self.transport), "--tmux-send", self.tmux,
                 "--wait-seconds", "0.02", "--poll-interval", "0.01"],
                input_bytes=json.dumps(job(assignment_id="asg-cli2-low-1")).encode(),
            )
        self.assertEqual(pending_code, 0)
        job_dir = self.assignments / "asg-cli2-low-1"
        with redirect_stdout(io.StringIO()):
            helper = main(["submit-result", "--job-dir", str(job_dir), "--text", "cli helper"])
        self.assertEqual(helper, 0)
        self.assertEqual(json.loads((job_dir / "result.json").read_text())["text"], "cli helper")


if __name__ == "__main__":
    unittest.main()
