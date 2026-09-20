"""Jev adapter contract. The HTTP client lives in jev.py (owned by another agent).

Expected `sprint_coordinator.jev` surface (any one is enough):

- `client_from_config(config) -> client`
- `JevClient.from_config(config)` / `from_key_file(path)` / `from_env()`
- `JevClient(api_key=..., model=..., timeout=...)`

A client must provide:

- `configured() -> bool`  (False when no installation key is present)
- `judge(request: dict) -> dict`

`request` keys: questions, state, model, prompt_version.
`result` keys: answers (dict), raw, usage ({calls, spend_usd, tokens}), model.

Never read another user's key. Never log the secret. Missing credentials must
not fall back to a shared or example key, and must not approve work.
"""

from __future__ import annotations

from pathlib import Path

from sprint_coordinator.util import canonical_json, sha256_text


class MissingCredentials(RuntimeError):
    pass


class JevUnavailable(RuntimeError):
    pass


ROUTE_INTENTS = {
    "answer_question": "The user is asking a question that needs a reply.",
    "dispatch_work": "The user wants coding or other work dispatched.",
    "change_scope": "The user is changing the scope of existing work.",
    "status_only": "The user is only acknowledging status, not asking a question.",
    "approval": "The user is approving something already proposed.",
}


class UnconfiguredJev:
    def __init__(self, reason: str = "jev_unconfigured"):
        self.reason = reason
        self.calls = []

    def configured(self) -> bool:
        return False

    def route(self, obl: dict) -> dict:
        self.calls.append(obl)
        return {"intents": ["unclear"], "escalate": True, "reason": self.reason}

    def verify_code(self, **kwargs):
        raise MissingCredentials(self.reason)

    def judge(self, request: dict) -> dict:
        self.calls.append(request)
        raise MissingCredentials(self.reason)


class JevPolicy:
    """Adapts sprint_coordinator.jev without depending on a fixed factory name."""

    def __init__(self, client, jev_mod):
        self.client = client
        self.jev_mod = jev_mod
        self.calls = []

    def configured(self) -> bool:
        return True

    def route(self, obl: dict) -> dict:
        outcome = self.jev_mod.route_request(
            self.client,
            message=obl.get("question_text") or "",
            context={"reply_to": obl.get("reply_to")},
            intents=ROUTE_INTENTS,
        )
        self.calls.append(outcome)
        usage = {"calls": 1, "spend_usd": 0.0, "tokens": getattr(outcome, "usage_tokens", 0)}
        if getattr(outcome, "action", "") == "escalate":
            return {
                "intents": ["unclear"],
                "escalate": True,
                "reason": getattr(outcome, "reason", "") or "jev_escalate",
                "usage": usage,
            }
        selected = list(getattr(outcome, "intents", ()) or ())
        return {
            "intents": selected or ["unclear"],
            "escalate": len(selected) != 1,
            "reason": "multi_intent" if len(selected) > 1 else "",
            "usage": usage,
        }

    def verify_code(self, task: str, candidate: str, evidence: dict, checks: list):
        independent = []
        for check in checks:
            independent.append({
                "passed": int(check.get("exit_code", 1)) == 0,
                "command": " ".join(check.get("command") or []),
                "source": "independent",
            })
        return self.jev_mod.verify_candidate(
            self.client,
            task=task,
            constraints=[],
            candidate=candidate,
            evidence=evidence,
            code_change=True,
            independent_checks=independent,
        )


def read_typesafe_key(key_file: Path) -> str | None:
    """Read TYPESAFE_API_KEY from a local env file. Never return a default key."""
    path = Path(key_file).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "TYPESAFE_API_KEY":
            secret = value.strip().strip("'").strip('"')
            return secret or None
    return None


def cache_key(prompt_version: str, model: str, request: dict) -> str:
    payload = {
        "prompt_version": prompt_version,
        "model": model,
        "questions": request.get("questions"),
        "state": request.get("state"),
    }
    return sha256_text(canonical_json(payload))


def load_jev_client(config, override=None):
    """Load jev.py if present; never invent credentials."""
    if override is not None:
        return override
    if not getattr(config, "jev_enabled", True):
        return UnconfiguredJev("jev_disabled")
    try:
        from sprint_coordinator import jev as jev_mod
    except ImportError:
        return UnconfiguredJev("jev_module_missing")
    err_cls = getattr(jev_mod, "JevConfigurationError", MissingCredentials)
    key = None
    load_key = getattr(jev_mod, "load_api_key", None)
    try:
        if load_key is not None:
            key = load_key(secret_file=getattr(config, "jev_key_file", None))
        else:
            key = read_typesafe_key(config.jev_key_file)
            if not key:
                raise err_cls("not configured")
    except Exception as exc:
        if isinstance(exc, err_cls) or isinstance(exc, MissingCredentials):
            return UnconfiguredJev("missing_typesafe_api_key")
        return UnconfiguredJev("jev_key_unreadable")
    client = _instantiate(jev_mod, config, key)
    if client is None:
        return UnconfiguredJev("jev_client_unconstructable")
    if isinstance(client, UnconfiguredJev):
        return client
    return JevPolicy(client, jev_mod)


def os_has_key() -> bool:
    import os
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def _instantiate(jev_mod, config, key):
    if hasattr(jev_mod, "client_from_config"):
        try:
            return jev_mod.client_from_config(config)
        except TypeError:
            pass
    cls = getattr(jev_mod, "JevClient", None)
    if cls is None:
        return None
    for builder in ("from_config", "from_key_file", "from_env"):
        fn = getattr(cls, builder, None)
        if not callable(fn):
            continue
        try:
            if builder == "from_config":
                return fn(config)
            if builder == "from_key_file":
                return fn(config.jev_key_file)
            return fn()
        except TypeError:
            continue
        except Exception:
            return UnconfiguredJev("jev_construct_failed")
    try:
        return cls(key, timeout=getattr(config, "jev_timeout_seconds", 8.0))
    except TypeError:
        try:
            return cls(api_key=key)
        except TypeError:
            try:
                return cls(key)
            except TypeError:
                return None


def routing_request(text: str, reply_to: str, candidates: list | None = None) -> dict:
    return {
        "questions": [
            {
                "id": "intents",
                "primitive": "choice",
                "instructions": (
                    "Select every distinct user intent present in the message. "
                    "Independent questions must not be collapsed into one choice. "
                    "Use unclear when the request is ambiguous."
                ),
                "criteria": [
                    "answer_question",
                    "dispatch_work",
                    "change_scope",
                    "status_only",
                    "approval",
                    "multi_intent",
                    "unclear",
                    "none",
                ],
            }
        ],
        "state": {
            "message_text": text,
            "reply_to": reply_to,
            "candidate_cards": candidates or [],
        },
    }


def reply_check_request(question: str, reply: str, reply_to: str) -> dict:
    return {
        "questions": [
            {
                "id": "addresses_question",
                "primitive": "noul",
                "instructions": (
                    "Does this proposed reply address the specific user question? "
                    "A status update or unrelated progress note is not sufficient."
                ),
                "criteria": ["yes", "no"],
            }
        ],
        "state": {
            "question": question,
            "reply": reply,
            "reply_to": reply_to,
        },
    }


def extract_intents(result: dict) -> list:
    answers = (result or {}).get("answers") or {}
    value = answers.get("intents")
    if isinstance(value, dict):
        value = value.get("value") or value.get("choice") or value.get("label")
    if isinstance(value, list):
        return [str(v) for v in value]
    if value:
        return [str(value)]
    return ["unclear"]
