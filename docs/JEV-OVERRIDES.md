# Provider overrides (Jev)

Every deviation from the installation default provider or model needs a
case-specific reason and a Jev decision. Claude, Codex, and Grok are equivalent
labels: main agent and task workers use the same transport. Preference is not a
reason.

`judgments.py` (owned elsewhere) should call:

```python
from sprint_coordinator.jev import evaluate_provider_override

outcome = evaluate_provider_override(
    client,
    task=task,
    default_provider=default_provider,
    requested_provider=requested_provider,
    default_model=default_model,
    requested_model=requested_model,
    reason=reason,
    evidence=evidence,
)
```

`VerificationOutcome.action` is `accept`, `deny`, or `escalate`. Treat only
`accept` as permission to *select* the requested label. The outcome is not
execution permission, a credential, or a merge/deploy grant. Claims inside
`evidence` are inspected; they do not authorize work.

## Code gates (no model)

| Input | Action | Model call |
| --- | --- | --- |
| `reason` blank or whitespace | `deny` | no |
| requested provider and model match the default (case-insensitive) | `accept` | no |

## Independent questions (one Jev request)

All four are Noul questions over the same state (`task`, default/requested
provider and model, `reason`, `evidence`):

1. `reason_specific` — concrete to this task, not preference or a blanket rule
2. `evidence_supports_need` — evidence shows the default is insufficient here
3. `model_fits` — requested label matches that need; vendors are not ranked
4. `forbidden_convenience` — preference, convenience, or cost-blind blanket

Thresholds stay at 0.80 accept / 0.20 reject. Composition:

- clearly bad (`forbidden_convenience` high, or any required yes-gate ≤ 0.20) → `deny`
- all required yes-gates ≥ 0.80 and convenience ≤ 0.20 → `accept`
- otherwise, or on service/parse errors → fail closed (`escalate` / `deny`)

Expected shape: `"I prefer Claude"` is `deny`. Repeated failed default attempts
with the actual errors can `accept`.

## Response verification (related)

`verify_candidate` still uses 0.80 / 0.20. It now asks whether the candidate
needs independent evidence. Arithmetic, transformations, and direct answers
justified by `task` do not use the evidence gate. External facts and
execution/completion claims still do. Code changes still require a passing
independent executable check before Jev runs. Unsupported claims still fail.
