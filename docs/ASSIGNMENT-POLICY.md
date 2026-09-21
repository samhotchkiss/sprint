# Assignment policy (main-session POST /assign)

Portable Claude / Codex / Grok sessions assign cards with
`POST /api/cards/:n/assign`. They do not go through `dispatch_policy.py`.
This module is the server-boundary equivalent. Root must call it; this
package does not patch `bin/sprintd`.

Default builder is `settings.worker.default_executor`. Claude, Codex, and Grok
are equivalent labels. A different executor or model needs a case-specific
`model_reason` or `override_reason` every time, then
`evaluate_provider_override` (existing Jev contract). Preference is not a
reason. The outcome is not execution permission.

## Root hook

```python
from sprint_coordinator.assignment_policy import gate_assignment

result = gate_assignment(
    data_dir,                    # board .sprint dir
    settings=app.settings(),
    card_num=num,
    payload={
        "executor": executor,
        "model": model,
        "model_reason": model_reason,
        "override_reason": override_reason,
        "assignment_id": assignment_id,  # stable id for this assign
        "task": title or card body,
        "evidence": evidence or {},
    },
    assignment_id=assignment_id,
    task=title,
    existing=False,              # True only to preserve live handoff rows
)
if not result.allowed:
    raise ApiError(422, result.reason, result.reason)
```

Revalidate the **same** `assignment_id` at the write boundary with the same
payload. A matching fingerprint that was already `allowed` returns allow
without a second Jev call. A changed default, model, reason, or card is a
new fingerprint and must pass again.

Handoff of in-flight assignments: `existing=True`. New POSTs: `existing=False`.

## Config (opt-in)

Board file `assignment-policy.json` (0600), next to sprintd data:

```json
{
  "enabled": true,
  "key_file": "~/.config/sprint/typesafe.env",
  "daily_max_calls": 200
}
```

Missing file or `enabled` not true → allow, no Jev (policy off). Key is never
read into the audit DB, gate result, or this repo. Default path is
`~/.config/sprint/typesafe.env`.

SQLite `assignment-policy.sqlite`: usage reservation **before** Jev, then
audit rows (`passthrough` / `denied` / `reserved` / `allowed` / `unavailable` /
`revalidated` / `preserved`). Daily cap fail-closes as `unavailable`.

## Fail closed (overrides only)

| Case | action | Jev |
| --- | --- | --- |
| same default provider+model | `allow` | no |
| blank reason | `deny` | no |
| Jev `accept` | `allow` | yes |
| Jev `deny` | `deny` | yes |
| Jev `escalate` / exception / no key | `unavailable` | reserved |
| daily cap | `unavailable` | no further call |

Treat only `result.allowed` as permission to write the assignment.
