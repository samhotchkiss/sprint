"""Standalone user-message quality checks. Does not publish to the board.

Jev supplies typed noul judgments. This module never rewrites draft text.
A missing or failing verifier cannot mark a draft verified or publishable.

400 characters is standing Sprint guidance for sidebar/card summaries, not a
universal hard cut. This module does not auto-truncate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


SURFACES = ("sidebar", "card")
GUIDANCE_VISIBLE_CHARS = 400
HARD_VISIBLE_CHARS = 8000
HARD_DETAIL_CHARS = 32000
DEFAULT_THRESHOLD = 0.80

# Exact dimension ids returned to generation when action is "revise".
DIMENSIONS = (
    "advances_next_decision",
    "no_superfluous_narration",
    "answer_first",
    "readable_structure",
    "preserves_decision_material",
)

ACTIONS = ("accept", "revise", "reject", "unavailable")


def _noul(instructions: str, true: str, false: str) -> dict:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": true, "false": false},
    }


QUALITY_QUESTIONS = {
    "advances_next_decision": _noul(
        "Does `visible` (with `detail` if present) change what the user should "
        "do next, decide next, or understand about `outcome`? "
        "A status placeholder, process recap, or agreement echo is not enough.",
        "Yes: the user can act, decide, or update their understanding of the outcome.",
        "No: the user is no closer to a next action, decision, or outcome.",
    ),
    "no_superfluous_narration": _noul(
        "Does the draft avoid irrelevant implementation narration, tool-trace "
        "recap, apology essays, and repetition that do not help the next action? "
        "Necessary blockers, uncertainty, questions, and acceptance evidence "
        "are not superfluous.",
        "Yes: extra process/implementation chatter is absent; remaining text earns its place.",
        "No: the draft spends the user's time on narration or repetition they do not need.",
    ),
    "answer_first": _noul(
        "Is the main answer or changed outcome the first sentence or first list "
        "item of `visible`? Setup, hedging, and 'I looked at the code' must not lead.",
        "Yes: the first visible unit is the answer or changed outcome.",
        "No: the user has to hunt past preamble to find the point.",
    ),
    "readable_structure": _noul(
        "Is `visible` scannable as short paragraphs and/or a short list, not one "
        "unformatted block? Markdown used for lists or emphasis is fine; a wall of text is not.",
        "Yes: a person can scan it on a sidebar or card without decoding a blob.",
        "No: it is an unformatted block or oversized paragraph dump.",
    ),
    "preserves_decision_material": _noul(
        "Does the draft keep every fact from `outcome` and `context` that the user "
        "needs: blockers, uncertainty required for a decision, questions asked of "
        "the user, and evidence needed to accept work? "
        "Do not hide those by omitting them or burying them only in inaccessible form. "
        "Do not treat shortening as a fix for an unsupported claim.",
        "Yes: decision-critical material is present and not stripped.",
        "No: a blocker, needed uncertainty, question, or acceptance evidence is missing or hidden.",
    ),
}


FORMAT_TOOLS = (
    "Lead with the answer or changed outcome in the first sentence or first bullet.",
    "Use short paragraphs or a short Markdown list. Do not paste one unformatted block.",
    "Sidebar and card summaries: aim for %d characters of visible text (standing guidance, not a hard cut)."
    % GUIDANCE_VISIBLE_CHARS,
    "Put logs, diffs, and long traces in collapsed `detail`, not in `visible`.",
    "Keep blockers, decision-critical uncertainty, questions, and acceptance evidence visible.",
    "Omit agreement echoes, apology essays, tool traces, and implementation narration that do not change the next action.",
    "Do not auto-truncate. If the draft is too large, revise the content; do not slice mid-sentence.",
)


def generation_instructions(*, surface: str, outcome: str, context: str = "") -> str:
    """Plain instructions a generator can follow. Not a rewrite of any draft."""
    surface = surface if surface in SURFACES else "sidebar"
    lines = [
        "Write a user-facing %s message." % surface,
        "Requested outcome: %s" % (outcome or "(unspecified)"),
    ]
    if context.strip():
        lines.append("Context: %s" % context.strip())
    lines.append("")
    lines.extend("- %s" % item for item in FORMAT_TOOLS)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class QualityDecision:
    action: str
    verified: bool
    reason: str
    dimensions: Mapping[str, float] = field(default_factory=dict)
    failing: tuple[str, ...] = ()
    envelope_notes: tuple[str, ...] = ()
    usage_tokens: int = 0
    visible_chars: int = 0
    guidance_chars: int = GUIDANCE_VISIBLE_CHARS

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "verified": self.verified is True,
            "reason": self.reason,
            "dimensions": dict(self.dimensions),
            "failing": list(self.failing),
            "envelope_notes": list(self.envelope_notes),
            "usage_tokens": int(self.usage_tokens),
            "visible_chars": int(self.visible_chars),
            "guidance_chars": int(self.guidance_chars),
            "rewritten": False,
        }


def _decision(action: str, reason: str, *, verified: bool = False, visible: str = "",
              notes: tuple[str, ...] = (), dimensions: Mapping[str, float] | None = None,
              failing: tuple[str, ...] = (), usage_tokens: int = 0) -> QualityDecision:
    if action not in ACTIONS:
        action = "unavailable"
        verified = False
        reason = "unknown_action"
    if action != "accept":
        verified = False
    return QualityDecision(
        action=action,
        verified=verified is True,
        reason=reason,
        dimensions=dict(dimensions or {}),
        failing=tuple(failing),
        envelope_notes=tuple(notes),
        usage_tokens=int(usage_tokens or 0),
        visible_chars=len(visible) if isinstance(visible, str) else 0,
    )


def validate_envelope(surface: Any, visible: Any, detail: Any = None) -> QualityDecision | dict:
    """Deterministic envelope check. Never truncates. Returns a dict on success."""
    notes: list[str] = []
    if surface not in SURFACES:
        return _decision("reject", "invalid_surface", visible=visible if isinstance(visible, str) else "")
    if not isinstance(visible, str):
        return _decision("reject", "visible_not_string")
    if "\x00" in visible:
        return _decision("reject", "visible_nul")
    if not visible.strip():
        return _decision("reject", "visible_empty", visible=visible)
    if len(visible) > HARD_VISIBLE_CHARS:
        return _decision("reject", "visible_too_long", visible=visible)
    if len(visible) > GUIDANCE_VISIBLE_CHARS:
        notes.append("over_guidance")
    if detail is not None:
        if not isinstance(detail, str):
            return _decision("reject", "detail_not_string", visible=visible, notes=tuple(notes))
        if "\x00" in detail:
            return _decision("reject", "detail_nul", visible=visible, notes=tuple(notes))
        if len(detail) > HARD_DETAIL_CHARS:
            return _decision("reject", "detail_too_long", visible=visible, notes=tuple(notes))
        if not detail.strip():
            return _decision("reject", "detail_empty", visible=visible, notes=tuple(notes))
    return {
        "surface": surface,
        "visible": visible,
        "detail": detail,
        "notes": tuple(notes),
        "visible_chars": len(visible),
        "guidance_chars": GUIDANCE_VISIBLE_CHARS,
    }


def _usage_tokens(response: Any) -> int:
    direct = getattr(response, "usage_tokens", None)
    if isinstance(direct, int):
        return max(0, direct)
    input_tokens = getattr(response, "input_tokens", 0) or 0
    output_tokens = getattr(response, "output_tokens", 0) or 0
    try:
        return max(0, int(input_tokens) + int(output_tokens))
    except (TypeError, ValueError):
        return 0


def _noul_score(answers: Mapping[str, Any], name: str) -> float | None:
    raw = answers.get(name)
    if not isinstance(raw, dict):
        return None
    value = raw.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if score != score or score < 0.0 or score > 1.0:
        return None
    return score


def _evaluate(client: Any, state: dict, questions: dict) -> Any:
    if client is None:
        raise RuntimeError("verifier_unavailable")
    fn = getattr(client, "evaluate", None)
    if not callable(fn):
        raise RuntimeError("verifier_unavailable")
    return fn(state, questions)


def assess_message(
    *,
    client: Any,
    surface: str,
    visible: str,
    outcome: str,
    context: str = "",
    detail: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> QualityDecision:
    """Judge a proposed visible message. Jev does not rewrite it.

    Returns action accept/revise/reject/unavailable. ``verified`` is True only
    on accept after a successful Jev pass. Coordinator publication is out of
    scope for this module.
    """
    envelope = validate_envelope(surface, visible, detail)
    if isinstance(envelope, QualityDecision):
        return envelope
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return _decision("unavailable", "invalid_threshold", visible=visible,
                         notes=envelope["notes"])
    threshold = float(threshold)
    if threshold != threshold or threshold < 0.0 or threshold > 1.0:
        return _decision("unavailable", "invalid_threshold", visible=visible,
                         notes=envelope["notes"])

    state = {
        "surface": envelope["surface"],
        "outcome": outcome if isinstance(outcome, str) else "",
        "context": context if isinstance(context, str) else "",
        "visible": envelope["visible"],
        "detail": envelope["detail"] if envelope["detail"] is not None else "",
        "guidance_chars": GUIDANCE_VISIBLE_CHARS,
        "format_tools": list(FORMAT_TOOLS),
    }
    try:
        response = _evaluate(client, state, QUALITY_QUESTIONS)
    except Exception:
        return _decision(
            "unavailable", "verifier_unavailable", visible=visible,
            notes=envelope["notes"],
        )

    answers = getattr(response, "answers", None)
    if not isinstance(answers, Mapping):
        return _decision(
            "unavailable", "malformed_verifier_response", visible=visible,
            notes=envelope["notes"], usage_tokens=_usage_tokens(response),
        )

    dimensions: dict[str, float] = {}
    failing: list[str] = []
    for name in DIMENSIONS:
        score = _noul_score(answers, name)
        if score is None:
            return _decision(
                "unavailable", "incomplete_verifier_response", visible=visible,
                notes=envelope["notes"], usage_tokens=_usage_tokens(response),
            )
        dimensions[name] = score
        if not (score >= threshold):
            failing.append(name)

    usage = _usage_tokens(response)
    if failing:
        return _decision(
            "revise", "dimensions_unmet", verified=False, visible=visible,
            notes=envelope["notes"], dimensions=dimensions,
            failing=tuple(failing), usage_tokens=usage,
        )
    return _decision(
        "accept", "quality_gates_passed", verified=True, visible=visible,
        notes=envelope["notes"], dimensions=dimensions, failing=(),
        usage_tokens=usage,
    )
