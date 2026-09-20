from __future__ import annotations

import threading

from sprint_coordinator.board import BoardClient
from sprint_coordinator.config import CoordinatorConfig
from sprint_coordinator.judgments import load_jev_client
from sprint_coordinator.lock import acquire
from sprint_coordinator.routing import Router, budget_day, is_user_request
from sprint_coordinator.store import Store
from sprint_coordinator.takeover import takeover_report
from sprint_coordinator.util import Clock
from sprint_coordinator.workers import SlotGate, SubprocessRunner, WorkerError


class WorkerPool:
    def __init__(self, runner, slots):
        self.runner = runner
        self.slots = slots
        self._lock = threading.Lock()
        self._done = []
        self._threads = []

    def start(self, row: dict, command: list, role: str) -> bool:
        if not self.slots.acquire(role):
            return False

        def work():
            result, err = None, None
            try:
                result = self.runner.run(command, row.get("job") or {})
            except Exception as exc:
                err = exc
            with self._lock:
                self._done.append((row["id"], role, result, err))

        thread = threading.Thread(target=work, daemon=True, name="asg-%s" % row["id"])
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return True

    def pop_done(self) -> list:
        with self._lock:
            items = list(self._done)
            self._done.clear()
        return items

    def join(self, timeout: float) -> None:
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    def alive(self) -> int:
        with self._lock:
            return sum(1 for t in self._threads if t.is_alive())


class Coordinator:
    def __init__(self, config: CoordinatorConfig, mode: str = "shadow", clock=None,
                 board=None, jev=None, runner=None, check_runner=None,
                 acknowledge_autoheal_gap: bool = False):
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
        self.pool = WorkerPool(self.runner, self.slots)
        self.router = Router(self.store, config, self.jev, self.runner, self.clock,
                             check_runner=check_runner)
        self.acknowledge_autoheal_gap = acknowledge_autoheal_gap
        self.lock_handle = None
        self.last_takeover = None

    def open(self, recover: bool = True) -> dict:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock_handle = acquire(self.config.data_dir)
        if self.lock_handle is None:
            raise RuntimeError("another coordinator holds the flock")
        gen = self.store.bump_generation()
        recovered = {"restarted": [], "uncertain": []}
        if recover:
            recovered = self.store.recover_uncertain(self.clock.now())
        self.heartbeat("open")
        return {"generation": gen, "recovery": recovered, "mode": self.mode}

    def close(self) -> None:
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
        page = self.board.events_after(after)
        events = page.get("events") or []
        head = int(page.get("head") or 0)
        if head and after and head < after:
            raise ValueError("board history moved backwards; inspect before resetting")
        inserted = self.store.ingest_events(events, self.clock.now())
        # Pagination: if the page was full, caller ticks again.
        return inserted

    def tick(self, wait: bool = False) -> dict:
        self.heartbeat("tick")
        inserted = self.ingest()
        events = [self.store.event(seq) for seq in inserted]
        created = self.router.create_obligations(events)
        routed = self.router.route_received()
        expired = self.router.expire_deadlines()
        started = self._launch()
        if wait:
            self.pool.join(min(2.0, self.config.worker_timeout_seconds))
        reaped = self._reap()
        if wait:
            started.extend(self._launch())
            self.pool.join(min(2.0, self.config.worker_timeout_seconds))
            reaped.extend(self._reap())
        published = self.router.publish_outbox(self.board, self.mode)
        if wait:
            started.extend(self._launch())
            self.pool.join(min(2.0, self.config.worker_timeout_seconds))
            reaped.extend(self._reap())
            published.extend(self.router.publish_outbox(self.board, self.mode))
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
            "user_requests_open": [
                o["id"] for o in self.store.open_obligations() if is_user_request(
                    self.store.event(o["event_seq"]) or {})
            ],
        })
        return snap

    def _launch(self) -> list:
        started = []
        for row in self.store.assignments_by_status("pending"):
            role = row["role"] if row["role"] in ("response", "code") else "response"
            if not self.slots.can_start(role):
                continue
            spec = self.config.worker(row["worker"])
            now = self.clock.now()
            self.store.update_assignment(row["id"], status="running", started_at=now,
                                         progress_at=now)
            self.store.set_clock("job_progress:%s" % row["id"], now, "running")
            if self.pool.start(row, spec["command"], role):
                started.append(row["id"])
            else:
                self.store.update_assignment(row["id"], status="pending")
        return started

    def _reap(self) -> list:
        done = []
        for aid, role, result, err in self.pool.pop_done():
            row = self.store.assignment(aid)
            if row is None:
                self.slots.release(role)
                continue
            try:
                if err is not None:
                    if isinstance(err, WorkerError):
                        self.router._fail(row, str(err))
                    else:
                        self.router._fail(row, type(err).__name__)
                else:
                    self.router._finish(row, result)
            finally:
                self.slots.release(role)
            done.append(aid)
        return done

    def status(self) -> dict:
        snap = self.store.snapshot()
        usage = self.store.usage_day(budget_day(self.clock.now()))
        snap.update({
            "mode": self.mode,
            "data_dir": str(self.config.data_dir),
            "lock_held": self.lock_handle is not None,
            "usage": usage,
            "clocks": {
                "service_heartbeat": self.store.get_clock("service_heartbeat"),
            },
            "takeover": self.last_takeover,
            "jev_configured": bool(getattr(self.jev, "configured", lambda: False)()),
        })
        return snap
