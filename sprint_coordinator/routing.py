from __future__ import annotations

from datetime import datetime, timezone

from sprint_coordinator import judgments
from sprint_coordinator.evidence import EvidenceError, validate_code_result
from sprint_coordinator.store import assignment_id_for, obligation_id_for, outbox_id_for
from sprint_coordinator.workers import WorkerError


USER_REQUEST_KINDS = {"chat", "submitted", "answer", "verdict", "action"}
NOISE_KINDS = {"heartbeat", "cursor", "progress", "phase"}
DETERMINISTIC_KINDS = {"verdict", "answer", "action"}


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


class Router:
    def __init__(self, store, config, jev, workers, clock, check_runner=None):
        self.store = store
        self.config = config
        self.jev = jev
        self.workers = workers
        self.clock = clock
        self.check_runner = check_runner
        self.jev_calls = 0

    def create_obligations(self, events: list) -> list:
        created = []
        now = self.clock.now()
        for ev in events:
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

    def route_received(self) -> list:
        routed = []
        now = self.clock.now()
        for obl in self.store.obligations("received"):
            event = self.store.event(obl["event_seq"])
            if event and event.get("kind") in DETERMINISTIC_KINDS:
                self.store.update_obligation(
                    obl["id"], status="routed", intent="deterministic", routed_at=now)
                self._assign(obl["id"], "low")
                routed.append(obl["id"])
                continue
            decision = self._classify(obl)
            if decision.get("usage") and not decision.get("cached"):
                usage_info = decision["usage"]
                self.store.add_usage(budget_day(self.clock.now()),
                                     int(usage_info.get("calls") or 1),
                                     float(usage_info.get("spend_usd") or 0.0))
            intents = decision["intents"]
            if "status_only" in intents and len(intents) == 1:
                self.store.update_obligation(
                    obl["id"], status="routed", intent="status_only", routed_at=now)
                # Status chatter is recorded, not treated as answering another question.
                self._assign(obl["id"], "low")
                routed.append(obl["id"])
                continue
            if decision["escalate"] or "unclear" in intents or "multi_intent" in intents or len(intents) > 1:
                self.store.update_obligation(
                    obl["id"], status="escalated", intent=",".join(intents), routed_at=now,
                    last_error=decision.get("reason") or "ambiguous_or_multi_intent")
                self._assign(obl["id"], "high")
                routed.append(obl["id"])
                continue
            self.store.update_obligation(
                obl["id"], status="routed", intent=intents[0], routed_at=now)
            self._assign(obl["id"], "low")
            routed.append(obl["id"])
        return routed

    def _classify(self, obl: dict) -> dict:
        if not getattr(self.jev, "configured", lambda: False)():
            return {"intents": ["unclear"], "escalate": True, "reason": "jev_unconfigured"}
        usage = self.store.usage_day(budget_day(self.clock.now()))
        if usage["calls"] >= self.config.daily_max_calls or usage["spend_usd"] >= self.config.daily_max_spend_usd:
            return {"intents": ["unclear"], "escalate": True, "reason": "budget_exhausted"}
        request = {"text": obl.get("question_text") or "", "reply_to": obl["reply_to"]}
        key = judgments.cache_key(self.config.jev_prompt_version, self.config.jev_model, request)
        cached = self.store.get_judgment(key)
        if cached:
            return dict(cached["result"], cached=True)
        try:
            if hasattr(self.jev, "route"):
                decision = self.jev.route(obl)
            else:
                payload = judgments.routing_request(obl.get("question_text") or "", obl["reply_to"])
                result = self.jev.judge(payload)
                intents = judgments.extract_intents(result)
                decision = {"intents": intents, "escalate": False,
                            "usage": result.get("usage") or {"calls": 1, "spend_usd": 0.0}}
        except judgments.MissingCredentials:
            return {"intents": ["unclear"], "escalate": True, "reason": "missing_jev_key"}
        except Exception:
            return {"intents": ["unclear"], "escalate": True, "reason": "jev_unavailable"}
        self.jev_calls += 1
        usage_info = decision.get("usage") or {"calls": 1, "spend_usd": 0.0}
        self.store.put_judgment(key, self.config.jev_prompt_version, self.config.jev_model,
                                key, decision, usage_info, self.clock.now())
        return decision

    def _assign(self, oid: str, worker: str, idempotent: bool = False) -> str | None:
        obl = self.store.obligation(oid)
        existing = self.store.assignments_for(oid)
        attempt = len(existing) + 1
        spec = self.config.worker(worker)
        aid = assignment_id_for(oid, attempt, worker)
        if self.store.assignment(aid):
            return aid
        job = {
            "assignment_id": aid,
            "obligation_id": oid,
            "kind": spec["role"],
            "tier": worker,
            "reply_to": obl["reply_to"],
            "question": obl.get("question_text"),
            "thread_revision": obl["bound_seq"],
            "allowed_checks": self.config.allowed_checks,
            "idempotent": idempotent,
        }
        now = self.clock.now()
        self.store.put_assignment({
            "id": aid,
            "obligation_id": oid,
            "worker": worker,
            "role": spec["role"],
            "status": "pending",
            "attempt": attempt,
            "idempotent": idempotent,
            "job": job,
            "started_at": now,
            "progress_at": now,
        })
        self.store.update_obligation(oid, status="assigned")
        self.store.set_clock("job_progress:%s" % aid, now, worker)
        return aid

    def launch_pending(self, slots) -> list:
        started = []
        while True:
            progressed = False
            for row in self.store.assignments_by_status("pending"):
                role = row["role"] if row["role"] in ("response", "code") else "response"
                if not slots.can_start(role):
                    continue
                if not slots.acquire(role):
                    continue
                spec = self.config.worker(row["worker"])
                now = self.clock.now()
                self.store.update_assignment(row["id"], status="running", started_at=now,
                                             progress_at=now)
                self.store.set_clock("job_progress:%s" % row["id"], now, "running")
                try:
                    result = self.workers.run(spec["command"], row["job"] or {})
                    self._finish(row, result)
                except WorkerError as exc:
                    self._fail(row, str(exc))
                finally:
                    slots.release(role)
                started.append(row["id"])
                progressed = True
            if not progressed:
                break
        return started

    def _fail(self, row: dict, error: str) -> None:
        now = self.clock.now()
        self.store.update_assignment(row["id"], status="failed", finished_at=now,
                                     last_error=error, result={"ok": False, "error": error})
        obl = self.store.obligation(row["obligation_id"])
        if row["worker"] == "low" and obl and obl["escalations"] < self.config.max_escalation_retries:
            self.store.update_obligation(obl["id"], escalations=obl["escalations"] + 1,
                                         last_error=error, status="escalated")
            self._assign(obl["id"], "high")
            return
        self.store.update_obligation(row["obligation_id"], status="failed", last_error=error)

    def _finish(self, row: dict, result: dict) -> None:
        now = self.clock.now()
        if not result.get("ok"):
            self._fail(row, result.get("error") or "worker_not_ok")
            return
        obl = self.store.obligation(row["obligation_id"])
        kind = result.get("kind") or "reply"
        try:
            if kind == "code_result":
                evidence = validate_code_result(
                    result, self.config.check_catalog, runner=self.check_runner)
                result = dict(result)
                result["checks"] = evidence
                if self.config.require_jev_for_approval:
                    if not getattr(self.jev, "configured", lambda: False)():
                        raise EvidenceError("missing_jev_key")
                    if hasattr(self.jev, "verify_code"):
                        outcome = self.jev.verify_code(
                            task=obl.get("question_text") or "",
                            candidate=result.get("text") or "code_result",
                            evidence={"checks": evidence},
                            checks=evidence,
                        )
                        if not getattr(outcome, "accepted", False):
                            raise EvidenceError("jev_did_not_accept")
            elif kind == "reply":
                if result.get("status_update") and not result.get("addresses_obligation"):
                    raise EvidenceError("status_update_does_not_answer")
        except EvidenceError as exc:
            self.store.update_assignment(row["id"], status="failed", finished_at=now,
                                         last_error=str(exc), result=result)
            if row["worker"] == "low" and obl and obl["escalations"] < self.config.max_escalation_retries:
                self.store.update_obligation(obl["id"], escalations=obl["escalations"] + 1,
                                             last_error=str(exc), status="escalated")
                self._assign(obl["id"], "high")
                return
            self.store.update_obligation(row["obligation_id"], status="failed",
                                         last_error=str(exc))
            return
        self.store.update_assignment(row["id"], status="succeeded", finished_at=now,
                                     progress_at=now, result=result)
        self._queue_reply(obl, result, now)

    def _queue_reply(self, obl: dict, result: dict, now: float) -> None:
        text = result.get("text") or ""
        if not text and result.get("kind") == "code_result":
            text = "Work finished with trusted executable checks."
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
            if obl["escalations"] < self.config.max_escalation_retries:
                nxt = int(obl["revision"]) + 1
                self.store.update_obligation(
                    obl["id"], revision=nxt, bound_seq=head, status="escalated",
                    escalations=obl["escalations"] + 1, last_error=error)
                self._assign(obl["id"], "high")
            else:
                self.store.update_obligation(obl["id"], status="failed", last_error=error)
            return
        # Shadow vs active is applied by the publisher.

    def publish_outbox(self, board, mode: str) -> list:
        published = []
        now = self.clock.now()
        for row in self.store.outbox_all():
            if row["status"] != "pending":
                continue
            if mode != "active":
                self.store.update_outbox(row["id"], status="skipped_shadow")
                obl = self.store.obligation(row["obligation_id"])
                if obl and obl["status"] not in ("answered", "failed"):
                    self.store.update_obligation(
                        row["obligation_id"], status="answered", answered_at=now)
                    self.store.link_reply(row["id"], row["obligation_id"])
                published.append(row["id"])
                continue
            obl = self.store.obligation(row["obligation_id"])
            head = self.store.user_thread_head(obl["thread_key"])
            if head > int(obl["bound_seq"]):
                self.store.update_outbox(row["id"], status="blocked_stale",
                                         last_error="newer_relevant_message")
                if obl["escalations"] < self.config.max_escalation_retries:
                    nxt = int(obl["revision"]) + 1
                    self.store.update_obligation(
                        obl["id"], revision=nxt, bound_seq=head, status="escalated",
                        escalations=obl["escalations"] + 1,
                        last_error="newer_relevant_message")
                    self._assign(obl["id"], "high")
                else:
                    self.store.update_obligation(
                        obl["id"], status="failed", last_error="newer_relevant_message")
                continue
            self.store.update_outbox(row["id"], status="sending", sent_at=now)
            dest = row["destination"]
            text = (row["body"] or {}).get("text") or ""
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
                       if a["status"] in ("pending", "running")]
            if running:
                continue
            if obl["escalations"] >= self.config.max_escalation_retries:
                continue
            if any(a["worker"] == "high" for a in self.store.assignments_for(obl["id"])):
                continue
            self.store.update_obligation(
                obl["id"], escalations=obl["escalations"] + 1, status="escalated",
                last_error="reply_deadline")
            self._assign(obl["id"], "high")
            acted.append(obl["id"])
        return acted
