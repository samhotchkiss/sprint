"""Jev adapter contract. The HTTP client lives in jev.py (owned by another agent).

Expected `sprint_coordinator.jev` surface (any one is enough):

- `client_from_config(config) -> client`
- `JevClient.from_config(config)` / `from_key_file(path)` / `from_env()`
- `JevClient(api_key=..., model=..., timeout=..., max_attempts=1)`

A client must provide:

- `configured() -> bool`  (False when no installation key is present)
- `route(obligation) -> {intents, escalate, usage?}`
- `verify_candidate(...)`  (missing method is a hard failure)

Never read another user's key. Never log the secret. Missing credentials must
not fall back to a shared or example key, and must not approve work.

Spend is derived from reported input tokens at INPUT_TOKEN_USD_PER_MILLION.
That figure is not a live invoice. Worker spend is an explicit config estimate.
"""

from __future__ import annotations

from pathlib import Path

from sprint_coordinator.util import canonical_json, sha256_text


class MissingCredentials(RuntimeError):
    pass


class JevUnavailable(RuntimeError):
    pass


INPUT_TOKEN_USD_PER_MILLION = 0.042
MAX_WORKER_ESTIMATED_CALLS = 8
MAX_WORKER_ESTIMATED_SPEND_USD = 1.0
DEFAULT_WORKER_ESTIMATED_SPEND_USD = 0.01

ROUTE_INTENTS = {
    "answer_question": "The user is asking a question that needs a reply.",
    "dispatch_work": "The user wants coding or other work dispatched.",
    "change_scope": "The user is changing the scope of existing work.",
    "status_only": "The user is only acknowledging status, not asking a question.",
    "approval": "The user is approving something already proposed.",
}


def spend_usd_from_input_tokens(input_tokens: int) -> float:
    return (max(0, int(input_tokens)) / 1_000_000.0) * INPUT_TOKEN_USD_PER_MILLION


def usage_from_tokens(input_tokens: int, calls: int = 1) -> dict:
    tokens = max(0, int(input_tokens or 0))
    return {
        "calls": int(calls),
        "input_tokens": tokens,
        "spend_usd": spend_usd_from_input_tokens(tokens),
        "billing": "input_tokens_rate",
        "rate_usd_per_million_input_tokens": INPUT_TOKEN_USD_PER_MILLION,
    }


def worker_budget_estimate(config, worker_name: str) -> tuple[int, float, bool]:
    raw_workers = {}
    if getattr(config, "raw", None) and isinstance(config.raw.get("workers"), dict):
        raw_workers = config.raw["workers"]
    spec = raw_workers.get(worker_name) or {}
    try:
        calls = int(spec.get("estimated_calls", 1))
    except (TypeError, ValueError):
        calls = 1
    try:
        spend = float(spec.get("estimated_spend_usd", DEFAULT_WORKER_ESTIMATED_SPEND_USD))
    except (TypeError, ValueError):
        spend = DEFAULT_WORKER_ESTIMATED_SPEND_USD
    calls = max(1, min(calls, MAX_WORKER_ESTIMATED_CALLS))
    spend = max(0.0, min(spend, MAX_WORKER_ESTIMATED_SPEND_USD))
    return calls, spend, True


class UnconfiguredJev:
    def __init__(self, reason: str = "jev_unconfigured"):
        self.reason = reason
        self.calls = []

    def configured(self) -> bool:
        return False

    def route(self, obl: dict) -> dict:
        self.calls.append(obl)
        return {"intents": ["unclear"], "escalate": True, "reason": self.reason}

    def verify_candidate(self, **kwargs):
        raise MissingCredentials(self.reason)

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
        if not hasattr(self.jev_mod, "route_request"):
            raise JevUnavailable("missing_route_request")
        outcome = self.jev_mod.route_request(
            self.client,
            message=obl.get("question_text") or "",
            context={
                "reply_to": obl.get("reply_to"),
                "thread_context": obl.get("thread_context") or [],
            },
            intents=ROUTE_INTENTS,
        )
        self.calls.append(outcome)
        tokens = int(getattr(outcome, "usage_tokens", 0) or 0)
        usage = usage_from_tokens(tokens)
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

    def verify_candidate(self, *, task, constraints, candidate, evidence,
                         code_change=False, independent_checks=(), context=None,
                         **_kwargs):
        fn = getattr(self.jev_mod, "verify_candidate", None)
        if not callable(fn):
            raise JevUnavailable("missing_verify_candidate")
        packed = dict(evidence or {})
        if context is not None:
            packed = dict(packed)
            packed["context"] = context
        independent = []
        for check in independent_checks or ():
            if not isinstance(check, dict):
                continue
            independent.append({
                "passed": True if check.get("passed") is True else bool(
                    check.get("passed") is True),
                "command": check.get("command") or "",
                "source": check.get("source") or "independent",
            })
            if check.get("passed") is True:
                independent[-1]["passed"] = True
        outcome = fn(
            self.client,
            task=task,
            constraints=list(constraints or []),
            candidate=candidate,
            evidence=packed,
            code_change=bool(code_change),
            independent_checks=independent,
        )
        self.calls.append(outcome)
        return outcome

    def verify_code(self, **kwargs):
        raise JevUnavailable("missing_verify_candidate")


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


def cache_key(prompt_version: str, model: str, request: dict,
              relevant_state=None) -> str:
    """Hash the full canonical typed request plus relevant state/version."""
    payload = {
        "prompt_version": prompt_version,
        "model": model,
        "request": request,
        "state": relevant_state,
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
    timeout = getattr(config, "jev_timeout_seconds", 8.0)
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
    # Production adapter: one HTTP attempt per reserved coordinator call.
    for kwargs in (
        {"timeout": timeout, "max_attempts": 1},
        {"timeout": timeout},
        {},
    ):
        try:
            return cls(key, **kwargs)
        except TypeError:
            try:
                return cls(api_key=key, **kwargs)
            except TypeError:
                continue
    try:
        return cls(key)
    except TypeError:
        return None


def routing_request(text: str, reply_to: str, candidates: list | None = None,
                    thread_context=None) -> dict:
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
            "thread_context": thread_context or [],
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
