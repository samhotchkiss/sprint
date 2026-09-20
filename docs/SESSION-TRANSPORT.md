# Session transport adapter

Session work is a configured tmux pane plus private assignment files. Every
message into that pane goes through **tmux-send**. The adapter never uses
`tmux send-keys`, `paste-buffer`, provider CLI prompt/stdin, `--force`, or
`--no-verify`.

**Codex, Claude, and Grok are identical** on this protocol, whether the pane
is the main/supervisor agent or a task agent. Transport, result schema, and
crash recovery do not change by provider. `provider` is a label only.
`agent_role` (`main`, `supervisor`, `task`) is separate from provider. Neither
selects a CLI, prompt template, JSON envelope, or parser. There is no
`grok` / `claude` / `codex` command path, `--prompt-file`, `--print`, or
stdin-to-model delivery.

Operator configuration chooses the pane, tmux-send executable, assignment
directory, and (for code) worktree/scope. Jobs cannot select a pane, provider
CLI, or messaging path.

```sh
bin/sprint-session-worker --pane '%5' \
  --tmux-send /absolute/tmux-send \
  --assignments-dir /absolute/private/assignments \
  --transport-root /absolute/private/transport < job.json

bin/sprint-session-worker --reconcile --pane '%5' \
  --assignments-dir /absolute/private/assignments \
  --transport-root /absolute/private/transport < job.json
```

Active coordinator workers must invoke this binary (see `validate_active`).

## Delivery

Command array, verification on:

```
[tmux-send, --no-stash, --wait, 0, <canonical-pane>, --file, <prompt>]
```

Canonical pane IDs match `%` + digits (`tmux display-message -p '#{pane_id}'`).
Assignment IDs are `[A-Za-z0-9][A-Za-z0-9._-]*` with no `/`, `\`, `..`, or
leading `-`.

Per-job directory mode `0700`. `job.json` and `prompt.txt` mode `0600`. The
prompt only names the job path and expected `result.json` path. Session
stdout/screen is never parsed as completion.

tmux-send exit **0** means the instruction was submitted, not that the job
finished.

## Durable states

`prepared` → `delivering` → `delivered` → `completed`

Intent is fsynced as `delivering` before tmux-send. A process restart reads
`manifest.json` / `result.json` before any send and never blindly redelivers.
A valid matching `result.json` completes the assignment even when the manifest
is still `delivering`, `uncertain`, or `blocked`: uncertainty is about delivery,
not about ignoring a real candidate.

Prepared restart sends only through the operator-configured tmux-send executable
(never `sys.executable`, `grok`, `claude`, `codex`, or another provider CLI).

`--reconcile` / `reconcile` never sends, including from `prepared`. The
coordinator can poll durable artifacts with no session message and no model cost.

| Manifest | Restart (may send only if prepared) | `--reconcile` (never sends) |
| --- | --- | --- |
| prepared | send via configured tmux-send | `pending` `not_sent` |
| delivering | if result: `completed`; else `uncertain`, no send | if result: `completed`; else `pending` `in_flight` |
| delivered | wait for `result.json`, no send | if result: `completed`; else `pending` |
| completed | return candidate, no send | same |
| uncertain | if result: `completed`; else `uncertain`, no send | same |
| blocked | if result: `completed`; else `blocked`, no send | same |
| missing job dir | prepare + send | `pending` `not_prepared` |

A wait timeout does **not** cancel or kill the session and does **not** start a
duplicate code job. It returns `pending` with assignment/job-dir identity for
later reconciliation.

## Adapter result statuses

The process writes one JSON object to stdout and exits **0** for `completed`,
`pending`, `uncertain`, and `blocked` so the coordinator can inspect `status`
instead of collapsing those outcomes into an untyped `exit_N` failure.

| `status` | `ok` | When | Coordinator |
| --- | --- | --- | --- |
| `completed` | true | Valid `result.json` (candidate only) | Treat `text`/`kind` as a **candidate**. Run catalog checks/Jev yourself. Never trust session `checks`. |
| `pending` | false | Bound wait elapsed after delivery (`reason=wait_timeout`) or interrupt after delivery | Keep the assignment. Reconcile later with `job_dir` / `result_path`. Do not resend or escalate to a duplicate job. |
| `uncertain` | false | tmux-send exit 4, send timeout, interrupt during send, restart of `delivering` | **Never resend.** Operator inspects the pane. |
| `blocked` | false | Invalid pane; pane flock/marker busy; exit 3/5/6; code not enabled | Do not force keystrokes. Exit 5 = dialog, 6 = draft. |
| `failed` | false | Bad job JSON, injection/override keys, invalid/mismatched result | Do not resend that assignment. |

### `reason` values

- `invalid_pane`, `invalid_assignment_id`, `job_transport_override`, `unsupported_job_kind`
- `invalid_provider_label`, `invalid_agent_role`, `provider_role_collision`, `role_provider_collision`
- `tmux_send_not_absolute`, `tmux_send_missing`, `spawn_failed`
- `pane_busy` (flock), `pane_held` (active marker after the previous process exited)
- `pane_not_found` (tmux-send 3)
- `dialog_refusal` (5), `draft_refusal` (6)
- `send_unverified` (4), `send_timeout`, `interrupted`, `already_uncertain`
- `send_failed` (other tmux-send codes)
- `wait_timeout`, `not_sent`, `not_prepared`, `in_flight`
- `code_not_enabled`, `missing_worktree`, `invalid_worktree`, `missing_task_scope`
- `result_token_mismatch`, `result_assignment_mismatch`, `invalid_result`, `invalid_result_kind`, `invalid_result_text`, `result_too_large`, `untrusted_evidence_rejected`

## Pane lock

`flock` on `{transport_root}/pane-<id>.lock` is exclusive and non-blocking.
`{transport_root}/pane-<id>.active.json` remains after the adapter exits while
the assignment is delivering, delivered (including pending wait), uncertain, or
blocked on a dialog/draft. Two boards cannot use the same pane at once.

## Result protocol

The session writes atomic `result.json` in the job directory:

```json
{
  "ok": true,
  "assignment_id": "asg-…",
  "token": "<unpredictable token from job.json>",
  "kind": "reply",
  "text": "candidate answer",
  "addresses_obligation": true,
  "status_update": false
}
```

`kind` is `reply` for response jobs, or `reply` / `code_result` for
operator-enabled code jobs. Payload size is capped (64KiB file, 32k text).
Wrong id/token is `failed`. Fields that look like trusted checks are rejected.
Provider envelopes (`structured_output`, CLI stdout, pane capture) are not
completion.

Optional job labels, recorded and ignored by transport:

| field | values | not |
| --- | --- | --- |
| `provider` | `claude`, `codex`, `grok` (or another string label) | a CLI, model, or parser |
| `agent_role` | `main`, `supervisor`, `task` | a provider name |

A provider name in `kind` or `agent_role`, or an agent role in `provider`, is
rejected. `kind` remains `response` / `code` only.

Helper (no delivery):

```sh
bin/sprint-session-worker submit-result --job-dir /absolute/job --text "candidate"
```

## Code jobs

Accepted only when the operator passes `--allow-code --worktree /absolute/dir
--task-scope "…"`. Capability is never inferred from model intent. Merge, push,
and deploy stay forbidden by default. The coordinator still runs independent
catalog checks and Jev.

## Tests

```sh
python3 -m unittest tests.test_session_worker -q
```

Tests use a fake tmux-send executable only, including Grok, Claude, and Codex
labels on main/supervisor/task roles. No live pane, paid call, or credential
is required.
