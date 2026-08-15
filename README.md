# sprint

A kanban board that sits on top of a live Claude Code session: dump
feedback or work items at a tailnet URL, the session dispatches
subagents to work them, and every interaction you make on the board —
answering a question, approving a card, dropping a note in the sidebar
— is passed straight through to the terminal session underneath it.
The board holds state; the session is the only brain. It's called
"sprint" because the same tool works for a quick feedback pass and for
a proper project sprint.

## Install

For local development/testing, point Claude Code at this repo directly:

```
claude --plugin-dir /path/to/sprint
```

For distribution, install it from a marketplace:

```
/plugin install sprint@<marketplace>
```

Either way, once loaded, say **"start a sprint"** (or "resume the
sprint", "feedback session", "open the board") in a Claude Code session
running inside the project you want a board for.

## Dependencies

- Python 3.9+ (stdlib only — no `pip install` anywhere in this plugin)
- git
- tailscale (optional — if present, the board binds your tailnet IP so
  it's reachable from your phone/other machines; if absent, it degrades
  to loopback-only and says so loudly)

## Quickstart

1. `cd` into the project you want a board for, inside a `claude` session
   running in tmux (see "Power-outage recovery" for why tmux matters).
2. Say **"start a sprint."** The skill runs `sprintd doctor`, starts the
   server, and prints a URL like `http://100.x.x.x:8377/?t=<token>`.
3. Open that URL. Drop feedback into the submit box at the top — text,
   pasted screenshots, or both. Cmd/Ctrl+Enter submits.
4. Watch cards move across the board (Held → Queued → In progress →
   Needs you / Blocked → Ready → Done) as the session dispatches
   subagents, one worktree and branch per card (or per batch of related
   cards, if you told it not to start yet and let a pile build up).
5. Answer questions inline on the card face, approve/bounce/reject from
   the card drawer once something's `ready`, or just chat in the
   sidebar — it's the same session, so it can act on what you say there,
   not just answer.

Dumping a bunch of issues at once? Say "hold" first — new cards land in
a `held` column instead of dispatching immediately, so the session can
propose sensible batches (e.g. a dozen small CSS issues → one agent, one
branch) instead of spinning up a worktree per card. Release when you're
ready.

## What the session dot means

Top-right of the board, next to "session":

| Dot | Reads | What's true |
|---|---|---|
| green | session live | It's caught up on everything you've sent. |
| amber | session catching up | It's attached and working, but hasn't read the last few items yet — usually because it's mid-dispatch. Nothing to do; it'll catch up. |
| red + banner | session offline | Nothing is listening. What you send waits in the queue and gets picked up when the session comes back. |

Only the red state raises a banner. "Catching up" is a working session,
so it stays on the dot where you can ignore it. The board knows the
difference because the session's waiter polls `/api/events` continuously
while it's attached — that polling is the proof of life, not the drain
cursor, which legitimately falls minutes behind during a busy dispatch.

## Multiple sprints on one machine

One Claude Code session is one sprint, and you'll often have several
going at once in different tmux windows — usually one per repo. Each
board is its own server on its own port, which means several URLs to
keep straight and no way to tell, from the board you're looking at,
that something on a *different* board has been waiting on you for
twenty minutes.

The hub fixes that. Start it once per machine:

```
bin/sprintd hub
```

It prints `http://100.x.x.x:8300/?t=<hub-token>` — bookmark that one
URL. Every board registers itself when it starts, so the hub lists all
of them with, per row:

- the project name (the repo folder), and how many cards **need you /
  are ready / are in motion**
- the session dot (live / catching up / offline) and how long ago
  anything happened
- an **open board** link that carries that board's token, so clicking
  through lands you already signed in
- any sprint with something in **Needs you** sorted to the top, with an
  amber rail and how long the oldest question has been waiting
  ("stuck 22m")

It refreshes every 10 seconds and never guesses: it health-checks each
board rather than trusting the registry file, so a board that died
shows greyed with "unreachable — last seen 4m" instead of quietly
vanishing or reporting stale counts.

```
bin/sprintd hub --stop        # stop it
bin/sprintd hub --new-token   # rotate the hub's own token
```

Details worth knowing:

- **The hub has its own token**, in `~/.sprint/hub-token` (0600), with
  the same `?t=…` → cookie handshake the boards use. It links into
  every board on the machine, so it's effectively a keyring — it is
  never left unauthenticated. Its cookie name is deliberately different
  from a board's, so signing into the hub never signs you out of a board.
- **Boards register themselves.** `sprintd start` writes a row into
  `~/.sprint/registry.json` (one row per project root, so restarting a
  board updates its row rather than adding a second one); `sprintd stop`
  removes it. The file is advisory: the hub health-checks every row, and
  forgets one only once the process is gone *and* the port has been
  unreachable for a while.
- **Nothing about the hub is required.** Don't start it and everything
  works exactly as before; the registry is just a small JSON file.
- `SPRINT_REGISTRY=/path/to/registry.json` (or `--registry`) moves the
  whole machine-wide state — registry, hub token, hub pidfile — somewhere
  else. That's how the tests run without touching your real `~/.sprint`.
- The hub is started once per machine, by hand. There's no launchd
  parity for it yet (`sprintd doctor --install-launchd` covers boards
  only).

## Power-outage recovery

The Mac this runs on is online 24/7, but power outages happen. Recovery
is meant to be one line:

```
tmux new -s sprint 'claude'
```

then, inside that session:

```
/sprint resume
```

`sprintd start` is idempotent and handles a stale PID file from an
unclean shutdown on its own. Resume drains the event log from the last
cursor the session persisted before it went down, reattaches any agent
that was still working (and tells it to re-verify its own worktree state
first — a power cut mid-write leaves a half-finished file that looks
like real work), and picks the drain loop back up exactly where it left
off. There's no separate "resume mode" to remember the syntax for — the
same "start a sprint" trigger works too; the skill figures out on its
own whether this is a fresh boot or a resume.

**Your board URL survives a restart.** `sprintd stop` followed by
`sprintd start` reuses the same port *and the same token*, so the URL you
bookmarked keeps working and any browser already logged in stays logged
in. The token is kept in `.sprint/token` (mode 0600), separate from
`.sprint/server.json` — `server.json` is a liveness record and is deleted
when the server exits; the token is an identity and isn't. To deliberately
rotate it (and log every open browser out), start with `--new-token`:

```
bin/sprintd stop
bin/sprintd start --new-token      # prints a fresh URL; the old one 401s
```

`--token <value>` still forces a specific token, and whatever you force
becomes the one that's reused on the next plain `start`.

Optional: `sprintd doctor --install-launchd` writes a macOS LaunchAgent
(`~/Library/LaunchAgents/com.sprint.<hash-of-project-root>.plist`) with
`KeepAlive` so the board process itself comes back after a reboot
without you doing anything — you'd still need the `tmux`/`/sprint
resume` step above to bring the *session* (the brain) back, since that's
a live Claude Code conversation, not a daemon.

## Architecture

```
 browser (board UI)                 you, via curl/Bash            worker subagents
        |                                    |                            |
        |  HTTP + SSE                        | HTTP (full API)            | HTTP (bearer token,
        v                                    v                            v  card-scoped only)
 +----------------------------------------------------------------------------+
 |                              bin/sprintd                                   |
 |   http.server + sqlite3 (WAL) — owns ALL state, decides NOTHING            |
 |   .sprint/sprint.db  .sprint/attachments/  .sprint/server.json  server.log |
 +----------------------------------------------------------------------------+
                                     ^
                                     | GET /api/events?after=SEQ  (drain loop)
                                     | sprintd wait --after SEQ   (latency optimization only)
                                     |
                    +----------------------------------+
                    |   Claude Code session (you)       |
                    |   the only brain: dispatch,        |
                    |   batch, resume, verdict, nudge     |
                    +----------------------------------+
                                     |
                                     | Agent tool, subagent_type: sprint-worker
                                     v
                    one git worktree + branch per card/batch,
                    fetched fresh from origin/main, never the
                    primary checkout or a serving dev worktree
```

The server never spawns, supervises, or judges an agent — it just
records events and enforces the state machine (illegal transitions
409, evidence gate 422s a bad `ready` packet). All of that judgment
lives in the session, driven by the skill in
`skills/sprint/SKILL.md`.

## Security posture

- **Tailnet-only bind.** The server binds your `tailscale ip -4` address
  plus `127.0.0.1` — never `0.0.0.0`, never a plain LAN interface. No
  tailnet available → loopback-only, and it says so instead of silently
  narrowing your reach.
- **Bearer token.** A random token is generated on first start and
  stored in `.sprint/token` (mirrored into `.sprint/server.json` while
  the server is up), both mode 0600. The browser gets it once via
  `?t=TOKEN` (sets a cookie); the API accepts either the cookie or an
  `Authorization: Bearer` header. Worker subagents receive the token
  only through their dispatch brief — it's never hardcoded, never
  checked in.
- **Single user, single tailnet.** This is not built for public exposure
  or multi-user access — see the spec's non-goals. If you need that,
  this isn't it yet.
