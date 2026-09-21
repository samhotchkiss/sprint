# Continuity inbox

Standalone durable lifecycle inbox for live handoff. It does **not** send
tmux-send, call a model, POST board mutations, or own the coordinator loop.
Root wires supervisor delivery and the lease.

## Files

- `sprint_coordinator/continuity.py`
- `tests/test_continuity.py`

## What it records

After an explicit snapshot and cutoff seq:

- User `answer`, `verdict`, `action`, `retry`
- Worker `evidence`, `error`, `question`
- `state` transitions to `queued`, `ready`, `integrating`, `failed`

Ignored: `progress`, `phase`, `heartbeat`, `cursor`, `session` echoes, user
`chat`/`submitted` (chat generation), and anything at or before cutoff.

The board has already applied answers/verdicts/actions. This module does not
re-POST them.

| Event | `intent` | Meaning |
| --- | --- | --- |
| user `answer` | `resume` | needs_you was answered; work may continue |
| `verdict` bounce | `resume` | bounce back to the worker |
| `verdict` approve/reject/cancel/hold | `notice` | record only; no merge steal |
| `retry` / action retry | `resume` | |
| other user `action` | `notice` | |
| worker evidence/error/question | `notice` | supervisor must see it |
| state queued/ready/integrating/failed | `notice` | |

## Snapshot

`register_snapshot(cards, cutoff_seq)` once. No historical replay. Unfinished
cards (`queued`, `in_progress`, `needs_you`, `ready`, `integrating`) plus
held cards:

- **held** → `dispatch=excluded` (never `prepare_delivery`)
- **needs_you** / **queued** / **ready** → `waiting` (not work to start)
- **in_progress** / **integrating** → `occupied`

`excluded_panes()` is every pane recorded on those cards. Do not launch a new
assignment into them.

## Delivery

```python
from sprint_coordinator.continuity import ContinuityInbox

inbox = ContinuityInbox(store.conn)  # or a sqlite3 connection
inbox.register_snapshot(cards, cutoff_seq=head)
inbox.record_events(events)
batch = inbox.prepare_delivery(card_num)   # durable before send
# root: tmux-send via sprint-session-worker only
inbox.mark_submitted(batch["id"], status="submitted")  # or "uncertain"
inbox.acknowledge(batch["id"], through_seq=batch["through_seq"])
```

- One in-flight delivery per card. Newer events stay `pending`.
- `uncertain` stays visible. `prepare_delivery` will not create a second send.
- `acknowledge(delivery_id, through_seq)` covers only that delivery's items
  with `seq <= through_seq`. Later events and other cards are untouched.

Restart reads the same tables. Duplicates (`seq` / `event_id`) are ignored.
