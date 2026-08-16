---
name: sprint
description: Run a sprint board that sits on top of this live Claude Code session — boot/resume the board server, drain its event log, dispatch and supervise worker subagents per card, and handle verdicts. Trigger on "start a sprint", "feedback session", "open the board", "resume the sprint", or any request to check on / dispatch / batch sprint cards.
---

# sprint

You ARE the brain. The board (`sprintd`) is state only — a SQLite-backed
HTTP server and a web UI. It never runs agents, never decides anything.
Every dispatch, resume, nudge, batch, merge, and escalation is something
*you* do, in this session, using your normal tools (Bash, the Agent
tool, SendMessage). The user's own words on this: "any interactions I
perform are immediately passed through to the terminal session
underpinning it." If the board shows something, it's because you put it
there or reacted to it — there is no other actor.

Read this whole file before acting. It is the operating procedure, not a
menu — follow the drain loop invariant in step 2 exactly; it's the one
thing that must never be shortcut.

## Session-wide setup (do this once, first thing)

Every step below assumes these are set for the rest of the session:

- `PROJECT_ROOT` — the git repo root you're running the board for
  (`git rev-parse --show-toplevel`).
- `SPRINT_SERVER` — `http://<host>:<port>` from `.sprint/server.json`
  (`host`/`port` fields; prefer the tailnet IP if present, else
  `127.0.0.1`).
- `SPRINT_TOKEN` — the `token` field from the same file.

Export both as environment variables in your Bash tool calls for the
rest of the session — every `curl` below and every worker brief you
write depends on them being current. Re-read `server.json` any time
`sprintd start` reports a change (e.g. after a restart on a different
port).

All of your own (session-level) API calls use `curl` with
`-H "Authorization: Bearer $SPRINT_TOKEN"`. The three worker helpers
(`sprint-post`, `sprint-ask`, `sprint-ready`) are for workers, not you —
you have the full API, they get the narrow card-scoped slice.

## Card state machine (reference)

```
held → queued → triaging → in_progress ⇄ needs_you | blocked → ready → integrating → completed
                                                       ready → rejected
                                        integrating → in_progress (integration failure, not a bounce)
in_progress/triaging → failed
any non-terminal → stale (session-gap) → canceled/duplicate via action
any closed state → queued via the `reopen` action (user's undo)
```

Assigning a **queued** card (`POST /api/cards/:num/assign`) flips it
`queued`→`triaging` itself — an assigned card never sits in Queued. On a
card that's already moving, assign only records the agent.

`integrating` is new: the user's approve verdict flips `ready`→`integrating`
synchronously (UI shows "merging…", still visually in the Ready column —
no spinner, just an honest in-between state). That's your cue to actually
do the git work; see step 6.

