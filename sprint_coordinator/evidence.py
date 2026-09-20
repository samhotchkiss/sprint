from __future__ import annotations

import subprocess


class EvidenceError(ValueError):
    pass


def run_command_array(command: list, timeout: float = 60.0, cwd=None) -> dict:
    if not isinstance(command, list) or not command or not all(
            isinstance(p, str) for p in command):
        raise EvidenceError("command must be a non-empty string array")
    proc = subprocess.run(
        command, cwd=cwd, timeout=timeout, capture_output=True, text=True,
        shell=False, check=False)
    return {
        "command": list(command),
        "exit_code": int(proc.returncode),
        "adapter": "trusted",
        "stdout_bytes": len(proc.stdout or ""),
        "stderr_bytes": len(proc.stderr or ""),
    }


def collect_checks(check_ids, catalog: dict, runner, timeout: float = 60.0) -> list:
    if not check_ids:
        raise EvidenceError("code-result success requires executable-check ids")
    evidence = []
    for cid in check_ids:
        spec = catalog.get(cid)
        if spec is None:
            raise EvidenceError("check %s is not in the local catalog" % cid)
        command = list(spec["command"])
        result = runner(command, timeout=timeout)
        result = dict(result)
        result["id"] = cid
        result["command"] = command
        result["adapter"] = "trusted"
        evidence.append(result)
    return evidence


def validate_code_result(result: dict, catalog: dict, runner=None, timeout: float = 60.0) -> list:
    """Success is only the coordinator-run catalog checks, never model claims."""
    if not isinstance(result, dict):
        raise EvidenceError("worker result must be a JSON object")
    if result.get("model_command") or result.get("commands"):
        raise EvidenceError("arbitrary model-generated commands are rejected")
    check_ids = result.get("check_ids") or [
        c.get("id") for c in (result.get("checks") or []) if isinstance(c, dict) and c.get("id")
    ]
    runner = runner or (lambda command, timeout=timeout: run_command_array(command, timeout))
    evidence = collect_checks(check_ids, catalog, runner, timeout=timeout)
    failed = [c["id"] for c in evidence if int(c.get("exit_code", 1)) != 0]
    if failed:
        raise EvidenceError("checks failed: %s" % ",".join(failed))
    return evidence
