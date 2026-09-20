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
    AGENT_ROLES, PROVIDER_LABELS, STATUS_BLOCKED, STATUS_COMPLETED,
    STATUS_FAILED, STATUS_PENDING, STATUS_UNCERTAIN, main, run_job, submit_result,
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
        tmux = kwargs.pop("tmux_send", self.tmux)
        return run_job(
            payload if payload is not None else job(),
            pane=pane,
            assignments_dir=self.assignments,
            transport_root=self.transport,
            tmux_send=tmux,
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

    def test_providers_and_roles_share_tmux_send_protocol(self):
        templates = []
        for provider in PROVIDER_LABELS:
            for agent_role in AGENT_ROLES:
                assignment_id = "asg-%s%s-1" % (provider[:2], agent_role[:3])
                payload = job(assignment_id=assignment_id, provider=provider,
                              agent_role=agent_role)
                result = self.run_adapter(payload=payload, pane="%5")
                self.assertEqual(result["status"], STATUS_COMPLETED, result)
                args = self.recorded_args()[-1]
                prompt = Path(args[args.index("--file") + 1])
                templates.append(args[1:args.index("--file")] + ["--file"])
                self.assertEqual(Path(args[0]).resolve(), Path(self.tmux).resolve())
                self.assertEqual(args[1:6], ["--no-stash", "--wait", "0", "%5", "--file"])
                for banned in ("--prompt-file", "--print", "--model", "--json-schema",
                               "send-keys", "paste-buffer", "--force", "grok", "claude",
                               "codex"):
                    self.assertNotIn(banned, args)
                prompt_text = prompt.read_text()
                self.assertIn(str(prompt.parent / "job.json"), prompt_text)
                self.assertIn(str(prompt.parent / "result.json"), prompt_text)
                for name in PROVIDER_LABELS:
                    self.assertNotIn(name, prompt_text)
                stored = json.loads((prompt.parent / "job.json").read_text())
                self.assertEqual(stored["provider"], provider)
                self.assertEqual(stored["agent_role"], agent_role)
                self.assertNotIn("structured_output", result)
                self.assertEqual(result["candidate"]["text"], "session candidate")
        self.assertEqual(len(templates), 9)
        self.assertEqual(len(set(tuple(item) for item in templates)), 1)
        self.assertEqual(self.send_count(), 9)

    def test_provider_cli_stdout_is_not_completion(self):
        env_dir = self.root / "env-send"
        env_dir.mkdir()
        executable = fake_tmux_send(env_dir, """
import json, os, pathlib, sys
capture = pathlib.Path(os.environ['CAPTURE_ARGS'])
existing = json.loads(capture.read_text()) if capture.exists() else []
existing.append(sys.argv[:])
capture.write_text(json.dumps(existing))
count = pathlib.Path(os.environ['SEND_COUNT'])
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
prompt = pathlib.Path(sys.argv[sys.argv.index('--file') + 1])
(prompt.parent / 'structured_output.json').write_text(json.dumps({
    'structured_output': {'text': 'from-cli-envelope'}, 'result': 'from-cli-envelope',
}))
print(json.dumps({'structured_output': {'text': 'from-cli-envelope'}}))
sys.exit(0)
""")
        os.environ["WRITE_RESULT"] = "0"
        result = self.run_adapter(
            payload=job(assignment_id="asg-env-low-1", provider="claude"),
            tmux_send=executable, wait_seconds=0.05)
        self.assertEqual(result["status"], STATUS_PENDING)
        self.assertEqual(result["reason"], "wait_timeout")
        job_dir = Path(result["job_dir"])
        self.assertTrue((job_dir / "structured_output.json").exists())
        self.assertFalse((job_dir / "result.json").exists())
        self.assertNotEqual(result.get("text"), "from-cli-envelope")

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
            ({"model": "claude-opus"}, {}, "job_transport_override"),
            ({"prompt_file": "/tmp/p"}, {}, "job_transport_override"),
            ({"kind": "codex"}, {}, "unsupported_job_kind"),
            ({"kind": "grok"}, {}, "unsupported_job_kind"),
            ({"provider": "task"}, {}, "provider_role_collision"),
            ({"agent_role": "codex"}, {}, "role_provider_collision"),
            ({"role": "claude"}, {}, "role_provider_collision"),
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

    def seed_assignment(self, state, assignment_id="asg-seed-low-1", pane="%5",
                        with_result=False, text="later candidate"):
        self.assignments.mkdir(parents=True, exist_ok=True)
        os.chmod(self.assignments, 0o700)
        self.transport.mkdir(parents=True, exist_ok=True)
        os.chmod(self.transport, 0o700)
        job_dir = self.assignments / assignment_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(job_dir, 0o700)
        token = "ab" * 16
        stored = job(assignment_id=assignment_id)
        stored.update(token=token, result_token=token, job_dir=str(job_dir),
                      result_path=str(job_dir / "result.json"), pane=pane)
        (job_dir / "job.json").write_text(json.dumps(stored, indent=2, sort_keys=True))
        os.chmod(job_dir / "job.json", 0o600)
        (job_dir / "prompt.txt").write_text("job path\nresult path\n")
        os.chmod(job_dir / "prompt.txt", 0o600)
        manifest = {
            "assignment_id": assignment_id, "pane": pane, "state": state,
            "token": token, "kind": "response",
            "job_path": str(job_dir / "job.json"),
            "prompt_path": str(job_dir / "prompt.txt"),
            "result_path": str(job_dir / "result.json"),
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        marker = self.transport / ("pane-%s.active.json" % pane[1:])
        marker.write_text(json.dumps({
            "assignment_id": assignment_id, "job_dir": str(job_dir),
            "pane": pane, "state": state, "pid": 0,
        }))
        if with_result:
            submit_result(job_dir, text=text)
        return job_dir, token

    def test_result_resolves_uncertain_delivering_and_blocked(self):
        for state in ("uncertain", "delivering", "blocked"):
            with self.subTest(state=state):
                assignment_id = "asg-%s-low-1" % state
                job_dir, _token = self.seed_assignment(
                    state, assignment_id=assignment_id, with_result=True,
                    text="resolved-%s" % state)
                before = self.send_count()
                result = self.run_adapter(payload=job(assignment_id=assignment_id))
                self.assertEqual(result["status"], STATUS_COMPLETED)
                self.assertEqual(result["text"], "resolved-%s" % state)
                self.assertEqual(self.send_count(), before)
                self.assertEqual(
                    json.loads((job_dir / "manifest.json").read_text())["state"],
                    "completed")

    def test_prepared_restart_uses_configured_tmux_send(self):
        assignment_id = "asg-prep-low-1"
        self.seed_assignment("prepared", assignment_id=assignment_id)
        result = self.run_adapter(payload=job(assignment_id=assignment_id))
        self.assertEqual(result["status"], STATUS_COMPLETED)
        args = self.recorded_args()[0]
        self.assertEqual(Path(args[0]).resolve(), Path(self.tmux).resolve())
        self.assertNotEqual(Path(args[0]).resolve(), Path(sys.executable).resolve())
        self.assertEqual(args[1:6], ["--no-stash", "--wait", "0", "%5", "--file"])
        self.assertEqual(Path(args[6]).resolve(),
                         (self.assignments / assignment_id / "prompt.txt").resolve())

    def test_reconcile_never_sends_including_prepared(self):
        prepared_id = "asg-recon-prep-1"
        self.seed_assignment("prepared", assignment_id=prepared_id)
        prepared = self.run_adapter(
            payload=job(assignment_id=prepared_id), reconcile=True)
        self.assertEqual(prepared["status"], STATUS_PENDING)
        self.assertEqual(prepared["reason"], "not_sent")
        self.assertEqual(self.send_count(), 0)

        missing = self.run_adapter(
            payload=job(assignment_id="asg-recon-missing-1"), reconcile=True)
        self.assertEqual(missing["status"], STATUS_PENDING)
        self.assertEqual(missing["reason"], "not_prepared")

        delivering_id = "asg-recon-deliv-1"
        self.seed_assignment("delivering", assignment_id=delivering_id)
        inflight = self.run_adapter(
            payload=job(assignment_id=delivering_id), reconcile=True)
        self.assertEqual(inflight["status"], STATUS_PENDING)
        self.assertEqual(inflight["reason"], "in_flight")
        self.assertEqual(
            json.loads((self.assignments / delivering_id / "manifest.json").read_text())["state"],
            "delivering")

        uncertain_id = "asg-recon-unc-1"
        self.seed_assignment("uncertain", assignment_id=uncertain_id, with_result=True,
                             text="from session after unverified send")
        done = self.run_adapter(payload=job(assignment_id=uncertain_id), reconcile=True)
        self.assertEqual(done["status"], STATUS_COMPLETED)
        self.assertEqual(done["text"], "from session after unverified send")
        self.assertEqual(self.send_count(), 0)

        absent_bin = self.run_adapter(
            payload=job(assignment_id=prepared_id), reconcile=True,
            tmux_send=self.root / "missing-tmux-send")
        self.assertEqual(absent_bin["status"], STATUS_PENDING)
        self.assertEqual(self.send_count(), 0)

    def test_cli_typed_outcomes_exit_zero(self):
        for status, payload, extra in (
            ("pending", job(assignment_id="asg-cli-pend-1"),
             ["--pane", "%5", "--wait-seconds", "0.02"]),
            ("uncertain", job(assignment_id="asg-cli-unc-1"), ["--pane", "%6"]),
            ("blocked", job(assignment_id="asg-cli-blk-1"), ["--pane", "%7"]),
        ):
            if status == "pending":
                os.environ["WRITE_RESULT"] = "0"
                os.environ.pop("TMUX_SEND_EXIT", None)
            elif status == "uncertain":
                os.environ["WRITE_RESULT"] = "1"
                os.environ["TMUX_SEND_EXIT"] = "4"
            else:
                os.environ["WRITE_RESULT"] = "1"
                os.environ["TMUX_SEND_EXIT"] = "5"
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(
                    extra + ["--assignments-dir", str(self.assignments),
                             "--transport-root", str(self.transport),
                             "--tmux-send", self.tmux, "--poll-interval", "0.01"],
                    input_bytes=json.dumps(payload).encode(),
                )
            self.assertEqual(code, 0, buf.getvalue())
            emitted = json.loads(buf.getvalue())
            self.assertEqual(emitted["status"], status)
            self.assertFalse(emitted["ok"])

        self.seed_assignment("prepared", assignment_id="asg-cli-recon-1")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(
                ["reconcile", "--pane", "%5", "--assignments-dir", str(self.assignments),
                 "--transport-root", str(self.transport), "--tmux-send", self.tmux],
                input_bytes=json.dumps(job(assignment_id="asg-cli-recon-1")).encode(),
            )
        self.assertEqual(code, 0)
        emitted = json.loads(buf.getvalue())
        self.assertEqual(emitted["status"], STATUS_PENDING)
        self.assertEqual(emitted["reason"], "not_sent")


if __name__ == "__main__":
    unittest.main()
