# Sprint coordinator

A persistent Python service reads Sprint events, schedules bounded assignments,
checks results, and posts replies. Idle time does not invoke a model. Grok,
Claude and Codex share the same `tmux-send` transport and durable result protocol.

## Provider policy

The board's `settings.worker.default_executor` controls the default provider.
Every active worker profile explicitly names its provider, model and tmux pane.
Unsupported settings or an unavailable settings endpoint prevent dispatch.

An alternate provider or model needs a case-specific reason and Jev approval.
Approval is bound to the assignment, task revision, requested provider/model,
command, production base and override request ID. Changes invalidate approval.
The final send boundary checks this again, then consumes approval in the same
SQLite transaction that marks the assignment running. No blanket approvals.

Missing reasons, denials and unavailable verification cannot launch an alternate.
Escalation carries the actual previous failure and candidate as evidence. Jev's
approval permits provider selection only; it never grants merge or deployment
permission. The audit is append-only in SQLite's `provider_decisions` table.

A supervisor can request an override for an **unsent** assignment:

```sh
bin/sprint-coordinate override --config /private/coordinator.json \
  --assignment ASSIGNMENT_ID --worker high \
  --reason 'Specific evidence explaining why this task needs the alternate'
```

That command requests approval; it does not send the job. Budget limits apply to
verification as well as work. Worker prices are configured estimates, not an
invoice or a provider-enforced spending limit.

## Delivery and recovery

- The board-wide ownership flock prevents two service configurations owning one
  board. A separate store lock protects each coordinator database.
- `sprintd` recognizes ownership only while both a fresh lease and the OS lock
  exist. A stale JSON file cannot hide a dead coordinator. No fake cursor writes.
- The macOS supervisor restarts a crashed service without asking a model.
- Assignment IDs, input, result tokens and outcomes are durable. Uncertain
  session delivery is reconciled from the result file, never blindly resent.
- A pane with unfinished work is not given a second assignment.
- Replies have stable HTTP idempotency keys. Uncertain HTTP delivery can retry
  the same key with backoff without another model call.
- A newer user message prevents an older pending assignment from launching, and
  invalidates an older candidate before publication.

Run or supervise a private installation:

```sh
bin/sprint-coordinate init-config --project-root /absolute/project
bin/sprint-coordinate run --shadow --config /private/coordinator.json
bin/sprint-coordinate run --active --config /private/coordinator.json
bin/sprint-coordinate-service install --config /private/coordinator.json
bin/sprint-coordinate-service status --config /private/coordinator.json
bin/sprint-coordinate status --config /private/coordinator.json
```

The service installer is macOS-specific. Other hosts can supervise the same
foreground `run --active` command. `uninstall` stops and removes the launchd job.

Shadow mode is ingest-only: no inference, workers, publication or completion.
Active startup needs an explicit `start_cursor`; use a fresh store for a live
handoff, with the agreed cutoff. Do not turn a historical shadow store into an
active replay without reviewing its obligations.

## Code work and review

Code profiles use `--allow-code` and a bounded `--task-scope`. Configure
`code_base_ref` to the verified production commit, never an unreleased branch.
The coordinator creates a separate git worktree for each assignment, runs the
entire trusted check catalog there, and independently reads its diff. New source
files must be committed before review. Oversized diffs need smaller assignments.
Code cannot pass by returning an ordinary reply or claiming tests succeeded.

The result remains a candidate until executable checks and Jev pass. External
factual and completion claims need evidence; self-contained arithmetic and text
transformations can be checked against the task itself. Results are posted to
the originating thread. Review, integration, deployment and product-specific
acceptance remain separate operations; the coordinator does not auto-merge.

Main-agent responses receive current board context and standing instructions.
The board has already applied deterministic answers/verdicts/actions; this first
coordinator does not reapply those mutations. Existing worker conversations and
unfinished board work must be explicitly handed over, not silently abandoned.

## Live handoff

1. Verify the updated server's `/api/autoheal` reports `coordinator_supported`.
2. Confirm the existing owner has stopped its board monitor and new dispatches.
   Preserve its current workers and obtain their card/worktree mappings.
3. Stop the old deterministic dispatcher, if present. Do not run two owners.
4. Prepare private profiles, current production base, check catalog, explicit
   history cutoff and fresh coordinator store.
5. Install supervision. Verify the lease, default-provider round trip, denied
   override, crash recovery, and absence of idle model calls.
6. Keep the previous config and service available for rollback. Stop the new
   coordinator before restoring the previous owner.

Keys stay in `~/.config/sprint/typesafe.env` (0600), or the installation's own
configured key file. No account key ships in this repository.
