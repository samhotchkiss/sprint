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
| `bin/sprint-recover`, `bin/sprint-limit` | The SESSION's two helpers, not the workers'. `sprint-recover <num…>` prints the recovery brief for a card whose agent died; `sprint-limit declare/list/clear` records a provider limit window from the kill message. |
| `bin/sprint-serve-setup` | One-time, run by hand: publishes the hub on the tailnet under a NAME via a Tailscale Service. Read-only preflight + printed plan by default; `--apply` runs it, `--verify` re-checks. |
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
- `.sprint/last-restart.json` — the self-restart stamp (when, which sha, how many lately). The board
  hashes the source file it is running (`bin/sprintd`) every few seconds and, when that file becomes
  DIFFERENT code, re-execs itself in place with the same argv (`os.execv`, so the pid, port, token,
  log fds and launchd job all survive; the browser re-syncs off `generation` exactly as it does after
  a manual restart). Gates, all of them load-bearing: content not mtime; the same new sha seen twice
  and not written in the last couple of seconds; it must `compile()`; no in-flight non-streaming
  request (SSE is excluded by design — it is held open for hours); and the DB write lock is held
  across the exec. Crash-loop guard: never twice inside 60s, never more than 3 in 10 minutes, stamp
  written BEFORE the exec so a build that never comes back still counts. When a guard blocks it, the
  board appends a `note` (`actor: "server"`, `payload.restart_pending: true`) for the SESSION —
  never a banner telling the user to run `sprintd stop`. That banner is gone from `web/` for good
  (user, verbatim: "don't show me this notice"). Every threshold is env-tunable
  (`SPRINT_CODE_WATCH_TICK`, `SPRINT_CODE_SETTLE_SECONDS`, `SPRINT_SELFRESTART_MIN_INTERVAL`,
  `SPRINT_SELFRESTART_MAX_BURST`, `SPRINT_SELFRESTART_BURST_WINDOW`,
  `SPRINT_RESTART_DRAIN_SECONDS`).
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
  executor NULL, model NULL, blocked_by NULL, blocked_reason NULL, created_at, updated_at)` —
  `executor`/`model` are the per-card dispatch choice (NULL = the board's defaults);
  `blocked_by`/`blocked_reason` are the card-to-card wall (see Blocked by);
  **`num` is global across sprints**; the UI renders `#num` and #num means the same card forever.
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
- `questions(id, card_num, text, options JSON NULL, answered_at NULL)` — answer idempotency: second
  answer to the same question id is a 409, surfaced gently in UI.
