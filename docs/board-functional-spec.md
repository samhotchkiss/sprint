# Sprint Board — Functional Spec (for design)

This describes what the board must *do* and *communicate*, screen by screen, so the visual design
can be dialed in. It is implementation-complete (a working vanilla-JS build exists at `web/`);
design may restyle anything but must not remove states, information, or honesty rules below.

## 1. What this thing is

A kanban board that sits on top of a live Claude Code terminal session. The user (one person —
this is single-user software) dumps feedback and work items into it; AI agents pick them up, work
them in isolated branches, ask questions, and present finished work for signoff. Every interaction
on the board passes straight through to the underlying session — the board is a pane of glass, not
an app with its own brain.

Design north star: **relief, not wow.** The user opens this to *stop* losing the thread across a
dozen parallel work items. Calm, dense, plain English. It should feel like a hired chief-of-staff's
status wall, not a SaaS dashboard.

## 2. Contexts of use (the only three that matter)

| Device | Viewport (CSS px) | Input | Layout |
|---|---|---|---|
| Linux laptop / Mac desktop | ≥1200 wide | pointer | Full kanban + sidebar docked right |
| Samsung Z Fold 8, interior screen | ~980 × 740 (2448×1848 @ ~2.5dpr) | touch | 3 columns visible, sidebar as slide-over, 44px min targets |
| Anything narrower | — | — | Usable fallback; explicitly NOT optimized |

The Fold is nearly square: vertical space is scarce. Card rows stay compact; columns scroll
independently; nothing important lives at the bottom of a tall fixed layout.

Light and dark themes are both first-class (`prefers-color-scheme`). Any design decision must be
checked in both — no dark-only tokens.

## 3. Page anatomy

Top to bottom:

1. **Topbar** — sprint title, session liveness dot (see §8), sidebar toggle (Fold). HARD RULE:
   no counts of any kind in the chrome. No "12 held", no unread badges, no numbers. Ever.
2. **Submit box** — always visible at top. Multiline text; paste (or drop, or file-pick) multiple
   images with removable thumbnails; a **Hold** toggle ("don't start yet"); ⌘/Ctrl-Enter submits.
   Text or images alone are each a valid submission (screenshot-only is common).
3. **Board** — columns: **Held** (only rendered when nonempty) · **Queued** · **In progress** ·
   **Needs you** · **Blocked** · **Ready** · **Done** (collapsed rail: count + expandable list).
4. **Sidebar** (docked ≥1200, slide-over on Fold) — chat with the session itself. See §7.
5. **Card drawer** — opens over the board from a card click; deep-linkable (`#/c/131`).

## 4. Cards

Every card is `#N` — a short permanent number the user speaks in ("why is #127 blocked?").
Numbers never recycle. Any `#N` in any text on the board autolinks to that card's drawer.

