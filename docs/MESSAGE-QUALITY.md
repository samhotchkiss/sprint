# Message quality (standalone)

User-facing drafts can be checked before the coordinator publishes. This module
does **not** post to the board, mutate sprintd, or enforce UI layout. Root wires
`assess_message` into the publish path.

Claude (and any other generator) should use the formatting tools, then Jev
checks that the draft does not waste the user's time.

## Files

- `sprint_coordinator/message_quality.py`
- `tests/test_message_quality.py`

## Formatting tools

`generation_instructions(surface=..., outcome=..., context=...)` returns plain
instructions. `FORMAT_TOOLS` is the same checklist:

- Lead with the answer or changed outcome.
- Short paragraphs or a short Markdown list; no unformatted blob.
- Sidebar/card visible text: **400-character standing guidance**, not a hard cut.
- Long traces go in optional collapsed `detail`.
- Keep blockers, decision-critical uncertainty, questions, and acceptance evidence.
- No auto-truncation.

## Envelope (deterministic, no model)

```python
from sprint_coordinator.message_quality import validate_envelope, assess_message

validate_envelope("sidebar", visible, detail=None)
```

Rejects invalid surface, empty/NUL/non-string text, empty detail when provided,
and pathological size (`visible` > 8000, `detail` > 32000). Over 400 characters
adds note `over_guidance` and still proceeds. Text is never sliced.

## Jev assessment

```python
decision = assess_message(
    client=jev_client,          # object with evaluate(state, questions)
    surface="sidebar",          # or "card"
    visible=draft,
    outcome="what the user needed",
    context="optional thread/task facts",
    detail=None,                # optional collapsed Markdown
)
```

`client.evaluate` is the `JevClient` contract from `jev.py`. Tests inject a fake.
There are no paid calls in this module's tests.

Jev **cannot rewrite**. It returns noul scores on:

| id | pass means |
| --- | --- |
| `advances_next_decision` | changes next action, decision, or outcome-understanding |
| `no_superfluous_narration` | no irrelevant implementation/process dump |
| `answer_first` | main answer is the first sentence or bullet |
| `readable_structure` | short paragraphs/list, not one block |
| `preserves_decision_material` | blockers, needed uncertainty, questions, acceptance evidence kept |

`decision.action`:

| action | `verified` | meaning |
| --- | --- | --- |
| `accept` | true | envelope ok and every dimension ≥ 0.80 |
| `revise` | false | generation must fix `decision.failing` exactly |
| `reject` | false | envelope invalid; do not send |
| `unavailable` | false | verifier missing/failed; **fail closed** — do not pretend verified |

Exact comparisons: accept only when `action == "accept"` and `verified is True`.

## Wiring (root)

Call `assess_message` on the proposed outbox body. Publish only on `accept`.
On `revise`, send `failing` back to the generator. On `unavailable`, hold the
draft. This file does not implement that path.