- `limits(id, kind, model, resets_at, declared_at, cleared_at NULL, source NULL, note NULL)` — provider
  usage limit windows (see below). `cards.model_reason` is the other half: free text saying WHY a
  card is not on the default model ("fable limited until 23:50"), which is what makes "downgraded
  because of a limit" a queryable fact rather than something the session has to remember.

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
  **Every POST that takes `images` also accepts the same list under `attachments`** — that is the name
  the pictures come back under, and posting them back under it must not be a silent drop.
  `POST /api/cards/:num/answer`
  `{question_id, text?, images?}` — flips needs_you→in_progress optimistically. The answer box takes
  screenshots exactly like a chat line does, and they ride the answer event's own
  `payload.attachments`; an answer that is ONLY a picture is a complete answer.
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
- `POST /api/cards/:num/verdict` `{verdict: approve|bounce|reject, notes?, images?}` — approve:
  ready→integrating; bounce: ready→in_progress, bounce_count++; server emits event either way, session
  does the git work. A bounce carries the screenshot that shows what is wrong (same ingest, same caps,
  same refusals as submission): it lands on the verdict event's `payload.attachments`, renders under
  the bounce in the card's timeline, and gives the agent picking the card back up an absolute `path`
  to Read. Nothing is written to disk until every target has passed the transition check.
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
  `{agent_name, worktree?, branch?, title?, work_kind?: code|ops, external_agent?: bool,
  executor?, model?}` (worktree/branch are optional: **ops work has neither**; executor/model are the
  per-card dispatch choice — see Settings & executors) — assigning a **queued** card also flips it
  queued→triaging in the same transaction (state event reads "assigned to sprint-card-N — picking
  it up"); assigning a card in any other state only records the agent and never regresses state;
  `POST /api/sprint` `{action: open|close|set_hold_mode, ...}`; `POST /api/cursors/orchestrator` `{seq}`;
  `GET /api/settings` + `PUT /api/settings` (dispatch policy — see Settings & executors);
  `GET /api/reground?reason=revival|boot|manual_reset|periodic` — the whole working state in one
  read (see Re-grounding).
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
- **It is also the reviver.** The same poll that builds the page wakes a board whose own session
  died — the hub is the only process on this machine that watches every board and is not itself one
  of the sessions that can die. See *Autoheal*. A reviver that throws can never stop the page from
  rendering the other boards.
- Started once per machine by hand; boards register themselves. No launchd parity yet.
- **Reach it by name (SHIPPED).** User verbatim: "figure out how to set things up so I can just
  navigate any browser to 'sprint' from any device on my tailnet and get to the project picker."
  The mechanism is a **Tailscale Service** — `tailscale serve --service=svc:sprint --https=443
  --yes http://127.0.0.1:8300` (plus `--http=80`), which gives the hub its own virtual IP, its own
  MagicDNS name `sprint.<tailnet>.ts.net`, and its own auto-provisioned certificate. Not plain
  `tailscale serve 8300`: that publishes under the *machine's* name and evicts whatever already
  holds that machine's `/` on :443. Not a hostname rename: the machine hosts other things.
  - **`bin/sprint-serve-setup`** reads the hub's port and token off `~/.sprint/hub.json`, preflights
    read-only (tailscale ≥1.86, MagicDNS suffix, the hub actually answering `/healthz` with
    `hub:true`, node tags, whether `svc:<name>` is already defined), prints the admin-console and
    policy-file steps it cannot perform itself, prints the serve commands, and stops. `--apply`
    runs them and then verifies by fetching the published name; `--verify` re-checks any time.
    Idempotent — it reads `tailscale serve status --json` and says "nothing to change" when the
    mounts already point at the hub.
  - **The hub knows its own name.** `~/.sprint/hub-canonical` (written by the setup script, or
    `sprintd hub --canonical-host`, or `$SPRINT_HUB_CANONICAL_HOST`) records the published origin.
    Re-read on a 5s TTL, so publishing does not mean restarting the hub under an open browser.
    A request whose `Host:` is a **single label** (`sprint`, i.e. someone followed the tailnet
    search domain) 302s to that origin, query string and all, **before** auth and never for
    `/healthz`. Reason: `sprint` and `sprint.<tailnet>.ts.net` are two cookie origins to a browser
    and only the second one has a certificate, so signing in on one leaves you signed out on the
    other. Hosts with a dot or a colon in them (every IP:port) are never touched, so the hub is
    unchanged with no proxy in front of it. Unset -> the whole behaviour is off.
  - **Boards are NOT proxied, deliberately.** `hub_summarize` hands out `board_url(hosts[0], port,
    token)` — the board's own absolute `http://<tailnet-ip>:<port>/?t=…`, which already works from
    every tailnet device and is unaffected by how the hub was reached. Path-mounting them would
    break their root-relative `/api/…` fetches, and per-port serve mounts would have to be
    re-applied every time a board started or stopped.
  - **Honest limits**: the bare name works only where the device honours the tailnet search domain,
    and only over plain HTTP — `https://sprint/` cannot be made to work, because the certificate is
    for the FQDN. The FQDN is the deliverable; the bare name is a bonus that redirects onto it.
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
  **Order is stable and server-side**, user verbatim: "the order of sprints should stay the same in
  the list, so I can count on .1 always going to session a, .2 always going to session b, etc, and not
  have to reassess the list each time." The registry stamps every project an `ordinal` the first time
  it registers and never edits it again, so a board keeps its place across restarts and a new one
  appends. `/api/siblings` sorts by that and by nothing else — including the `self` row, which sits in
  its own place rather than first, so all boards on the machine agree on what "2" means. Attention is
  **shown** (the dot, the counts) and never sorted by. Dropping a dead row compacts the list: a number
  that navigates nowhere is worse than one that shifted, and the board gets its place back when it
  returns. The hub page keeps its attention sort — it is read cold, top to bottom, and nothing on it
  is keyed to a number.
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
- **The clock never reads older than the card's last MOVE**, and this is load-bearing rather than a
  nicety. The baseline is per-AGENT (an agent posting on card A is demonstrably not dead on card B),
  which meant a card could inherit hours of perfectly correct silence and then be failed the instant
  it was handed back to somebody. It happened to card #70: the worker posted a decision request and
  ended its turn (the documented contract), the user answered 1h34m later, answering moved the card
  `needs_you` → `in_progress`, and the very next sweep tick read *"worker gone: no events for 1h
  34m"* and killed it — before the fresh agent could say a word. The silence was the user's thinking
  time, charged to the worker. A transition is the board saying the situation just changed, so
  `card_baseline` takes the later of (the agent's last word, the card's last state event). Every
  hand-back is covered by the same line: an answered question, a bounce, a retry, a failed
  integration. A worker that then really does die still fails on schedule, because nothing moves the
  card again and the transition itself keeps ageing.
- **A `needs_you` card is never a dead worker**, whether or not a question row is still open. The
  guard used to require an UNANSWERED question, which stopped applying the moment `answered_at` was
  stamped. Whatever a `needs_you` card owes, it is not owed by the worker — it ended its turn on
  purpose, and the `needs_you` sweep rule is already nagging the right person.
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
  lets the session re-notify in words), `ready` >24 h (one aged-review reminder, then quiet — see
  below).
- Payload: `{state, stuck_for_seconds, threshold_seconds, text}` — `text` is a plain-English
  one-liner ("approved 12m ago, still merging — the session owes it a /integrated").
- Repeats back off: one opening notice, then reminders at 10m → 30m → 90m, then silence. Three
  reminders per episode, and an episode **re-arms only when the card changes state** — the one act
  that proves somebody dealt with it. Thresholds, tick, backoff and cap are env-tunable
  (`SPRINT_SWEEP_INTEGRATING_SECONDS`, `SPRINT_SWEEP_QUEUED_SECONDS`, `SPRINT_SWEEP_BLOCKED_SECONDS`,
  `SPRINT_SWEEP_NEEDS_YOU_SECONDS`, `SPRINT_SWEEP_READY_SECONDS`, `SPRINT_SWEEP_TICK`,
  `SPRINT_SWEEP_MAX_REMINDERS`), which is the only honest way to test a ten-minute rule.
