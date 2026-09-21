# Assignment policy (main-session POST /assign)

Portable Claude / Codex / Grok sessions assign cards with
`POST /api/cards/:n/assign`. They do not go through `dispatch_policy.py`.
This module is the server-boundary equivalent. Root must call it; this
package does not patch `bin/sprintd`.

Default builder is `settings.worker.default_executor`. Claude, Codex, and Grok
are equivalent labels. A different **provider** needs a case-specific
`model_reason` or `override_reason` every time, then
`evaluate_provider_override` (existing Jev contract). Preference is not a
reason. The outcome is not execution permission.

Live `worker.executors.grok` is `{kind, command, session}` with **no** `model`.
Incoming default grok + model `grok-4.6` is free passthrough. Compare model
only when a default model is actually configured.

## Root hook

Root COALESCE-fills the payload from the **existing card** (omitted
executor/model keep the card's values, not board defaults), then generates a
**fresh `assignment_id` per HTTP mutation**. Revalidate under `App.lock`. Do
not pass `existing`; live assignments are preserved by not calling assign
during handoff. Do not cache an allow across requests.

```python
from sprint_coordinator.assignment_policy import gate_assignment

result = gate_assignment(
    data_dir,                    # board .sprint dir
    settings=app.settings(),
    card_num=num,
    payload={                    # effective assignment, already COALESCE'd
        "executor": executor,    # from request or old card
        "model": model,          # from request or old card; may be grok-4.6
        "model_reason": model_reason,
        "override_reason": override_reason,
        "task": title or card body,
        "evidence": evidence or {},
    },
    assignment_id=fresh_id,      # new per POST, not a client nonce
    task=title,
)
if not result.allowed:
    raise ApiError(422, result.reason, result.reason)
# result.identity and result.fingerprint are for the lock revalidate
```

`gate_assignment` still accepts `existing=` so older callers do not break; the
flag is ignored.

## Config (opt-in)

Board file `assignment-policy.json` (0600), next to sprintd data:

```json
{
  "enabled": true,
  "key_file": "~/.config/sprint/typesafe.env",
  "daily_max_calls": 200
}
```

Missing file or `enabled` not true → allow, no Jev (policy off). `daily_max_calls: 0`
means zero paid calls, not "use 200". Malformed config (bad JSON, non-object,
non-integer cap) fail-closes as `unavailable`. Key is never read into the audit
DB, gate result, or this repo. Default path is `~/.config/sprint/typesafe.env`.

SQLite `assignment-policy.sqlite`: usage reservation **before** Jev, then
audit rows. A decision nonce is shared across the `reserved` row and the
outcome row (`allowed` / `denied` / `unavailable`). Row identity is `audit.id`
(PRIMARY KEY), not UNIQUE(nonce). Daily cap fail-closes as `unavailable`.

## Fail closed (overrides only)

| Case | action | Jev |
| --- | --- | --- |
| same default provider; model compared only if configured | `allow` | no |
| blank reason | `deny` | no |
| Jev `accept` | `allow` | yes |
| Jev `deny` | `deny` | yes |
| Jev `escalate` / exception / no key | `unavailable` | reserved |
| daily cap, including `0` | `unavailable` | no further call |
| malformed policy config | `unavailable` | no |

Treat only `result.allowed` as permission to write the assignment.
