# Sprint — canonical build spec

A distributable Claude Code plugin: a kanban board web UI ("the board") sitting on top of a live
Claude Code session ("the session"). The user dumps feedback/work items at a tailnet URL; the
session dispatches subagents to work them; every board interaction passes through to the session
immediately. Named "sprint" because the same tool serves feedback sessions AND project sprints.

**Prime ruling (user, verbatim):** "I'm imagining this as a board that's just sitting on top of a
claude code session. so any interactions I perform are immediately passed through to the terminal
session underpinning it." → The server owns STATE only. The session is the only BRAIN: it alone
dispatches, resumes, nudges, batches, merges, escalates. The server never runs agents or makes
decisions.

Deployment reality: user's Mac Studio, online 24/7 (no sleep concerns), power outages happen —
recovery must be easy. The user always runs claude inside tmux.

## Components

| Piece | What |
|---|---|
| `bin/sprintd` | Single-file executable Python 3.9+ **stdlib only** (http.server + sqlite3). Subcommands: `start`, `stop`, `status`, `wait`, `doctor`. Owns all state. |
| `web/` | Vanilla JS/CSS/HTML SPA served by sprintd from disk. No build step, no CDN, no external requests. |
| `skills/sprint/SKILL.md` | The orchestrator brain: boot, resume, event-drain loop, dispatch, batching, liveness response, evidence gate, verdicts, end-sprint. |
| `agents/sprint-worker.md` | Worker subagent definition + reporting contract. |
| `bin/sprint-post`, `bin/sprint-ask`, `bin/sprint-ready` | Thin curl wrappers workers call (python3, no deps). Client-side validate before POST; fail with one named missing field. |
| `.claude-plugin/plugin.json` | Plugin manifest (name: `sprint`). |

## Data dir & server lifecycle

- Data dir: `<project-root>/.sprint/` — `sprint.db` (SQLite, WAL mode), `attachments/` (content-addressed
  `<sha256>.png`), `server.json` (`{pid, port, host, token, project_root, started_at}`), `server.log`.
