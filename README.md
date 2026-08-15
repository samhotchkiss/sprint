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
3. Open that URL. Hit **+ Drop work** and type — text, pasted
   screenshots, or both. Return sends; Shift+Return makes a new line.
   (You can also just paste an image anywhere on the page.)
4. Read the page top to bottom. The 4px meter is the shape of the whole
   sprint; **Needs you** is the only section that wants anything from
   you, and everything under it — In motion, Blocked, Queued & held —
   gets quieter on purpose. **LIST/BOARD** in the header swaps the
   reading order for the kanban columns; it remembers which you picked.
5. Answer a multiple-choice question straight from its row in the list.
   Everything else happens in the right rail: click a card and its whole
   thread opens there, with the evidence packet — claim, "check it
   yourself" steps, screenshots, Approve / Bounce / Reject — in the
   stream where it arrived. Click **Chat** for the session itself; it's
   the same brain as the terminal, so it can act on what you say there,
   not just answer. The rail holds one or the other, never both.

Dumping a bunch of issues at once? Say "hold" first — new cards land in
a `held` column instead of dispatching immediately, so the session can
propose sensible batches (e.g. a dozen small CSS issues → one agent, one
branch) instead of spinning up a worktree per card. Release when you're
ready.

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
  stored in `.sprint/server.json`. The browser gets it once via
  `?t=TOKEN` (sets a cookie); the API accepts either the cookie or an
  `Authorization: Bearer` header. Worker subagents receive the token
  only through their dispatch brief — it's never hardcoded, never
  checked in.
- **Single user, single tailnet.** This is not built for public exposure
  or multi-user access — see the spec's non-goals. If you need that,
  this isn't it yet.
