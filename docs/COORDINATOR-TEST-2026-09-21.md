# Same-board coordinator switch

Sam plans to test Claude → Codex around **09:05 MDT on September 21**.
The Yunagi board stays at its existing address and keeps its current name,
cursor, cards, worker assignments, worktrees, and Grok builder default.

## Acceptance

| Check | Evidence |
| --- | --- |
| Crashed watcher restarts without a model call or duplicate wake | Demo: PID 30410 → 30876; cursor 20 → 20; deliveries 0 → 0. `/tmp/sprint-main-crash-rehearsal.json` |
| Signal termination restarts; explicit stop stays stopped | Process tests in `tests/test_sprint_dispatch.py` |
| Two ingress owners cannot run together | Shared `coordinator-owner.lock`; regression test |
| Old main heartbeat cannot impersonate task-service ownership | Actual server class test |
| Claude → Codex keeps unread messages | Demo: message 27 received one reply, event 30; Codex cursor advanced from 20 to 29. `/tmp/sprint-handoff-demo-acceptance.json` |
| Worker remains alive and assigned | Claude pane %114 closed; worker fixture PID 31727 stayed alive in %134; card 1 and Grok default unchanged. This checks process lifetime, not a product build. |
| Actual shutdown without a graceful source receipt | Rehearsal pending |
| Live Yunagi switch | Reserved for Sam's test |

## Before the live test

1. Verify the reviewed revision is on `origin/main`, and live Sprint reports
   `event_dispatch_supported: true`.
2. Read the current registered pane. Do not infer the target from an old session
   name. Start a fresh Codex session in the same Yunagi project and obtain its
   exact tmux pane ID.
3. Follow `OWNER-SWITCH.md`. Stop the old main watcher/Monitor, not worker panes.
   Keep all worker assignments. Use the recorded cursor; never `--reset` to get
   past a mismatched dispatcher target.
4. Start the main-session supervisor after the target acknowledges. A completed
   handoff must include the running target watcher, not just a receipt file.
5. Send a message during the switch and confirm one answer on the same board.
   Check one existing worker still runs and retains its executor/worktree.

The main-session watcher and the autonomous task coordinator are distinct ingress
modes. Do not run both on the same board. Changing the main agent must not change
which provider builds a card. Incoming email and unrelated Yunagi product work
are outside this infrastructure rehearsal.
