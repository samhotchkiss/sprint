from __future__ import annotations

import subprocess


class EvidenceError(ValueError):
    pass


def run_command_array(command: list, timeout: float = 60.0, cwd=None) -> dict:
    if not isinstance(command, list) or not command or not all(
            isinstance(p, str) for p in command):
        raise EvidenceError("command must be a non-empty string array")
    try:
        proc = subprocess.run(
            command, cwd=cwd, timeout=timeout, capture_output=True, text=True,
            shell=False, check=False)
    except subprocess.TimeoutExpired as exc:
        return {
            "command": list(command),
            "exit_code": -1,
            "adapter": "trusted",
            "error": "timeout",
            "stdout_bytes": len(getattr(exc, "stdout", None) or b""),
            "stderr_bytes": len(getattr(exc, "stderr", None) or b""),
        }
    return {
        "command": list(command),
        "exit_code": int(proc.returncode),
        "adapter": "trusted",
        "stdout_bytes": len(proc.stdout or ""),
        "stderr_bytes": len(proc.stderr or ""),
    }


def _invoke_runner(runner, command, timeout, cwd):
    try:
        return runner(command, timeout=timeout, cwd=cwd)
    except TypeError:
        return runner(command, timeout=timeout)


def collect_checks(check_ids, catalog: dict, runner) -> list:
    if not check_ids:
        raise EvidenceError("code-result success requires executable-check ids")
    evidence = []
    for cid in check_ids:
        spec = catalog.get(cid)
        if spec is None:
            raise EvidenceError("check %s is not in the local catalog" % cid)
        command = list(spec["command"])
        timeout = float(spec.get("timeout_seconds") or 60.0)
        cwd = spec.get("cwd")
        result = _invoke_runner(runner, command, timeout, cwd)
        result = dict(result)
        result["id"] = cid
        result["command"] = command
        result["adapter"] = "trusted"
        result["cwd"] = cwd
        result["timeout_seconds"] = timeout
        evidence.append(result)
    return evidence


def required_check_ids(catalog: dict) -> list:
    if not catalog:
        raise EvidenceError("code task has no required catalog checks")
    return list(catalog.keys())


def validate_code_result(result: dict, catalog: dict, runner=None) -> list:
    """Coordinator chooses and runs every catalog check. Worker ids are ignored."""
    if not isinstance(result, dict):
        raise EvidenceError("worker result must be a JSON object")
    if result.get("model_command") or result.get("commands"):
        raise EvidenceError("arbitrary model-generated commands are rejected")
    if not catalog:
        raise EvidenceError("code task has no required catalog checks")
    runner = runner or (
        lambda command, timeout=60.0, cwd=None: run_command_array(
            command, timeout=timeout, cwd=cwd))
    evidence = collect_checks(required_check_ids(catalog), catalog, runner)
    failed = [c["id"] for c in evidence if int(c.get("exit_code", 1)) != 0]
    if failed:
        raise EvidenceError("checks failed: %s" % ",".join(failed))
    return evidence