- All paths derived from project root at boot. Zero hardcoded paths. `sprintd doctor` appends
  `.sprint/` to `.git/info/exclude` (not .gitignore — don't dirty shared repos).
- Bind: `tailscale ip -4` result + `127.0.0.1`, both. If no tailnet IP: bind loopback only and say so
  loudly. **Never 0.0.0.0, never a LAN interface.** Fixed default port **8377** (`--port` overridable);
  same port reused on restart so the URL survives reboots.
- Auth: random bearer token generated at first start, stored in `server.json`. Browser: `/?t=TOKEN`
  sets a cookie. API: `Authorization: Bearer` or cookie. Workers get the token via their brief.
- `sprintd start` is idempotent: if a live server owns the port (health check + token match), exit 0
  saying so. Handles stale PID files after power loss (PID recycling: verify the process is actually
  sprintd before believing the pidfile; else clean up and start).
- `sprintd doctor`: checks python3 ≥3.9, git, tailscale (optional), prints one actionable line per
  problem. `--install-launchd` writes `~/Library/LaunchAgents/com.sprint.<hash-of-project-root>.plist`
  with KeepAlive so the board auto-recovers from power outages/reboots (macOS only; skip elsewhere).
- Upload cap: 25 MB per request, streamed to disk, clear 413 over that.

## Data model (SQLite, WAL)

- `sprints(id, opened_at, closed_at, title)` — a sprint is one bounded run of the board. Board shows
  the open sprint; past sprints listable.
- `cards(num INTEGER PRIMARY KEY AUTOINCREMENT, sprint_id, state, title, body, batch_id NULL,
  agent_name NULL, worktree NULL, branch NULL, bounce_count DEFAULT 0, pinned DEFAULT 0,
  dup_of NULL, long_running DEFAULT 0, created_at, updated_at)` — **`num` is global across sprints**;
  the UI renders `#num` and #num means the same card forever.
- `batches(id, sprint_id, agent_name, worktree, branch, created_at)`.
- `events(seq INTEGER PRIMARY KEY AUTOINCREMENT, card_num NULL, ts, actor, kind, payload JSON)` —
  the single append-only truth. `card_num NULL` = sprint-level (sidebar chat, session status).
  `actor ∈ {user, session, worker, server}`. `kind ∈ {submitted, state, chat, question, answer,
  progress, evidence, verdict, agent_silent, note, error}`. Card state and timelines are projections
  of this log. seq is the global cursor for ingress.
- `evidence(card_num, packet JSON, created_at)`.
- `cursors(name PRIMARY KEY, seq)` — the session persists its drain cursor here (`orchestrator`).
- `questions(id, card_num, text, options JSON NULL, answered_at NULL)` — answer idempotency: second
  answer to the same question id is a 409, surfaced gently in UI.

## Card states

`held → queued → triaging → in_progress ⇄ needs_you | blocked → ready → integrating → completed`
plus `rejected`, `failed`, `stale`, `duplicate`, `canceled`.

- **integrating**: user approved; the session is doing the real git work (rebase → gate → merge →
  prune). UI shows "merging…" on the card (still in the Ready column). Session then calls
  `POST /api/cards/:num/integrated` `{ok: true}` → `completed`, or `{ok: false, reason}` →
  back to `in_progress` with an `error` event (integration failure is NOT a review bounce —
  bounce_count does not increment). A card only reaches Done when its branch actually landed.

- **held**: captured, never dispatched (hold mode). **queued**: dispatchable, waiting on concurrency cap.
- **needs_you**: a question the user can answer fixes it. **blocked**: external wall (CI red, overlaps
  another card, dependency) — machine-named reason required; distinct column; nothing the user types
  fixes it; the session re-checks blocked cards periodically.
- **ready**: ONLY reachable via a validated evidence packet (server 422s otherwise — see gate).
- **failed**: agent died/unrecoverable; card shows last error + Retry (fresh agent, full timeline as
  brief, honestly labeled as a new agent). **stale**: no activity across a session gap.
- State transitions are server-validated (illegal transition → 409). Every transition appends a
  `state` event; history is free.

## HTTP API (JSON; all POSTs idempotent via optional `Idempotency-Key` header)

- `GET /` + static `web/` assets. `GET /healthz` (no auth).
- `POST /api/cards` `{text?, images?: [base64 png/jpeg], hold?: bool}` → card. Server stores
  attachments first, then the card+`submitted` event. At least one of text/images required.
- `GET /api/board` — open sprint, all cards w/ latest state + last event + queue positions + session
  liveness (see below). `GET /api/cards/:num` — full interleaved timeline + evidence + attachments.
- `POST /api/cards/:num/chat` `{text}` (user→card). `POST /api/cards/:num/answer`
  `{question_id, text}` — flips needs_you→in_progress optimistically.
- `POST /api/cards/:num/action` `{action: pin|unpin|cancel|hold|release|duplicate_of|retry|reopen}`.
  `reopen` is the user's undo for a card closed too early: any terminal state → `queued` with a
  "reopened" state event (409 on a non-terminal card). **Closing is a user verb — the session never
  puts a card in a terminal state on its own; work with no code change goes to `ready` with an
  answer-style packet and the user closes it.**
- `POST /api/cards/:num/verdict` `{verdict: approve|bounce|reject, notes?}` — approve: ready→integrating;
  bounce: ready→in_progress, bounce_count++; server emits event either way, session does the git work.
- `POST /api/cards/:num/integrated` `{ok: bool, reason?}` (session surface) — integrating→completed,
  or integrating→in_progress with an `error` event on failure.
- Worker surface (bearer token): `POST /api/cards/:num/events` `{kind: progress|chat|note|error,
  payload}`; `POST /api/cards/:num/question` `{text, options?}` (→needs_you);
  `POST /api/cards/:num/ready` `{packet}` (the gate); `POST /api/cards/:num/state`
  `{state: triaging|in_progress|blocked, reason?, title?}`; `{long_running: true, note}` flag via
  events to suppress the silence timer during legit long jobs.
- Session surface: `POST /api/sidebar` `{text, actor: user|session}`; `POST /api/batches`
  `{card_nums[], agent_name, branch}`; `POST /api/cards/:num/assign`
  `{agent_name, worktree, branch, title?}` — assigning a **queued** card also flips it
  queued→triaging in the same transaction (state event reads "assigned to sprint-card-N — picking
  it up"); assigning a card in any other state only records the agent and never regresses state;
  `POST /api/sprint` `{action: open|close|set_hold_mode, ...}`; `POST /api/cursors/orchestrator` `{seq}`.
- `GET /api/events?after=SEQ&limit=N` — the drain endpoint. `GET /api/stream` — SSE (browser),
  heartbeat comment every 15s, browsers auto-reconnect with Last-Event-ID.
- `sprintd wait --after SEQ [--timeout 60]` — CLI: blocks until events exist past SEQ or timeout;
  exit 0 = events waiting, exit 2 = timeout (relaunch me), nonzero-other = server unreachable.
  This is the session's ingress primitive (background task; its exit re-invokes the session).

## Liveness (auto — there is NO manual nudge button)

User verbatim: "i shouldn't need to hit the nudge button. if there's no update for 5 minutes, the
master session should get nudged and it should check on the subagent."

- Server timer: any `in_progress` card with no worker event for **5 min** (and `long_running` not
  set) → server appends `agent_silent` event (once; re-arm only after new worker activity). The
  session drains it, investigates the agent (SendMessage ping / transcript inspection), posts what it
  found to the card as a `note`, and acts: annotate long-running work (set `long_running`), restart a
  wedged agent, or flip to needs_you/failed.
- Cards show last activity + elapsed; UI ambers a silent card. No spinners anywhere.
- Session liveness: the session heartbeats by advancing its cursor / a `session` note on drain; if
  the server hasn't seen the orchestrator cursor move within 90s of pending events, `GET /api/board`
  reports `session: offline` and the UI shows one banner: "session offline — items will queue".
  Submissions/answers still accepted and queue.

## Evidence gate ("ready")

Server-side schema validation; missing/empty fields → **422 with the named missing fields**, card
stays in_progress, rejection appended to the timeline so the worker sees exactly why.

**Prime rule (user, verbatim): "we need to make sure there's a way for the human to easily validate
the fix without having to read the code. so, either before/after screenshots or a link to a staging
url for the branch."** Every ready card must be validatable with zero code reading.

Packet: `{claim (one sentence), diffstat, branch, test_cmd, test_result ("N pass, 0 fail" — counts,
never "tests pass"), validate (REQUIRED: 1-3 plain-English steps a human follows to confirm the fix
without reading code), ui_change (REQUIRED bool), screenshots?: [attachment refs], live_url?,
per_card?: [{card_num, claim, screenshots?}] }`.
- `ui_change: true` → `screenshots` REQUIRED (before/after, light+dark, from the worker's OWN
  worktree preview, never a shared dev server) and `live_url` strongly encouraged: the worker starts
  its preview bound to the tailnet IP (loopback fallback) on a deterministic port
  (`8400 + card_num % 100`; batches use batch id) and keeps it alive until the verdict; the card's
  "See it live" button links there; the session kills the preview and prunes on terminal state.
- Non-UI changes → `validate` carries the burden: an exact observable check (a command whose
  before/after output differs, a URL to hit, a behavior to try) — never "read the diff". For batches: `per_card` required, one entry per member card;
ready flips all members together; verdicts can approve the batch wholesale or bounce individual
member cards (bounced stay with the agent; approved subset merges).

Bounce discipline: `bounce_count == 2` → server tags the event `escalate`; session stops blind
retries and brings it to the user for co-design.

## Batching & hold mode

User verbatim: "I may say 'hey, I'm going to dump a bunch of issues — don't start working on them
yet' ... if I have 12 minor design issues ... preferable for them to be tackled by a single
subagent — we don't need 12 worktrees for 12 minor css issues."

- Hold mode: sprint-level toggle (UI switch + settable from sidebar). While on, new cards land
  `held`. "Go" releases them.
- Grouping is the SESSION's judgment at dispatch: it proposes batches over held/queued cards
  ("#131–#142 are all minor CSS — one agent, one branch"), the user confirms/redraws via sidebar or
  card actions. One batch = one subagent (`sprint-batch-<id>`) + one worktree + one branch; member
  cards keep individual timelines/states on the board and share an agent badge. Chat on a member
  card routes to the batch agent with that card's context.

## Sidebar (chat with the session)

User verbatim: "a sidebar where I can have an ongoing chat with the main session without the noise
of all the underlying work... 'hey, why have #123, #127 and #128 been blocked for so long?'"

- Sprint-level chat thread: only `actor ∈ {user, session}` events with `card_num NULL`. No worker
  telemetry. The session's replies may reference cards as `#N` — UI links them.
- The sidebar is the master session itself (same brain, one conversation), not a separate chat
  agent. Anything said there is as authoritative as typing in the terminal, and the session can ACT
  on it (unblock, re-batch, approve), not just answer.

## Orchestration contract (SKILL.md must encode)

1. **Boot** ("start a sprint"): run doctor → `sprintd start` → print URL (`http://<tailnet-ip>:8377/?t=…`)
   → open sprint via API → start the waiter (background Bash: `sprintd wait --after <cursor>`).
2. **Drain loop invariant**: on EVERY wakeup (waiter exit, resume, boot): relaunch the waiter FIRST,
   then `GET /api/events?after=<cursor>` until empty, act on each event, then persist cursor. The
   transport is a latency optimization; the cursor drain is the truth (at-least-once, dedupe by seq).
3. **Dispatch**: respect concurrency cap (default 3 agents incl. batches). Fetch-first
   `git worktree add` from origin/main (never the primary checkout, never a serving worktree); reap
   orphaned worktrees on boot. Deterministic agent names `sprint-card-<num>` / `sprint-batch-<id>`.
   Brief = card text + absolute attachment paths (workers Read PNGs directly) + server URL/token/
   card num + worker contract. Auto-split multi-complaint submissions into sibling cards (tagged,
   one-click merge-back) — split, don't ask.
4. **Answers/chat**: SendMessage to the agent by name (mid-run lands next turn; finished agents
   resume with transcript intact). NEVER SendMessage a terminal card's agent (worktree may be
   pruned) — terminal cards get notes or a fresh dispatch.
5. **agent_silent**: investigate (transcript/SendMessage ping), post findings to the card, act.
6. **Verdicts**: approve → rebase branch on main, run the repo's gate, merge, prune worktree, flip
   completed (via API). Bounce → SendMessage notes to the agent. Two bounces → co-design with user.
7. **Resume** (after crash/power loss): `sprintd start` (idempotent) → drain from persisted cursor →
   `task list`-equivalent via `GET /api/board` → reattach non-terminal agents by name; unresumable →
   `failed` with retry. Tell reattached agents to RE-VERIFY their worktree state before continuing
   (power cut mid-write leaves plausible-looking half-work). tmux note: the user always runs claude
   in tmux; document respawn (`tmux new -s sprint 'claude'` → `/sprint resume`) in README.
8. **End sprint**: close via API; summary card (shipped/bounced/open/rejected); board keeps serving
   read-only history.

## Worker contract (agents/sprint-worker.md)

Pre-allowed tool profile (no `rm`, no permission-prompting tools — a background worker must never
be able to freeze the session; treat "would prompt" as: post a `blocked` event and return). Report
via helpers: `sprint-post <num> progress "one-liner"` after each meaningful step; `sprint-ask <num>
"question" [--options json]` then END YOUR TURN; `sprint-ready <num> packet.json` (client-side
validates, then POSTs; on 422 fix and retry). First act on pickup: state→triaging + one-line
restatement ("I read this as: X") + a condensed ≤8-word `title` on that same state POST. Long jobs: set `long_running` with a note first. Work only in
your assigned worktree; one branch; never push to main; never touch other cards' files.

## UI (web/)

- Form factors (the ONLY three that matter): Linux laptop + Mac desktop (≥1200px: full kanban +
  docked right sidebar) and Samsung Z Fold 8 interior screen — 2448×1848 physical, ~2.5 dpr ⇒
  treat as **~980×740 CSS px landscape**: 3 columns + sidebar as slide-over, touch-sized targets
  (44px min). Vertical space is scarce on the Fold: compact card rows, independently scrolling
  columns. No narrow-phone layout work (a basic usable fallback is fine, not optimized).
- Columns: Held (only when nonempty) / Queued / In progress / Needs you / Blocked / Ready / Done
  (Done collapses to a count + list). Card face: `#num`, title, state age, agent badge (batch
  shared), last activity one-liner, amber-on-silence. needs_you cards render the question + inline
  answer box + quick-reply buttons (when options supplied) ON the card face.
  The title on the face is the **condensed** title (≤8 words, set by the session at assign time and
  refined by the worker at triage); the user's original submission is never rewritten and shows in
  full in the drawer.
- Card drawer: one interleaved timeline (status changes are system lines in the chat), chat input,
  evidence packet above the fold when ready (claim, diffstat, test counts, screenshot thumbs →
  lightbox, live URL), Approve / Bounce-with-notes / Reject.
- Submit box (top): textarea + paste-to-attach multiple images (thumbnails, removable) +
  `<input type=file multiple accept="image/*">` fallback + Hold toggle. Cmd/Ctrl+Enter submits.
- Sidebar: session chat thread, `#N` autolinks to cards, session online/offline dot.
- SSE-live throughout; optimistic UI with reconciliation; Last-Event-ID reconnect; one tab-title/
  favicon badge + one soft chime on flips to needs_you/ready (no repeat, no unread counters
  anywhere else). Light + dark via `prefers-color-scheme`, both first-class.
- Aesthetic: calm, dense, plain-English labels. No spinners (show last activity + elapsed instead).
  No held-count in any header/chrome.

## Packaging

Plugin root = repo root: `.claude-plugin/plugin.json` (name `sprint`), `skills/sprint/SKILL.md`,
`agents/sprint-worker.md`, `bin/` (sprintd, sprint-post, sprint-ask, sprint-ready — all executable,
python3 stdlib or POSIX sh only), `web/`, `README.md`. Install: `claude --plugin-dir` for testing,
`/plugin install sprint@<marketplace>` for distribution; `claude plugin validate` must pass. Deps:
python3 ≥3.9 + git; tailscale optional (loopback-only degrade); everything else stdlib.

## Non-goals (v1)

Multi-user, public exposure, auto-dup-detection, priority pickers/drag-reorder, batch blind-approve,
session summaries beyond the end-sprint card, phone-portrait optimization, Monitor-based ingress
(waiter first; Monitor is a later upgrade), agent-list integration (possible later mirror).
