# Sprint deterministic coordinator

Code owns coordination. Jev supplies bounded language judgments. Workers run
specific assignments and return JSON results. This service does **not** take
over a live board in this change.

## Modes

**Shadow** is ingest-only. It records board events and creates unanswered
obligations. It does not call Jev, launch workers, run checks, publish, or mark
obligations answered.

**Active** classifies, assigns workers, verifies, and publishes. It requires an
explicit history origin (`start_cursor` / `ingest_start_seq` in the JSON config,
or `Coordinator(..., start_cursor=)`). A new active coordinator without that
origin refuses to start rather than replaying the whole board into paid work.
Restart of an already-initialized store keeps the ingest cursor and unanswered
obligations.

Production `--active` validation (worker adapters, takeover, autoheal) is
enforced in the CLI, which this package does not own.

## What it stores

SQLite under `{board_data_dir}/coordinator/` (override with `data_dir`):

- ingest cursor: the event was recorded
- obligation: one user request, independent of the cursor
- assignment: worker job with stable id and lifecycle
- outbox: reply with a stable idempotency key bound to a specific obligation
- judgments / daily usage: cache and spend caps

Advancing the ingest cursor never marks a question answered. A worker progress
or heartbeat event never satisfies an unanswered user request.

If ingest commits and obligation creation does not, the next `open`/`tick`
scans stored user events that still lack an obligation and creates them
idempotently.

## CLI

Local config and keys live **outside the repo** (`~/.config/sprint/` by default).

```sh
bin/sprint-coordinate init-config --project-root /absolute/project
bin/sprint-coordinate status --config ~/.config/sprint/coordinator.json
bin/sprint-coordinate run --shadow --config ~/.config/sprint/coordinator.json
bin/sprint-coordinate run --active --config ~/.config/sprint/coordinator.json
bin/sprint-coordinate run-once --shadow --config ~/.config/sprint/coordinator.json
```

Put `TYPESAFE_API_KEY` in `~/.config/sprint/typesafe.env` mode `0600`. Never
commit it. Missing credentials hold the request; they cannot approve results.

Worker entries are **command arrays** (no shell). The process reads one JSON
job on stdin and writes one JSON result on stdout.

Active JSON should set `start_cursor` or `ingest_start_seq` to the last board
seq that must not be treated as new work. Shadow may start at 0.

## Cheap-first policy (active)

1. Deterministic board actions (`verdict`, `answer`, `action`) are recorded
   only. The board already applied them; a worker is not launched.
2. Status-only acknowledgements are recorded. No worker chatter.
3. Approvals, unclear, and multi-intent messages are held. They cannot dispatch
   privileged/code work.
4. A question uses the configured `response`/`low` worker. `dispatch_work` uses
   a trusted `code` worker profile, not the response default.
5. On a failed low attempt, assign `high` at most `max_escalation_retries`
   times (default 1), counting every high assignment. Budget exhaustion holds;
   it does not escalate to a paid high worker.
6. Stop. No retry storm, no turn cap.

User-facing work is scheduled with a reserved concurrency slot so a blocked
code worker cannot starve a response assignment. Routing, catalog checks, and
Jev verification run off the scheduling thread so a blocked check cannot block
ingest of a new user message or heartbeat. SQLite writes stay on the owner
thread.

## Jev adapter (owned file)

`sprint_coordinator/jev.py` and `tests/test_jev_client.py` are owned by the
Jev client agent. Do not rewrite them from this coordinator work.

The coordinator loads that module through `sprint_coordinator.judgments`:

- credentials: `load_api_key(secret_file=...)` → unconfigured client if missing
- client constructed with `max_attempts=1` so each reserved call is one HTTP try
- routing: `route_request(...)`
- verification: `verify_candidate(...)` for both replies and code. A missing
  method, missing key, or service failure prevents publish.

Judgments are cached on the full canonical typed request plus relevant state
and prompt/model version. Different questions do not share a key.

