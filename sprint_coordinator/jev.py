"""Small, dependency-free TypeSafe/Jev client and Sprint judgment policy.

The model makes bounded semantic judgments.  This module deliberately does not
turn those judgments into permissions or claims about executable work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import random
import time
from typing import Any, Callable, Mapping, Sequence
from urllib import error, request


API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
DEFAULT_SECRET_FILE = Path("~/.config/sprint/typesafe.env").expanduser()
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


class JevError(RuntimeError):
    """Base class for failures that must be escalated rather than accepted."""


class JevConfigurationError(JevError):
    pass


class JevServiceError(JevError):
    pass


class JevResponseError(JevError):
    pass


@dataclass(frozen=True)
class JevResponse:
    model: str
    answers: Mapping[str, Mapping[str, Any]]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class RouteOutcome:
    action: str
    intents: tuple[str, ...] = ()
    probabilities: Mapping[str, float] = field(default_factory=dict)
    reason: str = ""
    usage_tokens: int = 0


@dataclass(frozen=True)
class VerificationOutcome:
    action: str
    reason: str
    judgments: Mapping[str, float] = field(default_factory=dict)
    usage_tokens: int = 0

    @property
    def accepted(self) -> bool:
        return self.action == "accept"


def load_api_key(
    environ: Mapping[str, str] | None = None,
    secret_file: Path = DEFAULT_SECRET_FILE,
) -> str:
    """Load a local credential without logging or returning its source."""
    env = os.environ if environ is None else environ
    value = env.get("TYPESAFE_API_KEY", "").strip()
    if value:
        return value
    try:
        lines = secret_file.expanduser().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise JevConfigurationError("TypeSafe API key is not configured") from exc
    except OSError as exc:
        raise JevConfigurationError("TypeSafe credential file is unreadable") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, configured = line.partition("=")
        if separator and name.strip() == "TYPESAFE_API_KEY":
            value = configured.strip().strip("'\"")
            if value:
                return value
    raise JevConfigurationError("TypeSafe API key is not configured")


class JevClient:
    """Synchronous stdlib client with a finite timeout and bounded retries."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 8.0,
        max_attempts: int = 3,
        opener: Callable[..., Any] = request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0 or max_attempts < 1:
            raise ValueError("timeout and max_attempts must be positive")
        self._api_key = api_key
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._opener = opener
        self._sleep = sleep

    def evaluate(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> JevResponse:
        if not questions:
            raise ValueError("at least one question is required")
        key = self._api_key or load_api_key()
        payload = {"state": state, "model": MODEL, "questions": questions}
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            API_URL,
            data=encoded,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(self.max_attempts):
            try:
                with self._opener(req, timeout=self.timeout) as response:
                    document = json.loads(response.read())
                return _validate_response(document, questions)
            except error.HTTPError as exc:
                exc.close()
                if exc.code not in RETRYABLE_STATUS or attempt + 1 == self.max_attempts:
                    raise JevServiceError(f"TypeSafe request failed with HTTP {exc.code}") from exc
                delay = _retry_delay(exc.headers.get("Retry-After"), attempt)
            except (error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 == self.max_attempts:
                    raise JevServiceError("TypeSafe request failed after bounded retries") from exc
                delay = _retry_delay(None, attempt)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise JevResponseError("TypeSafe returned invalid JSON") from exc
            self._sleep(delay)
        raise AssertionError("unreachable")


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    if retry_after:
        try:
            return min(5.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(5.0, 0.25 * (2**attempt) + random.uniform(0, 0.05))


def _probability(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise JevResponseError(f"invalid {field_name}")
    return float(value)


def _validate_response(document: Any, questions: Mapping[str, Mapping[str, Any]]) -> JevResponse:
    if not isinstance(document, dict) or document.get("model") != MODEL:
        raise JevResponseError("response did not use the pinned Jev model")
    answers = document.get("answers")
    usage = document.get("usage")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevResponseError("response answer ids do not match questions")
    if not isinstance(usage, dict):
        raise JevResponseError("response usage is missing")
    token_values = (usage.get("input_tokens"), usage.get("output_tokens"))
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in token_values):
        raise JevResponseError("response usage is invalid")
    for question_id, question in questions.items():
        answer = answers[question_id]
        expected = question.get("type")
        if not isinstance(answer, dict) or answer.get("type") != expected:
            raise JevResponseError(f"answer type mismatch for {question_id}")
        if expected == "noul":
            _probability(answer.get("noul"), f"{question_id}.noul")
        elif expected == "choice":
            criteria = question.get("criteria")
            probabilities = answer.get("probabilities")
            if not isinstance(criteria, dict) or not isinstance(probabilities, dict):
                raise JevResponseError(f"invalid choice answer for {question_id}")
            if set(probabilities) != set(criteria) or answer.get("choice") not in criteria:
                raise JevResponseError(f"invalid choice options for {question_id}")
            values = [_probability(v, f"{question_id}.probability") for v in probabilities.values()]
            _probability(answer.get("confidence"), f"{question_id}.confidence")
            if abs(sum(values) - 1.0) > 0.02:
                raise JevResponseError(f"choice probabilities do not sum to one for {question_id}")
        else:
            raise ValueError(f"unsupported question type: {expected}")
    return JevResponse(MODEL, answers, token_values[0], token_values[1])


def route_request(
    client: JevClient,
    *,
    message: str,
    context: Mapping[str, Any],
    intents: Mapping[str, str],
    threshold: float = 0.75,
) -> RouteOutcome:
    """Return every independently supported intent; uncertainty escalates once."""
    if not intents:
        raise ValueError("at least one candidate intent is required")
    questions = {
        intent: {
            "type": "noul",
            "instructions": f"Does `message` request this intent: {description}?",
            "criteria": {"true": description, "false": "This intent is not requested."},
        }
        for intent, description in intents.items()
    }
    try:
        response = client.evaluate({"message": message, "context": dict(context)}, questions)
    except JevError as exc:
        return RouteOutcome("escalate", reason=str(exc))
    probabilities = {key: float(response.answers[key]["noul"]) for key in intents}
    selected = tuple(key for key in intents if probabilities[key] >= threshold)
    usage = response.input_tokens + response.output_tokens
    if not selected:
        return RouteOutcome("escalate", probabilities=probabilities,
                            reason="no candidate intent cleared the routing threshold",
                            usage_tokens=usage)
    return RouteOutcome("route", selected, probabilities, usage_tokens=usage)


def verify_candidate(
    client: JevClient,
    *,
    task: str,
    constraints: Sequence[str],
    candidate: str,
    evidence: Mapping[str, Any],
    code_change: bool = False,
    independent_checks: Sequence[Mapping[str, Any]] = (),
    threshold: float = 0.80,
    uncertainty_threshold: float = 0.20,
) -> VerificationOutcome:
    """Verify four separate dimensions and conservatively accept or escalate.

    Authorization is intentionally absent: a semantic verifier cannot grant it.
    Code work is ineligible for acceptance unless an independent source supplied at
    least one passing executable check.
    """
    if code_change and not _has_independent_passed_check(independent_checks):
        return VerificationOutcome(
            "escalate", "code completion lacks an independently supplied passed executable check"
        )
    state = {
        "task": task,
        "constraints": list(constraints),
        "candidate": candidate,
        "evidence": dict(evidence),
    }
    questions = {
        "task_satisfied": _noul("Does `candidate` fully satisfy `task`?"),
        "constraints_satisfied": _noul(
            "Does `candidate` comply with every item in `constraints`? Empty constraints count as yes."
        ),
        "evidence_supports": _noul(
            "Does `evidence` directly support the factual and completion claims in `candidate`?"
        ),
        "material_uncertainty": {
            "type": "noul",
            "instructions": (
                "Is there material uncertainty, ambiguity, missing evidence, or missing information "
                "that warrants stronger review?"
            ),
            "criteria": {
                "true": "Yes, including when the supplied state or evidence is insufficient.",
                "false": "No; the supplied evidence is sufficient and there is no material uncertainty.",
            },
        },
    }
    try:
        response = client.evaluate(state, questions)
    except JevError as exc:
        return VerificationOutcome("escalate", str(exc))
    judgments = {key: float(answer["noul"]) for key, answer in response.answers.items()}
    usage = response.input_tokens + response.output_tokens
    passing = all(judgments[key] >= threshold for key in (
        "task_satisfied", "constraints_satisfied", "evidence_supports"
    )) and judgments["material_uncertainty"] <= uncertainty_threshold
    if passing:
        return VerificationOutcome("accept", "all semantic verification gates passed", judgments, usage)
    return VerificationOutcome("escalate", "one or more verification gates require stronger review", judgments, usage)


def _noul(instructions: str) -> Mapping[str, Any]:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": "Yes, based only on the supplied state.",
                     "false": "No, or the supplied state is insufficient."},
    }


def _has_independent_passed_check(checks: Any) -> bool:
    return bool(checks) and isinstance(checks, (list, tuple)) and all(
        isinstance(check, dict)
        and check.get("passed") is True
        and bool(check.get("command"))
        for check in checks
    )
