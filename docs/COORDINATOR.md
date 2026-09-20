# Sprint deterministic coordinator

Code owns coordination. Jev supplies bounded language judgments. Workers run
specific assignments and return JSON results. This service does **not** take
over a live board in this change.

## What it stores

SQLite under `{board_data_dir}/coordinator/` (override with `data_dir`):

- ingest cursor: the event was recorded
- obligation: one user request, independent of the cursor
- assignment: worker job with stable id and lifecycle
- outbox: reply with a stable idempotency key bound to a specific obligation
- judgments / daily usage: cache and spend caps

Advancing the ingest cursor never marks a question answered. A worker progress
or heartbeat event never satisfies an unanswered user request.

## CLI

Local config and keys live **outside the repo** (`~/.config/sprint/` by default).

```sh
bin/sprint-coordinate init-config --project-root /absolute/project
bin/sprint-coordinate status --config ~/.config/sprint/coordinator.json
bin/sprint-coordinate run --shadow --config ~/.config/sprint/coordinator.json
bin/sprint-coordinate run --active --config ~/.config/sprint/coordinator.json
bin/sprint-coordinate run-once --shadow --config ~/.config/sprint/coordinator.json
```

`--shadow` ingests and routes only. `--active` publishes replies after
explicit takeover checks. There is no automatic live takeover.

Put `TYPESAFE_API_KEY` in `~/.config/sprint/typesafe.env` mode `0600`. Never
commit it. Missing credentials escalate; they cannot approve code results.

Worker entries are **command arrays** (no shell). The process reads one JSON
job on stdin and writes one JSON result on stdout.

## Cheap-first policy

1. Route a request (deterministic events skip Jev).
2. Assign the configured `low` worker.
3. On failure, assign `high` once (`max_escalation_retries`, default 1).
4. Stop. No retry storm, no turn cap.

User-facing response work uses a reserved concurrency slot so a blocked code
worker cannot starve replies.

## Jev adapter (owned file)

`sprint_coordinator/jev.py` and `tests/test_jev_client.py` are owned by the
Jev client agent. Do not rewrite them from this coordinator work.

The coordinator loads that module through `sprint_coordinator.judgments`:

- credentials: `load_api_key(secret_file=...)` → `JevConfigurationError` if missing
- client: `JevClient(api_key, timeout=...)`
- routing: `route_request(client, message=..., context=..., intents=...)`
- code check: `verify_candidate(...)` only **after** trusted executable checks

If `jev.py` later adds `client_from_config` / `from_key_file` / `from_env`,
the loader will use them. A duck-typed test double needs `configured()`,
`route(obligation) -> {intents, escalate, usage?}`, and `verify_code(...)`.

Jev is not invoked on idle heartbeats. Daily call and spend caps apply.
Choice confidence is not authorization.

### Coordinate file for the Jev agent

Keep `jev.py` a stdlib HTTP client plus the two policy functions already
tested. The coordinator will not import TypeSafe's SDK. Pin `jev-1.13.0`.
Do not log the API key. Do not accept model-generated shell commands.

## Evidence

A `code_result` is successful only when the coordinator runs catalog
`allowed_checks` itself (`adapter: trusted`). Worker text cannot substitute.
Arbitrary `model_command` / `commands` fields are rejected.

## Crash recovery

Non-idempotent assignments found `running` after a crash become `uncertain`
and are **not** relaunched. Outbox rows found `sending` become `uncertain`
and are **not** resent. Idempotent jobs may return to `pending`.

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
6. Shadow on a copy of events before any active publish.

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
  plus a local key. Tests never call the network.
- **Uncertain sends.** A crash during publish is visible as `uncertain`, not
  retried automatically.

## Tests

```sh
python3 -m unittest tests.test_sprint_coordinator tests.test_jev_client -q
```
