# Event-driven Sprint coordination

Sprint's watcher is ordinary Python, not an agent. Quiet boards consume no model
calls. It watches the local board, filters routine telemetry, and uses
`tmux-send` only when the registered coordinator has an actionable event batch.
The coordinator handles the batch and ends its turn. Workers report completion,
questions, or failures to the board rather than being polled by the coordinator.

## Start and handoff

Update the board and helper from the same revision first. The helper refuses an
older board without the dispatch capability so it cannot race the old hub
reviver. Confirm that your session owns the board, obtain its exact pane ID with
`tmux display-message -p '#{pane_id}'`, and register it through Sprint settings.
Never guess a pane or take another session's binding.

```sh
bin/sprint-dispatch start --project-root /absolute/project --target '%5'
bin/sprint-dispatch status --project-root /absolute/project
bin/sprint-dispatch stop --project-root /absolute/project
```

`start` detaches a singleton watcher and confirms its process/lock. `run` is the
foreground form for launchd/systemd. `status` reports the OS lock, last heartbeat,
read position, actual board acknowledgment, delivery count, and status. On
handoff, stop the watcher, clear the old target, and let the new owner register
and start its own watcher. The helper never changes board ownership itself.
A changed/cleared target stops the old watcher. An ownership change observed
immediately before a send also prevents delivery; registration changes and
external terminal input cannot be made one atomic operation, so stop the old
watcher before rebinding.

Stop ingress before ending a sprint. Do not also run a model-held tail or a
second scheduler. `start` is idempotent for the same target; another target or a
reset while running is rejected.

## What wakes the coordinator

- User input and actions, including approvals, answers, and submitted cards.
- Worker evidence, questions, errors, or chat; actionable state transitions.
- Stalled/silent worker notices, cleared account limits, freed dependencies,
  user settings changes, or a pending server restart requiring intervention.

Session echoes, heartbeat/cursor events, routine progress/phase updates, and
ordinary server notes do not wake a model. The watcher keeps a private read
position. It **never advances the board's orchestrator cursor** and sends no
waiter header. Reading an event is not evidence that someone handled it.

One API page is read per tick (two seconds by default); a backlog is drained
before one compact wakeup is sent. The signal contains the project, sequence
boundary, and at most 20 card IDs, not the conversation or message bodies. The
coordinator reads authoritative card contents and checks current assignments.
It must acknowledge handled events through the existing cursor API.

## Limits and failure handling

Only one wakeup can await acknowledgment at a time. Later events remain pending
until that batch is acknowledged. Cursor movement acknowledges only through the
reported sequence, not later events. A final pre-send cursor read avoids waking
for work another turn has already handled.

Delivery intent is persisted and fsynced before the terminal send. On send
failure, uncertain delivery, process crash, or a five-minute acknowledgment
timeout, no automatic resend occurs. Status becomes `needs_attention`, visible
in board liveness and the CLI. Inspect the target, resolve dialogs/drafts or
account limits, then `stop` and `start --reset` if a new wake is actually needed.
Reset discards delivery state and rereads from the board cursor; it does not
acknowledge or delete board events. Do not automate resets.

A rolling default cap of **30 wake attempts per hour** prevents feedback from
becoming an unlimited wake loop (`--max-wakes-per-hour 1..120`). The cap persists
across ordinary restarts; `budget_limited` retains pending work and resumes when
a slot expires. It limits wake attempts, not the token consumption inside a
worker/coordinator turn. Explicit reset clears the cap too; reserve reset for
investigated recovery. Account-wide limits pause unsent work automatically.

A lock plus a fresh lease makes the watcher the sole normal sender. Server
liveness distinguishes an idle watcher from an unacknowledged wake; its heartbeat
is not coordinator progress. The hub's old autoheal path is suppressed while
that watcher owns delivery, including during budget/ack holds. A dead watcher
releases its lock immediately; a wedged watcher loses its lease after 30 seconds,
allowing the existing bounded hub recovery to act. Without a hub/supervisor,
crash recovery still requires restarting the watcher; do not claim otherwise.

Board/API outages are retried by Python without model calls. A backwards board
cursor/history fails closed instead of silently skipping a restored backlog.
No token, auth URL, or message body is stored in the dispatcher state. Its files
under `.sprint/` are mode 0600. It invokes `tmux-send` without shell evaluation,
with `--no-stash --wait 0`; it will not overwrite a person's draft or force
through a permission dialog. An uncertain send remains a manual recovery case.

## Supervision

For unattended operation, supervise `run` instead of nesting `start` inside a
supervisor. Example launchd ProgramArguments (substitute actual absolute paths):

```xml
<key>ProgramArguments</key>
<array>
  <string>/absolute/sprint/bin/sprint-dispatch</string>
  <string>run</string>
  <string>--project-root</string><string>/absolute/project</string>
  <string>--target</string><string>%5</string>
</array>
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>
<key>ThrottleInterval</key><integer>30</integer>
```

Clean stop/ownership change exits successfully and should stay stopped. KeepAlive
on *every* exit would fight a handoff; do not use it. No launch service is
installed automatically, and this change does not attach to existing boards.

## Luna and specialists

For a **new dedicated coordinator session**, choose Luna/low reasoning if it is
available in your account. Model choice belongs to the session, not the watcher.
There is no extra paid classification call in the deterministic process.

Give the coordinator small routing context: relevant card, incoming event,
current worker ownership, and standing policy. Use Grok for routine building,
and stronger agents for complex work, review, or consequential decisions. Each
assignment needs a concrete outcome, scope, and time/usage budget. Workers should
report results once and stop. No full-history forks, recurring status requests,
or indefinite reviewer conversations. A lower-priced model running the same
polling loop would still be the wrong design.

## Verification

`python3 -m unittest discover -s tests -p test_sprint_dispatch.py -v` exercises
quiet/noise filtering, batching, partial acknowledgments, pagination, ownership,
account limits, uncertain delivery, rolling wake caps, real HTTP semantics,
singleton process startup/restart/stop, and server lease/recovery behavior.
Tests use temporary boards and fake terminal delivery; no live board, model, or
user tmux pane is contacted. Existing server and adapter suites remain required.
