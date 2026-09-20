# Sprint coordinator implementation plan

September 20, 2026. Implementation approved and underway. No live coordinator
cutover has been performed. Existing boards retain their current owner.

## Outcome

Sprint continues running without an always-on model conversation. User questions
have their own response path and cannot get stuck behind a build or merge.
Code owns coordination; Jev supplies bounded language judgments; coding and
reasoning agents run specific assignments and return results.

## Evidence from Yunagi

The board repeatedly posts ownership/recovery replies instead of useful answers.
Examples: event 15444 says it is not dead, just heads-down; 15669 attributes the
silence to a long test run; 16065 and 16315 say the board is still active.
At inspection the board reports cursor 16298, head 16323, user event 16307 unread,
and recovery paused after three wakes in an hour. This is a snapshot, not a claim
that these values remain current.

The current design conflates transport health, progress on a job, and responsiveness
to a person. A global read cursor also cannot represent several independently
handled messages. Our earlier sprint-dispatch branch bounds wakeups but still
wakes one coordinator conversation and waits for its cursor. It is useful plumbing,
not the completed architecture.

## Responsibility split

Deterministic Python service beside sprintd:
- Persist incoming events, obligations, assignments, and outgoing commands.
- Enforce one coordinator owner through leases and fencing generations.
- Deduplicate delivery and apply changes only against the expected card revision.
- Dispatch by existing eligibility rules, capacity, dependencies, and user policy.
- Keep machine recovery notices out of user chat. Recovery never asks a model to
  prove ownership in prose; adapters register board, run, and assignment identities.
- Track service health, worker health, job progress, and response deadlines separately.
- Restart its own failed process through launchd/systemd; never confuse a surviving
  process or a transport heartbeat with a completed obligation.

Jev:
- Interpret natural-language requests as bounded intents. Mixed requests can carry
  several intents; independent yes/no questions prevent a single-choice router
  from dropping half a message.
- Choose among candidate cards supplied by code, including none/unclear.
- Identify whether a follow-up changes scope or belongs to a separate discussion.
- Help check whether a proposed reply addresses a specific question. This is an
  advisory semantic check, never proof of successful execution.
- Receive the message, relevant thread/card context, and explicit policy only.
  Event types, elapsed time, permissions, arithmetic, and worker availability are
  calculated in code. No model runs just because a timer ticks.

Generative agents:
- A short-lived response assignment handles discussion or judgment that templates
  cannot express. It can run while coding jobs continue. Use a configured separate
  session or service, not input queued behind the same busy terminal.
- Grok handles ordinary builds, the configured stronger agent handles complex work,
  and an independent reviewer checks completion. Retain the user's provider policy.
- Merge/deploy are separate serialized jobs. Authorization and current release rules
  are enforced in code; Jev does not grant permission or declare something shipped.

## Durable workflow

Use SQLite alongside the existing event store initially. Persist:
- event ingest cursor: the service safely recorded the event;
- obligation per user request: received, routed, assigned, answered, failed;
- assignment: job ID, run ID, worker/session ID, lease, attempt, last progress;
- outbox command: stable deduplication key, destination, delivery/acceptance state;
- judgment: input revision/hash, question version, model version, result, usage;
- reply: explicit links to the obligations it addresses.

Advancing the ingest cursor does not mark a request answered. Posting unrelated
release updates cannot satisfy a question. If newer input arrives before a reply
publishes, recheck the relevant thread revision and incorporate it when it changes
that answer. Do not invalidate replies because an unrelated card changed.

No claim of exactly-once terminal delivery: crashes can leave an uncertain send.
All agent-session messages go through `tmux-send`, as Sam required during
implementation. Use stable job IDs and private result artifacts for acknowledgments
and candidates; never bypass delivery with direct provider prompts, stdin, raw
`tmux send-keys`, or `paste-buffer`. An uncertain send requires reconciliation,
not another delivery. Detect dialogs and unavailable runners explicitly.

User-facing responses get reserved capacity. Suggested initial targets: routing
starts within two seconds; a simple factual answer is posted within ten seconds;
a substantive reply is targeted within thirty seconds. These are proposed service
objectives, not measured Jev latency guarantees. At the deadline, escalate to a
separate response worker once; do not emit repetitive 'still active' messages.

