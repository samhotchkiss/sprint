---
name: sprint
description: Run or resume a Sprint board from a live Codex session, including event draining, card dispatch, worker supervision, evidence handoff, and verdict integration. Use for "start a sprint", "resume the sprint", "open the board", or requests to check, dispatch, batch, or tend Sprint cards.
---

# Sprint for Codex

This is the Codex host adapter for Sprint. The board is state only; this live
Codex session is its brain.

## Load the canonical contract

This adapter is installed as a symlink by `bin/sprint-codex-install`. Resolve
the Sprint repository root from the directory containing this file:

```bash
git -C <directory-containing-this-SKILL.md> rev-parse --show-toplevel
```

Before acting on a Sprint board, read these files from that repository
completely:

1. `skills/sprint/SKILL.md`
2. `agents/sprint-worker.md`

Follow that contract except where this adapter replaces a Claude-specific
mechanism. Do not copy or fork the canonical instructions here. If the two
files change, the new canonical behavior applies immediately.

## Never steal a live board

One board has one orchestrator. Before writing settings, moving the cursor, or
dispatching, inspect `sprintd status`, `/api/settings`, `/api/board`, and the
registered tmux target. If another session has a current waiter or owns the
registered window, do not attach or race its cursor. Tell the user the board is
already owned and either use a different project root or wait for an explicit
handoff.

Sprint registers one board per project root. When the user asks for a second
board for the same repository, create a distinct git worktree and use that
worktree as the new board's project root. Do not point a second data directory
at the same project root: the machine registry is keyed by project root, so the
two boards would overwrite each other's registry identity.

Linked worktrees still share one Git branch namespace. On a second board,
namespace worker branches with the board slug (for example,
`design-sync/card-3`) instead of reusing the canonical `sprint-card-3` branch
name. Keep `sprint-card-3` as the board-facing agent name and worktree folder;
only the Git branch needs the extra namespace.

Register this session's exact tmux target only after Codex owns the board.
Never overwrite another live session's target. The signed launch URL is meant
for the user and should be shared when the board starts; do not put its bearer
token into board messages or unrelated logs.

## Codex tool mapping

Interpret the canonical host terms as follows:

| Canonical term | Codex mechanism |
|---|---|
| Bash | `functions.exec` with `exec_command`, `write_stdin`, and `apply_patch` as appropriate |
| Agent tool | `collaboration.spawn_agent` |
| SendMessage | `collaboration.send_message` for a running turn; `collaboration.followup_task` to resume an idle worker |
| Agent/task inspection | `collaboration.list_agents` |
| Stop a canceled worker | `collaboration.interrupt_agent` |
| Monitor | the yielded-tail ingress below |

Codex task names accept underscores, not the board's hyphens. Use
`sprint_card_<n>`, `sprint_batch_<id>`, and `sprint_review_<n>` as collaboration
task names while keeping `sprint-card-<n>`, `sprint-batch-<id>`, and
`sprint-review-<n>` as board-facing agent names. Record the returned Codex
agent id or canonical task path in a collapsed card-note detail so a compacted
or resumed orchestrator can recover the mapping from the board.

Before relaying to a worker, call `list_agents`. Send to a running worker; use
`followup_task` for an idle worker. A worker missing from the current agent tree
is dead for recovery purposes; follow the canonical fresh-agent procedure.

## Persistent event ingress

Codex has no tool literally named `Monitor`, but a yielded `functions.exec`
cell provides the same behavior:

1. Start `bin/sprintd tail --after <persisted-cursor>` with `exec_command` and a
   short initial yield.
2. In the same JavaScript cell, loop on `write_stdin` for that command session.
   When output arrives, emit it with `text(...)` and call `yield_control()`.
3. Keep the returned cell id. After handling and fully draining the event log,
   call `functions.wait` on that cell to return to the same live tail.
4. A tail line is only a wake signal. Always fetch the full event/card and run
   the canonical cursor drain before acting.

If a yielded cell is unavailable, use the canonical `sprintd wait --timeout
60` fallback. Re-arm before draining every time. Keep the orchestrator turn
alive while the sprint is open; use commentary for concise progress and put
all board-originated replies back on the board. If the host forces a turn end,
re-ground before the next action and rely on the correctly registered tmux
autoheal target for recovery.

Shell environments do not persist automatically across Codex tool calls.
Re-read `.sprint/server.json` after restarts and pass `SPRINT_SERVER` and
`SPRINT_TOKEN` inline to each API/helper command that needs them.

## Dispatching Codex workers

Create the exact worktree first, assign the card, then spawn a worker with a
self-contained brief. Use `fork_turns: "none"` when selecting a model, because
the brief and canonical worker file supply the context. Every worker brief must
tell the agent to:

- read `agents/sprint-worker.md` from this Sprint repository completely;
- run every file-changing command in the assigned worktree, not the shared
  session cwd;
- use the supplied card number, board URL/token, branch, attachment paths, and
  standing instructions;
- report through `sprint-post`, `sprint-ask`, and `sprint-ready` exactly as the
  canonical contract requires.

All Codex agents share the filesystem. Worktree isolation is therefore a
behavioral boundary, not an automatic sandbox; verify the worker's `git status`
and command workdir when supervising it.

Declare a `codex` executor with `kind: "subagent"` on a Codex-owned board and
stamp the exact Codex model slug on each assignment. Never label Codex work as
`claude`. Preserve an existing user executor policy; do not rewrite a live
board's settings merely to make it convenient.

For `lowest_feasible`, use:

- `gpt-5.6-luna` for narrow, mechanical, repeatable cards;
- `gpt-5.6-terra` for ordinary implementation and read-heavy work;
- `gpt-5.6-sol` for architecture, subtle correctness or concurrency work,
  independent review, or repeated bounces.

The board's configured concurrency is an upper bound. Also honor the current
Codex collaboration-slot limit and keep one slot for the orchestrator. Count a
batch as one worker, as the canonical contract says.

The reviewer is a separate read-only Codex agent and follows the canonical
rule: it may annotate or bounce, but never approve, close, merge, or impersonate
the user.

## Repository authority still wins

The target repository's `AGENTS.md`, safety rules, branch protections, and
required playbooks override generic examples in the canonical Sprint skill.
In particular, do not push directly to `main`, bypass required independent
review, or use a direct-merge integration path when the repository requires a
PR. Adapt the approved-card integration step to the repository's authorized
landing flow and keep the board truthful about what has and has not landed.
