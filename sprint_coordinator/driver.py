from __future__ import annotations

import time
from pathlib import Path

from sprint_coordinator.board import BoardClient
from sprint_coordinator.config import CoordinatorConfig
from sprint_coordinator.judgments import load_jev_client, worker_budget_estimate
from sprint_coordinator.lock import acquire
from sprint_coordinator.routing import EVENT_PAGE_LIMIT, Router, budget_day, is_user_request
from sprint_coordinator.store import Store
from sprint_coordinator.takeover import takeover_report
from sprint_coordinator.util import Clock
from sprint_coordinator.workers import SlotGate, SubprocessRunner, TaskPool, WorkerError

_UNSET = object()


def _start_cursor_from_config(config) -> int | None:
    raw = getattr(config, "raw", None) or {}
    for key in ("start_cursor", "ingest_start_seq", "ingest_after"):
        if key in raw and raw[key] is not None:
            return int(raw[key])
    return None


class Coordinator:
    def __init__(self, config: CoordinatorConfig, mode: str = "shadow", clock=None,
                 board=None, jev=None, runner=None, check_runner=None,
                 acknowledge_autoheal_gap: bool = False, start_cursor=_UNSET):
        if mode not in ("shadow", "active"):
            raise ValueError("mode must be shadow or active")
        self.config = config
        self.mode = mode
        self.clock = clock or Clock.live()
        self.store = Store(config.data_dir / "coordinator.sqlite", self.clock)
        self.board = board or BoardClient(config.board_data_dir)
        self.jev = load_jev_client(config, override=jev)
        self.runner = runner or SubprocessRunner(config.worker_timeout_seconds)
        self.slots = SlotGate(config.concurrency, config.reserved_response_slots)
        self.pool = TaskPool()
        self.router = Router(self.store, config, self.jev, self.runner, self.clock,
                             check_runner=check_runner)
        self.acknowledge_autoheal_gap = acknowledge_autoheal_gap
        self.lock_handle = None
        self.last_takeover = None
        self.caught_up = False
        self._closed = False
        self._session_poll_at = {}
        if start_cursor is _UNSET:
            self.start_cursor = _start_cursor_from_config(config)
        else:
            self.start_cursor = start_cursor

    def open(self, recover: bool = True) -> dict:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock_handle = acquire(self.config.data_dir)
        if self.lock_handle is None:
            raise RuntimeError("another coordinator holds the flock")
        self._init_history_cursor()
        gen = self.store.bump_generation()
        recovered = {"restarted": [], "uncertain": []}
        if recover:
            recovered = self.store.recover_uncertain(self.clock.now())
        self.router.reconcile_obligations()
        self.heartbeat("open")
        return {"generation": gen, "recovery": recovered, "mode": self.mode}

    def _init_history_cursor(self) -> None:
        if self.store.get_meta("start_cursor_set") is not None:
            return
        if self.store.ingest_cursor() > 0 or self.store.obligations():
            self.store.set_meta("start_cursor_set", str(self.store.ingest_cursor()))
            return
        start = self.start_cursor
        if start is None:
            if self.mode == "active":
                raise RuntimeError(
                    "active coordinator refuses unsafely uninitialized history. "
                    "Set start_cursor or ingest_start_seq in the coordinator config "
                    "to the board event seq to begin after (use 0 only if the full "
                    "log is intended). Shadow mode may ingest from 0 without this."
                )
            start = 0
        start = int(start)
        if start < 0:
            raise ValueError("start_cursor must be >= 0")
        self.store.set_meta("start_cursor_set", str(start))
        self.store.set_meta("ingest_cursor", str(start))

    def close(self) -> None:
        self._closed = True
        try:
            if getattr(self, "runner", None) is not None and hasattr(self.runner, "close"):
                self.runner.close()
            if getattr(self, "pool", None) is not None:
                self.pool.close(timeout=min(2.0, float(self.config.worker_timeout_seconds)))
            if self.lock_handle is not None and self.store.conn is not None:
                self.store.recover_uncertain(self.clock.now())
        finally:
            if self.lock_handle is not None:
                self.lock_handle.close()
                self.lock_handle = None
            if getattr(self, "store", None) is not None:
                self.store.close()

    def heartbeat(self, detail: str = "") -> None:
        self.store.set_clock("service_heartbeat", self.clock.now(), detail)

    def check_takeover(self) -> dict:
        autoheal = {"event_dispatch_supported": True}
        try:
            autoheal = self.board.autoheal()
        except Exception as exc:
            autoheal = {"event_dispatch_supported": False, "error": type(exc).__name__}
        report = takeover_report(
            autoheal, self.config.board_data_dir, self.mode,
            acknowledge_autoheal_gap=self.acknowledge_autoheal_gap)
        self.last_takeover = report
        return report

    def ingest(self) -> list:
        after = self.store.ingest_cursor()
        limit = EVENT_PAGE_LIMIT
        try:
            page = self.board.events_after(after, limit=limit)
        except TypeError:
            page = self.board.events_after(after)
        events = page.get("events") or []
        head = int(page.get("head") or 0)
        if head and after and head < after:
            raise ValueError("board history moved backwards; inspect before resetting")
        inserted = self.store.ingest_events(events, self.clock.now())
        cursor = self.store.ingest_cursor()
        page_full = len(events) >= limit
        if head and cursor < head:
            self.caught_up = False
        elif page_full and events:
            # A full page with no head (or head already reached) still needs another read.
            self.caught_up = bool(head) and cursor >= head and len(events) < limit
        else:
            self.caught_up = (not head) or cursor >= head
        return inserted

    def ingest_until_caught_up(self, max_pages: int = 32) -> list:
        inserted = []
        for _ in range(max_pages):
            batch = self.ingest()
            inserted.extend(batch)
            if self.caught_up:
                break
            if not batch:
                break
        return inserted

    def tick(self, wait: bool = False) -> dict:
        self.heartbeat("tick")
        inserted = self.ingest()
        created = self.router.reconcile_obligations()
        if self.mode == "shadow":
            snap = self.store.snapshot()
            snap.update({
                "mode": self.mode,
                "ingested": inserted,
                "created": created,
                "routed": [],
                "expired": [],
                "started": [],
                "reaped": [],
                "published": [],
                "jev_calls": 0,
                "caught_up": self.caught_up,
                "user_requests_open": [
                    o["id"] for o in self.store.open_obligations() if is_user_request(
                        self.store.event(o["event_seq"]) or {})
                ],
            })
            return snap
        self._reconcile_sessions()
        routed = self.router.start_routing(self.pool.submit,
                                          available=max(0, 2 - self.pool.count("classify")))
        expired = self.router.expire_deadlines()
        started = self._launch()
        reaped = self._drain()
        if wait:
            reaped.extend(self._settle())
            started.extend(self._launch())
            reaped.extend(self._drain())
        inserted.extend(self.ingest_until_caught_up())
        created.extend(self.router.reconcile_obligations())
        published = self.router.publish_outbox(self.board, self.mode, self.caught_up)
        if wait:
            started.extend(self._launch())
            reaped.extend(self._settle())
            inserted.extend(self.ingest_until_caught_up())
            created.extend(self.router.reconcile_obligations())
            published.extend(self.router.publish_outbox(
                self.board, self.mode, self.caught_up))
        snap = self.store.snapshot()
        snap.update({
            "mode": self.mode,
            "ingested": inserted,
            "created": created,
            "routed": routed,
            "expired": expired,
            "started": started,
            "reaped": reaped,
            "published": published,
            "jev_calls": self.router.jev_calls,
            "caught_up": self.caught_up,
            "user_requests_open": [
                o["id"] for o in self.store.open_obligations() if is_user_request(
                    self.store.event(o["event_seq"]) or {})
            ],
        })
        return snap

    def _launch(self) -> list:
        started = []
        if self._closed:
            return started
        for row in self.store.assignments_by_status("pending"):
            role = row["role"] if row["role"] in ("response", "code") else "response"
            obl = self.store.obligation(row["obligation_id"])
            if obl and obl["status"] in ("held", "failed", "answered", "recorded"):
                continue
            if not self.slots.can_start(role):
                continue
            calls, spend, _est = worker_budget_estimate(self.config, row["worker"])
            if not self.router.reserve(calls, spend):
                if obl is not None:
                    self.router.hold(obl["id"], "budget_exhausted")
                continue
            spec = self.config.worker(row["worker"])
            if not self.slots.acquire(role):
                continue
            now = self.clock.now()
            self.store.update_assignment(row["id"], status="running", started_at=now,
                                         progress_at=now)
            self.store.set_clock("job_progress:%s" % row["id"], now, "running")
            if self.pool.submit("worker", row["id"], self.runner.run, spec["command"],
                                row.get("job") or {}):
                started.append(row["id"])
            else:
                self.slots.release(role)
                self.store.update_assignment(row["id"], status="pending")
        return started

    def _drain(self) -> list:
        done = []
        if self._closed or self.store.conn is None:
            return done
        for kind, key, result, err in self.pool.pop_done():
            if kind == "classify":
                self.router.apply_classify(key, result, err)
                done.append(key)
                continue
            if kind == "session":
                row = self.store.assignment(key)
                if row:
                    if err is not None or not isinstance(result, dict) or not result.get("ok"):
                        self._hold_session(row, result, err)
                    else:
                        self._submit_verify(row, result)
                done.append(key)
                continue
            if kind == "worker":
                row = self.store.assignment(key)
                role = row["role"] if row and row["role"] in ("response", "code") else "response"
                self.slots.release(role)
                if row is None:
                    done.append(key)
                    continue
                if self._is_session_worker(row) and (err is not None or not isinstance(result, dict)
                                                     or not result.get("ok")):
                    self._hold_session(row, result, err)
                    done.append(key)
                    continue
                if err is not None:
                    if isinstance(err, WorkerError):
                        self.router._fail(row, str(err))
                    else:
                        self.router._fail(row, type(err).__name__)
                elif not isinstance(result, dict) or not result.get("ok"):
                    packed = dict(row)
                    packed["result"] = result if isinstance(result, dict) else {}
                    error = "worker_not_ok"
                    if isinstance(result, dict):
                        error = result.get("error") or error
                    self.router._fail(packed, error)
                else:
                    self._submit_verify(row, result)
                done.append(key)
                continue
            if kind == "verify":
                self.router.apply_verify(key, result, err)
                done.append(key)
        self.pool.prune()
        return done

    def _is_session_worker(self, row: dict) -> bool:
        command = self.config.worker(row["worker"])["command"]
        return any(Path(arg).name == "sprint-session-worker" for arg in command[:2])

    def _hold_session(self, row: dict, result, err=None) -> None:
        """A transport timeout is not a failed candidate or permission to duplicate work."""
        payload = result if isinstance(result, dict) else {}
        reason = payload.get("reason") or (type(err).__name__ if err else "delivery_pending")
        self.store.update_assignment(row["id"], status="awaiting_session", result=payload,
                                     last_error=reason)
        self.store.update_obligation(row["obligation_id"], status="assigned", due_at=None,
                                     last_error=reason)

    def _reconcile_sessions(self) -> None:
        now = self.clock.now()
        for row in self.store.assignments_by_status("awaiting_session", "uncertain"):
            if not self._is_session_worker(row):
                continue
            if row["status"] == "uncertain":
                self._hold_session(row, row.get("result"))
            if self.pool.count("session") >= 2:
                break
            if now - self._session_poll_at.get(row["id"], float("-inf")) < 2:
                continue
            command = self.config.worker(row["worker"])["command"]
            # The adapter's reconcile-only path cannot call tmux-send. No model
            # budget is charged for checking a previously submitted result.
            if self.pool.submit("session", row["id"], self.runner.run,
                                command + ["--reconcile", "--wait-seconds", "0"],
                                row.get("job") or {}):
                self._session_poll_at[row["id"]] = now

    def _submit_verify(self, row: dict, result: dict) -> None:
        if not self.router.reserve(1, 0.0):
            packed = dict(row)
            packed["result"] = result
            self.router._fail(packed, "budget_exhausted")
            return
        now = self.clock.now()
        self.store.update_assignment(row["id"], status="verifying", progress_at=now, result=result)
        snapshot = self.router.verify_snapshot(row, result)
        if not self.pool.submit("verify", row["id"], self.router.verify_offline, snapshot):
            packed = dict(row)
            packed["result"] = result
            self.router._fail(packed, "verify_submit_failed")

    def _settle(self) -> list:
        done = []
        timeout = min(2.0, float(self.config.worker_timeout_seconds))
        deadline = time.time() + timeout
        while time.time() < deadline and not self._closed:
            done.extend(self._drain())
            done.extend(self._launch())
            if self.pool.alive() == 0:
                done.extend(self._drain())
                if self.pool.alive() == 0:
                    break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self.pool.join(min(0.05, remaining))
        return done

    def status(self) -> dict:
        snap = self.store.snapshot()
        usage = self.store.usage_day(budget_day(self.clock.now()))
        snap.update({
            "mode": self.mode,
            "data_dir": str(self.config.data_dir),
            "lock_held": self.lock_handle is not None,
            "usage": usage,
            "usage_estimated": True,
            "clocks": {
                "service_heartbeat": self.store.get_clock("service_heartbeat"),
            },
            "takeover": self.last_takeover,
            "jev_configured": bool(getattr(self.jev, "configured", lambda: False)()),
            "start_cursor": self.store.get_meta("start_cursor_set"),
            "caught_up": self.caught_up,
        })
        return snap