- **`needs_you` and `ready` are the two exceptions to the repeat cadence (#67, #74).** The other three
  states are parked on the session or the agent — something that can act on a repeat nag. `needs_you`
  is parked on the human, and re-nagging the session every 10-30 minutes about a question only the
  user can answer gives the session nothing to do with it; the gold square and the hub badge already
  carry the signal for as long as the question stays open. `ready` is the same shape: with a dozen-plus
  cards sitting in review overnight, the old cadence dripped "ready for review 1d — waiting on a
  verdict" per card, and the session cannot act on that either — the review column and the card's own
  green square already carry "this is unreviewed" continuously. So each of them gets **at most one**
  `stuck` event per episode — the normal opening delay, no 10m/30m/90m repeats — and then goes quiet.
  For `needs_you`, answering the question and getting asked a new one re-arms it for exactly one more;
  for `ready`, a bounce (back to `in_progress`) followed by a re-`sprint-ready` does the same. Both are
  the normal rule: any state change re-arms an episode. Neither cap is env-tunable — they are
  `NEEDS_YOU_MAX_REMINDERS = 0` and `READY_MAX_REMINDERS = 0` in `bin/sprintd`, distinct from
  `SPRINT_SWEEP_MAX_REMINDERS` which still governs the other three states (`integrating`, `queued`,
  `blocked`).
  Because the timeline won't keep restating the age, an open question's card face carries its own
  ticking "waiting Nm" line (`web/board.js`, `.needsyou-wait` in `web/styles.css`) built from the
  card's `state_since` — a cheap, always-on stand-in for the reminder that no longer repeats.
  `agent_silent` and the worker-gone clock are untouched: this cap is a `stuck`-event rule only, and
  applies only while the card sits in `needs_you` or `ready` respectively.
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
  A session that is offline **and** has work owed to it for twenty minutes is no longer merely
  offline — see *Autoheal*, which is the same three signals on a much longer clock, plus the two
  guards (owed work, account limit) that turn "quiet" into "dead".

## Provider limit windows (auto-resume — SHIPPED)

User verbatim, on the killed-agent work: **"does this also detect when the model tells us when the
reset happens? i want to make sure it's surfaced to the user and it auto-restarts when the window
resets"**. The incident behind it: three workers killed at once by a usage limit whose message said
`resets 11:50pm (America/Denver)`; nothing captured that sentence, and the cards sat open for hours.

- **A window is one fact**: this model is unavailable until this instant. Declared by the session the
  moment it reads a kill message — `bin/sprint-limit declare --model fable --resets "11:50pm"` →
  `POST /api/limits {model, resets_at, source?, note?}`.
- **`resets_at` is parsed server-side, once**, so the CLI and the board can never disagree: an epoch,
  an ISO 8601 timestamp (naive = local, offsets honoured), or a HUMAN CLOCK TIME (`11:50pm`, `9pm`,
  `23:50`) meaning the **next occurrence** — at 11:52pm, "11:50pm" is tomorrow. The provider's own
  zone may ride along in parentheses (`11:50pm (America/Denver)`) so the kill message pastes
  verbatim; an unknown zone is a named 400, never a silent local reading.
- **Active is COMPUTED, never a column**: `cleared_at IS NULL AND resets_at > now`. A board that was
  down across the reset time comes back up knowing the window is over — no cleanup job is load-bearing.
  `cleared_at` exists only to make the end-of-window event fire once.
- **One active window per model.** Re-declaring the same window (the session sees the same kill
  message on the second and third dead agent) updates it; a corrected reset time moves the window it
  corrects. Two identical lines on the board is the bug this prevents.
- **Ending a window emits exactly ONE `limit_cleared`** (`card_num` NULL, `actor: "server"`,
  `payload.model`, `payload.reason ∈ {window_passed, cleared_early}`), guaranteed by a conditional
  `UPDATE … WHERE cleared_at IS NULL`: whoever wins writes the event and every other caller — a
  second sweep tick, a manual clear racing the clock, another thread — says nothing. This is not
  log hygiene: the session re-dispatches on this event, so a duplicate is duplicate AGENTS on one
  card. `POST /api/limits/:id/clear` ends one early; `limit_declared` is emitted on declaration.
- **The sweep is a third rule on the existing sweep tick**, beside staleness and worker-gone.
- API: `POST /api/limits`, `GET /api/limits` (`{active, recent, server_time}`),
  `POST /api/limits/:id/clear`; `/api/board` carries `limits: [...]` (open windows only) and
  `account_limit` (the account window with its words, or null).
  `assign` accepts `model_reason` — `""` clears it, omitting it leaves it alone.
- **UI: one quiet board-level line per open window**, in the session-offline banner's slot and
  register — *"fable is rate-limited until 11:50pm — work is running on opus"*. Not a modal, not a
  toast, dashed rather than coloured, and **no new per-card chrome**: a re-dispatched card already
  wears its model tag. The second half of the sentence is read off the board's own cards (the
  distinct models of non-terminal cards carrying a `model_reason`) so it is only said when true.
  The line goes away on the clock, not on an event.
- **SKILL.md owns the reaction** (step 5b): declare the window → dispatch the fallback with
  `model` + `model_reason` → on `limit_cleared`, re-dispatch everything downgraded or parked for it
  back on the original model and clear the reason with `"model_reason": ""`.

### The account-level window (`kind: "account"`)

User verbatim: **"i'm about to hit my overall claude weekly limit... once i do, I need a big warning
on top of every board, and then I'm going to go to the session, log out, log back in with a
different claude session, then I should be able to hit a button in the big notice to have it
auto-resume"**.

- **Two kinds, one table.** `kind ∈ {model, account}` (default `model`, so every row that existed
  before this is what it always was). A model window is one model going away; an ACCOUNT window is
  the whole Claude account, where nothing runs at all and there is no next model down. An account
  window carries no model — saying one would be a lie the banner then repeats.
- **One active account window at a time**, keyed on the kind alone, because there is one account.
  Everything already proved for model windows holds unchanged: activeness computed, exactly one
  `limit_cleared` per window per board (`payload.kind` says which kind cleared).
- **Machine-wide, via one shared file** (`account-limit.json`, beside `registry.json`, moved by the
  same `--registry`/`$SPRINT_REGISTRY` override). The declaring board publishes; every board pulls
  it into its OWN `limits` row on its next read of `/api/board`, `/api/limits`, or the sweep tick.
  Chosen over pushing the declaration to siblings over HTTP for two reasons: no board holds another
  board's bearer token, and a push cannot reach a board that STARTS after the declaration — which is
  exactly what happens when a wedged project gets restarted mid-limit. Mirroring rather than
  rendering someone else's row is what keeps every board's `limit_cleared` its own, which is right:
  each board has its own parked cards to re-dispatch.
- **It must work with NO session attached**, because a dead session is the precondition. The board
  server and the browser are the only two things still moving: the banner is served off
  `/api/board`, propagation happens on that same read, and Resume is one POST.
- **UI: a big banner at the top of EVERY board** — accent-bordered, three lines and a button, in the
  same slot as the quiet lines and directly above them (both kinds render together). Not a modal and
  not a toast: the user must still be able to read cards and type. Copy is composed server-side
  (`headline`, `detail`, `action`, `resume_label`, `resume_url`) so the board, the event log and the
  CLI cannot drift — *"Claude account limit reached — nothing can run" / "Every sprint board on this
  machine is stopped until 11:50pm." / "Log out of Claude, sign in with another Claude session, then
  press Resume."*
