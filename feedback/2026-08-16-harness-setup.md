# How the harness could better set up the sprint approach

Sam's ask (verbatim): *"add notes into a new sprint feedback doc on how the
harness can better set up this approach"*

Written from one live run: a russ sprint with 8 concurrent workers, two
mid-flight API outages, a 74-agent adversarial review, and ~15 cards. This is
about the **harness** (Claude Code, the sprint skill, the orchestration
contract) — the board product's own gaps are in `2026-08-16-russ-dogfood.md`.

---

## 1. Every worker heartbeat costs a full orchestrator turn

The single biggest tax. `sprintd tail` wakes the session on every worker
`progress` line. Each wake is a model turn whose only output is "routine,
cursor advanced." With 8 workers posting every few minutes, the orchestrator
spends most of its budget acknowledging telemetry it will never act on.

**What the harness could do:** let the ingress declare an *interest filter* —
wake on `question`, `evidence`, `error`, `state → ready/failed/blocked`, and
any `actor: user` event; batch everything else into a digest delivered with the
next real wake-up. `--user-only` exists but is too blunt: it also suppresses
`evidence` and worker `error`, which are exactly the events that need a
response. The missing tier is "wake me for decisions, summarize the narration."

## 2. Worker context is rebuilt from scratch, N times

Eight workers each independently discovered the repo layout, the test commands,
the "never pipe go test" rule, the DB naming trap, the vendored-russ-mail
relationship. That is eight identical explorations, paid for eight times, and
each one is a chance to get it subtly wrong.

**What the harness could do:** a per-project *worker preamble* — a cached,
versioned briefing (build/test/lint commands, known traps, repo topology,
worktree conventions) injected into every worker automatically, so the
orchestrator's brief only carries what is specific to the card. Today I paste
the same six warnings into every dispatch by hand, and the quality of a worker
depends on whether I remembered all of them that time.

## 3. Mid-flight death is common and recovery is manual

Two outages in one night (session limit, then credits). Each killed 3+ workers
mid-verification. Recovery worked, but only because the orchestrator knew to
treat the dead worker's leftovers as suspect — and that lesson was learned the
hard way in an earlier session, not supplied by the harness.

Both times the dead worker had done real work: commits landed, tests run,
findings discovered. Once, its uncommitted worktree changes were a *deliberately
planted mutation* mid-falsification; another time they were a **real orphaned
fix** worth keeping. Identical-looking states, opposite correct actions.

**What the harness could do:**
- **Checkpoint on every phase transition,** not just on request: what was
  committed, what is intentionally dirty and why, what is running in the
  background, what the next step is. A worker killed at any moment should leave
  a machine-readable note that says "the dirty files are a planted mutant" vs
  "the dirty files are unfinished work."
- **Auto-resume with the checkpoint,** so the orchestrator does not have to
  hand-write "you are a fresh agent, distrust the dead one's leftovers, here is
  what it claimed" every time.
- **Surface the outage as one event,** not as N independent worker deaths plus
  M per-card stuck-nags. During a session-wide outage every card nagged
  independently; the board had no way to know the *session* was gone.

## 4. Background work outlives the agent that started it

A worker started a 45-minute DB suite, went idle waiting, and got reaped. The
suite kept running; its log was still being written by a process whose owner no
longer existed. The next agent had to be told a killed run prints FAIL with
zero `--- FAIL` lines, and that a half-written log is not a verdict.

**What the harness could do:** treat "long-running command + waiting agent" as a
first-class state. The agent should be able to *park* on a command's exit and be
revived by it, without occupying a slot or looking silent — and the command's
exit status should be captured by the harness, not reconstructed from a log the
agent may or may not trust.

## 5. Concurrency is invisible policy

The cap lives only in the orchestrator's head. Nothing on the board reflects it,
nothing survives a session restart, and the queue's `stuck` nags do not know it
exists — 15 cards each nagged "dispatch it or say why not" while every slot was
legitimately full.

**What the harness could do:** make the cap a declared, queryable property of the
run, and let the scheduler own dispatch: the orchestrator declares intent
("these cards, this order, this cap"), the harness admits them as slots free.
Today the orchestrator is the scheduler, which is a poor use of a model.

## 6. Shared singleton resources collide silently

