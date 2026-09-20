from __future__ import annotations

from datetime import datetime, timezone

from sprint_coordinator import judgments, workspace
from sprint_coordinator.evidence import EvidenceError, validate_code_result
from sprint_coordinator.store import assignment_id_for, obligation_id_for, outbox_id_for
from sprint_coordinator.workers import WorkerError


USER_REQUEST_KINDS = {"chat", "submitted", "answer", "verdict", "action"}
NOISE_KINDS = {"heartbeat", "cursor", "progress", "phase"}
DETERMINISTIC_KINDS = {"verdict", "answer", "action"}
NO_ESCALATE_REASONS = {
    "budget_exhausted",
    "missing_jev_key",
    "missing_verify_candidate",
    "jev_unavailable",
    "jev_service",
    "escalation_exhausted",
    "zero_budget",
}
BASE_CONSTRAINTS = [
    "Do not merge or deploy; semantic approval is not authorization.",
    "Address the user's actual request, not a status placeholder.",
]
EVENT_PAGE_LIMIT = 500


def thread_key(event: dict) -> str:
    reply = event.get("reply_to") or (event.get("payload") or {}).get("reply_to")
    if reply:
        return str(reply)
    card = event.get("card_num")
    return "sidebar" if card is None else "card:%d" % int(card)


def is_user_request(event: dict) -> bool:
    if event.get("actor") != "user":
        return False
    if event.get("kind") in NOISE_KINDS:
        return False
    return event.get("kind") in USER_REQUEST_KINDS


def question_text(event: dict) -> str:
    payload = event.get("payload") or {}
    return str(payload.get("text") or payload.get("body") or "")


def budget_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def compact_event(event: dict) -> dict:
    payload = event.get("payload") or {}
    return {
        "seq": event.get("seq"),
        "actor": event.get("actor"),
        "kind": event.get("kind"),
        "text": payload.get("text") or payload.get("body") or "",
        "reply_to": event.get("reply_to") or payload.get("reply_to"),
    }


def select_worker(config, role: str, tier: str):
    workers = config.workers or {}
    spec = workers.get(tier)
    if spec and spec.get("role") == role:
        return tier
    for name, item in workers.items():
        if item.get("role") == role and (item.get("cost") == tier or name == tier):
            return name
    return None


def verification_accepted(outcome) -> bool:
    if outcome is None:
        raise EvidenceError("unknown_verification_result")
    if isinstance(outcome, dict):
        if "accepted" in outcome:
            return bool(outcome["accepted"])
        action = outcome.get("action")
        if action == "accept":
            return True
        if action:
            return False
        raise EvidenceError("unknown_verification_result")
    accepted = getattr(outcome, "accepted", None)
    if accepted is not None:
        return bool(accepted)
    action = getattr(outcome, "action", None)
    if action == "accept":
        return True
    if action:
        return False
    raise EvidenceError("unknown_verification_result")