- **The Resume button** POSTs `/api/limits/:id/clear` — the same single-writer clear, so a double
  press cannot fire two `limit_cleared` events — and every other board drops its banner on its next
  tick.
- CLI: `sprint-limit declare --account --resets "11:50pm"`; `list` shows `ACCOUNT` in the subject
  column; `clear <id>` is the same button by another door.

## Evidence gate ("ready")

Server-side schema validation; missing/empty fields → **422 with the named missing fields**, card
stays in_progress, rejection appended to the timeline so the worker sees exactly why.

**Prime rule (user, verbatim): "we need to make sure there's a way for the human to easily validate
the fix without having to read the code. so, either before/after screenshots or a link to a staging
url for the branch."** Every ready card must be validatable with zero code reading.

Packet: `{claim (one sentence), diffstat, branch, test_cmd, test_result ("N pass, 0 fail" — counts,
never "tests pass"), validate (REQUIRED: 1-3 plain-English steps, ~8th-grade reading level — see
"Voice & microcopy" in docs/board-functional-spec.md — a human follows to confirm the fix without
reading code), ui_change (REQUIRED bool), screenshots?: [attachment refs], live_url?,
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

**One card per work unit.** User ruling, verbatim (card #55): *"I just want the single card that
lives in review, then when I open it up, it outlines everything that changed, and I can approve them
together."* Work that shipped together — one batch, else one branch, else one agent — is ONE
reviewable object, so Awaiting review holds **one entry per work unit**: a six-card branch is one
card in the list, not six and not one that unfolds into six. A card on its own is a unit of one and
looks exactly as it always did. The entry leads with the packet's claim and its first "Check it
yourself" step and carries the first screenshot as a thumbnail; like every other face on the board
(card #53) **it does nothing but open the rail**.

Everything that used to arrange a long review list is GONE, deliberately and not as a follow-up: the
grouped expand-to-see-the-members row, the per-member review rows, and the Review-next walkthrough
with its next/skip/stop. All three answered "how do we arrange many review rows", which was the wrong
question.

- **The counts are DECISIONS, not cards.** The meter, the headline, the Needs-you section head and
  the Board's Awaiting-review count all ask how many things are waiting on you: an open question is
  one, a work unit is one whatever its size. "6 need you" over a list showing one card is the old
  list talking.
- **Naming.** A unit is named after the WORK in it: the member cards' own condensed titles joined
  ("Restart-proof tabs + reply routing"), falling back to the branch's claim, then to a descriptive
  branch name. **Never the agent** — "Agent card-23 · 2 cards" names a process, not the thing you
  are being asked about, and the agent belongs in the metadata beside the timestamp. A singleton
  leads with the card's own title, never with a control's label.
- **The outline.** Opening a unit fills the rail with everything that changed, in one page read top
  to bottom: what the branch says it did and how to check it once at the top, then **one numbered
  section per member card** with its own claim, its own checks and its own screenshots, then the
  branch's pictures, reports and diffstat as supporting evidence underneath. Nothing is behind an
  expander — an outline you have to unfold is the deleted list wearing a different hat. Each section
  carries the member's `#N`, which opens that card's own timeline in the rail with a way back.
- **One Approve, per work unit.** User verbatim: *"i feel like i just hit approve way too many
  time"*. The unit's verdict bar is pinned at the bottom of the rail under the outline — the same
  place a single card's bar sits (card #53), outside the scrolling thread so six sections can never
  push it off. **Approve all N** covers the unit; it is not a bulk endpoint and not a new one: it
  issues the same per-card `POST /api/cards/:num/verdict` for every member in order, so every card
  keeps its own verdict event and its own completion. It reports "approving 3 of 6…" in words (no
  spinner) and **stops on the first failure**, saying which card it stopped at and that nothing
  after it was sent.
- **Bounce works at both grains.** Each section in the outline can send back just that member with
  its own notes — the rest of the unit stays yours to approve, and the bounced member falls out of
  the unit on the next frame. The bar's "Send it all back" sends every waiting member back with the
  same notes.
- **A unit is a PROJECTION, never a row in the database.** It is recomputed from the cards under
  review every paint, so a bounced member leaves it by itself and a unit whose members have all
  landed simply stops existing. Nothing is stored: `batch_id`, `branch`, `agent_name` and the shared
  packet are already on the board payload. A synthetic card would be a second card identity that
  every keyed thing on this board — events, evidence, verdicts, the rail, the hash route, the meter,
  the sweep — would need an exception for, and it would need a state machine for "half approved,
  half bounced" that the projection answers for free.
- **Member cards keep existing** for tracking, chat and history. They just do not each demand a
  verdict and do not each appear in the review list. Opening one on its own gets the ordinary
  per-card bar (Approve / Bounce / Reject), which decides that card alone — the branch is approved
  from its outline, never from under one card's thread.

## The reviewer — a second pair of eyes, never a signoff (SHIPPED)

User verbatim: *"we need an option to set the reviewer as well."* An agent that reads a card that
has reached `ready` — the packet against the card, the diff, the timeline — and writes down what it
found, before the user gets there.

**The reviewer NEVER approves.** This is not a v1 limitation, it is the shape: the user's standing
rule is that he sees and acks every card, so a reviewer that could sign one off would be deleting
the only step this whole board exists to protect. It cannot approve, cannot reject, cannot close and
cannot merge — the server refuses all three by name (`reviewer_cannot_approve`,
`reviewer_cannot_reject`), because an agent that tried has a bug and should read why.

