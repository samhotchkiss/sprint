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
| `bin/sprintd` | Single-file executable Python 3.9+ **stdlib only** (http.server + sqlite3). Subcommands: `start`, `stop`, `status`, `tail`, `wait`, `doctor`, `hub`. Owns all state. |
| `web/` | Vanilla JS/CSS/HTML SPA served by sprintd from disk. No build step, no CDN, no external requests. |
| `skills/sprint/SKILL.md` | The orchestrator brain: boot, resume, event-drain loop, dispatch, batching, liveness response, evidence gate, verdicts, end-sprint. |
| `agents/sprint-worker.md` | Worker subagent definition + reporting contract. |
| `bin/sprint-post`, `bin/sprint-ask`, `bin/sprint-ready` | Thin curl wrappers workers call (python3, no deps). Client-side validate before POST; fail with one named missing field. |
| `.claude-plugin/plugin.json` | Plugin manifest (name: `sprint`). |

## Data dir & server lifecycle

- Data dir: `<project-root>/.sprint/` — `sprint.db` (SQLite, WAL mode), `attachments/` (content-addressed
  `<sha256>.png`), `server.json` (`{pid, port, host, token, project_root, started_at}`), `token`
  (the bearer token, 0600, **deliberately outside server.json so it survives `stop`** — a restart
  that mints a new token logs every open browser out and breaks the printed URL), `server.log`.
- All paths derived from project root at boot. Zero hardcoded paths. `sprintd doctor` appends
  `.sprint/` to `.git/info/exclude` (not .gitignore — don't dirty shared repos).
- Bind: `tailscale ip -4` result + `127.0.0.1`, both. If no tailnet IP: bind loopback only and say so
  loudly. **Never 0.0.0.0, never a LAN interface.** Fixed default port **8377** (`--port` overridable);
  same port reused on restart so the URL survives reboots.
- Auth: random bearer token generated at first start, stored in `.sprint/token` (and mirrored into
  `server.json` while running). `start` reuses it across stop/start; `--token` forces a value,
  `--new-token` rotates. Browser: `/?t=TOKEN` sets a cookie. API: `Authorization: Bearer` or cookie.
  Credential precedence is `Authorization` → `?t=` → cookie, and ANY of them matching authenticates.
  **`?t=` outranks the cookie deliberately**: cookies are scoped by host and ignore the port, so every
  board on one machine shares a jar; preferring the ambient cookie made a valid link into a second
  board 401. For the same reason the cookie name carries the port (`sprint_token_<port>`), with the
  legacy bare name still accepted so existing browsers aren't logged out. A 401 on a *browser
  navigation* renders a short sign-in page pointing at the hub; `/api/*` keeps its JSON.
  Workers get the token via their brief.
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
  dup_of NULL, long_running DEFAULT 0, external_agent DEFAULT 0, work_kind DEFAULT 'code',
  created_at, updated_at)` — **`num` is global across sprints**;
  the UI renders `#num` and #num means the same card forever.
