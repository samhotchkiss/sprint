"""Response-only adapters for installation-local Grok and Claude CLIs.

Configured worker command examples::

    ["python3", "-m", "sprint_coordinator.model_worker", "--provider", "grok",
     "--model", "<installation-approved-cheap-model>"]
    ["python3", "-m", "sprint_coordinator.model_worker", "--provider", "claude",
     "--model", "<installation-approved-strong-model>"]

The coordinator job is read as one JSON object from stdin.  The adapter starts a
fresh, tool-free, non-persistent CLI turn and emits exactly one strict worker JSON
object on stdout.  It supports response assignments only.  Code assignments need
a separately configured workspace/scope adapter and are rejected here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any


class AdapterError(RuntimeError):
    pass


REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"const": True},
        "kind": {"const": "reply"},
        "text": {"type": "string", "minLength": 1},
        "addresses_obligation": {"type": "boolean"},
        "status_update": {"type": "boolean"},
    },
    "required": ["ok", "kind", "text", "addresses_obligation", "status_update"],
}


def _validate_job(job: Any) -> dict:
    if not isinstance(job, dict):
        raise AdapterError("invalid_job")
    if job.get("kind", "response") != "response":
        raise AdapterError("unsupported_job_kind")
    if not isinstance(job.get("question"), str) or not job["question"].strip():
        raise AdapterError("missing_question")
    for name in ("candidate", "rejection_context"):
        if name in job and not isinstance(job[name], (str, dict, list, type(None))):
            raise AdapterError("invalid_" + name)
    return job


def _prompt(job: dict) -> str:
    context = {
        "assignment_id": job.get("assignment_id"),
        "obligation_id": job.get("obligation_id"),
        "reply_to": job.get("reply_to"),
        "thread_revision": job.get("thread_revision"),
        "question": job["question"],
        "candidate": job.get("candidate"),
        "rejection_context": job.get("rejection_context"),
    }
    return (
        "You are a response-only Sprint worker. Use only the supplied JSON context; "
        "there is no hidden conversation history. Answer the user's obligation directly. "
        "A progress update does not answer a question. Do not claim commands ran, work "
        "completed, or authorization was granted unless the supplied context explicitly "
        "contains trustworthy evidence. Return only JSON matching the supplied schema.\n\n"
        "CONTEXT_JSON\n" + json.dumps(context, sort_keys=True, ensure_ascii=False)
    )


def _command(provider: str, executable: str, model: str, prompt_path: Path) -> tuple[list[str], bytes | None]:
    schema = json.dumps(REPLY_SCHEMA, separators=(",", ":"))
    if provider == "grok":
        return ([
            executable, "--prompt-file", str(prompt_path), "--json-schema", schema,
            "--output-format", "json", "--no-memory", "--no-subagents",
            "--disable-web-search", "--tools", "", "--permission-mode", "dontAsk",
            "--model", model,
        ], None)
    if provider == "claude":
        # Claude has no prompt-file option. Feeding the mode-0600 file through stdin
        # keeps private context out of argv and starts no persistent conversation.
        return ([
            executable, "--print", "--bare", "--no-session-persistence",
            "--tools", "", "--permission-mode", "dontAsk", "--json-schema", schema,
            "--output-format", "json", "--model", model,
        ], prompt_path.read_bytes())
    raise AdapterError("unsupported_provider")


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _provider_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith(("GIT_", "GH_", "GITHUB_", "SSH_")):
            environment.pop(name, None)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _run(command: list[str], stdin: bytes | None, timeout: float, cwd: Path) -> bytes:
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            start_new_session=True, close_fds=True, cwd=cwd,
            env=_provider_environment(),
        )
    except OSError as exc:
        raise AdapterError("spawn_failed") from exc
    try:
        stdout, _stderr = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        raise AdapterError("timeout") from exc
    if proc.returncode != 0:
        raise AdapterError("provider_exit_%d" % proc.returncode)
    return stdout


def _model_payload(stdout: bytes) -> Any:
    try:
        outer = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("invalid_provider_json") from exc
    # Grok may return the schema object directly. Claude JSON mode returns an
    # envelope whose structured_output contains the schema-constrained object.
    if isinstance(outer, dict) and "structured_output" in outer:
        return outer["structured_output"]
    if isinstance(outer, dict) and isinstance(outer.get("result"), str):
        try:
            return json.loads(outer["result"])
        except json.JSONDecodeError as exc:
            raise AdapterError("invalid_structured_output") from exc
    return outer


def _validate_reply(payload: Any) -> dict:
    if not isinstance(payload, dict) or set(payload) != set(REPLY_SCHEMA["required"]):
        raise AdapterError("invalid_worker_shape")
    if payload.get("ok") is not True or payload.get("kind") != "reply":
        raise AdapterError("invalid_worker_shape")
    if not isinstance(payload.get("text"), str) or not payload["text"].strip():
        raise AdapterError("invalid_worker_text")
    if type(payload.get("addresses_obligation")) is not bool or type(
            payload.get("status_update")) is not bool:
        raise AdapterError("invalid_worker_flags")
    return payload


def run_job(*, provider: str, model: str, executable: str | None = None,
            timeout: float = 90.0, input_bytes: bytes | None = None) -> dict:
    if not model:
        raise AdapterError("missing_model")
    if timeout <= 0:
        raise AdapterError("invalid_timeout")
    executable = executable or shutil.which(provider)
    if not executable:
        raise AdapterError("provider_not_installed")
    raw = sys.stdin.buffer.read() if input_bytes is None else input_bytes
    try:
        job = _validate_job(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("invalid_job_json") from exc
    with tempfile.TemporaryDirectory(prefix="sprint-model-worker-") as directory:
        temp = Path(directory)
        os.chmod(temp, 0o700)
        prompt_path = temp / "request.txt"
        fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_prompt(job))
        command, stdin = _command(provider, executable, model, prompt_path)
        return _validate_reply(_model_payload(_run(command, stdin, timeout, temp)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a response-only model worker")
    parser.add_argument("--provider", required=True, choices=("grok", "claude"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--executable", help="installation-local CLI path")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    try:
        result = run_job(provider=args.provider, model=args.model,
                         executable=args.executable, timeout=args.timeout)
    except AdapterError as exc:
        # Stable codes only: provider stderr and request content may be private.
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