**It CAN bounce, and that was the user's call, not the board's.** Card #70 shipped the annotate-only
path and ended in a decision request on exactly this fork; his answer, verbatim: *"Reviewer can
bounce with notes."* So the reviewer has one action and two opinions — `--recommends bounce` sends
the card back to the worker then and there, `approve` and `look` move nothing. A bounce is a REAL
bounce (`bounce_count` increments, the second one still escalates, the worker re-readies as usual);
the only difference is `by: "reviewer"` on the verdict event, so nobody has to guess who sent it
back. Its notes are required and are the findings verbatim — one-liner plus detail — because those
notes are the only thing the worker gets. The doctrine for when to use it lives in SKILL.md: bounce
on the CHECKABLE (red suite, counts that never ran, two identical screenshots, a `validate` step
that does not work), annotate on the judged.

- **Config**: `reviewer: {enabled, executor, model}` in `config.json`, default
  `{false, "subagent", ""}`. `executor` is the SAME vocabulary a worker's is — a name in
  `worker.executors` or the builtin `"subagent"` — and it is validated against the executors the
  same request settled on, so enabling a reviewer on an executor nobody declared is a 400 naming
  `reviewer.executor` rather than a dispatch that fails the first time a card finishes. `model` is
  optional; `""` means the executor's own model, else the board's model policy. A typo inside the
  block is a 400 naming `reviewer.<key>`.
- **Resolved on the payload**: `/api/board`, `/api/cards/:num` and `/api/settings` carry
  `reviewer: {enabled, executor, kind, command, session, model}` — the same shape a card's
  `dispatch` has, so the session dispatches a reviewer exactly the way it dispatches a worker and no
  surface looks an executor up twice. It resolves even when disabled: the panel can show what it
  WOULD run as.
- **The session drives it**, as it drives everything (SKILL.md, step 6): on the `ready` event, if
  enabled and the packet is not already reviewed, spawn `sprint-review-<num>` with the card's packet
  + timeline + diff, the standing instructions, and a read-only stance — the card's worktree to read
  or no worktree at all. It commits nothing and starts no preview server.
- **Its findings are a NOTE**, and there is no new endpoint, no new event kind and no new state for
  them: `sprint-post <num> note "…" --reviewer [--recommends approve|bounce|look]`, the one-liner
  being what it found and `--detail` carrying the checks performed and the discrepancies.
  `--recommends bounce` also issues the bounce, in the same command: the note posts FIRST (so the
  thing the bounce cites is on the timeline before the worker wakes), then `POST /verdict {verdict:
  "bounce", by: "reviewer"}`. A refused bounce leaves the findings on the card and exits non-zero
  without retrying — the worst case is an annotated card the user bounces himself, never a lost
  review. `approve`/`look` stay opinions and the verdict bar is untouched.
- **`card.reviewed` is DERIVED, never stored.** `{at, seq, by, recommendation, text}` when a note
  carrying `payload.reviewer` is not older than the card's current evidence packet; `null`
  otherwise. Three things were on the table and the reasoning matters more than the answer:
  - *A state* was wrong because the reviewer never approves — "reviewed" is a fact about a PACKET,
    not a place a card sits, and a state would need a transition, a column, and a rule for a card
    the user acks before the reviewer arrives.
  - *A stored column* was wrong because it would have to be cleared by hand on every bounce, and the
    one time that was missed a re-readied card would sit there looking reviewed with nobody having
    read the new packet. Comparing the note's timestamp to the packet's is free and self-heals.
  - *A text convention* (`"Reviewer: …"`) was wrong because a worker who happens to write that
    sentence would look like the reviewer. One typed boolean in the payload costs nothing and means
    what it says.
  Same family as `queue_position` and the review UNIT: computed at read time off the append-only
  log, so nothing can go stale behind it.
