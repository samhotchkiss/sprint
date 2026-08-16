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
5. Everything you do happens in the right rail: click a card and its
   whole thread opens there, with the evidence packet — claim, "check it
   yourself" steps, screenshots — and Approve / Bounce / Reject pinned
   at the bottom where they can't be scrolled off. Work that shipped
   together is **one card** in Awaiting review: open it and you get an
   outline of everything that changed, one section per change, with a
   single Approve under the lot. Click **Chat** for the session itself;
   it's the same brain as the terminal, so it can act on what you say
   there, not just answer. The rail holds one thing at a time.
6. When an agent's output is a **document** rather than a line — an audit, a
   findings write-up, a comparison — it attaches it as a report. It reads in
   the thread as a title you can skim and expand, and a quiet **Reports** link
   appears in the header, which is your library of everything this sprint
   wrote. The link isn't there until there's something behind it. Agents are
   told to write **markdown**: the board renders `.md` itself, in its own
   typography and skin (raw HTML inside it is escaped, never run). `.html` is
   still accepted for documents that arrive already-HTML, but it renders
   sandboxed and unstyled, so it looks plainer.

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
difference because the session's ingress stays attached the whole time
it's alive — being attached is the proof of life, not the drain cursor,
which legitimately falls minutes behind during a busy dispatch.

## How the session hears about you

Two modes, and both end in the same place: the session drains
`/api/events` from its saved cursor, which is the only source of truth
either way. **`sprintd tail`** is the primary one — it holds a single
streaming connection to the board open and prints exactly one compact
JSON line per real event, so the session runs it under its streaming
monitor and gets woken once per thing that actually happened and never
in between (heartbeats, cursor moves and the session's own posts never
reach it — pass `--include-self` if you want the raw stream; a quiet
board costs zero wakeups). It never exits on its own: it reconnects through
dropped connections by itself and resumes from the last event it saw,
tells the session in one line if the board has been unreachable for a
minute or if it restarted underneath, and counts as session liveness for
as long as it's attached. **`sprintd wait`** is the fallback — a 60s
long-poll the session relaunches each time it returns, which is the
older, noisier arrangement (a wakeup a minute whether or not anything
happened) and still the right tool where the streaming monitor isn't
available, or as a slow heartbeat that would notice a wedged tail. You
can watch either one yourself: `bin/sprintd tail --user-only` in a
terminal prints a line the moment you post anything on the board.

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
- **Every link signs you in, whichever board you were on last.** Browser
  cookies are scoped by host and ignore the port, so all your boards
  share one jar. A link carrying `?t=…` always wins over whatever cookie
  is already there, and each board keeps its own cookie
  (`sprint_token_<port>`) — so signing into one board never signs you out
  of another. If you do land unauthorized (a rotated token, a link from
  before a restart), you get a short page telling you where to get a
  working link, not a wall of JSON.
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

## Reach it by name

`http://100.67.2.108:8300/` is not a thing anyone types on a phone. If
the machine running the hub is on a tailnet with MagicDNS, you can
publish the hub as a **Tailscale Service** and get a real name instead:

```
bin/sprint-serve-setup            # prints exactly what it would do
bin/sprint-serve-setup --apply    # does it
bin/sprint-serve-setup --verify   # checks the name answers
```

Then, from any device on the tailnet:

| you type | what happens |
|---|---|
| `https://sprint.<your-tailnet>.ts.net/?t=…` | first time on a device — signs you in, cookie lasts a year |
| `https://sprint.<your-tailnet>.ts.net/` | every time after that |
| `sprint/` | the bare name, where the device honours the tailnet search domain |

The script prints all three filled in with your tailnet and your hub
token. It never runs a privileged command without `--apply`, and it is
safe to re-run: `tailscale serve` config is declarative.

A few things worth knowing before you run it:

- **It cannot do the whole job alone.** Defining the service and
  granting access to it live in the tailnet policy file / admin console.
  The script prints the exact JSON to paste and the exact page to open,
  and checks whether it's been done. Three one-time steps: define
  `sprint` with endpoints `tcp:443` and `tcp:80`, a grant letting members
  reach `svc:sprint`, and an `autoApprovers` entry so this machine is
  approved as its host without a click.
- **Why a service and not `tailscale serve 8300`.** Plain serve
  publishes under the *machine's* name, and it takes over that machine's
  `/` on :443 — which usually already has something on it. A service has
  its own virtual IP, its own name, and its own certificate, so it
  collides with nothing and stays true if the hub ever moves boxes.
- **The bare name is a bonus, not the deliverable.** `sprint/` only
  resolves on devices that accept the tailnet's search domain, and it
  only works over plain HTTP — the certificate is issued for
  `sprint.<tailnet>.ts.net`, so `https://sprint/` fails on the name, and
  no amount of configuration can fix that. The hub bounces a bare-name
  visit to the full name so you land on one origin and one sign-in
  either way. **The FQDN is the URL to bookmark.**