A stalled job is recoverable independently. Do not interrupt a live deploy or launch
another merge simply because a message reply is overdue.

## Jev integration and failure behavior

Use the existing Python stack and the documented HTTP endpoint or official Python
SDK. Pin the evaluated model version (currently documented as jev-1.13.0), retain
raw judgments, and validate thresholds on Sprint examples. Choice confidence is
not a guarantee of truth or authorization. Include abstention and validate allowed
IDs, transitions, source actor, and current state after inference.

Cache judgments by relevant input, prompt version, and model. Bound timeouts,
retries, parallelism, and daily cost. No automatic repeated reasoning-agent wake
when Jev is unavailable. Explicit button actions and known deterministic routes
continue; ambiguous natural language stays durable and escalates once under a
configured policy. Worker output cannot masquerade as user authorization.

Credentials: each installation provides TYPESAFE_API_KEY using a local secret
file/environment or platform secret store. The supplied key was saved only in
~/.config/sprint/typesafe.env with mode 0600. Never send it to the browser or
commit it, include it in diagnostics, fixtures, or example configuration.
No private board data has been sent to Jev during this investigation.

## Delivery sequence

1. Introduce per-request obligations and reply links. Keep the existing agent in
   charge while establishing a trustworthy measure of unanswered questions.
2. Build the durable scheduler, outbox, leases, and adapters. Exercise restart,
   duplicate delivery, a busy terminal, long builds, and wrong-session cases using
   fake workers before connecting real agents.
3. Evaluate Jev on representative Sprint messages, including corrections, multi-
   intent input, ambiguous approvals, repeated text, and unrelated worker updates.
   Run routing in shadow mode; measure errors, latency, cost, and fallback rate.
4. Give the service ownership of user-message routing and replies. Keep code/build
   assignments as-is initially. Verify response deadlines while builds are busy.
5. Move dispatch, review handoff, merge scheduling, and recovery into the service.
   Disable old autoheal and model-held board watchers before activating the new
   owner. Migration is one board at a time with an explicit rollback switch.
6. Audit the broad feature-branch/main divergence separately. Do not use this
   refactor to silently publish the backlog of unrelated Sprint changes.

Acceptance: no paid inference during idle periods; no repeated ownership chat;
no lost or double-applied commands across restarts; questions tracked independently
of builds; stale judgments cannot mutate new state; unavailable Jev does not drop
input; missing credentials never fall back to another user's key; completion and
release status require actual evidence.

## Decisions for discussion

Recommended first cut: reliable conversation/response coordination, then build
orchestration. Choose the response worker/provider separately from Jev; reusing a
busy build terminal would reproduce the current problem. Keep recovery limits
configurable by time/cost and useful progress instead of an arbitrary low turn cap.

## Sources read

- https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md
- https://docs.typesafe.ai/api.md
- https://docs.typesafe.ai/models.md
- https://docs.typesafe.ai/patterns/intent-routing.md
- https://docs.typesafe.ai/cookbooks/function_calling.md
- https://docs.typesafe.ai/confidence.md
- https://docs.typesafe.ai/model-jaggedness/jev-1.13.md

## Approved addition: start cheap, verify, escalate

Sam approved implementation and requested a lower-cost first attempt with Jev
verification and escalation to a stronger model. Routing may start complex or
consequential work at a stronger tier. Otherwise the configured lower-cost worker
produces a candidate; deterministic checks run; Jev evaluates task satisfaction,
constraints, supporting evidence, and material uncertainty. A failed check or
unclear judgment escalates with the candidate and concrete failed dimensions.
Worker-provided claims never substitute for coordinator-owned executable checks.

Track cost and latency per accepted outcome across all attempts and verification,
not just the first call. Bound escalation depth and preserve unfinished jobs at
limits; do not invent completion. No-confidence/service-outage cases remain
explicit, and publication/merge/deploy authorization remains code policy.

Initial connectivity verification: the supplied private credential authenticated.
Two synthetic-only Jev calls successfully routed a status question and accepted a
supported status answer (500 and 656 total reported tokens). Routing took about
0.48 seconds in that one sample; this is not a latency benchmark or SLA.