- `batches(id, sprint_id, agent_name, worktree, branch, created_at)`.
- `events(seq INTEGER PRIMARY KEY AUTOINCREMENT, card_num NULL, ts, actor, kind, payload JSON)` —
  the single append-only truth. `card_num NULL` = sprint-level (sidebar chat, session status).
  `actor ∈ {user, session, worker, server}`. `kind ∈ {submitted, state, chat, question, answer,
  progress, evidence, verdict, agent_silent, stuck, note, error}`. Card state and timelines are projections
  of this log. seq is the global cursor for ingress.
  **Every `actor: "user"` event carries `payload.reply_to`** — `"sidebar"` (sprint-level) or
  `"card:<num>"` — stamped by the server at write time, in the one place any event is written, so it
  is total by construction. It is the authoritative routing key for any orchestrator ("where does my
  answer belong"), replacing prose in SKILL.md. It is a biconditional: stamped on every user event,
  **stripped** from every worker/session/server event, and workers cannot forge it — so `reply_to`
  present means "a human said this and is waiting."

  **Actor attribution (who wrote it).** A card the SESSION filed over the API used to show its
  `submitted` event as the user's, so `reply_to` claimed a human was waiting behind the session's own
  writing. `POST /api/cards`, `/api/cards/bulk`, `/api/sidebar` and the per-card write routes
  therefore accept an optional `actor` from `{user, session, worker}` (never `server` — that is the
  board's own voice). It is honoured **only when the request is not from a browser**, decided off the
  headers page JavaScript is forbidden to set or remove on `fetch` — `Cookie`, `Origin`,
  `Sec-Fetch-Site`, `Sec-Fetch-Mode`, `Sec-Fetch-Dest`. The board's own SPA sends a bearer token
  exactly like a worker does, so the Authorization header cannot answer this; the forbidden-header
  list can, and cannot be forged away from the page. It fails CLOSED both ways: no claim means the
  route's own default (never an upgrade), and a browser's claim is dropped in favour of `user`. The
  biconditional above is untouched — a session-authored card still routes the user's later chat and
  verdict as `card:<num>`, because attribution says who WROTE a card, never who owns it.
- `evidence(card_num, packet JSON, created_at)`.
- `cursors(name PRIMARY KEY, seq)` — the session persists its drain cursor here (`orchestrator`).
- `questions(id, card_num, text, options JSON NULL, artifacts JSON NULL, answered_at NULL)` — answer
  idempotency: second answer to the same question id is a 409, surfaced gently in UI. `artifacts`
  is what a **decision request** hands over with its question (`{url?, attachments?, notes?}`) —
  NULL for a plain question, and added by the idempotent ALTER pass on boards created before it.

## Card states

`held → queued → triaging → in_progress ⇄ needs_you | blocked → ready → integrating → completed`
plus `rejected`, `failed`, `stale`, `duplicate`, `canceled`.

- **integrating**: user approved; the session is doing the real git work (rebase → gate → merge →
  prune). User verbatim: *"Why do these cards stay in 'needs you' once they're already
  approved?"* — so an approved card LEAVES Needs you / Awaiting review the moment the verdict
  lands and runs under **In motion / In progress** as the phase `merging` until its branch
  actually lands. Needs you means awaiting YOU; merging is the session's job. Session then calls
  `POST /api/cards/:num/integrated` `{ok: true}` → `completed`, or `{ok: false, reason}` →
  back to `in_progress` with an `error` event (integration failure is NOT a review bounce —
  bounce_count does not increment). A card only reaches Done when its branch actually landed.

- **held**: captured, never dispatched (hold mode). **queued**: dispatchable, waiting on concurrency cap.
- **needs_you**: a question the user can answer fixes it. **blocked**: external wall (CI red, overlaps
  another card, dependency) — machine-named reason required; distinct column; nothing the user types
  fixes it; the session re-checks blocked cards periodically.
- **needs_you vs ready — the user's definition, verbatim**:
  *"needs you is where we talk through things. review means the session genuinely thinks the card
  is 100% complete. needs you is that the card is waiting for my input before it can keep moving
  forward."* So an agent has **two** handoffs
  and they mean different things: an **evidence packet** (`sprint-ready` → `ready`) says "I believe
  this is done", and a **decision request** (`sprint-ask` → `needs_you`) says "I need you to
  choose/answer before I continue". Mockups to pick between, a design call, "which of these three",
  "is this the behaviour you meant" are all decision requests — never packets. A decision request
  carries **artifacts** so the choice can actually be made: `{url?, attachments?, notes?}` on the
  question (see the API + worker contract), rendered in the rail **above** the answer box.
  `sprint-ready` prints an advisory stderr notice — never a refusal — when a packet looks like a
  question in disguise (a `validate` step that is a question, a `claim` that is a question, an
  `options`/`artifacts` field a packet has no room for).
- **ready**: ONLY reachable via a validated evidence packet (server 422s otherwise — see gate).
- **failed**: agent died/unrecoverable; card shows last error + Retry (fresh agent, full timeline as
  brief, honestly labeled as a new agent). **stale**: no activity across a session gap.
- State transitions are server-validated (illegal transition → 409). Every transition appends a
  `state` event; history is free.

## HTTP API (JSON; all POSTs idempotent via optional `Idempotency-Key` header)

- `GET /` + static `web/` assets. `GET /healthz` (no auth) — carries `generation`, one id per server
  **process**, alongside pid/started_at/seq.
- `POST /api/cards` `{text?, images?: [base64 png/jpeg], hold?: bool, actor?: user|session|worker}`
  → card. Server stores attachments first, then the card+`submitted` event. At least one of
  text/images required. **`actor` is honoured only for a non-browser caller** — see "Actor
  attribution" below; a browser is always `user`.
- `POST /api/cards/bulk` `{items: [{text?, images?} | "text", …], hold?: bool, actor?}` → every card
  in ONE transaction. **`hold` defaults to `true`**: importing N issues used to mean N POSTs and a
  board flooded against intent (14 unwanted cards, then 14 hand-written cancels), so the held pile IS
  the preview and the user releases it. All-or-nothing — one bad item creates nothing — and capped at
  50 items (`413 too_many` over it). Returns `{ok, count, hold, state, card_nums, cards}`.
- `POST /api/cards/bulk-action` `{card_nums: [...], action: cancel|release|hold}` — the undo, in one
  call. Deliberately only the flood-control verbs. Every card must EXIST (404 for the whole call
  otherwise), then each is applied independently: `{ok, action, applied: [...], failed:
  [{card_num, error, message}], count}`. Same 50-card cap.
- `GET /api/board` — open sprint, all cards w/ latest state + last event + queue positions + session
  liveness (see below). `GET /api/cards/:num` — full interleaved timeline + evidence + attachments.
- `POST /api/cards/:num/chat` `{text?, images?: [base64 png/jpeg or data: URL]}` (user→card) — same
  attachment path as submission (magic-byte sniff, content-addressed dedupe, 25 MB cap); at least one
  of text/images required; stored refs land in the chat event's `payload.attachments` with a
  ready-to-use `url` (and the on-disk `path`, so a relayed message gives the agent something to Read).
  `POST /api/cards/:num/answer`
  `{question_id, text}` — flips needs_you→in_progress optimistically.
- `POST /api/cards/:num/action`
  `{action: pin|unpin|cancel|hold|release|duplicate_of|retry|reopen|long_running|external_agent}`.
  The last two are **session-only** (`403 session_only` from a browser — both turn the silence timer
  off, and the user has no way to know whether an agent is legitimately quiet); each takes
  `{value?: bool (default true), note?: str}`. `long_running` is a stretch; `external_agent` marks an
  assignee that emits no worker telemetry at all, so `agent_silent` never runs on that card and the
  session owns checking it.
  `reopen` is the user's undo for a card closed too early: any terminal state → `queued` with a
  "reopened" state event (409 on a non-terminal card). **Closing is a user verb — the session never
  puts a card in a terminal state on its own; work with no code change goes to `ready` with an
  answer-style packet and the user closes it.**
- `POST /api/cards/:num/verdict` `{verdict: approve|bounce|reject, notes?}` — approve: ready→integrating;
  bounce: ready→in_progress, bounce_count++; server emits event either way, session does the git work.
- `POST /api/cards/:num/integrated` `{ok: bool, reason?}` (session surface) — integrating→completed,
  or integrating→in_progress with an `error` event on failure.
- Worker surface (bearer token): `POST /api/cards/:num/events` `{kind: progress|chat|note|error,
  payload}`; `POST /api/cards/:num/question` `{text, options?, artifacts?}` (→needs_you) — a
  **decision request** is this same call with `artifacts: {url?: http(s), attachments?: [abs path |
  ref], notes?: str}`; attachments are ingested exactly like a packet's screenshots (read off disk,
  content-addressed, served back as `/api/attachments/...`), the whole payload is stored on the
  question and echoed on the `question` event, and the rail renders it above the answer box. A
  malformed/empty `artifacts` is a `400 bad_artifacts` naming the field;
  `POST /api/cards/:num/ready` `{packet}` (the gate); `POST /api/cards/:num/state`
  `{state: triaging|in_progress|blocked, reason?, title?}`; `{long_running: true, note}` flag via
  events to suppress the silence timer during legit long jobs.
- Session surface: `POST /api/sidebar` `{text?, images?, actor: user|session}` (images exactly as on
  card chat — one attachment path for every surface); `POST /api/batches`
  `{card_nums[], agent_name, branch}`; `POST /api/cards/:num/assign`
  `{agent_name, worktree?, branch?, title?, work_kind?: code|ops, external_agent?: bool}`
  (worktree/branch are optional: **ops work has neither**) — assigning a **queued** card also flips it
  queued→triaging in the same transaction (state event reads "assigned to sprint-card-N — picking
  it up"); assigning a card in any other state only records the agent and never regresses state;
  `POST /api/sprint` `{action: open|close|set_hold_mode, ...}`; `POST /api/cursors/orchestrator` `{seq}`.
- `GET /api/events?after=SEQ&limit=N` — the drain endpoint. `GET /api/stream` — SSE (browser),
  heartbeat comment every 15s, browsers auto-reconnect with Last-Event-ID. The stream opens with a
  named `hello` frame (`generation`, `started_at`, `cursor`, `head`) and every `cursor` frame carries
  `generation` too; neither carries an `id:` (they are not events and must never become
  Last-Event-ID). `/api/board` and `/api/events` carry `generation` as well, so the polling fallback
  sees a restart on the same terms as SSE.
- `sprintd tail --after SEQ [--user-only]` — CLI: the session's PRIMARY ingress. Holds one
  `/api/stream` connection open (carrying the waiter marker, so being attached is liveness) and
  prints exactly ONE compact JSON line per event to stdout —
  `{seq, card, actor, kind, reply_to, text (120 chars)}` — for a streaming monitor to wake on.
  `--after` catches up from the cursor first; `--user-only` narrows to `actor: "user"`.
  **`actor: "session"` events are suppressed by DEFAULT** (`--include-self` restores them): the
  session's own posts came back down its own ingress and woke it to read what it had just said,
  which is the same class of non-event as a heartbeat. The event still rides the stream and the
  cursor drain still sees it — only the wakeup is dropped.
  Heartbeats and cursor frames are consumed, never printed (they would wake the monitor for
  nothing); a generation change prints `{"restart": true, …}`. Reconnects with backoff and
  resumes from the last seq on a drop, printing nothing extra; after 60s unreachable prints ONE
  `{"error": "unreachable"}` line and keeps retrying. **Never exits on its own.**
- `sprintd wait --after SEQ [--timeout 60]` — CLI: the long-poll FALLBACK (Monitor unavailable, or
  a long-timeout crash-detection heartbeat beside the tail); blocks until events exist past SEQ or
  timeout; exit 0 = events waiting, exit 2 = timeout (relaunch me), nonzero-other = server
  unreachable (background task; its exit re-invokes the session).
- Either way the **cursor drain is the truth**: a wakeup is transport, and every wakeup re-drains
  `GET /api/events?after=<cursor>` at-least-once, deduped by seq.

## Reports — documents as first-class attachments (SHIPPED)

User verbatim: **"Some way built into sprint to embed reports."** His chosen shape, verbatim:
**"Yes, 1& 2, but not a rail.  A link in the header"**, refined to **"link should only appear once
there's a report within the sprint"**. An agent's real output is often a DOCUMENT — a findings
write-up, a comparison table, an audit — and flattening one into a one-liner destroys the document,
while pasting it into `--detail` destroys the timeline.

- **A report IS an attachment.** Markdown (`.md`) and standalone `.html` ride the same
  content-addressed store a PNG does (`<sha256>.md` / `.html` in `attachments/`), arrive in the same
  `payload.attachments` list, and are told apart by one server-set field, `doc ∈ {md, html}`. So
  every surface that already showed attachments shows reports: card chat, sidebar, submission, and
  evidence packets. There is no second pipeline.
- **The sniff is the honest one for text.** No magic bytes exist, so a report is: a known extension,
  strict UTF-8, and no NUL/control bytes (that is what a binary wearing a `.md` looks like). Cap
  **2 MB** — an order of magnitude under the request cap, because a report is prose, not a payload.
  `images:` stays png/jpeg only: nothing that ever worked starts accepting text.
- **Markdown renders SERVER-SIDE, in-house, with no dependency ever** — headings, lists (nested,
  with lazy continuation), fenced code, tables, blockquotes, rules, links, images, emphasis. The
  safety story is not a sanitizer: every character the author wrote is escaped **before a single tag
  exists**, so raw HTML inside a `.md` renders as its own characters. A `.md` containing `<script>`
  renders the characters `<script>`, inert. `javascript:`/`data:`/`vbscript:` link targets drop to
  plain text (the words survive; the link does not).
- **Author `.html` is never rendered, rewritten, or inlined.** It is served under
  `Content-Security-Policy: sandbox` (unique opaque origin, no scripts, no same-origin) AND displayed
  only inside an iframe whose `sandbox` attribute is **empty** — every restriction at once. Two
  independent walls, so one mistake is not a hole.
- **In a thread**: the existing skim/expand pattern. The document's own title (its first heading, or
  `<title>`) is the line you skim; the whole rendered page is behind the expand. A stable
  "Open full page →" link is on the row, not behind the expand.
- **The header link, NOT a rail.** A quiet `Reports` link beside Calm/Chaos and List/Board, drawn
  **only when the OPEN sprint has at least one report** — `/api/board` carries `reports` (a count
  scoped to the open sprint, never a lifetime total). It opens a library page listing this sprint's
  reports: title, source card `#N` (linked), author, date. Each row opens the report at a stable URL,
  `#/report/<sha256>.<ext>`, under the board's own auth. Both skins, Fold-friendly.
- API: `GET /api/reports[?scope=all]` (default = open sprint), `GET /api/reports/<sha>.<ext>`
  (rendered; `html` for markdown, `raw_url` + `sandboxed` for author HTML),
  `GET /api/attachments/<sha>.<ext>` (raw bytes). `reports` is accepted alongside `images` on
  `/api/cards`, `/api/cards/:num/chat`, `/api/sidebar`, worker events, and in an evidence packet.
- **Markdown is the recommended format; HTML is the fallback.** User verbatim, on the bounce:
  **"let's also advise agents that md reports are preferable to html. our html rendering isn't
  great"**. `.md` renders with the board's own typography in both skins; author `.html` is
  deliberately sandboxed and therefore unstyled, which is exactly why it looks worse. HTML support
  stays — a document that arrives already-HTML still has a home — but every place an agent reads
  (`agents/sprint-worker.md`, `sprint-post --help`, `sprint-ready --help`, this spec, the README)
  says write `.md`, and both helpers print a one-line stderr notice when an `.html` report is passed.
  A notice, never an error: the report still posts.
- Workers: `sprint-post <num> chat "summary" --report path.md` (repeatable, validated client-side)
  and a `reports` field in the packet. A `phase` refuses a report — a phase is about the present, a
  document is not. Documented in `agents/sprint-worker.md`.

## Hub — every sprint on this machine (SHIPPED)

User verbatim: "a landing page I can use if there are multiple sprints running on the same computer
at the same time to be able to switch between them. And it should be able to show me if there is
anything stuck in needs me on a different board." One Claude session == one sprint == one project
root; several run at once in different tmux windows.

- **Registry**: `~/.sprint/registry.json`, keyed by `project_root`, written atomically (tmp + fsync
  + rename, 0600) under an flock. `sprintd start` writes/updates its row
  (`{project_root, name (repo basename), host, hosts, port, pid, data_dir, started_at}`); `stop`
  removes it (pid-guarded, so a slow shutdown can't delete a newer server's row). Restarting the same
  board updates its row — never a second row for one project. `--registry` / `$SPRINT_REGISTRY`
  relocates the whole machine-wide state (registry + hub token + hub pidfile); tests use it so the
  real `~/.sprint` is never touched.
- **Entries are advisory.** The hub health-checks every row (`/healthz` + `project_root` match, then
  `GET /api/board` with the token read from that project's `.sprint/token`) and prunes a row only
  when BOTH proofs land: the pid is gone AND the port has been unreachable past
  `SPRINT_HUB_PRUNE_SECONDS` (default 15 min). Anything else stays and renders greyed —
  "unreachable — last seen Xm" — rather than disappearing.
- **`sprintd hub`**: fixed port **8300**, same tailnet+loopback bind rules as a board, idempotent
  start (health check + reuse), `--stop`, `--new-token`. Its own bearer token lives in
  `~/.sprint/hub-token` (0600) with the same `?t=` → cookie handshake, under a DISTINCT cookie name
  (cookies ignore the port; sharing the board's name would sign every board out). It is never
  unauthenticated — it is a keyring into token-guarded boards.
- **The page** is embedded in sprintd (not `web/`): self-contained, no external requests, calm dark,
  system fonts, deliberately outside the board's design system. One row per sprint: name, "N need
  you / N ready / N in motion", session dot (online/busy/offline), last activity age, "open board"
  link carrying that board's token. `needs_you > 0` sorts to the top with an amber left rail and the
  oldest open question's age ("stuck 22m"); longest wait first. 10s polling of `GET /api/hub`; no SSE.
- Started once per machine by hand; boards register themselves. No launchd parity yet.
- **From inside a board — the title switcher.** User verbatim: "When there are multiple sprints going
  on my box, the title should turn into a dropdown. Also, it should show a dot when another sprint has
  something waiting on me, and when I invoke the dropdown, it should show the dot next to the sprint
  that needs me." `GET /api/siblings` (board token; 401 without it) answers the hub's question from
  inside one board: every **live** sprint on this machine, each row `{name, url (signed with that
  board's own token), needs_you, ready, in_motion, blocked, queued, self, alive}`, plus
  `needs_you_elsewhere`. Same registry, same health checks (`/healthz` + `project_root` match, so a
  recycled port is never offered wearing the wrong project's name), and the same counter the hub
  renders — `board_rollup` is shared, so the two can never drift on what "2 need you" means. This
  board is always in the list flagged `self: true` and rolled up straight from its own DB, never over
  HTTP. Unreachable rows are **dropped**, not greyed: a hub row exists to tell you a board died, a
  menu row exists to be clicked. Cached 10s server-side (`SPRINT_SIBLINGS_TTL`).
  UI: >1 live board and the header title becomes a dropdown — one ≥44px row per sprint (name, its
  counts, a dot when that sprint has `needs_you > 0`, "here" on the current one), clicking navigates
  to that board's signed URL in the same tab, on the host you are already using. The title carries a
  dot when any OTHER sprint needs you; this board's own needs-you pile is already on the page behind
  it. One live board and the title stays the plain `<h1>` it is today. Polled every 30s; no SSE.

## Phases (what an agent is DOING, not what it last said)

User verbatim: **"But these states need to be better so it doesn't look like everything is
broken when it's not."** A card face carrying the last thing an agent typed is stale by
construction, and the silence amber that follows says "broken" about work that is fine.

- A worker declares a phase at every stretch of work — `sprint-post <num> phase "testing"
  [--expect 300|5m]`. Sugar for a `progress` event carrying `payload.phase` and optional
  `payload.expected_seconds`; not a new event kind. Phases are free-form strings from a
  recommended vocabulary (reading, coding, testing, capturing evidence, assembling packet,
  waiting); `--expect` for anything slower than two minutes.
- The server keeps NO phase column: the live phase is a projection of the log — the latest
  phase-carrying event since the card's last `state` event — so a state change ends the phase by
  construction. `/api/board` and `/api/cards/:num` carry `phase`, `phase_since`,
  `phase_expected_seconds`.
- **A declared phase suppresses `agent_silent` for as long as it claimed and not one second
  longer.** Declaring "testing, about 5 minutes" IS an act of liveness — it is the one thing the
  five-minute timer cannot otherwise know. Once the expected time elapses with no new event, the
  card ambers on the normal rule. A phase with no expectation shields nothing. Per-agent clock
  semantics are otherwise unchanged.
- UI: the card face (List row, Board card) and the rail head render a **phase chip on its own
  fresh clock** — `testing · 2m` — in place of the bare state age, and an overdue phase reads
  amber: `testing · 6m (expected 5m)`. The In-motion hairline drains over the phase's expected
  duration while one is live (still recency, never invented progress). Chip is one component over
  tokens, styled per skin (Calm pill, Chaos plate).

## Liveness (auto — there is NO manual nudge button)

User verbatim: "i shouldn't need to hit the nudge button. if there's no update for 5 minutes, the
master session should get nudged and it should check on the subagent."

- Server timer: any `in_progress` card with no worker event for **5 min** (and neither
  `long_running` nor `external_agent` set) → server appends `agent_silent` event (once; re-arm only
  after new activity). **A `session` event counts as activity** alongside a worker's: the session
  posting its findings on a card IS the check this event asked for, and re-nagging it five minutes
  later nags it about work it already did. (`server` events never count — the sweep's own reminders
  must not reset the clock they are complaining about.) The
  session drains it, investigates the agent (SendMessage ping / transcript inspection), posts what it
  found to the card as a `note`, and acts: annotate long-running work (set `long_running`), restart a
  wedged agent, or flip to needs_you/failed.
- Cards show last activity + elapsed; UI ambers a silent card. No spinners anywhere.

## Staleness sweep (auto — the board notices what nobody did)

User verbatim: **"We need some sort of auto sweep on the board to keep these from going stale."**
Three approved cards sat at "merging" for two hours because the session missed their approve
events. Nothing was silent and nothing was broken — the work was simply *owed and forgotten*, which
`agent_silent` (a clock on a working AGENT) cannot see.

- Second server timer beside the silence one: any card parked in a state someone owes an action on
  appends a `stuck` event (`card_num` set, `actor: "server"`) —
  `integrating` >10 min (the session owes `/integrated` — today's failure), `queued` with no agent
  >15 min, `blocked` >30 min since its last real activity (a re-check interval, so a note on a
  blocked card restarts it), `needs_you` unanswered >30 min (the UI chimed once at the flip; this
  lets the session re-notify in words), `ready` >24 h (gentle review reminder).
- Payload: `{state, stuck_for_seconds, threshold_seconds, text}` — `text` is a plain-English
  one-liner ("approved 12m ago, still merging — the session owes it a /integrated").
- Repeats back off: one opening notice, then reminders at 10m → 30m → 90m, then silence. Three
  reminders per episode, and an episode **re-arms only when the card changes state** — the one act
  that proves somebody dealt with it. Thresholds, tick, backoff and cap are env-tunable
  (`SPRINT_SWEEP_INTEGRATING_SECONDS`, `SPRINT_SWEEP_QUEUED_SECONDS`, `SPRINT_SWEEP_BLOCKED_SECONDS`,
  `SPRINT_SWEEP_NEEDS_YOU_SECONDS`, `SPRINT_SWEEP_READY_SECONDS`, `SPRINT_SWEEP_TICK`,
  `SPRINT_SWEEP_MAX_REMINDERS`), which is the only honest way to test a ten-minute rule.
- `stuck` rides the normal stream, so the session's default `sprintd tail` wakes on it with no
  special casing. `--user-only` does NOT show it — that filter means "a human is waiting", and the
  point of the sweep is that no human is. SKILL.md's event-reaction table carries a per-state row.
- UI: no new chrome. The event renders as a quiet amber system line in the card timeline
  ("stuck: approved 12m ago, still merging") and the card's age text — the one thing on a face
  already about time passing — goes amber (`/api/board` carries `stuck` per card, cleared the moment
  the card moves).
- Session liveness — three states, each grounded in something the session actually did:
  `online` (nothing pending, or the orchestrator cursor moved within 90s), `busy` (the cursor is
  behind pending events past 90s **but** the session's waiter has polled within 30s — "session is on
  it — catching up"), `offline` (cursor stale AND the waiter itself gone >30s). The waiter's own
  polling of `/api/events` is the primary heartbeat: it keeps ticking while the orchestrator
  legitimately lags minutes mid-dispatch, which the cursor-only rule misread as a dead session.
  A poll only counts as the waiter when it carries `X-Sprint-Waiter:` (or `?waiter=1`) — a browser
  falling back to polling `/api/events` is not the session and must never fake liveness. Sightings
  are in-memory with a throttled write to `cursors('waiter')`. Thresholds env-tunable
  (`SPRINT_SESSION_OFFLINE_SECONDS`, `SPRINT_WAITER_ONLINE_SECONDS`, `SPRINT_WAITER_GONE_SECONDS`).
  UI: banner ("session offline — items will queue") ONLY on `offline`; `busy` is the dot + tooltip,
  never a banner. Submissions/answers still accepted and queue in every state.

## Evidence gate ("ready")

Server-side schema validation; missing/empty fields → **422 with the named missing fields**, card
stays in_progress, rejection appended to the timeline so the worker sees exactly why.

**Prime rule (user, verbatim): "we need to make sure there's a way for the human to easily validate
the fix without having to read the code. so, either before/after screenshots or a link to a staging
url for the branch."** Every ready card must be validatable with zero code reading.

Packet: `{claim (one sentence), diffstat, branch, test_cmd, test_result ("N pass, 0 fail" — counts,
never "tests pass"), validate (REQUIRED: 1-3 plain-English steps a human follows to confirm the fix
without reading code), ui_change (REQUIRED bool), screenshots?: [attachment refs], live_url?,
work_kind?: code|ops, readback? (ops), per_card?: [{card_num, claim, screenshots?}] }`.

**Ops cards — the same bar, a different shape of proof.** Non-code work (reprocess a mailbox, rotate
a key, rerun a job) has no diff, no branch and no preview, and demanding them made ops cards either
liars or second-class. `work_kind: "ops"` therefore SWAPS the required set rather than relaxing it:
**claim, validate, readback**, where `readback` is the observed evidence — a log excerpt or command
output, string or array of lines — rendered verbatim and preformatted in the packet block.
`diffstat`, `branch`, `ui_change` and `screenshots` are not required; `test_cmd`/`test_result` are
optional and still need real counts when present; an ops packet that volunteers `ui_change: true`
still owes screenshots. The kind is taken from the packet, else from the card
(`assign {work_kind: "ops"}`), and an accepted packet stamps it onto the card so a second packet
after a bounce is judged by the same rules. Ops cards need no worktree and no branch, and `assign`
may omit them.
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

### Awaiting review — the signoff surface (SHIPPED)

Ready cards are reviewed **by work unit**, not one identical row at a time. A unit is a batch, or
failing that an agent, and it renders as one expandable row; a card on its own is a unit of one and
renders as it always did. Every row leads with the packet's claim and its first "Check it yourself"
step and carries the first screenshot as a thumbnail. **The row itself does nothing but open the
card** (card #53 — see "Every action lives in the rail"): the verdict, the live link and the
lightbox are all on the open card in the rail, one click away and pinned where they cannot be
scrolled off. **Review next** is a filled button in its own bar above the stack (never another row)
and walks the queue oldest-first in the rail, one STEP at a time with a running "3 of 13".

- **Naming.** A unit row is named after the WORK in it: the member cards' own condensed titles
  joined ("Restart-proof tabs + reply routing · 2 cards"), falling back to the branch's claim, then
  to a descriptive branch name. **Never the agent** — "Agent card-23 · 2 cards" names a process, not
  the thing you are being asked about, and the agent belongs in the row's metadata beside the
  timestamp. A singleton row leads with the card's own title, never with a control's label.
- **See-and-ack is per WORK UNIT for shared-packet groups, per card otherwise.** User verbatim:
  *"i feel like i just hit approve way too many time"*, and his GO on the fix: *"ONE Approve per
  work unit when the unit shipped as one branch with one packet (the six design cards = one click);
  per-card records still written underneath, and you can still expand a group to bounce a single
  member."* So a unit with ONE branch and ONE packet covering every member gets a single **Approve
  all N** button; anything looser keeps per-card verdicts. The single button is not a bulk approve
  and not a new endpoint: it issues the same per-card `POST /api/cards/:num/verdict` for every
  member in order, so every card keeps its own verdict event and its own record. It reports
  "approving 3 of 6…" in words (no spinner) and **stops on the first failure**, saying which card it
  stopped at and that nothing after it was sent. Since card #53 that button lives in the RAIL, on
  any member of the unit, over the packet you are actually reading — the unit ROW only expands and
  opens. The walkthrough treats a shared-packet unit as one step: approve the unit, bounce the
  member you are reading, or skip.

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
   → open sprint via API → arm ingress (`sprintd tail --after <cursor>` under the streaming Monitor
   tool; `sprintd wait` in background Bash as the fallback).
2. **Drain loop invariant**: on EVERY wakeup (tail line, waiter exit, resume, boot): arm ingress FIRST,
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
via helpers: `sprint-post <num> progress "one-liner"` after each meaningful step;
`sprint-post <num> phase "testing" [--expect 300]` at every stretch boundary (see Phases);
`sprint-ask <num>
"question" [--options json] [--url URL] [--attach PATH …] [--notes TEXT]` then END YOUR TURN — the
artifact flags are what make it a **decision request** rather than a bare question, and the rule for
which handoff to use is the needs_you/ready definition above; `sprint-ready <num> packet.json`
(client-side validates, then POSTs; on 422 fix and retry; advisory stderr notice when the packet
reads like a question). First act on pickup: state→triaging + one-line
restatement ("I read this as: X") + a condensed ≤8-word `title` on that same state POST. Long jobs: set `long_running` with a note first. Work only in
your assigned worktree; one branch; never push to main; never touch other cards' files.

## UI (web/)

- Form factors (the ONLY three that matter): Linux laptop + Mac desktop (≥1200px: full kanban +
  docked right sidebar) and Samsung Z Fold 8 interior screen — 2448×1848 physical, ~2.5 dpr ⇒
  treat as **~980×740 CSS px landscape**: 3 columns + sidebar as slide-over, touch-sized targets
  (44px min). Vertical space is scarce on the Fold: compact card rows, independently scrolling
  columns. No narrow-phone layout work (a basic usable fallback is fine, not optimized).
- **Two layouts, one toggle** (persisted in `localStorage`). **List** is the default and the daily
  driver: a 4px proportional meter + legend, then Needs you (the only generously spaced section) /
  In motion / Blocked / Queued & held, each quieter than the last, then Done. In the List, Needs you
  deliberately holds both shapes of asking (an open question, and a packet waiting on a verdict);
  they are told apart by rail colour and an ASKS/SIGNOFF tag, and Done is a count that opens into a
  plain list.
  **Board** is the kanban, and it is ordered as the life of a card rather than as a reading order —
  user ruling, verbatim: *"column 1 should have 3 sections (when needed) queued, then held, then
  blocked … then column 2 is in progress, then column 3 is 'needs you', then column 4 is two
  sections: awaiting review and complete (they start in awaiting review, then move to complete once
  I've approved)"*. So: **Waiting** (Queued → Held → Blocked, each section drawn only when it has
  something in it) · **In progress** · **Needs you** (open questions only) · **Review** (Awaiting
  review = ready only, then Complete = the closed states, dimmed; an approved card is merging
  over in In progress, not sitting in a review list you already dealt with). The two things that are
  on the user are deliberately apart: answering a question and signing off a finished branch are
  different jobs. The Blocked section carries the answer to "what even is blocked?" in place —
  *"an external wall (red CI, waiting on another branch). Nothing you type fixes these; the session
  re-checks and unblocks them itself"* — and the List's Blocked intro is the same sentence.
  The meter stays in **urgency** order (need you → in motion → blocked → queued), not column order:
  it says what shape the sprint is in, and the columns say where everything is.
  On the Fold the three live columns (In progress / Needs you / Review) keep the grid and the whole
  Waiting pile — queued, held **and blocked** — drops into the Elsewhere strip as pills.
  Card face: `#num`, title, tag, last-activity one-liner, agent, state age, amber-on-silence —
  and, whenever the agent has declared one, a **phase chip with its own clock** (`testing · 2m`)
  in the tag slot instead of the state age, amber only once the phase outruns what it claimed.
  The title on the face is the **condensed** title (≤8 words, set by the session at assign time and
  refined by the worker at triage); the user's original submission is never rewritten and shows in
  full in the rail.
- **Two skins, one toggle** (persisted in `localStorage` under `sprint.skin`, default **Calm**),
  sitting beside the layout toggle and present at every width including the phone fallback — it is
  the only way back out. **Calm** is the warm near-black editorial interface. **Chaos** is a 16-bit
  JRPG skin: pixel display type (Press Start 2P), ink outlines and hard bevels, radius 0 everywhere,
  gold column plates, a CRT scanline overlay, and one 8-bit blip on every interaction (square wave,
  660 Hz → ×1.5 at 60 ms, gain .05, ramped to silence over 130 ms). **Identical information
  architecture and identical copy — only the surface changes**, and the skin is a class on `<html>`
  over one set of custom properties, never a second component tree. Motion: Calm runs `driftIn` and
  `softPulse` and nothing else; Chaos adds `goldEdge` on needs-you cards, `plateSheen` on the gold
  plate and `goldFlash` on the waiting Chat button, and drops all three under
  `prefers-reduced-motion`. Sound is Chaos-only; Calm's only sound is the needs_you/ready chime.
  **Both skins hold the same readability floor: no 11–13px text below 4.5:1** (Chaos checks against
  the worst stop of every gradient it sits on).
- **Every action lives in the rail.** User ruling, verbatim: *"get the actions out of cards. I click
  the card, it loads in the sidebar, and that's where I review and act."* So a card face (Board
  tile, List row, review row, Done row, Elsewhere pill) is a **single click target and nothing
  else** — no verdict buttons, no quick-reply chips, no thumbnail lightbox, no live link, no
  expander. Clicking anywhere on it opens the card in the rail; Enter/Space does the same from the
  keyboard. Approve / Bounce / Reject, the quick-reply options, pin/hold/cancel/retry/duplicate
  (the ⋯ menu) and the bounce notes are all in the rail, on the open card.
  **The verdict is a bar pinned at the bottom of the rail**, above the composer, in the same place
  the Review-next walkthrough's bar sits — exactly one of the two is ever up, and neither can be
  scrolled off by a packet with six screenshots in it. Card #26's *one Approve per work unit* moved
  with it: on any member of a unit that shipped as one branch with one packet, the rail's bar reads
  **"Approve all N"** and issues the same per-card POSTs in order. The one exception is a card that
  does not exist yet — an un-submitted card's "not sent — retry" stays on its pill, because there is
  no card to open.
  The Review-next walkthrough keeps its own **navigation** controls (next / skip / stop) and the
  Awaiting-review unit rows keep their expander and "Review these N": those move you around, they
  do not act on a card.
- **Answering**: every question answers in the rail, on the card — options as ≥46px rows under the
  question, free text in the composer under them. The List row says *"3 options — open it to
  choose"* rather than carrying the chips itself (see above): a decision is never half on a row and
  half in a panel.
- **A decision request renders above the answer box.** A question carrying `artifacts` (card #50)
  puts the agent's notes, its screenshots and an **Open the preview ↗** button in a panel directly
  above the options/composer, so you are never asked to choose between three mockups you cannot
  see. Once answered the panel stays in the thread, dimmed, as the history of the choice.
- **A report's sidebar is its card.** User ruling, verbatim: *"when I'm looking at a report, the
  sidebar should be the card that created it, not the session chat."* Opening `#/report/<sha>.<ext>`
  loads the originating card into the rail (thread + composer, #46's focus behaviour minus the
  caret grab — you came to read, not to type) while the report keeps the main area and the URL. A
  report with no originating card (one posted into the sidebar) keeps the session chat, and the page
  says so in one line rather than leaving you to guess.
- **URLs in prose are links.** User ruling, verbatim: *"make links clickable and should auto open in
  a new tab."* Every surface that renders agent/user text through `autolink`/`richText` — thread
  messages, session chat, card bodies, expanded `detail` bodies, a packet's claim, its check steps
  and its `readback` — turns a bare `http(s)://…` into `target="_blank" rel="noopener noreferrer"`.
  `#N` stays what it was: an in-app card link, same tab. Links are built from text nodes, never by
  injecting markup, so agent-authored text can still never become markup. Markdown reports get the
  same treatment server-side (bare URLs autolink; `[text](url)` already did).
- **Right rail (480px)**: the session chat OR one card's thread, never both. Card thread is one
  interleaved timeline of four item types — message bubbles (yours right-aligned, a "More context"
  disclosure only where an event really carries `payload.detail`), screenshot tiles → lightbox,
  question panels of ≥46px option rows, and centred status changes (events, not speech). The
  evidence packet renders **as a message in the stream** — claim, "Check it yourself" numbered
  steps, screenshots, mono metadata (branch, diffstat, test counts), live URL. The verdict is NOT
  in it: Approve / Bounce-with-notes / Reject are the pinned bar above the composer (see "Every
  action lives in the rail"). Composer pinned at the bottom: Return sends, Shift+Return newlines —
  and it takes images exactly like the Drop-work sheet does (paste, drop, or the file picker →
  removable thumbnails above the line → sent with the text as one message → screenshot tiles in the
  thread → lightbox). Card threads and the session chat share one implementation (`compose.js`).
  A half-written message survives a re-render, words and thumbnails both.
- **The rail repaints without blinking.** Thread items are keyed and versioned: a frame that changed
  nothing changes no DOM, a new line is appended, and the delivery pill under your own messages is
  patched in place. Cursor frames (the most frequent thing on the wire — an active session moves its
  drain cursor about once a second) paint the rail alone, because nothing else on the page reads the
  cursor. Rebuilding the rail per frame threw away every `<img>` in the thread, which is exactly what
  a blink is.
- **Submit**: a "+ Drop work" modal sheet — textarea + paste-to-attach multiple images (thumbnails,
  removable) + `<input type=file multiple accept="image/*">` fallback + Hold toggle. Return submits,
  Shift+Return makes a new line (Cmd/Ctrl+Enter still submits). Pasting an image anywhere opens it,
  and so does pressing `/` — including from a text box that is still **empty** (user, verbatim:
  *"Typing / should open the light box even when my focus is in a chat entry box unless there's
  already other text in that box (i should be able to type a / in the middle of a message, but not
  at the beginning)"*). Once the box holds any non-whitespace text a slash is just a slash, and
  inside the Drop-work sheet's own textarea it always is.
- **Chat button**: the product's single notification surface, with four states — closed, open
  (sage dot), **gold when a session line arrived while you were not looking** (Chaos pulses it with
  `goldFlash`; Calm holds the gold steady), and dimmed because a card has taken the rail. No
  counters anywhere.
- **Session liveness dot** (header/chat): green live / amber "catching up" / red offline; only red
  raises the "session offline — items will queue" banner.
- **A send is never silent, and a restart is never a dead tab.** Every POST has a deadline (a
  backend that goes away mid-request must not leave `fetch` hanging), and a send that fails or times
  out flips that message's delivery pill to **"failed to send — tap to retry"** — the pill IS the
  button, and the retry re-POSTs with the SAME `Idempotency-Key`, so a slow-but-landed send replays
  instead of duplicating. This covers all four user surfaces: sidebar chat, card chat, answers, and
  verdicts (a failed verdict writes a visible error line into the thread, never just a toast that
  is gone in four seconds). A failed line survives every subsequent refresh until it is retried.
  On a `generation` change — SSE hello, cursor frame, a poll, or an unauthenticated `/healthz` probe
  fired when the transport falls over — the tab knows the backend restarted: it re-opens the stream
  from its own cursor, refetches the board and the open card, and keeps every composer draft and
  pasted screenshot. If the restart also invalidated the token, the sign-in wall says so in those
  words ("the board restarted — it needs its link again") instead of the tab dying quietly; a board
  fetch that succeeds again takes the wall back down.
- SSE-live throughout; optimistic UI with reconciliation; Last-Event-ID reconnect; one tab-title/
  favicon badge + one soft chime on flips to needs_you/ready (no repeat, no unread counters
  anywhere else).
- **Dark only.** This line previously called for light + dark via `prefers-color-scheme`, both
  first-class. The design work in 2026-08 scoped light out and shipped a dark-only Calm palette; a
  light Calm palette has not been designed, and a half-translated one reads worse than an honest
  single theme. Light is a real open item, not a shipped feature — and Calm/Chaos is a *skin*, not
  a theme: both are dark, and neither is the missing light mode.
- Aesthetic: calm, dense, plain-English labels. No spinners — the In-motion hairline is *recency*
  (it drains across the five-minute silence window), never invented progress, because nothing on
  the wire knows how far along a job is. No held-count in any header/chrome.
- All type is self-hosted from `web/fonts/` (Instrument Serif, IBM Plex Sans/Mono, Press Start 2P,
  all OFL): the page makes zero external requests.

## Packaging

Plugin root = repo root: `.claude-plugin/plugin.json` (name `sprint`), `skills/sprint/SKILL.md`,
`agents/sprint-worker.md`, `bin/` (sprintd, sprint-post, sprint-ask, sprint-ready — all executable,
python3 stdlib or POSIX sh only), `web/`, `README.md`. Install: `claude --plugin-dir` for testing,
`/plugin install sprint@<marketplace>` for distribution; `claude plugin validate` must pass. Deps:
python3 ≥3.9 + git; tailscale optional (loopback-only degrade); everything else stdlib.

## Non-goals (v1)

Multi-user, public exposure, auto-dup-detection, priority pickers/drag-reorder, batch blind-approve,
session summaries beyond the end-sprint card, phone-portrait optimization, agent-list integration
(possible later mirror).