Two sessions shared one Playwright MCP browser; tabs were stolen mid-audit and
an auditor nearly attributed another session's page to its own run. Same class:
the default sprintd port was already owned by another project, and the failure
was a hard bind error rather than a graceful pick.

**What the harness could do:** scope singleton tools per session (or per agent)
by default, and make cross-session sharing opt-in. If sharing is unavoidable,
expose ownership so an agent can *tell* it is looking at someone else's tab
rather than inferring it from weirdness.

## 7. Cross-card collisions are found by workers, not by the harness

Two cards independently modified the same predicate. That was caught because one
worker volunteered a heads-up — not because anything checked. With 8 parallel
branches off one main, "who else is touching this file" is knowable
mechanically.

**What the harness could do:** compute the file-overlap graph across live
worktrees and warn on dispatch ("card 27 touches a file card 26 is editing"),
which is exactly the serialize-or-stack decision the orchestrator has to make
anyway. Migration numbers are the same problem in a narrower form and already
have a hand-maintained ledger.

## 8. The evidence-packet gate is the best thing here — extend it

`sprint-ready` rejecting an incomplete packet (missing diffstat, test_result,
screenshots) caught the orchestrator itself trying to file thin evidence twice.
It is a forcing function that works precisely because it is mechanical.

**What the harness could do:** extend the same idea to *claims*. A worker
asserting "mutation-proven" could be required to attach the RED output; a worker
asserting "suite green" could be required to attach the exit code and the
`grep -c '^--- FAIL'` count. Today those are honour-system prose, and the one
time a worker's mutant "survived" it was its own too-narrow test selection —
caught only because that worker happened to be honest about it.

## 9. Cost shape

One night: ~5M subagent tokens in the review workflow alone, plus 8 workers each
running full suites. Most of it was worth it; the waste was concentrated in
(a) telemetry wake-ups, (b) repeated context rediscovery, (c) re-running whole
suites after outages because partial results could not be trusted. All three are
harness-level fixes, not model-level ones.

## 10. What already works and should not be lost

- **Deterministic worker names** (`sprint-card-N`) surviving agent death, so a
  fresh agent can be pointed at the same card without inventing identity.
- **Worktree-per-card** — recovery after both outages was trivial because the
  work was on disk and on a branch, not in an agent's head.
- **The event log as the source of truth**, with a persisted cursor: no events
  were lost across two crashes and a server restart.
- **Card state machine forcing `ready` before `completed`**, with closing
  reserved to the user. It prevented the orchestrator from quietly closing its
  own work — a failure mode it has demonstrated before.

---

## 11. "Needs you" must be a card state, not a sentence in a chat

Sam, verbatim: *"when something needs me it needs to move to the needs you
column and have a clear question"* — and, on a card where I had written the
trade-off as prose in the body: *"there's no question for me in #39"*.

The failure was mine, but the harness made the wrong thing easy and the right
thing awkward:

- **The orchestrator has no natural path to `needs_you`.** `sprint-ask` is a
  *worker* helper, and the state machine only allows `in_progress → needs_you`.
  A decision the orchestrator itself needs from the user (a design trade-off, a
  scope question) has no home: the card is `queued` or `ready`, both illegal
  transitions, so the question falls back to the sidebar — which is exactly the
  channel Sam does not want status in. I had to assign the card to a fake agent
  and force it to `in_progress` just to be allowed to ask.
  **Fix:** let the session raise a question on a card in any live state, or add
  an explicit `blocked_on_user` transition available from `queued`/`ready`.
- **Prose in a card body is invisible as a question.** A body can contain a
  genuine "which way do you want this?" and the card face shows nothing. Only
  the typed `question` payload renders as an ask. Anything that needs an answer
  should have to be a typed question — the same forcing function the evidence
  packet already applies to claims.
- **A `ready` card can also need a decision.** Today `ready` means "waiting on
  approve/bounce". A card that is ready *and* carries an open question about
  scope has to become a second card (I filed #44 to ask about the kebab menu,
  because #16 was already `ready`). That is a workaround, not a design.

**The general rule this points at:** every path by which the system can want
something from the user should terminate in a card in the needs-you column with
a typed question and options. If a message to the human is the only way to
express it, the board is not carrying its own weight — and the orchestrator will
drift back to narrating in chat, because that is the only channel that always
works.