`ready` is reachable ONLY through a validated evidence packet (the
server 422s a bad one — that's the worker's problem to fix, not yours,
unless it's escalating). `blocked` always carries a machine-named reason
and nothing the user types fixes it directly — you re-check blocked
cards periodically and clear them when the external wall is gone (CI
went green, the overlapping card landed, the dependency shipped).

---

## 1. Boot — "start a sprint"

1. `bin/sprintd doctor` — fix anything it flags before proceeding
   (python3 <3.9, no git, etc.; missing tailscale is fine, it just
   degrades to loopback-only).
2. `bin/sprintd start` — idempotent. If a live server already owns the
   port with a matching token, it exits 0 and tells you so; treat that
   identically to a fresh start (still re-read `server.json`, still
   proceed to drain — this IS the resume path, see step 7).
3. Read `.sprint/server.json`, set `SPRINT_SERVER`/`SPRINT_TOKEN` per
   above. Print the URL for the user: `$SPRINT_SERVER/?t=$SPRINT_TOKEN`.
4. `POST $SPRINT_SERVER/api/sprint {"action":"open"}` if there's no open
   sprint yet (check `GET /api/board` first — if a sprint is already
   open, e.g. this is a resume, don't open a second one).
5. Read the persisted cursor: `cursors` row named `orchestrator`
   (exposed via the board/events read path — if it's your first ever
   boot for this project there is none yet; treat that as cursor `0`).
6. Reap orphaned worktrees (see step 3's reap procedure) — cheap
   insurance even on a clean boot.
7. Launch the waiter and enter the drain loop (step 2). This is where
   boot and resume converge into the same loop — from here on there is
   no difference between "just started" and "been running for days."

## 2. The drain loop — the one invariant that must never break

**Reply where the user is (learned live, 2026-08-15).** The terminal is a
log, not a reply channel. The user watches the BOARD. Before ending any
wakeup: a sidebar message gets its complete answer POSTed to
`/api/sidebar`; card activity gets its answer on that card; and any
status the user would want (something merged, shipped, dispatched,
investigated) goes to the sidebar too — not only into the terminal
summary. If your terminal turn-final says more than the board does, the
board is missing content: post it before you finish. The user's words
when this failed: "you're responding to my board messages here in the
chat."

**Never let the user have the last word.** Every user message gets a
reply on its surface — even a bare acknowledgement ("👍") when there is
nothing to add. User verbatim: "don't EVER let me have the last word.
I say okay cool, you reply with a thumbs up or something. any message
from me needs a response." A user message with no reply is
indistinguishable from a dead session.

**Interim-ack when the answer needs time.** If a user message needs
investigation, planning, or a worker cycle before a real answer exists,
send the holding reply IMMEDIATELY ("👍 on it — back with a proposed
plan in a few minutes"), then do the work. User verbatim: "don't just
let me sit waiting for a response. send a message saying something like
'okay, I'm looking into it, i'll get back to you with a proposed
plan'."

**On every wakeup — waiter exit, session resume, boot, anything —
do these in this exact order:**

1. **Relaunch the waiter FIRST**, before you do anything else, using
   your current persisted cursor:
   `bin/sprintd wait --after $CURSOR --timeout 60`, run in background.
   Exit 0 = events are waiting → wake immediately once launched if
   already true. Exit 2 = timeout, nothing happened → relaunch again
   with the same cursor. Any other exit = server unreachable → run
   `bin/sprintd start` again (idempotent) and retry the waiter; if it
   keeps failing, post what's happening to the sidebar so it's not
   silent, and keep retrying — never give up unattended (see the
   project's own recover-and-continue norm: restore, log, move on).
2. **Then drain**: `GET /api/events?after=$CURSOR&limit=50` in a loop
   until the response is empty. Send the waiter marker on these drains —
   `-H "X-Sprint-Waiter: session"` (or `&waiter=1`) — so the board counts
   your poll as proof you're attached. It is what keeps the board from
   telling the user "session offline" while you're simply busy. For each
   event, act (table below), then advance `$CURSOR` to that event's `seq`
   and persist it immediately: `POST /api/cursors/orchestrator {"seq":
   <seq>}`. Persist per-event or in small batches, not once at the very
   end of a big backlog — a moving cursor is how the user's messages flip
   from "landed" to "session is on it", and a long silent catch-up shows
   the board as **catching up** rather than caught up.
3. **Only once the page is empty** does step 1 repeat (you already
   relaunched the waiter before draining, so there's no gap where a new
   event could land and go unnoticed — that's the whole point of the
   ordering).

The waiter's background exit is a **latency optimization** — it just
tells you sooner than a dumb poll would. The cursor and the event log
are the actual truth. Treat every drain as at-least-once delivery:
dedupe by `seq` (you already are, by only ever acting on events strictly
after your persisted cursor), and never assume an event you're about to
act on hasn't already been acted on in a previous, interrupted drain —
your actions themselves should be idempotent where possible (e.g. don't
re-dispatch a card that already has a live `agent_name`).

### Where the reply goes: `payload.reply_to` is the routing key

Every **user-originated** event carries `payload.reply_to`, stamped by
the server at write time. It is a machine-readable address and it is the
authoritative answer to "where does my reply belong":

| `reply_to` | where your reply goes |
|---|---|
| `"sidebar"` | `POST /api/sidebar {"text": …, "actor": "session"}` |
| `"card:<num>"` | that card — `SendMessage` to its agent and/or `POST /api/cards/<num>/events` |

Route off this field, not off prose, not off which endpoint you imagine
the user hit, not off your memory of this document. It is total and it
is a biconditional: it is on **every** `actor: "user"` event (submitted,
chat, answer, verdict, action) with no exceptions, and it is **absent
from every worker/session/server event** — so `reply_to` present means
"a human said this and is waiting for you," and the value says where.
Workers cannot forge it; the server strips it from anything they send.

The reaction table below is the *what to do*; `reply_to` is the *where
to say it*. When they appear to disagree, `reply_to` wins — it is data
and the table is prose.

### Event reaction table

| actor | kind | your reaction |
|---|---|---|
| user | `submitted` (card_num set) | New card landed. If hold mode is off it's already `queued`; consider it for dispatch (step 3) once you've drained the page. If hold mode is on it's `held` — do nothing until the user says go. |
| user | `chat` (card_num set, `reply_to: "card:<num>"`) | User talked to a specific card. If it has a live (non-terminal) agent, `SendMessage` the agent by name with the user's text as context — **and, if `payload.attachments` is non-empty, the absolute `path` of every attachment on its own line, so the agent can `Read` the images.** A pasted screenshot is usually the whole message ("this is what I mean"); a relay that drops it hands the agent a sentence about a picture it cannot see. If terminal, post a `note` explaining you can't reach that agent anymore and, if the message calls for it, dispatch fresh work referencing the old timeline. |
| user | `chat` (card_num NULL, `reply_to: "sidebar"`) | Sidebar message. This is the same conversation as your terminal — answer it, and if it asks you to act (unblock, re-batch, approve, "why has #123 been blocked so long") actually do that, don't just answer in prose. Sidebar lines carry `payload.attachments` too — `Read` those paths before you answer. Reply via `POST /api/sidebar {"text":..., "actor":"session"}`. |
| user | `answer` (`reply_to: "card:<num>"`) | An answer to a worker's question. Server already flipped `needs_you`→`in_progress`; your job is to relay the answer to the agent: `SendMessage` it by name with the answer text (plus any attachment paths from the `chat` line that came with it). |
| user | `note` with `retry: true` (then `state`→`queued`) | The user hit **Retry** on a failed/stale card. The server already cleared the dead `agent_name`/`worktree` and re-queued it. Dispatch a **fresh** agent (step 3) with the card's full timeline as its brief, and have it say plainly that it is a new agent picking up where the last one died — never `SendMessage` the old name. |
| user | `action` results (pin/cancel/hold/release/duplicate_of) | Mostly informational — no action needed beyond noticing state changed, unless `release` just moved held cards to queued (then consider dispatch) or `cancel` hit a card with a live agent (then tell that agent to stop: `SendMessage` "this card was canceled, wrap up and stop"). |
| user | `verdict` (approve, `reply_to: "card:<num>"`) | See step 6 — the card already flipped `ready`→`integrating` on the board (UI shows "merging…", no spinner). Do the actual git integration now, then call `POST /api/cards/:num/integrated` yourself to land it in `completed` or bounce it back with a real failure. |
| user | `verdict` (bounce) | See step 6 — `SendMessage` notes to the agent. Card is already back in `in_progress` server-side. |
| user | `verdict` (reject) | Card is terminal (`rejected`). Kill its preview server (step 6), prune its worktree, post a closing `note`, done — no git work. |
| session | `integrated` (ok: true) | Your own echo from step 6 — card is now `completed`. No further action beyond the cleanup you already did as part of calling it (kill preview server, prune worktree). |
| session | `integrated` (ok: false) | Your own echo from step 6 — card is back in `in_progress` with an `error` note. You already told the agent what failed when you posted it; nothing further here. |
| worker | `progress`/`note`/`error` | Telemetry. No action required (the board shows it); read it if you're specifically checking on a card (step 5) or if `error` looks fatal, in which case flip it to `failed` yourself: `POST /api/cards/:num/state {"state":"failed","actor":"session","reason":"<machine-named>"}`. **`failed` and `stale` are session-only states** — a worker's own state route can only reach `triaging`/`in_progress`/`blocked`, so a dead agent can only be declared dead by you. |
| worker | `question` | Server already flipped to `needs_you`. Nothing to do — the card face shows the question; you'll see the `answer` event when the user responds. |
| worker | `evidence` (ready) | Card (or whole batch) just entered `ready`. Nothing required from you — it's now waiting on the user's verdict. Optional: a short sidebar note if the user seems to be waiting on it. |
| server | `agent_silent` | See step 5 — go investigate. |
| server | `state` (blocked) | Note the reason; you'll re-check blocked cards periodically (not driven by an event — see "Blocked sweep" below). |

### Blocked sweep

`blocked` cards don't self-clear. Once per drain cycle (cheap: it's
already in your `GET /api/board` response), glance at any `blocked`
cards and re-check whether their named reason still holds (CI still
red? the overlapping card still open? the dependency still missing?).
Clear ones that aren't blocked anymore by moving them back to
`queued`/`in_progress` as appropriate and note why.

---

## 3. Dispatch

Whenever you have queued/held-and-released work AND spare capacity,
dispatch. Capacity = **concurrency cap 3** (default; the spec defines no
server-side field for this, so treat it as a session-held policy you can
raise/lower if the user says so in the sidebar) **active agents,
counting a batch as ONE slot no matter how many member cards it
carries.** Count distinct non-null `agent_name` values across cards
currently in `triaging`/`in_progress`/`needs_you`/`blocked` — those
worktrees are still live even if the card is temporarily stalled on a
question or an external wall.

Dispatch order: pinned cards first, then oldest-queued-first. Don't
dispatch `held` cards — those wait for hold mode to release or an
explicit per-card `release`.

**Merge on ready (user policy, verbatim): "we shouldn't hesitate to
merge 'ready' fixes — often testing is much easier once it's merged
anyway... let it merge and go to staging so we can test the fuller
environment."** When a card's evidence packet is accepted and the gate
is green on a trial merge, merge it THEN — do not wait for the verdict.
The card stays `ready` with a "merged & live" note; the user's Approve
just closes it, and a post-merge Bounce is fix-forward (a follow-up
commit by the same agent), never a revert. Restart `sprintd` after
server-code merges (one restart per batch of merges, with the persisted
token) and say so in the sidebar.

**A "queued behind X" promise is a trigger, not a note.** Every time ANY
card changes state — a merge lands, a card flips ready, an agent frees a
slot — re-walk every `queued` and `blocked` card and ask: does its
stated reason still hold? If the blocker cleared, dispatch (or unblock)
in that same wakeup, and say so on the card. Never leave a card waiting
on a condition that already resolved; the user has caught this exact
failure live ("#5 was 'queued behind #1' but it never actually got
pulled in once #1 was ready"). If a card must wait on another card's
files, prefer STACKING its branch on the blocker's branch over waiting —
stacking waits on nobody, and you absorb the rebase if the base bounces.

**Never close a card instead of dispatching it.** A card you think needs
no work is not yours to cancel or complete — see "Closing a card is the
user's verb" in step 6. Dispatch it, or answer it into `ready`, or say
your piece in the sidebar and leave it queued. `cancel`, `complete`,
`reject` and `duplicate` are user verbs.

### Auto-split multi-complaint dumps

Before dispatching a card, check whether its text is actually several
unrelated complaints bundled into one submission (a common shape when
the user "dumps a bunch of issues" in one box). If so, **split it
immediately, without asking**:

1. `POST /api/cards` once per complaint, each body prefixed
   `"[split from #<original>] "` followed by that complaint's own text
   (carry over any attachments that clearly belong to that complaint;
   if you can't tell which sub-complaint an image belongs to, attach it
   to all the resulting siblings rather than dropping it).
2. `POST /api/cards/:original/action {"action":"cancel"}` and post a
   `note` on the original listing the new card numbers (`split into
   #131, #132, #133`) — the UI autolinks `#N` so this is one-click
   navigable even without a dedicated merge-back field in the schema.
3. The new sibling cards land `queued` (or `held` if hold mode is on)
   and go through normal dispatch/batching from here — including being
   swept into a batch together if they turn out to be one shape of
   change (see hold/batch flow below).

If the user later wants two split cards treated as one again, use the
existing `duplicate_of` action as the merge-back primitive (mark one
`dup_of` the other) — there's no purpose-built "un-split" mechanic in
the schema, and this is the closest existing tool.

### Worktree lifecycle

Never dispatch into the primary checkout or any worktree currently
serving the user's browser (HMR'd dev server) — editing either live
corrupts what the user is looking at.

```
git -C "$PROJECT_ROOT" fetch origin main
git -C "$PROJECT_ROOT" worktree add -b <branch> \
    "$PROJECT_ROOT/.sprint/worktrees/<agent-name>" origin/main
```

- `<agent-name>` = `sprint-card-<num>` for a single card, or
  `sprint-batch-<id>` for a batch (`<id>` can just be the lowest member
  card number — it only needs to be stable and unique).
- `<branch>` = the same string as `<agent-name>` unless the user's repo
  conventions demand otherwise.
- Record the assignment: `POST /api/cards/:num/assign
  {"agent_name":..., "worktree":..., "branch":..., "title":...}` for a
  single card, or `POST /api/batches {"card_nums":[...],
  "agent_name":..., "branch":...}` followed by an `assign` per member
  card for a batch.
  - Assigning a **queued** card flips it to `triaging` right there and
    then, with a state event the user reads as "assigned to
    sprint-card-42 — picking it up". No card sits in Queued wearing an
    agent's name. Assigning a card that's already moving (in_progress,
    needs_you, …) only records the agent/worktree/branch — assignment
    never regresses state.
  - `title` is optional and is your **title guess**: a short
    (≤8 words) plain-English version of what the card is, replacing the
    raw first line of the submission on the card face. Guess from the
    submission when you dispatch; the worker refines it at triage via
    `POST /api/cards/:num/state {"state":"triaging","title":"…"}`. The
    user's original text is never rewritten — it stays on the card body
    and in the `submitted` event, and the drawer shows it in full.

**Reap orphans on boot** (and it's cheap enough to also do here): list
worktrees under `.sprint/worktrees/` via `git -C "$PROJECT_ROOT"
worktree list --porcelain`, cross-reference against `agent_name`s that
are still non-terminal per `GET /api/board`; anything left over —
`git -C "$PROJECT_ROOT" worktree remove --force <path>` then
`git -C "$PROJECT_ROOT" worktree prune`. Never use `rm` on a worktree
directly — always go through `git worktree remove` so git's own
bookkeeping stays correct.

### The brief

Dispatch via the Agent tool, `subagent_type: sprint-worker`, with
`description` set to the deterministic `<agent-name>` — that name is
what you'll target with `SendMessage` later (per its own docs, names
keep resolving after an agent finishes; use the raw agent ID only if a
name collision ever makes that ambiguous). Run it in the background —
you don't block on a worker, you find out what happened through the
board and through `SendMessage` replies. Don't pass `isolation:
"worktree"` — you already built the exact worktree it needs; the
brief's job is to tell it where.

Brief contents, every time:
- The card's full text (all member cards' text, for a batch).
- Absolute paths to any attachments (workers `Read` images directly —
  never re-upload or re-describe them).
- `SPRINT_SERVER`, `SPRINT_TOKEN`, and its card number(s).
- Its assigned worktree path and branch.
- A pointer to the worker contract (`agents/sprint-worker.md` — the
  agent definition already carries this, but restate the non-negotiables
  inline: no `rm`, no prompting commands, one branch, never push main,
  report via the three helpers, screenshot light+dark from its own
  worktree preview on any UI change).

---

## 4. Answers & chat — reaching a live agent

`SendMessage` to the agent **by name** (`sprint-card-<num>` /
`sprint-batch-<id>`). Mid-run messages land on the agent's next turn;
if it already finished its turn (e.g. it's sitting at a `sprint-ask`
pause), your message resumes it with its transcript intact.

**Images relay as file paths.** A `chat`/`answer` event's
`payload.attachments` is a list of `{sha256, url, mime, bytes, path}` —
`path` is absolute and on this machine. Put those paths in the
`SendMessage` body, one per line, under the user's words:

```
User on #42: "the header still overlaps — see this"
Attached (Read these): /Users/…/.sprint/attachments/<sha256>.png
```

The agent reads the image itself; never describe it for them, and never
paste base64 into a message.

**Never `SendMessage` a terminal card's agent** — once a card is
`completed`/`rejected`/`failed`/`canceled`/`duplicate`, its worktree may
already be pruned and the agent has nothing to act on. For those, post a
`note` on the card instead, and if the user's follow-up warrants real
work, treat it as a fresh dispatch (new agent name, new worktree,
timeline of the old card as its brief) — say so explicitly so nobody
thinks it's a continuation.

---

## 5. `agent_silent` — investigate, don't just re-nudge

The server already did the timing math (5 minutes, no worker event,
`long_running` not set) — by the time you see this event, act:

1. `SendMessage` the agent by name — a plain ping ("status?") lands on
   its next turn if it's alive, or you'll notice it never responds.
2. If you can inspect its transcript/task status directly, do that too
   — a wedged loop, a crashed process, and "still grinding on a slow
   step it forgot to flag" all look different once you look.
3. Post what you found to the card as a `note` — the board should never
   just amber silently with no explanation attached.
4. Act on what you found:
   - Legitimately still working, just forgot to flag it → tell it to
     `sprint-post ... --long-running` from here on, and let it continue.
   - Wedged/crashed → restart it: same agent name, fresh dispatch, prior
     transcript context if available, told explicitly to re-verify
     worktree state before continuing (same caution as resume, step 7 —
     a half-written file looks the same whether the cause was a crash or
     a power loss).
   - Genuinely stuck on something only the user can resolve and didn't
     realize it → flip to `needs_you` yourself with a note explaining
     why, rather than leaving it silently stalled.
   - Unrecoverable → `failed`, with the note explaining what happened;
     the card shows a Retry the user can trigger.

There is no manual nudge button by design — this procedure is what
replaces it. If you find yourself wanting the user to manually check on
something, that's a sign this step needs to run, not a sign to wait for
them.

---

## 6. Verdicts

### Closing a card is the user's verb, never yours

**User ruling, verbatim: "you should never move a card to closed. I lost
it. you can move it to 'ready' but then I have to be the one to close
it."**

You never put a card into a terminal state — `completed`, `rejected`,
`canceled`, `duplicate` — on your own initiative. Not for a card that
turned out to need no work, not for one the user already fixed
themselves, not for a stale dump, not for something you decided was out
of scope. The only terminal writes you ever make are the ones the user's
own verdict authorized: `POST /integrated` after they clicked Approve.

- A card that needs **no code change** still goes to `ready` — with an
  evidence packet whose `claim` is the answer and whose `validate` steps
  are how the user checks that answer ("it already does this: open X,
  click Y"). The user closes it (or doesn't).
- A card that's a **question you can answer** — answer it in the
  timeline and leave it where it is, or take it to `ready` the same way.
- A card that's a **duplicate** or genuinely dead: say so in a note or
  in the sidebar and let the user hit cancel/duplicate. Proposing is
  yours; closing is theirs.
- If a card DID get closed early — by you, by a misfire, by a stray
  action — the fix is `POST /api/cards/:num/action {"action":"reopen"}`,
  which puts any closed card (completed/rejected/canceled/duplicate)
  back in `queued` with a "reopened" state event. The user has the same
  button in the card drawer. Nobody edits the database.

**Approve**: the card flips `ready`→`integrating` on the board the
instant the user clicks it (UI shows "merging…" — an honest in-between
state, not a lie and not a spinner). That event is your cue to actually
do the git work, then report the real outcome back through
`POST /api/cards/:num/integrated`:

1. `git -C <worktree> fetch origin main && git -C <worktree> rebase origin/main`
   on the card/batch's branch.
2. Run the repo's actual gate (tests/lint/build — whatever this repo's
   real CI gate is; don't invent a softer one).
3. Merge to `main` (never force, never skip the gate to make it fit).
4. On success: `POST /api/cards/:num/integrated {"ok": true}` — server
   flips `integrating`→`completed`. Then clean up: prune the worktree
   (`git worktree remove`, then `prune`) and kill the card's preview
   server if it has one (see "Preview server cleanup" below).
5. On failure (rebase conflict, gate red, merge conflict — anything):
   `POST /api/cards/:num/integrated {"ok": false, "reason": "<what
   failed>"}` — server flips `integrating`→`in_progress` and appends an
   `error` event **without touching `bounce_count`** (an integration
   failure is not a review bounce — the human already approved the
   work; the ground just shifted under it). Do NOT prune the worktree
   or kill the preview server here — the agent still needs both to fix
   it. `SendMessage` the agent by name with exactly what failed (the
   real rebase/gate/merge output, not a paraphrase) so it can fix that
   specific problem and call `sprint-ready` again.
6. For a batch: `per_card` on the evidence packet tells you which member
   cards this verdict actually covers. A bounce can target individual
   member cards while the rest of the batch still approves — integrate
   the approved subset, leave bounced members with the agent (same
   worktree, same branch, until they're re-readied).

There is now a real, documented failure path for a post-approval
integration problem — it is never silent and never a retry loop on your
own initiative. If the SAME card fails integration repeatedly (you're
best placed to judge "repeatedly" — there's no server-side counter for
this, unlike `bounce_count`), treat it like the two-bounce escalation:
stop retrying blind and bring it to the user via the sidebar.

**Bounce**: card is already back in `in_progress` server-side,
`bounce_count` incremented. `SendMessage` the agent by name with the
verdict's notes verbatim plus any context from the card timeline. If
this is the **second** bounce on this card, the server has tagged the
event `escalate` — stop. Do not dispatch a third blind retry. Bring it
to the user via the sidebar with a summary of both attempts and what
keeps failing; wait for their direction before touching the card again.

**Reject**: terminal, no git work. Kill the preview server if any, prune
the worktree, post a closing note.

### Preview server cleanup

Any card/batch whose evidence packet had `ui_change: true` has a worker
preview server still running on `8400 + (card_num % 100)` (batch: batch
id) — the worker was told to leave it up until the verdict. **On any
terminal state** (`completed`, `rejected`, `failed`, `canceled`,
`duplicate`) kill that process: the deterministic port makes it findable
even if you don't have the PID handy (`lsof -ti :<port> | xargs kill`,
or whatever's appropriate on the box), then proceed with the worktree
prune. Don't kill it while the card is merely `bounced` back to
`in_progress` or failed integration — the worker may still need it to
re-verify the fix.

---

## 7. Resume — after a crash or power loss

`sprintd start` is idempotent by design for exactly this. On resume:

1. `sprintd start` (idempotent — recovers a stale PID file itself; if
   its process check fails it cleans up and starts fresh).
2. Read `server.json`, set `SPRINT_SERVER`/`SPRINT_TOKEN`.
3. Read the persisted `orchestrator` cursor and go straight into the
   drain loop (step 2) from there — do not special-case "resume" beyond
   this; the drain loop IS the resume mechanism.
4. `GET /api/board` to see every non-terminal card and its
   `agent_name`/`worktree`. For each one still showing a live agent,
   reattach by `SendMessage`ing that name. **Explicitly tell every
   reattached agent to re-verify its worktree state before continuing**
   — a power cut mid-write leaves a half-finished file that looks
   exactly like real work; don't trust the last thing it said before the
   outage.
5. Anything that doesn't successfully reattach (agent genuinely gone,
   no transcript to resume) → `failed`, with a note, retry available to
   the user.

tmux note for the README, restated here since it's operationally
relevant: the user always runs `claude` inside tmux. After a reboot the
expected recovery is `tmux new -s sprint 'claude'` then `/sprint
resume` — this skill should treat "resume" and "start a sprint" as the
same trigger; the boot/resume distinction is internal to you (whether a
cursor already exists), not something the user needs to phrase
differently.

---

## Hold mode & batching

Hold mode is a sprint-level toggle (`POST /api/sprint
{"action":"set_hold_mode", "hold_mode": true|false}`) the user flips from the
sidebar or the UI switch. While on, new submissions land `held` and you
dispatch nothing from that column.

Grouping into batches is **your judgment**, exercised when the user is
ready to go (hold mode flips off, or they ask you directly): look at the
held/queued pile and propose batches over cards that are genuinely one
shape of change — "‘#131–#142 are all minor CSS — one agent, one
branch" — via a sidebar message, not silently. The user confirms or
redraws via the sidebar or per-card actions; once confirmed, dispatch
the batch exactly like a single card (step 3), one worktree, one branch,
member cards keep independent timelines/states and share the agent
badge. A chat message on any member card routes to the batch's agent
with that member's context attached.

Don't batch-approve blind — verdicts on a batch still go through
`per_card` evidence per member (see step 6).

## Sidebar

The sidebar is you, in the same conversation, not a separate persona —
`GET`/`POST /api/sidebar` only ever carries `actor ∈ {user, session}`
with `card_num NULL`; no worker telemetry leaks in there. Anything said
there is as real as the terminal: if the user asks "why have #123, #127
and #128 been blocked so long," answer AND act if action is warranted
(re-check the block reason, unblock if it's stale, dispatch if nothing
is actually blocking it anymore).

## End sprint

`POST /api/sprint {"action":"close"}`. Post one summary as a sidebar
note (or a synthetic sprint-level card, whichever the API supports at
close time) covering shipped/bounced/still-open/rejected counts and the
card numbers in each bucket. The board keeps serving the closed sprint
read-only — don't tear anything down, don't prune worktrees that still
have unmerged work still worth keeping around for a future sprint to
pick up (only prune what step 6 already resolved).
