# Sprint dogfood — russ session, 2026-08-16

Running list of product feedback from live use (russ review sprint). Sam's ask:
"we should be able to track ops as well. As we're working on this, please keep a
running list for the sprint project so we can improve this product."

## Observations
1. **Port collision on second board.** `sprintd start` in a second project fails
   hard on the default port (8377 owned by ~/dev/sprint's own board); had to
   retry with `--port 8378` manually. Wanted: auto-pick a free port when the
   default is owned by a *different* project_root.
2. **No bulk-create / dry-run.** Importing N issues = N POSTs = 2N events and a
   flooded board before the user can say "no, stop" (happened live: 14 cards
   created against intent, then 14 cancel calls). Wanted: a propose/preview step
   or bulk create+cancel endpoints.
3. **Session-created cards attribute as `actor: "user"`.** Cards I created via
   the API showed `submitted` events as the user's. Card-create should accept an
   actor (or default to the bearer's identity).
4. **Ops cards are second-class.** Non-code work (mailbox reprocess) has no
   natural home: no worktree/branch, and the evidence-packet/preview flow
   assumes a code change. Wanted: an ops card flavor whose evidence is a log
   excerpt / readback rather than a diff+preview.
5. **Self-echo wakeups.** The orchestrator's own sidebar posts come back as tail
   events and wake it. Tail could suppress events whose author is the session
   itself (it already filters heartbeats/cursor moves).
6. **External agents can't be first-class assignees.** Work running as ordinary
   background agents (not sprint-workers) is only trackable by name-stamping
   `assign`; SendMessage-by-name doesn't reach them from the board's model. Fine
   in practice, but the board's "agent" concept and the session's agent registry
   don't line up 1:1.
7. **`long_running` is worker-only.** `agent_silent` fired on the two
   external-agent cards (expected — they emit no worker telemetry), but the
   session cannot set `long_running` (`bad_action`: only pin/unpin/cancel/hold/
   release/duplicate_of/retry/reopen). Session-posted notes don't count as
   worker activity, so these cards will re-amber every 5 min for their whole
   build. Wanted: session-settable long_running (or agent_silent suppression
   when the assignee is a declared external agent).
8. **Session-wide outage UX.** When the whole Claude session hit its API limit,
   every card ambered/stuck-nagged independently (#21 nagged at 15m/25m/55m/2h25m).
   The board had no way to know the *session* was down vs individual workers.
   Wanted: a session-offline banner (the waiter already detects it) that
   suppresses per-card nags while the session is gone, and one roll-up line on
   return. Recovery itself was clean: worktrees + event log made fresh-agent
   pickup straightforward.
9. **Cap raise is a session-side policy, not a board setting.** Sam said "raise
   the cap" — there is no server field for concurrency, so it lives only in the
   orchestrator's head and nothing on the board reflects the new number. A
   sprint-level `concurrency` field (settable from the sidebar) would make the
   policy visible and survive a session restart.
10. **`stuck` nags don't know about the cap.** 15 queued cards each nagged at
   15m and 25m while every worker slot was legitimately full. If the board knew
   the cap and the active count, "queued behind the cap (8/8 busy)" would be the
   honest message instead of "dispatch it or say why not" x15.
11. **The orchestrator will over-post to the sidebar unless the board carries
   status well.** Sam, verbatim: "why are you sending me so much here through
   this channel? the whole point of sprint is that the board should show me what
   needs my attention, and you're here for conversations or to raise when
   something needs me immediately." Root cause is partly product: card evidence
   is only visible after opening a card, long card notes are clipped in the API
   listing (~120 chars), and there is no "findings" surface on the card face —
   so a long diagnosis has nowhere to live except the sidebar. Wanted: card-face
   summary/finding field, un-clipped note rendering in the drawer, and maybe a
   per-card "needs your decision" flag that pulls just that card to the top
   instead of a sidebar message.