- **UI**: a Reviewer section in the settings sheet — an On/Off toggle, an executor select drawn from
  the same list the default-executor select uses, an optional model box — and the rule on screen in
  the panel's own voice rather than in a doc: *it never approves and never closes anything — every
  card still comes to you*. Removing or renaming an executor moves the reviewer with it, the way it
  already moves `default_executor`, so the sheet can never save into a state the server refuses.

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
  **The verdict is a bar pinned at the bottom of the rail**, above the composer, where it cannot be
  scrolled off by a packet with six screenshots in it. There is one of it and it is always in the
  same spot: a single card's bar sits under its thread, and a work unit's **"Approve all N"** sits
  under its outline (card #55) — never both at once, because the rail shows one thing at a time.
  Card #26's *one Approve per work unit* is that unit bar, and it still issues the same per-card
  POSTs in order. The one exception to the whole rule is a card that does not exist yet — an
  un-submitted card's "not sent — retry" stays on its pill, because there is no card to open.
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

## Blocked by (SHIPPED)

User verbatim: *"when one card is blocked by another, show that in the card details."* The link used
to live in prose — a card said `blocked` and the card it was waiting on was in somebody's sentence,
which nothing could read: no link to click, a sweep that nagged "dispatch it or say why not" at a
card nobody could dispatch, and a session that had to re-read a thread to find out what landed.

- **Store**: `cards.blocked_by` (nullable card number) + `cards.blocked_reason` (nullable one line,
  140 chars). A link, not a state: a card can be `queued` AND blocked, which is the common case.
- **API**: the existing card action —
  `POST /api/cards/:num/action {"action":"blocked_by","target":N,"reason":"…"}`, and `target: null`
  to clear. `target` is required and explicit (a missing key would quietly unblock). Three named
  refusals: `self_block`, `blocked_cycle` (400, carries the whole `chain` — A→B→A is a wall with
  nothing behind it), `blocker_closed` (409 — a closed blocker never lands, so the auto-clear that
  makes the link safe would never fire). A target that does not exist is the usual 404.
  Every card payload carries `blocked_by`/`blocked_reason`.
- **Auto-clear**: when the blocker reaches a terminal state, the server clears `blocked_by` on every
  card waiting on it and appends one `note` each (`actor: "server"`, `text: "no longer blocked — #N
  landed"`, `payload.blocked_by_change`). It rides inside the blocker's own transition transaction,
  and it fires exactly once per waiting card because the same write clears the column.
- **Sweep**: a blocked card's `stuck` line names the blocker (`"queued 20m — waiting on #58 to land"`)
  instead of asking someone to dispatch it. An unblock restarts that card's stuck clock and re-arms
  its reminders, so the board re-ambers it if nobody picks it up.
- **UI**: the rail shows "Blocked by #N — reason" under the head with #N as the ordinary in-app card
  link; the card face and the List row carry a small quiet marker; both skins.
- **Orchestration**: SKILL.md tells the session to set the link rather than describe it, and to treat
  the auto-clear note as a dispatch trigger.

## Settings & executors (SHIPPED)

User verbatim: *"we need some sprint settings options here … our standing instructions should be to
use the lowest feasible model (sonnet by default, opus if the orchestrator deems that necessary) …
but we also need to support an alternate setup, which is where, instead of subagents, we use
additional tmux sessions and tmux-send — so I may want it to use grok subagents via tmux, for
example."* Scope ruling: *"Peer per card — mix grok-via-tmux and claude subagents"*.

- **Store**: `.sprint/config.json` (a file, not a table — hand-editable, survives `stop`, diffs in a
  terminal). `{"worker": {"model_policy", "default_executor", "executors", "concurrency"},
  "agent_name": "", "session_tmux_window": "", "special_instructions": "",
  "reviewer": {"enabled", "executor", "model"}}`.
  `model_policy ∈ {lowest_feasible (default), always_opus, always_sonnet}`; `executors` is
  name → `{kind: subagent|tmux, command?, session?, model?, note?}` (a `tmux` executor REQUIRES a
  command; `session` defaults to `sprint-workers`); `concurrency` is 1–20, default 3.
- **API**: `GET /api/settings` → `{settings, defaults, choices}`; `PUT` (and `POST`) merges a patch
  and rewrites the file atomically. **Unknown keys and bad enums are a 400 naming the exact field** —
  a settings file is read hours later by the session, so a silently-accepted typo reads back as "the
  defaults are fine". `worker.executors` replaces wholesale (there is no honest merge-delete);
  everything else merges per key. A hand-edited broken file falls back to defaults rather than taking
  the board down, and a change appends a `note` event (`actor: "server"`, carrying the new settings)
  so the session's own tail sees it. Read on demand, cached on mtime: `$EDITOR .sprint/config.json`
  needs no restart.
- **The sprint's name** rides on the same endpoint (`PUT /api/settings {"name": "..."}`) but is NOT
  in that file: it is the open sprint's title in the database, one source of truth, echoed back as
  `name` on `/api/settings`, `/api/board` and `/healthz`. `sprintd start --name "..."` is the launch
  path (and renames a board that is already up); the registry row follows it, so the title switcher
  and the hub label a board by what it is about rather than by its directory. One line, ≤60 chars;
  default is the project directory's name. A rename appends one `note` (`actor: "server"`).
- **The session's own name** is the OTHER name, and the two are not the same thing: the sprint is
  named after the work, the session running it is named like a colleague. User, verbatim: *"I also
  meant that the session agent gave themselves a name. Like "Chuck""*. It IS in `config.json`
  (top-level `agent_name`, default `""`), settable with `PUT /api/settings {"agent_name": "Chuck"}`
  and echoed as `agent_name` on `/api/settings`, `/api/board` and `/healthz`. ≤24 chars, one line;
  `""` takes it back. Every UI label that would read *Session* — a bubble in the sidebar, a bubble in
  a card thread, the rail's chat header, a row in the report library — reads the name instead, and
  falls straight back to *Session* when there isn't one. Note the level: this is the SESSION's name,
  while a card's `agent_name` is the worker subagent on that card.
  `sprintd start --agent-name "Chuck"` is the launch path and is **first-write-wins** — a board whose
  session already has a name keeps it, so a restart is the same colleague coming back rather than a
  new hire, and the self-restart exec drops the flag for the same reason. Renaming on purpose is the
  `PUT` (or the settings panel's *Session name* field). Taking a name appends its own one-line `note`
  ("the session is called “Chuck”"), never a `settings changed` line — it isn't dispatch policy.
- **Where that session can be reached** is the third top-level key, `session_tmux_window` (default
  `""`), and it is an address rather than a name: **last write wins**, `""` unregisters, and it is
  what makes dead-session autoheal possible at all. See *Autoheal* below.
- **Standing instructions** (SHIPPED, card #71) are the fourth: `special_instructions`, default
  `""`, ≤4000 chars, free text with its line breaks kept (it is a paragraph, not a name). User
  verbatim: *"and a place in the settings for special instructions"*. Per-sprint policy rather than
  per-card instruction — the card says what to do, this says how work is done on this board ("all UI
  work must be checked at the Fold width", "the gate here is `make check`"). Over the ceiling is a
  400 naming `special_instructions` with `too_long`; a hand-edited file that busts it reads back as
  none, never as half a sentence.
  - **The SERVER composes the block a brief carries**, and that is the point of the design: one
    heading (`## Sprint standing instructions from the user`), one string, published as
    `standing_instructions` on `/api/board`, `/api/cards/:num` and `/api/settings` — `""` when there
    are none. The session appends that string to every worker's and the reviewer's brief; the rail
    renders that string on a card. Neither composes its own, so a heading cannot become two
    headings, and an agent can always tell standing policy from the card's own words. SKILL.md's
    *The brief* owns the obligation; the server owns the words.
  - Always present on the payload, `""` and all — settings is a whole document, and a key that
    disappears when it is empty is a key every reader has to guess about (`agent_name`'s rule).
  - Applies from the NEXT dispatch. Changing it appends its own one-line `note` (`actor: "server"`),
    with the composed block as its detail — never a `settings changed` line, because it is not a
    `worker.*` key and nobody would recognise it in a list of field names.
- **The reviewer** (SHIPPED, card #70) is the fifth: `reviewer: {enabled: false, executor:
  "subagent", model: ""}` — see *The reviewer* below.
- **Per card**: `cards.executor`/`cards.model` (both nullable; NULL = the board's defaults), set via
  `POST /api/cards/:num/assign {executor?, model?}`, which refuses an executor that is not declared.
  Every card payload carries `dispatch: {executor, kind, command, session, model, source,
  is_default}` — the server's resolution of card-over-policy, so no surface has to redo it.
- **UI**: a quiet `Settings` link in the header opens a sheet (sprint name, session name, model policy
  segmented control, default executor select, concurrency, the executor list and its form, the
  reviewer, and the standing instructions box last) whose standing sentence is *a change takes effect
  for the NEXT dispatch — cards already running keep the executor and model they started with*. Every
  free-text field in it remembers where your caret is and puts it back after a repaint (#46): the
  sheet rebuilds its whole body on any structural change, and the standing-instructions box is a
  PARAGRAPH, so losing your place there means hunting for it in your own prose. Focus is given back
  only when the repaint did not come from you clicking something else. Card
  faces, List rows and the rail head carry a small `grok · tmux` tag **only when that card is not on
  the defaults**; the model is on the tooltip and the rail head, not on the crowded face.
- **Orchestration** (SKILL.md owns the procedure, since the session drives, not the server): lowest
  feasible model, opus only for a nameable reason, and the chosen model **stamped on the card at
  dispatch**. A tmux worker gets the same worktree, the same `assign`, and the SAME brief contract,
  delivered into `tmux new-window -t <session> -n sprint-card-<num>` with the tmux-send skill's
  verified send (never raw `send-keys` — the Enter gets swallowed and the brief sits unsent).
  Follow-ups are `tmux-send` instead of `SendMessage`. Liveness is the pane plus the board's own
  events: a window whose `pane_current_command` fell back to a shell is a dead worker, and a
  non-terminal card there is `failed` + Retry (the tmux analogue of killed-agent detection). Terminal
  states kill the window, never the shared session.
- **The two executors fail differently, and that difference is load-bearing.** A subagent that stops
  is a signal in itself — the session finds out it stopped and can act. A tmux CLI agent has no such
  backstop: it can end its own turn silently, mid-card, with nothing posted and nothing telling the
  session anything happened, and the window just sits there at an idle prompt until someone types
  into it. Evidence: the first live grok-via-tmux batch had two of seven workers stall exactly this
  way, and both resumed instantly once prompted again. So silence-at-an-idle-prompt is the *expected*
  failure mode for a tmux executor, not an anomaly — the mitigations are the standing "never end a
  turn without posting" line every tmux brief closes with, and the amber runbook's tmux-first
  diagnostic (`tmux capture-pane`, then a continuation prompt if it shows idle) rather than a plain
  ping. See SKILL.md's tmux-dispatch and `agent_silent` sections.

## Autoheal — when the SESSION dies (SHIPPED)

User verbatim, on the day it happened twice: *"but also, how can we help this autoheal in the
future?"* A Claude session was killed at a provider limit. Its board server was untouched — still
serving, still sweeping, still accepting cards — but the brain was gone: five cards the user had
already approved sat in `integrating` for six hours, sixteen sat queued, and the only thing that
recovered it was a human noticing and typing a recovery brief into its tmux window by hand. Every
fact needed to notice was already on the board (its orchestrator cursor stopped at 11:15 and never
moved), and nothing was in a position to act on them.

Three pieces in three places, because no single process can do it — the dead session cannot restart
itself, and its board must not spawn processes.

- **1. Registration (the session, at boot).** `session_tmux_window` in `.sprint/config.json`
  (top-level, beside `agent_name`), settable with `PUT /api/settings {"session_tmux_window":
  "russ-machine"}` or `sprintd start --tmux-window <target>`. SKILL.md boot step 5: detect tmux
  (`$TMUX` + `tmux display-message`) and register what tmux says, verbatim. **Outside tmux, register
  nothing** — autoheal simply does not apply, which is a fine outcome and much better than a guessed
  window (the first hand-run recovery guessed wrong and a session that did not own that board started
  posting to it). **LAST write wins**, the opposite of `agent_name`: a name is an identity that must
  survive a restart untouched, a window is an ADDRESS and a session that moved has to correct it.
  `""` unregisters. Validated as a tmux target (`^[A-Za-z0-9%][A-Za-z0-9._:@/-]{0,79}$`) because the
  string becomes argv — anything readable as a second argument or a shell fragment is a 400 at the
  door, and a hand-edited config with a broken one reads back as no window at all.
- **2. Detection (the board's sweep).** One `session_dead` event, **once per death episode**, when
  all three hold: no proof of life for `SPRINT_SESSION_DEAD_SECONDS` (default 1200 / 20 min), AND
  cards exist in a state the SESSION owes an action on (`queued`, `in_progress`, `integrating` — not
  `needs_you`/`ready`, which are the human's court and where waking a session achieves nothing), AND
  no active account-kind limit. **Proof of life is three signals and they are all acts**: the drain
  cursor moved, the waiter long-polled, or the session posted as `actor: "session"`. A `user` event
  is deliberately not one (the russ outage had the user still typing into a board dead five hours),
  and neither is a `server` event, or the sweep would keep resurrecting the session it is complaining
  about. The episode runs from that last proof of life, exactly the way a card's stuck episode runs
  from its last transition (#67): coming back re-arms the notice. Detection is independent of
  revivability — a board with no registered window still says it died, and the event's detail says
  why nothing will happen about it.
- **3. Revival (the hub).** The hub is the natural home: it is the one machine-wide process that
  already polls every board and is not itself one of the sessions that can die. On each poll, for a
  board whose `/api/board` says `autoheal.revive_wanted`, it runs `tmux-send <window> <brief>` as a
  subprocess (`SPRINT_TMUX_SEND`, default `~/.local/bin/tmux-send`; absent = one log line and skip,
  checked BEFORE claiming so a machine without the skill never burns a board's budget;
  `SPRINT_AUTOHEAL=0` disables). Every decision belongs to the board, which owns the durable event
  log the guard is written in: the hub asks `POST /api/autoheal/revive`, which under one lock checks
  the guards, writes the `revive_attempted` event and hands back the brief. **The claim is written
  before the message is sent**, for the same reason the self-restart stamp is (#62): the case the
  guard exists for is the one where what happens next never comes back. The hub reports delivery to
  the same endpoint with `attempt_seq`, which appends a `revive_result` and spends no slot.
- **The brief** is generated by the board from its own state, in the shape of the one that worked by
  hand: the verdict, the cursor gap (`cursor: 750 of 773 — 23 events you never read`), the stranded
  cards by number and pile (approved-but-never-merged / in motion / queued), an ownership header
  naming the `project_root` (*"if this is not the project this session runs, reply “wrong session”,
  touch nothing, and stop"*), and an ORDER whose first instruction is **do not dispatch anything
  first**: catch the cursor up, land the approved branches one at a time gating each, then
  re-dispatch from card timelines. `GET /api/autoheal?brief=1` renders it without sending it. Since
  #79 the brief is the **re-grounding** procedure run with `reason=revival` (see below) — same
  builder, same gathered state, wake-up framing.
- **Crash-loop guard**, mirrored from self-restart (#62) — same three questions, same bias toward
  doing nothing: never twice inside `SPRINT_REVIVE_MIN_INTERVAL` (default 600 / 10 min) per board, at
  most `SPRINT_REVIVE_MAX_BURST` (3) inside `SPRINT_REVIVE_BURST_WINDOW` (3600), then one
  `revive_gave_up` event per episode and silence.
- **Submission is not revival.** The second outage of the day had the session's process alive with a
  `/status` dialog open in its terminal: tmux-send delivered and verified the message and nothing
  happened, because an undismissed modal blocks the session from processing anything. So a wake-up
  counts as having worked only if a proof of life lands within `SPRINT_REVIVE_GRACE_SECONDS`
  (default 180); a delivered message that never stirs the session is a failed attempt and spends its
  slot like any other. The give-up line branches on which failure it was, because they have different
  next acts: *"the wake-up was delivered but the session never stirred; its terminal may be blocked
  by an open dialog. Check the window by hand"* vs *"the wake-up could not be delivered to tmux window
  “X”"*.
- **Limit-aware.** While a machine-wide account window is on, a silent session is **parked, not
  dead** — nothing it could be woken to do would run. The rule stays out of the way entirely
  (`reason: "account_limited"`, no event, no claim) and #47's Resume flow re-arms it.
- **UI.** No new chrome. The board's existing offline banner gains one clause — `session offline —
  revival attempted 12:03`, or the give-up sentence when autoheal has stopped trying — and nothing
  else moves. The hub page carries the same line on that board's row in amber, which is the only
  place a person actually finds out: the board it happened on has no session left to tell anyone,
  which is the whole reason a landing page that outlives them exists.

## Re-grounding — when the SESSION degrades or is reset (SHIPPED)

The other half of the same problem autoheal solves, from the live end. A session tending dozens of
cards degraded until the user said it had gone "fully retarded" and cleared its context by hand.
Nothing here can tune Claude Code's own compaction — a summary of a long conversation is lossy, and
that is the whole of it. What can be fixed is the board: if the board is complete enough, a session
that remembers nothing is as good as one that remembers everything, and a context wipe stops being a
leap of faith.

#68's wake-up brief already proved the read works — it rebuilds a session out of board state with
zero reliance on memory. Its only flaw was its framing: it said *"you died"*, so nothing else could
use it. So it is now **one procedure with a reason**, and the reason changes the words around the
facts, never which facts are gathered.

- **`GET /api/reground?reason=revival|boot|manual_reset|periodic`** (default `boot`; anything else is
  a `400 bad_reason` naming `field: "reason"`). Returns the composed `brief` plus the state it was
  built from as fields: `agent_name`, `standing_instructions`, `cursor`/`head`/`pending`, `cards`
  (**every non-terminal card**, grouped by state — not just the three the session owes),
  `sidebar` (last 10 lines, newlines collapsed and each capped at 160 chars because a revival brief
  is TYPED into a tmux window), `board`/`project_root`/`url`/`port`, and `cadence`.
- **One builder.** `reground_state()` gathers; `reground_brief(reason)` frames; `revive_brief()` is
  the `revival` caller and nothing else. A reason may change the opening line, the `in_progress`
  label (*"agent probably dead"* at a wake-up, *"in motion right now"* on a routine check — the one
  place a wrong framing gets live agents re-dispatched on top of), the ownership header (only a
  wake-up can land in the wrong window), the step list, and the closing line. It may never change
  what is gathered.
- **The wake-up is unchanged.** Every line #68's brief said, it still says, word for word and in the
  same order; it gained the shared context (name, standing instructions, the cards parked on the
  user, the sidebar tail) that every other reason gets. Regression-tested line by line.
- **Entry points, all the same call** (SKILL.md step 1b): cold boot and resume (`boot`), a session
  woken by autoheal (`revival`), a session whose context the user cleared (`manual_reset`, run as its
  very first action), and the cadence (`periodic`).
- **Cadence rule.** The session re-grounds **every `REGROUND_EVERY_TOOL_CALLS` (50) tool calls or
  `REGROUND_EVERY_MINUTES` (30) minutes of continuous work, whichever comes first**, regardless of
  felt fatigue — degradation is not felt from the inside. 50 tool calls is about one drain cycle plus
  a couple of dispatches; 30 minutes is the staleness sweep's own threshold, so the session re-checks
  itself on the beat the board already uses to notice neglect. Advisory and unenforced: the numbers
  are served in `cadence` so the skill and the server cannot drift, and the session keeps its own
  count — no DB column, nothing to sweep.
- **Durable by default** (SKILL.md, the "reply where the user is" family). Every decision, finding or
  ruling that matters goes onto the board the moment it is made, never held only in conversational
  memory. The test: *if you were wiped right now, would the next session know this?* This is the half
  that actually failed on the night the card came from — the procedure above can only hand back what
  somebody wrote down.

## Non-goals (v1)

Multi-user, public exposure, auto-dup-detection, priority pickers/drag-reorder, batch blind-approve,
session summaries beyond the end-sprint card, phone-portrait optimization, agent-list integration
(possible later mirror).