Daily call and spend caps apply to routing, workers, and verification, including
failed attempts. Zero `daily_max_calls` or `daily_max_spend_usd` means zero
calls. Spend from Jev is `input_tokens * 0.042 / 1e6` when input tokens are
reported (conservative: `usage_tokens` treated as input when that is all the
adapter exposes). Worker cost is an explicit bounded config estimate
(`estimated_calls`, `estimated_spend_usd`). Status reports do not claim a live
invoice.

Choice confidence is not authorization. Semantic accept never authorizes merge
or deploy.

## Evidence

Code assignments always run the **entire** local `allowed_checks` catalog with
the configured cwd and timeout. Worker `check_ids` cannot omit a failing check.
A code assignment with an empty catalog is rejected. `kind=reply` on a code
assignment does not skip checks. Arbitrary `model_command` / `commands` fields
are rejected.

Empty candidate text and unknown result kinds fail. Worker
`addresses_obligation` is not proof.

## Crash recovery

Non-idempotent assignments found `running`/`starting`/`verifying` after a crash
or `close` become `uncertain` and are **not** relaunched. Outbox rows found
`sending` become `uncertain` and are **not** resent. Idempotent jobs may return
to `pending`. `close` kills tracked worker subprocesses so they cannot keep
mutating after restart.

Uncertain publishes stay reconcilable under the same idempotency key and are
not retried automatically.

## Publication (active only)

Replies are posted with the outbox row's stable idempotency key. Events are
refreshed first; publish is skipped until the ingest cursor has caught up to
the reported board head (or the current page is known to be complete). An
obligation is marked answered only after the board accepts the post.

Shadow never writes an outbox row.

## Activation compatibility (not performed here)

Must:

1. Same sprintd revision that serves `GET /api/autoheal` with
   `event_dispatch_supported: true`.
2. Endpoints used: `GET /api/events`, `GET /api/autoheal`,
   `GET /api/settings`, `GET /api/cursors/orchestrator`,
   `POST /api/sidebar`, `POST /api/cards/:num/chat`. Auth from
   `.sprint/server.json` (loopback + bearer), never printed.
3. Stop `bin/sprint-dispatch` first. Active mode refuses a live dispatch
   lock or `event_dispatcher` lease.
4. Stop hub autoheal or pass `--acknowledge-autoheal-gap` with eyes open.
   sprintd only suppresses autoheal for a fresh `dispatch.json` lease plus
   held `dispatch.lock`. This coordinator uses its own flock and does **not**
   spoof that lease.
5. Installation-local worker command arrays and TypeSafe key.
6. Shadow on a copy of events before any active publish. Active needs an
   explicit start cursor.

Must not (this task):

- mutate a live board, push, or rewrite `bin/sprintd`
- disable autoheal by writing fake dispatcher state
- treat the board orchestrator cursor as this service's answered set

## Honest blockers

- **Autoheal gap.** Until sprintd recognizes a coordinator lease, `--active`
  on a board with a registered tmux window can still be revived by the hub.
  That is a sprintd change, out of scope here.
- **Conversation-first.** This cut routes and replies. It does not take over
  worktree dispatch, merge, or review from the existing session.
- **Jev client file.** Live TypeSafe evaluation depends on the owned `jev.py`
  plus a local key. Tests never call the network. Combined `usage_tokens` are
  billed conservatively as input tokens; that is not a provider invoice.
- **CLI start cursor.** The CLI does not yet pass `--start-cursor`. Put
  `start_cursor` in the JSON config until the CLI grows that flag.
- **Uncertain sends.** A crash during publish is visible as `uncertain`, not
  retried automatically.
- **Held work.** Budget, missing Jev, and ambiguous/approval requests stay
  `held` until a human or a later policy change moves them. They are not
  discarded on restart.

## Tests

```sh
python3 -m unittest tests.test_sprint_coordinator tests.test_jev_client -q
```
