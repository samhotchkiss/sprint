---
name: sprint
description: Run or resume a Sprint board from a live Codex session, including event draining, card dispatch, worker supervision, evidence handoff, and verdict integration. Use for "start a sprint", "resume the sprint", "open the board", or requests to check, dispatch, batch, or tend Sprint cards.
---

# Sprint for Codex

This is the Codex host adapter for Sprint. The board is state only; this live
Codex session is its brain.

## Portable boards: preserve workers across host changes

On a board using `sprint-handoff`, the main session and task workers are
independent tmux sessions. All messages to them go through `tmux-send`, including
follow-ups and recovery. Do not use host-native Agent/SendMessage equivalents on
these boards. This rule overrides the legacy host mapping below.

Read the current card assignment and recorded pane before launching anything.
A worker absent from Codex's internal agent tree may still be working in tmux;
switching from Claude does not make it dead. Preserve its worktree, branch and
executor. Keep the board's default builder even when the coordinator provider
changes. Never silently substitute Codex workers because the coordinator is Codex.

Use `docs/OWNER-SWITCH.md` for the acknowledged handoff. Start from the saved
orchestrator cursor, read pending events and existing card timelines, and do not
reset a dispatcher to bypass an ownership mismatch. A receipt acknowledging the
handoff does not acknowledge unread board events.

For persistent main-session ingress use `bin/sprint-dispatch-service install
--project-root /absolute/project` after the handoff registers this pane. End idle
turns; the operating system restarts a crashed watcher without model polling.

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
| Monitor | `sprint-dispatch`, outside the model turn |

Codex task names accept underscores, not the board's hyphens. Use
`sprint_card_<n>`, `sprint_batch_<id>`, and `sprint_review_<n>` as collaboration
task names while keeping `sprint-card-<n>`, `sprint-batch-<id>`, and
`sprint-review-<n>` as board-facing agent names. Record the returned Codex
agent id or canonical task path in a collapsed card-note detail so a compacted
or resumed orchestrator can recover the mapping from the board.

Before relaying to a worker, call `list_agents`. Send to a running worker; use
`followup_task` for an idle worker. A worker missing from the current agent tree
is dead for recovery purposes; follow the canonical fresh-agent procedure.

## Event-driven ingress: end idle turns

Use `bin/sprint-dispatch`, the deterministic watcher described in the canonical
skill. It owns polling and `tmux-send`; it makes **no model calls**. There is no
Codex `Monitor` emulation, yielded-tail loop, timed `functions.wait`, or
`write_stdin` polling loop for ingress. Never keep a Codex turn alive merely
because a worker or test process is running.

After confirming ownership and registering this session's exact pane:

```bash
"$SPRINT_REPO/bin/sprint-dispatch" start --project-root "$PROJECT_ROOT" --target "%5"
```

Replace `%5` with the pane returned by tmux, never a guessed address. Read the
startup result. Do not claim the watcher is running if startup failed. After
handling the available event batch and persisting the orchestrator cursor,
**end your turn**. Workers must report decisions/results through the board so
the watcher can wake you. Stop the dispatcher on handoff or End Sprint; do not
leave a second ingress listener attached. The watcher will stop on target
ownership changes without claiming the new session.

If no tmux target exists, report that unattended wakeup is unavailable. Handle
the current batch and return; do not replace the missing channel with paid
model polling. The user can resume manually or set up a dedicated tmux session.

### Small dispatcher, separate specialists

For a newly created dedicated routing session, prefer `gpt-5.6-luna` with low
reasoning. Do not silently change the user's active session model. The watcher
itself is ordinary code and needs no model configuration or API credentials.
Keep routing context to the incoming event, relevant card, worker assignment,
and applicable standing instructions. Do not fork a whole coordinator history
into workers. Use stronger agents for ambiguous decisions, complex work, and
review. The dispatcher should assign once, then wait outside the model; worker
completion, a question, or failure is the next reason to wake.

Bound each assignment with an outcome, allowed scope, and time/usage limit.
No repeated “status?” follow-ups. No automatic review loop without new changes
or evidence. When a worker finishes, handle its result once; do not turn a
finished specialist into a permanent polling assistant.

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