class Router:
    def __init__(self, store, config, jev, workers, clock, check_runner=None):
        self.store = store
        self.config = config
        self.jev = jev
        self.workers = workers
        self.clock = clock
        self.check_runner = check_runner
        self.jev_calls = 0

    def reconcile_obligations(self) -> list:
        created = []
        now = self.clock.now()
        for ev in self.store.events_without_obligations():
            if not is_user_request(ev):
                continue
            oid = obligation_id_for(ev["event_id"], thread_key(ev))
            if self.store.obligation(oid):
                continue
            due = now + self.config.simple_reply_deadline_seconds
            rec = {
                "id": oid,
                "event_seq": ev["seq"],
                "event_id": ev["event_id"],
                "reply_to": thread_key(ev),
                "thread_key": thread_key(ev),
                "status": "received",
                "revision": 1,
                "bound_seq": ev["seq"],
                "question_text": question_text(ev),
                "created_at": now,
                "due_at": due,
                "intent": "deterministic" if ev.get("kind") in DETERMINISTIC_KINDS else None,
            }
            self.store.put_obligation(rec)
            created.append(oid)
        return created

    def create_obligations(self, events: list) -> list:
        # Kept for tests; durable path is reconcile_obligations over stored events.
        now = self.clock.now()
        if events:
            self.store.ingest_events(events, now)
        return self.reconcile_obligations()

    def zero_budget(self) -> bool:
        return self.config.daily_max_calls == 0 or self.config.daily_max_spend_usd == 0

    def budget_exhausted(self, extra_calls: int = 0, extra_spend: float = 0.0) -> bool:
        if self.zero_budget() and (extra_calls > 0 or extra_spend > 0):
            return True
        usage = self.store.usage_day(budget_day(self.clock.now()))
        if usage["calls"] + extra_calls > self.config.daily_max_calls:
            return True
        if usage["spend_usd"] + extra_spend > self.config.daily_max_spend_usd:
            return True
        return False

    def reserve(self, calls: int, spend: float) -> bool:
        calls = max(0, int(calls))
        spend = max(0.0, float(spend))
        if calls == 0 and spend == 0:
            return True
        if self.budget_exhausted(calls, spend):
            return False
        self.store.add_usage(budget_day(self.clock.now()), calls, spend)
        return True

    def high_used(self, oid: str) -> int:
        n = 0
        for row in self.store.assignments_for(oid):
            spec = self.config.workers.get(row["worker"]) or {}
            tier = (row.get("job") or {}).get("tier")
            if row["worker"] == "high" or spec.get("cost") == "high" or tier == "high":
                n += 1
        return n

    def can_assign_high(self, oid: str) -> bool:
        return self.high_used(oid) < self.config.max_escalation_retries

    def hold(self, oid: str, reason: str) -> None:
        now = self.clock.now()
        self.store.update_obligation(oid, status="held", last_error=reason, routed_at=now)

    def start_routing(self, submit, available: int = 2) -> list:
        started = []
        now = self.clock.now()
        for obl in self.store.obligations("received"):
            if len(started) >= available:
                break
            event = self.store.event(obl["event_seq"])
            if event and event.get("kind") in DETERMINISTIC_KINDS:
                self.store.update_obligation(
                    obl["id"], status="recorded", intent="deterministic", routed_at=now)
                started.append(obl["id"])
                continue
            cached = self._cached_decision(obl)
            if cached is not None:
                self.apply_decision(obl, cached)
                started.append(obl["id"])
                continue
            if not getattr(self.jev, "configured", lambda: False)():
                self.hold(obl["id"], "missing_jev_key")
                started.append(obl["id"])
                continue
            if not self.reserve(1, 0.0):
                self.hold(obl["id"], "budget_exhausted")
                started.append(obl["id"])
                continue
            snapshot = self._route_snapshot(obl)
            self.store.update_obligation(obl["id"], status="routing", routed_at=now)
            if submit("classify", obl["id"], self.classify_offline, snapshot):
                self.jev_calls += 1
                started.append(obl["id"])
            else:
                self.store.update_obligation(obl["id"], status="received")
        return started

    def _route_snapshot(self, obl: dict) -> dict:
        bound = int(obl["bound_seq"])
        context = [compact_event(e) for e in self.store.thread_events(obl["thread_key"], up_to=bound)]
        return {
            "id": obl["id"],
            "question_text": obl.get("question_text") or "",
            "reply_to": obl["reply_to"],
            "thread_key": obl["thread_key"],
            "bound_seq": bound,
            "thread_context": context,
        }

    def _typed_request(self, obl: dict) -> dict:
        return judgments.routing_request(
            obl.get("question_text") or "",
            obl["reply_to"],
        )

    def _cache_key_for(self, obl: dict) -> str:
        typed = self._typed_request(obl)
        relevant = {
            "message_text": obl.get("question_text") or "",
            "reply_to": obl["reply_to"],
            "questions": typed.get("questions"),
            "state": {
                "message_text": obl.get("question_text") or "",
                "reply_to": obl["reply_to"],
            },
        }
        return judgments.cache_key(
            self.config.jev_prompt_version, self.config.jev_model, typed, relevant)

    def _cached_decision(self, obl: dict):
        key = self._cache_key_for(obl)
        cached = self.store.get_judgment(key)
        if not cached:
            return None
        result = dict(cached["result"])
        result["cached"] = True
        return result

    def classify_offline(self, snapshot: dict) -> dict:
        if hasattr(self.jev, "route"):
            return self.jev.route(snapshot)
        payload = judgments.routing_request(
            snapshot.get("question_text") or "", snapshot.get("reply_to") or "")
        result = self.jev.judge(payload)
        intents = judgments.extract_intents(result)
        return {"intents": intents, "escalate": False,
                "usage": result.get("usage") or {"calls": 1}}

    def apply_classify(self, oid: str, decision, err) -> None:
        obl = self.store.obligation(oid)
        if obl is None:
            return
        if err is not None:
            reason = self._jev_error_reason(err)
            self.hold(oid, reason)
            return
        if not isinstance(decision, dict):
            self.hold(oid, "jev_unavailable")
            return
        key = self._cache_key_for(obl)
        usage = decision.get("usage") or {}
        self.store.put_judgment(
            key, self.config.jev_prompt_version, self.config.jev_model,
            key, decision, usage, self.clock.now())
        extra_spend = 0.0
        if usage.get("input_tokens"):
            extra_spend = judgments.spend_usd_from_input_tokens(int(usage["input_tokens"]))
            if extra_spend:
                self.store.add_usage(budget_day(self.clock.now()), 0, extra_spend)
        self.apply_decision(obl, decision)

    def apply_decision(self, obl: dict, decision: dict) -> None:
        now = self.clock.now()
        intents = list(decision.get("intents") or ["unclear"])
        reason = decision.get("reason") or ""
        if "status_only" in intents and len(intents) == 1:
            self.store.update_obligation(
                obl["id"], status="recorded", intent="status_only", routed_at=now)
            return
        privileged = "dispatch_work" in intents
        blocked = (
            decision.get("escalate")
            or "unclear" in intents
            or "multi_intent" in intents
            or "approval" in intents
            or len(intents) != 1
        )
        if blocked:
            self.hold(obl["id"], reason or "ambiguous_or_privileged_hold")
            self.store.update_obligation(obl["id"], intent=",".join(intents), routed_at=now)
            return
        if privileged:
            worker = select_worker(self.config, "code", "low")
            if worker is None:
                self.hold(obl["id"], "no_code_worker")
                return
            self.store.update_obligation(
                obl["id"], status="routed", intent="dispatch_work", routed_at=now)
            self._assign(obl["id"], worker, role="code", tier="low")
            return
        worker = select_worker(self.config, "response", "low")
        if worker is None:
            self.hold(obl["id"], "no_response_worker")
            return
        self.store.update_obligation(
            obl["id"], status="routed", intent=intents[0], routed_at=now)
        self._assign(obl["id"], worker, role="response", tier="low")

    def _assign(self, oid: str, worker: str, role: str, tier: str,
                previous=None, idempotent: bool = False) -> str | None:
        obl = self.store.obligation(oid)
        if obl is None:
            return None
        if worker not in self.config.workers or self.config.worker(worker)["role"] != role:
            self.hold(oid, "no_worker_for_role")
            return None
        if tier == "high" and not self.can_assign_high(oid):
            self.hold(oid, "escalation_exhausted")
            return None
        if tier == "high" and self.budget_exhausted(1, 0.0):
            self.hold(oid, "budget_exhausted")
            return None
        existing = self.store.assignments_for(oid)
        attempt = len(existing) + 1
        aid = assignment_id_for(oid, attempt, worker)
        if self.store.assignment(aid):
            return aid
        head = self.store.user_thread_head(obl["thread_key"])
        bound = max(int(obl["bound_seq"]), int(head or 0))
        context = [compact_event(e) for e in self.store.thread_events(obl["thread_key"], up_to=bound)]
        newly = []
        if previous is not None:
            prev_bound = int((previous.get("thread_revision") or obl["bound_seq"]))
            newly = [e for e in context if (e.get("seq") or 0) > prev_bound]
        revision = int(obl["revision"] or 1)
        if previous is not None:
            revision = revision + 1
        job = {
            "assignment_id": aid,
            "obligation_id": oid,
            "kind": role,
            "role": role,
            "tier": tier,
            "worker": worker,
            "provider": self.config.worker(worker).get("provider"),
            "selected_model": self.config.worker(worker).get("model"),
            "reply_to": obl["reply_to"],
            "question": obl.get("question_text"),
            "thread_revision": bound,
            "thread_context": context,
            "newly_arrived": newly,
            "previous_attempt": previous,
            "constraints": list(BASE_CONSTRAINTS),
            "allowed_checks": [item["id"] for item in self.config.allowed_checks] if role == "code" else [],
            "idempotent": idempotent,
        }
        if job.get("provider") is None:
            job.pop("provider", None)
        now = self.clock.now()
        self.store.put_assignment({
            "id": aid,
            "obligation_id": oid,
            "worker": worker,
            "role": role,
            "status": "pending",
            "attempt": attempt,
            "idempotent": idempotent,
            "job": job,
            "started_at": now,
            "progress_at": now,
        })
        status = "escalated" if tier == "high" else "assigned"
        self.store.update_obligation(
            oid, status=status, bound_seq=bound, revision=revision)
        self.store.set_clock("job_progress:%s" % aid, now, worker)
        return aid

    def previous_from(self, row: dict, error: str) -> dict:
        result = row.get("result") or {}
        job = row.get("job") or {}
        return {
            "worker": row.get("worker"),
            "role": row.get("role"),
            "tier": job.get("tier"),
            "candidate": result.get("text"),
            "kind": result.get("kind"),
            "checks": result.get("checks"),
            "rejection": error,
            "thread_revision": job.get("thread_revision"),
            "job": {k: job.get(k) for k in (
                "question", "thread_revision", "constraints") if k in job},
        }

    def _fail(self, row: dict, error: str) -> None:
        now = self.clock.now()
        result = row.get("result") if isinstance(row.get("result"), dict) else {"ok": False}
        packed = dict(result)
        packed["ok"] = False
        packed["error"] = error
        self.store.update_assignment(row["id"], status="failed", finished_at=now,
                                     last_error=error, result=packed)
        obl = self.store.obligation(row["obligation_id"])
        reason = error.split(":")[0].strip() if error else error
        escalate = (
            obl
            and (row.get("job") or {}).get("tier") != "high"
            and row.get("worker") != "high"
            and self.can_assign_high(row["obligation_id"])
            and reason not in NO_ESCALATE_REASONS
            and not self.zero_budget()
            and not self.budget_exhausted(1, 0.0)
        )
        if escalate:
            worker = select_worker(self.config, row["role"], "high")
            if worker:
                self.store.update_obligation(
                    obl["id"], escalations=int(obl["escalations"] or 0) + 1,
                    last_error=error, status="escalated")
                self._assign(
                    obl["id"], worker, role=row["role"], tier="high",
                    previous=self.previous_from(row, error))
                return
        self.store.update_obligation(row["obligation_id"], status="failed", last_error=error)

    def verify_offline(self, snapshot: dict) -> dict:
        result = dict(snapshot.get("result") or {})
        role = snapshot["role"]
        catalog = snapshot.get("catalog") or {}
        jev = snapshot["jev"]
        check_runner = snapshot.get("check_runner")
        task = snapshot.get("task") or ""
        constraints = list(snapshot.get("constraints") or BASE_CONSTRAINTS)
        context = snapshot.get("context") or []
        if snapshot.get("board_context"):
            context = list(context) + [{"source": "Sprint API", "board": snapshot["board_context"]}]
        kind = result.get("kind")
        text = result.get("text")
        text = "" if text is None else str(text)
        if result.get("model_command") or result.get("commands"):
            raise EvidenceError("arbitrary model-generated commands are rejected")
        if kind not in (None, "reply", "code_result"):
            raise EvidenceError("unknown_result_kind")
        if role == "code":
            if snapshot.get("workspace"):
                try:
                    snapshot["changes"] = workspace.evidence(snapshot["workspace"])
                except (OSError, ValueError) as exc:
                    raise EvidenceError(str(exc)) from exc
            if result.get("merge") or result.get("deploy"):
                raise EvidenceError("semantic_approval_is_not_authorization")
            evidence = validate_code_result(result, catalog, runner=check_runner)
            result = dict(result)
            result["checks"] = evidence
            if not text.strip():
                raise EvidenceError("empty_text")
            fn = getattr(jev, "verify_candidate", None)
            if not callable(fn):
                raise EvidenceError("missing_verify_candidate")
            independent = []
            for check in evidence:
                independent.append({
                    "passed": True,
                    "command": " ".join(check.get("command") or []),
                    "source": "independent",
                })
            try:
                outcome = fn(
                    task=task,
                    constraints=constraints,
                    candidate=text,
                    evidence={"checks": evidence, "context": context, "changes": snapshot.get("changes")},
                    code_change=True,
                    independent_checks=independent,
                    context=context,
                )
            except judgments.MissingCredentials as exc:
                raise EvidenceError("missing_jev_key") from exc
            except judgments.JevUnavailable as exc:
                raise EvidenceError(str(exc) or "jev_unavailable") from exc
            except Exception as exc:
                name = type(exc).__name__
                if "Service" in name or "Configuration" in name:
                    raise EvidenceError("jev_service") from exc
                raise EvidenceError("jev_unavailable") from exc
            if not verification_accepted(outcome):
                raise EvidenceError("jev_did_not_accept")
            return {"result": result, "outcome": "accept"}
        if role != "response":
            raise EvidenceError("unknown_assignment_role")
        if kind not in (None, "reply"):
            raise EvidenceError("unknown_result_kind")
        if not text.strip():
            raise EvidenceError("empty_text")
        if result.get("status_update") and not result.get("addresses_obligation"):
            raise EvidenceError("status_update_does_not_answer")
        fn = getattr(jev, "verify_candidate", None)
        if not callable(fn):
            raise EvidenceError("missing_verify_candidate")
        try:
            outcome = fn(
                task=task,
                constraints=constraints,
                candidate=text,
                evidence={"context": context, "worker_claim": {
                    "addresses_obligation": result.get("addresses_obligation"),
                }},
                code_change=False,
                independent_checks=(),
                context=context,
            )
        except judgments.MissingCredentials as exc:
            raise EvidenceError("missing_jev_key") from exc
        except judgments.JevUnavailable as exc:
            raise EvidenceError(str(exc) or "missing_verify_candidate") from exc
        except Exception as exc:
            name = type(exc).__name__
            if "Service" in name or "Configuration" in name:
                raise EvidenceError("jev_service") from exc
            raise EvidenceError("jev_unavailable") from exc
        if not verification_accepted(outcome):
            raise EvidenceError("jev_did_not_accept")
        return {"result": result, "outcome": "accept"}

    def apply_verify(self, aid: str, payload, err) -> None:
        row = self.store.assignment(aid)
        if row is None:
            return
        now = self.clock.now()
        if err is not None:
            if isinstance(err, EvidenceError):
                row = dict(row)
                if payload and isinstance(payload, dict) and payload.get("result"):
                    row["result"] = payload["result"]
                self._fail(row, str(err))
                return
            if isinstance(err, WorkerError):
                self._fail(row, str(err))
                return
            self._fail(row, type(err).__name__)
            return
        result = (payload or {}).get("result") or row.get("result") or {}
        self.store.update_assignment(row["id"], status="succeeded", finished_at=now,
                                     progress_at=now, result=result)
        obl = self.store.obligation(row["obligation_id"])
        self._queue_reply(obl, result, now)

    def verify_snapshot(self, row: dict, result: dict) -> dict:
        obl = self.store.obligation(row["obligation_id"])
        job = row.get("job") or {}
        catalog = self.config.check_catalog if row["role"] == "code" else {}
        changes = None
        work = job.get('execution_workspace')
        if row['role'] == 'code' and work:
            catalog = {k:dict(v, cwd=work['path']) for k,v in catalog.items()}
        return {
            "board_context": job.get("board_context"),
            "workspace": work,
            "changes": changes,
            "role": row["role"],
            "result": result,
            "catalog": catalog,
            "jev": self.jev,
            "check_runner": self.check_runner,
            "task": obl.get("question_text") if obl else job.get("question") or "",
            "constraints": job.get("constraints") or list(BASE_CONSTRAINTS),
            "context": job.get("thread_context") or [],
        }

    def _queue_reply(self, obl: dict, result: dict, now: float) -> None:
        text = result.get("text") or ""
        dest = obl["reply_to"]
        revision = int(obl["revision"])
        key = "reply:%s:r%d" % (obl["id"], revision)
        if self.store.outbox_by_key(key):
            return
        head = self.store.user_thread_head(obl["thread_key"])
        status = "pending"
        error = None
        if head > int(obl["bound_seq"]):
            status = "blocked_stale"
            error = "newer_relevant_message"
        rec_id = outbox_id_for(key)
        self.store.put_outbox({
            "id": rec_id,
            "idempotency_key": key,
            "obligation_id": obl["id"],
            "revision": revision,
            "destination": dest,
            "body": {"text": text, "actor": "session", "obligation_id": obl["id"]},
            "status": status,
            "created_at": now,
        })
        self.store.link_reply(rec_id, obl["id"])
        if status == "blocked_stale":
            self.store.update_outbox(rec_id, last_error=error)
            succeeded = [a for a in self.store.assignments_for(obl["id"]) if a["id"]]
            last = succeeded[-1] if succeeded else {"result": result, "job": {}, "role": "response", "worker": "low"}
            last = dict(last)
            last["result"] = result
            if self.can_assign_high(obl["id"]) and not self.budget_exhausted(1, 0.0):
                worker = select_worker(self.config, last.get("role") or "response", "high")
                nxt = int(obl["revision"]) + 1
                self.store.update_obligation(
                    obl["id"], revision=nxt, bound_seq=head, status="escalated",
                    escalations=int(obl["escalations"] or 0) + 1, last_error=error)
                self._assign(
                    obl["id"], worker, role=last.get("role") or "response", tier="high",
                    previous=self.previous_from(last, error))
            else:
                self.store.update_obligation(obl["id"], status="failed", last_error=error)
            return

    def publish_outbox(self, board, mode: str, caught_up: bool = True) -> list:
        published = []
        if mode != "active":
            return published
        if not caught_up:
            return published
        now = self.clock.now()
        for row in self.store.outbox_all():
            if row["status"] != "pending":
                continue
            obl = self.store.obligation(row["obligation_id"])
            if obl is None:
                continue
            head = self.store.user_thread_head(obl["thread_key"])
            if head > int(obl["bound_seq"]):
                self.store.update_outbox(row["id"], status="blocked_stale",
                                         last_error="newer_relevant_message")
                last = self.store.assignments_for(obl["id"])
                last = last[-1] if last else None
                if last and self.can_assign_high(obl["id"]) and not self.budget_exhausted(1, 0.0):
                    worker = select_worker(self.config, last.get("role") or "response", "high")
                    nxt = int(obl["revision"]) + 1
                    self.store.update_obligation(
                        obl["id"], revision=nxt, bound_seq=head, status="escalated",
                        escalations=int(obl["escalations"] or 0) + 1,
                        last_error="newer_relevant_message")
                    self._assign(
                        obl["id"], worker, role=last.get("role") or "response",
                        tier="high", previous=self.previous_from(last, "newer_relevant_message"))
                else:
                    self.store.update_obligation(
                        obl["id"], status="failed", last_error="newer_relevant_message")
                continue
            self.store.update_outbox(row["id"], status="sending", sent_at=now)
            dest = row["destination"]
            text = (row["body"] or {}).get("text") or ""
            key = row["idempotency_key"]
            try:
                if dest == "sidebar" or dest is None:
                    board.post_sidebar(text, idempotency_key=key)
                elif str(dest).startswith("card:"):
                    board.post_card_chat(int(str(dest).split(":", 1)[1]), text,
                                         idempotency_key=key)
                else:
                    board.post_sidebar(text, idempotency_key=key)
            except TypeError:
                try:
                    if dest == "sidebar" or dest is None:
                        board.post_sidebar(text)
                    elif str(dest).startswith("card:"):
                        board.post_card_chat(int(str(dest).split(":", 1)[1]), text)
                    else:
                        board.post_sidebar(text)
                except Exception as exc:
                    self.store.update_outbox(row["id"], status="uncertain",
                                             last_error=type(exc).__name__)
                    continue
            except Exception as exc:
                self.store.update_outbox(row["id"], status="uncertain",
                                         last_error=type(exc).__name__)
                continue
            self.store.update_outbox(row["id"], status="accepted")
            self.store.update_obligation(row["obligation_id"], status="answered",
                                         answered_at=now)
            self.store.link_reply(row["id"], row["obligation_id"])
            published.append(row["id"])
        return published

    def expire_deadlines(self) -> list:
        now = self.clock.now()
        acted = []
        for obl in self.store.open_obligations():
            due = obl.get("due_at")
            if due is None or now < float(due):
                continue
            self.store.set_clock("reply_deadline:%s" % obl["id"], now, "due")
            running = [a for a in self.store.assignments_for(obl["id"])
                       if a["status"] in ("pending", "running", "verifying")]
            if running:
                continue
            if not self.can_assign_high(obl["id"]):
                continue
            if self.budget_exhausted(1, 0.0) or self.zero_budget():
                self.hold(obl["id"], "budget_exhausted")
                acted.append(obl["id"])
                continue
            role = "response"
            assigns = self.store.assignments_for(obl["id"])
            if assigns:
                role = assigns[-1].get("role") or "response"
            elif obl.get("intent") == "dispatch_work":
                role = "code"
            worker = select_worker(self.config, role, "high")
            if worker is None:
                continue
            last = assigns[-1] if assigns else {
                "worker": "low", "role": role, "result": {}, "job": {"thread_revision": obl["bound_seq"]},
            }
            self.store.update_obligation(
                obl["id"], escalations=int(obl["escalations"] or 0) + 1, status="escalated",
                last_error="reply_deadline")
            self._assign(
                obl["id"], worker, role=role, tier="high",
                previous=self.previous_from(last, "reply_deadline"))
            acted.append(obl["id"])
        return acted

    def _jev_error_reason(self, err) -> str:
        if isinstance(err, judgments.MissingCredentials):
            return "missing_jev_key"
        if isinstance(err, judgments.JevUnavailable):
            return str(err) or "jev_unavailable"
        name = type(err).__name__
        if "Service" in name:
            return "jev_service"
        if "Configuration" in name:
            return "missing_jev_key"
        return "jev_unavailable"