**Card face** (board view): `#N`, title (first line of the submission or agent's restatement),
state age ("in progress · 12m"), agent badge (batch members share one badge), last-activity
one-liner ("running frontend tests", "waiting on your answer"), pin indicator, image-count glyph
when attachments exist.

**Special faces:**
- **Needs you** cards render the agent's question ON the card face with an inline answer box and,
  when the agent supplied discrete options, quick-reply buttons (plus free text always). Answering
  must not require opening the drawer. This is the single most important interaction on the board.
- **Queued** cards show queue position and a single ordering control: pin-to-front. No priority
  picker, no drag-reorder.
- **Silent** in-progress cards (no agent activity ~5 min) turn amber. There is NO nudge button —
  the session notices on its own and posts what it found; the amber is a status, not a call to act.
- **Blocked** cards show the machine-named reason ("overlaps #131", "CI red") — these are cards
  nothing the user types can fix; visually quieter than Needs you.
- **NO SPINNERS anywhere.** Activity is always shown as last-action + elapsed time. A spinner
  cannot distinguish working from wedged, which is the exact anxiety this tool exists to kill.

**Full state → column map:** held→Held · queued→Queued · triaging, in_progress→In progress ·
needs_you→Needs you · blocked, failed, stale→Blocked area (failed shows last error + **Retry**;
stale is a card from a previous run surfaced for revival) · ready, integrating→Ready ·
completed, rejected, duplicate, canceled→Done rail.

## 5. The drawer (card detail)

One **interleaved timeline** — chat and status history are a single thread; state changes render
as quiet system lines ("→ in progress", "bounced with notes"). Never two tabs. Chat input at the
bottom; user messages show a delivery hint when the agent is mid-task ("delivered — agent will see
it next turn") rather than pretending instant delivery.

**Skim, then dig in.** Every entry is a one-liner you can scan down the whole history. An entry
that has more behind it — a test log, the reasoning, a stack trace — carries an optional expanded
`detail` and shows a quiet "more" toggle under the line: collapsed by default, opening in place,
nothing above it moving. Card faces and "last activity" only ever render the one-liner. Same
toggle in the sidebar thread, so the session can answer in a line and park its working underneath.

**Ready cards put the evidence packet above the fold**, in this order:
1. One-sentence claim of what changed.
2. **"Check it yourself"** — 1–3 numbered plain-English steps to validate the fix *without reading
   code*. This block is the heart of signoff; give it visual primacy.
3. Before/after screenshots (thumbnails → lightbox), light+dark where relevant.
4. **See it live ↗** — button to the branch's staging preview when available.
5. Diffstat + branch; exact test command with counts ("42 pass, 0 fail" — never "tests pass").

Actions on ready: **Approve** · **Bounce with notes** · **Reject**, in a bar pinned at the bottom
of the rail so a packet with six screenshots in it can never push them off the screen. Approve
flips the card to a calm "merging…" badge (still in Ready; the bar goes quiet) until the branch
actually lands → Done. A merge failure returns it to In progress with an explanation line —
visibly NOT the user's fault and NOT counted as a bounce. Bounce requires notes (they go straight
to the agent); the card shows its bounce count; after two bounces the board shows an escalation
line ("brought to you for co-design — no third blind retry").

**Work that shipped together is ONE card in review.** Several cards on one branch (a batch of 12
minor CSS fixes, six design cards from one agent) show up as a single entry in Awaiting review,
named after the work in it. Opening it fills the rail with an **outline**: the branch's claim and
its check steps once at the top, then one numbered section per member card with that card's own
claim, checks and screenshots. **One Approve at the bottom covers the whole unit** — you can say
yes to six changes in one click only because the outline above it just showed you all six.

Underneath, nothing changed: the single Approve issues the ordinary per-card verdict for every
member in order, so each card keeps its own verdict event, its own record and its own completion.
It says "approving 3 of 6…" in words and stops at the first failure, naming the card it stopped
at. A bounce works at either grain — send back one section with its own notes and the rest of the
unit stays yours to approve, or send the whole branch back at once. Member cards still exist for
tracking, chat and history; they just do not each demand a verdict, and they do not each appear in
the review list. A card on its own is a unit of one and behaves exactly as it always did.

## 6. Submission behaviors

- Burst-friendly: the user may paste 6 items in 90 seconds. Cards appear instantly (optimistic),
  numbered, and queue visibly under the concurrency cap (~3 agents).
- Multi-complaint pastes are auto-split by the session into sibling cards (tagged "from the same
  dump", one-click merge-back). The board never asks "should I split this?"
- Hold mode: while on, everything lands in Held, untouched. Releasing dispatches. Held exists so
  the user can dump 12 things and *then* let the session propose batches.
- Edits are append-only: a submission is never rewritten; "add a note" appends to the timeline.

## 7. Sidebar — chat with the session

An ongoing conversation with the master session itself, free of worker noise: only the user and
the session speak here. Canonical use: *"why have #123, #127 and #128 been blocked for so long?"*
The session can answer AND act (unblock, re-batch, approve) — replies reference cards as
autolinked `#N`. Visually distinct from card chat: this is the manager channel. Session
online/offline dot lives here (and topbar).

## 8. Liveness & notifications

- **Session offline** (terminal dead/asleep): one plain banner — "session offline — items will
  queue". Board stays fully readable and accepts submissions/answers; nothing is disabled. Cards
  pause; they never vanish.
- Notification ladder, complete: tab-title + favicon badge, plus ONE soft chime, when a card flips
  to Needs you or Ready. Suppressed while the tab is focused; cleared on focus. Nothing repeats.
  No other badges/counters exist anywhere.
- Reopening after a gap surfaces stale cards first, honestly labeled: reviving one dispatches a
  *new* agent briefed with the full timeline — the UI says so ("new agent, full history"), it never
  pretends the same mind woke up.

## 9. Voice & microcopy

Plain English, terse, honest, roughly an 8th-grade reading level: short sentences, everyday words,
no jargon unless the jargon IS the subject. This isn't only the static UI copy below — it's the same
bar for everything an agent writes onto the board (progress one-liners, evidence claims, validate
steps, titles, chat replies); see `agents/sprint-worker.md` and `skills/sprint/SKILL.md` for the
worker/session-facing version of this rule. Examples of the register:
- "waiting on your answer" not "pending user input"
- "merging…" not "processing"
- "delivered — agent will see it next turn" not a fake sent-checkmark
- "42 pass, 0 fail" not "tests passing ✓"
- "new agent, full history — not the same mind" on revived cards
- "units are computed from existing cards, not stored as new rows" not "unit is a PROJECTION over
  member cards, no synthetic DB card"
Plain isn't vague or dumbed down — exact counts, file names, and flags never get paraphrased away;
precision survives, only the ornament goes.
Never: exclamation marks, celebration states, empty-state illustrations with pep talk. An empty
board just says "nothing yet — drop something in."

## 10. Hard constraints (engineering realities design must respect)

- No external requests of any kind: no CDN fonts, no icon kits, no analytics. System font stack or
  bundled assets only. (Served over tailnet by a tiny local server; fully self-contained.)
- No build step: vanilla HTML/CSS/JS. CSS custom properties for theming are the design surface.
- Live via SSE with polling fallback; optimistic updates reconcile from the event stream —
  design should assume any state text can update in place at any moment.
- Auth is a token in the URL on first visit; a plain re-auth message on 401. No login screen.

## 11. Out of scope (v1 — do not design)

Multi-user anything · priority pickers / drag-reorder · batch blind-approve ("approve all"
without per-item evidence) · unread counters · session summary views beyond the end-of-sprint
card · phone-portrait optimization · onboarding tours.

## Reference

Current implementation screenshots (both themes, both sizes, mock data): `web/.screens/`.
Composed real-system screenshots: `web/.screens/composed/`. Full system contract: `SPEC.md`
(states, API, evidence gate) — the functional truth if this doc and visuals disagree.
