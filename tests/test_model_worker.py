import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from sprint_coordinator.model_worker import AdapterError, run_job


GOOD = {"ok": True, "kind": "reply", "text": "Direct answer.",
        "addresses_obligation": True, "status_update": False}


def fake_cli(directory: Path, body: str) -> str:
    path = directory / "fake-model"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def job(**updates):
    value = {"kind": "response", "question": "What changed?", "candidate": "old answer",
             "rejection_context": {"reason": "did not answer"}, "reply_to": "card:1"}
    value.update(updates)
    return json.dumps(value).encode()


class ModelWorkerTests(unittest.TestCase):
    def test_grok_uses_private_prompt_file_and_expected_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.json"
            executable = fake_cli(Path(directory), """
import json, os, pathlib, sys
args = sys.argv[1:]
prompt = pathlib.Path(args[args.index('--prompt-file') + 1])
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({
    'args': args, 'mode': oct(prompt.stat().st_mode & 0o777), 'text': prompt.read_text(),
    'cwd': os.getcwd(), 'has_repo_token': 'GITHUB_TOKEN' in os.environ
}))
print(json.dumps(%r))
""" % GOOD)
            with mock.patch.dict(os.environ, {
                    "CAPTURE": str(capture), "GITHUB_TOKEN": "must-not-reach-provider"}):
                result = run_job(provider="grok", model="cheap", executable=executable,
                                 timeout=2, input_bytes=job())
            observed = json.loads(capture.read_text())
            self.assertEqual(result, GOOD)
            self.assertEqual(observed["mode"], "0o600")
            self.assertIn("--no-memory", observed["args"])
            self.assertIn("--disable-web-search", observed["args"])
            self.assertIn("old answer", observed["text"])
            self.assertIn("did not answer", observed["text"])
            self.assertFalse(observed["has_repo_token"])
            self.assertIn("sprint-model-worker-", observed["cwd"])
            self.assertFalse(Path(observed["args"][observed["args"].index("--prompt-file") + 1]).exists())

    def test_claude_receives_prompt_on_stdin_and_accepts_structured_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "prompt"
            executable = fake_cli(Path(directory), """
import json, os, pathlib, sys
pathlib.Path(os.environ['CAPTURE']).write_text(sys.stdin.read())
print(json.dumps({'structured_output': %r}))
""" % GOOD)
            with mock.patch.dict(os.environ, {"CAPTURE": str(capture)}):
                result = run_job(provider="claude", model="strong", executable=executable,
                                 timeout=2, input_bytes=job())
            self.assertEqual(result, GOOD)
            self.assertIn("What changed?", capture.read_text())

    def test_timeout_is_bounded_and_process_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = fake_cli(Path(directory), "import time\ntime.sleep(30)\n")
            started = time.monotonic()
            with self.assertRaisesRegex(AdapterError, "timeout"):
                run_job(provider="grok", model="cheap", executable=executable,
                        timeout=.1, input_bytes=job())
            self.assertLess(time.monotonic() - started, 3)

    def test_rejects_code_jobs_before_starting_provider(self):
        with self.assertRaisesRegex(AdapterError, "unsupported_job_kind"):
            run_job(provider="grok", model="cheap", executable="/not/executed",
                    input_bytes=job(kind="code"))

    def test_strict_output_rejects_malformed_extra_and_false_success(self):
        outputs = ("not json", json.dumps({**GOOD, "extra": 1}),
                   json.dumps({**GOOD, "ok": False}), json.dumps({**GOOD, "text": ""}))
        with tempfile.TemporaryDirectory() as directory:
            for index, output in enumerate(outputs):
                executable = fake_cli(Path(directory), "print(%r)\n" % output)
                with self.subTest(index=index), self.assertRaises(AdapterError):
                    run_job(provider="grok", model="cheap", executable=executable,
                            timeout=2, input_bytes=job())

    def test_diagnostics_do_not_include_provider_stderr_or_request(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = fake_cli(Path(directory),
                                  "import sys\nsys.stderr.write('private-secret')\nsys.exit(7)\n")
            with self.assertRaisesRegex(AdapterError, "provider_exit_7") as raised:
                run_job(provider="grok", model="cheap", executable=executable,
                        timeout=2, input_bytes=job(question="private-question"))
            self.assertNotIn("private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
