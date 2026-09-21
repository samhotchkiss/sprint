# Coordinator owner switch

Portable switch from one coordinator pane/provider to another on the **same**
board. Codex, Claude, and Grok use the identical tmux-send protocol.

This module does **not** kill panes or processes, does not change
`worker.default_executor`, and does not skip pending events by moving the
orchestrator cursor to head. It does not mention or install launchd; root
wires `bin/sprint-dispatch-service` after a completed switch. Module tests
are not a live session test. Sam's live window: 2026-09-21 15:05 UTC,
Claude → Codex on Yunagi.

## Files

- `sprint_coordinator/handoff.py`
- `bin/sprint-handoff`
- `tests/test_handoff_switch.py`

## CLI

User confirmation is invoking the plan (already authorized). No extra prompt.

```sh
bin/sprint-handoff plan \
  --board-data-dir /path/to/.sprint \
  --project-root /path/to/repo \
  --source-pane '%5' --source-provider claude \
  --target-pane '%8' --target-provider codex

bin/sprint-handoff status --board-data-dir /path/to/.sprint
bin/sprint-handoff advance --board-data-dir /path/to/.sprint
bin/sprint-handoff ack --board-data-dir /path/to/.sprint \
  --role source --switch-id osw-… --nonce-file /path/to/.sprint/nonce
bin/sprint-handoff rollback --board-data-dir /path/to/.sprint
```

Nonce is in a mode-0600 file, not printed. Board token from `server.json` is
never copied into the switch record or prompts.

## Stages

`prepared` → `source_quiesced` → `target_registered` → `target_acknowledged` → `complete`
or `failed`.

| Stage | What happens |
| --- | --- |
| prepared | Observe `/api/settings`, `/api/board`, `/api/cursors/orchestrator`. Snapshot `dispatch.json` (`target`, `scanned`, `acknowledged`, `pending`, `inflight`, `wake_times`). tmux-send source quiesce (idle monitor only). |
| source_quiesced | After source receipt. Uncertain inflight must already be covered by the **board** cursor or explicitly `abandon_inflight: true`. Then rebind `dispatch.json` `target` under `dispatch.lock`, keep unread pending, clear old inflight only because the source monitor stopped. Ownership settings change. Default executor unchanged. |
| target_registered | tmux-send target registration. Cursor is the **board** orchestrator seq, not head. |
| target_acknowledged | Target receipt bound to switch id + nonce + planned cursor. |
| complete | Next action: start target coordinator. No automatic kill. Task workers may keep running. |

Sends are durable-before-send (`prepared` then tmux-send). Exit 4 / timeout is
`uncertain` and **must not be resent**.

## Preserve

- project root and board data dir, URL/port
- `worker.default_executor`
- every card assignment, worktree, pane, executor/model on the board snapshot
- `dispatch.json` scanned/acknowledged/pending/wake_times (never `--reset`, never discard pending)

## Dispatch rebind

Root's ingress refuses to start if `dispatch.json` `target` ≠ the registered pane.
After source ack, `rebind_dispatch_target` (under `dispatch.lock`) writes the new
pane onto the existing file. Receipt + `dispatch_rebind` on the switch record are
enough to rebind safely. Uncertain inflight (`result` not `0`/`null`) blocks
until `orchestrator` seq ≥ `inflight.through` or the source receipt sets
`abandon_inflight` true (`sprint-handoff ack --abandon-inflight`).

## Rollback

Allowed only before `target_acknowledged` / `complete`, and only when observed
ownership still matches the record. Will not silently roll back an active new
owner.

## Supervisor

Compatible with an existing process supervisor. The switch never kills PIDs
and contains no launchd identifiers. `next_action` is the exact operator step.
Root may start the persistent watcher after `complete`; that live session test
is still required.