- **The boards are deliberately not proxied.** The hub links to each
  board at its own `http://<tailnet-ip>:<port>/?t=…`, which already
  works from every device on the tailnet. Those links are absolute, so
  they keep working however you reached the hub.
- **Nothing about this is required**, and nothing changes if you skip
  it: the hub still answers on its IP and port exactly as before.

## When a provider limit kills your agents

Agents get killed by usage limits, several at once, and the kill message
is usually the only thing that says when it ends:

```
You've hit your session limit · resets 11:50pm (America/Denver)
```

Tell the board, and it takes it from there:

```
bin/sprint-limit declare --model fable --resets "11:50pm" --source "kill message"
bin/sprint-limit list          # what's limited, and how long is left
bin/sprint-limit clear 3       # it came back early
```

While the window is open the board carries one quiet line — *"fable is
rate-limited until 11:50pm — work is running on opus"* — in the same
place the session-offline banner goes. When it passes, the board emits a
single `limit_cleared` event, and the session's job (per
`skills/sprint/SKILL.md` step 5b) is to put whatever it downgraded back
on the model it should have been on.

`--resets` takes the provider's own wording: a clock time (`11:50pm`,
`23:50` — meaning the *next* time it comes round), that same clock time
with the zone in parentheses, an ISO 8601 timestamp, or an epoch. It
prints back the exact instant it landed on, so a typo is caught before
the board acts on it.

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

**The board restarts itself when its code changes.** Nobody has to notice
that `bin/sprintd` was edited and go bounce the server: the board watches
the file it is running, and when that file becomes different code it
re-execs itself in place — same process id, same port, same token, same
open browser tab. It waits for the file to settle, refuses to exec into
anything that will not even compile, waits for in-flight requests to
finish, and will not restart twice inside a minute (or more than three
times in ten). When a guard stops it, that goes in the event log for the
session to pick up; it is never a notice telling you to run anything. The
board's log line for it is `restarted to pick up new code`.

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
                                     | GET /api/events?after=SEQ  (drain loop — the truth)
                                     | sprintd tail --after SEQ   (SSE, one line per event)
                                     | sprintd wait --after SEQ   (long-poll fallback)
                                     |
                    +----------------------------------+
                    |   Claude Code session (you)       |
                    |   the only brain: dispatch,        |
                    |   batch, resume, verdict, nudge     |
                    +----------------------------------+
                                     |
                                     | Agent tool, subagent_type: sprint-worker
                                     | ...or a tmux window + tmux-send, per card
                                     v
                    one git worktree + branch per card/batch,
                    fetched fresh from origin/main, never the
                    primary checkout or a serving dev worktree
```

## Naming a sprint

A board is called after its directory unless you say otherwise, which is
fine for one board and useless for four ("russ", "sprint", "project"...).
Name it after the work instead — at launch:

```
bin/sprintd start --name "Board self-restart and naming"
```

That name is what the header, the title switcher and the hub all show,
and it goes into `~/.sprint/registry.json` so other boards' switchers see
it too. `sprintd start --name` on a board that is already running is a
rename, so a session that re-boots its board every morning can keep the
name current. You can also rename it from the board's **Settings**
sheet (first field), or over the API:

```
curl -X PUT $SPRINT_SERVER/api/settings -H "Authorization: Bearer $SPRINT_TOKEN" \
     -H 'Content-Type: application/json' -d '{"name":"Billing week"}'
```

Names are one line, 60 characters or fewer. Leave it alone and it stays
the directory name.

## Settings — model policy and executors

The header's quiet **Settings** link edits `.sprint/config.json`, which is
this board's dispatch policy (and is a plain file you can also edit by
hand — the server re-reads it on change, no restart). The sheet's first
field is the sprint's **name**, which is not part of that file — it is
the sprint itself, stored in the board's database:

```json
{"worker": {
  "model_policy": "lowest_feasible",
  "default_executor": "subagent",
  "executors": {"claude": {"kind": "subagent"},
                "grok": {"kind": "tmux", "command": "grok", "session": "sprint-workers"}},
  "concurrency": 3}}
```

- `model_policy` — `lowest_feasible` (sonnet by default; the session
  stamps opus on a card that genuinely needs it), `always_sonnet`, or
  `always_opus`.
- `executors` — how a worker is run. `subagent` is a Claude worker via
  the Agent tool; `tmux` is any CLI agent (grok, codex, …) started in
  its own tmux window and driven with the `tmux-send` skill. The choice
  is **per card** — a sprint can mix grok-via-tmux and Claude subagents —
  and a card that is not on the defaults says so on its face: `grok · tmux`.
- A change takes effect for the **next** dispatch; cards already running
  keep the executor and model they started with.

The procedure for driving a tmux worker (window naming, the verified
send, liveness by pane, cleanup) lives in `skills/sprint/SKILL.md`,
including a by-hand checklist — that path opens real windows and starts
real agents, so it is verified by a human, not by the test suite.

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
