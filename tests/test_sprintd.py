#!/usr/bin/env python3
"""Tests for bin/sprintd. Stdlib unittest only.

Every test spins a real HTTP server on 127.0.0.1 with an ephemeral port and a
temp data dir. Port 8377 (the real default) is never touched.
"""

import base64
import http.client
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
SPRINTD_PATH = os.path.join(os.path.dirname(HERE), "bin", "sprintd")

_loader = SourceFileLoader("sprintd", SPRINTD_PATH)
_spec = importlib.util.spec_from_loader("sprintd", _loader)
sprintd = importlib.util.module_from_spec(_spec)
_loader.exec_module(sprintd)

# 1x1 transparent PNG
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
           "DwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

GOOD_PACKET = {
    "claim": "The header no longer overlaps the sidebar at 980px.",
    "diffstat": "web/app.css | 4 +-",
    "branch": "sprint/card-1",
    "test_cmd": "make test",
    "test_result": "12 pass, 0 fail",
    "validate": ["Open the board at 980px wide.",
                 "The header sits above the sidebar, nothing overlaps."],
    "ui_change": False,
}


class Base(unittest.TestCase):
    SILENCE_SECONDS = 300.0
    SILENCE_TICK = 5.0
    SESSION_OFFLINE = 90.0
    WAITER_ONLINE = 10.0
    WAITER_GONE = 30.0
    WAITER_PERSIST = 5.0
    SSE_HEARTBEAT = 15.0
    START_BACKGROUND = False
    # None == production defaults (the staleness sweep never fires inside a
    # test that didn't ask for it). TestStaleSweep shrinks them to seconds.
    SWEEP_TICK = None
    SWEEP_THRESHOLDS = None
    SWEEP_BACKOFF = None
    SWEEP_MAX_REMINDERS = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project_root = os.path.join(self.tmp, "project")
        os.makedirs(self.project_root)
        self.logfh = open(os.path.join(self.tmp, "server.log"), "a", encoding="utf-8")
        self.addCleanup(self.logfh.close)
        self.app = sprintd.App(
            self.project_root,
            log=self.logfh,
            token="test-token",
            silence_seconds=self.SILENCE_SECONDS,
            silence_tick=self.SILENCE_TICK,
            session_offline_seconds=self.SESSION_OFFLINE,
            waiter_online_seconds=self.WAITER_ONLINE,
            waiter_gone_seconds=self.WAITER_GONE,
            waiter_persist_seconds=self.WAITER_PERSIST,
            sse_heartbeat=self.SSE_HEARTBEAT,
            sweep_tick=self.SWEEP_TICK,
            sweep_thresholds=self.SWEEP_THRESHOLDS,
            sweep_backoff=self.SWEEP_BACKOFF,
            sweep_max_reminders=self.SWEEP_MAX_REMINDERS,
        )
        self.httpd = sprintd.make_server(self.app, "127.0.0.1", 0)
        self.host, self.port = self.httpd.server_address[0], self.httpd.server_address[1]
        self.assertNotEqual(self.port, sprintd.DEFAULT_PORT)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        if self.START_BACKGROUND:
            self.app.start_background()
        self.addCleanup(self._teardown)

    def _teardown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            self.app.close()

    # -- http helpers ---------------------------------------------------

    def req(self, method, path, body=None, token="test-token", headers=None,
            timeout=10.0):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            hdrs = {"Accept": "application/json"}
            if token:
                hdrs["Authorization"] = "Bearer " + token
            if body is not None:
                hdrs["Content-Type"] = "application/json"
            hdrs.update(headers or {})
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        finally:
            conn.close()
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, raw

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body if body is not None else {}, **kw)

    # -- fixtures -------------------------------------------------------

    def new_card(self, text="fix the header", images=None, hold=False):
        status, card = self.post("/api/cards",
                                 {"text": text, "images": images or [], "hold": hold})
        self.assertEqual(status, 201, card)
        return card

    def to_in_progress(self, num):
        status, _ = self.post("/api/cards/%d/state" % num, {"state": "triaging"})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        self.assertEqual(status, 200)

    def state_of(self, num):
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(status, 200, detail)
        return detail["card"]["state"]

    def kinds_for(self, num):
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(status, 200, detail)
        return [e["kind"] for e in detail["timeline"]]


class TestAuthAndHealth(Base):
    def test_healthz_needs_no_auth(self):
        status, body = self.get("/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(os.path.realpath(body["project_root"]),
                         os.path.realpath(self.project_root))

    def test_api_requires_token(self):
        status, body = self.get("/api/board", token=None)
        self.assertEqual(status, 401)
        status, _ = self.get("/api/board", token="wrong")
        self.assertEqual(status, 401)
        status, _ = self.get("/api/board")
        self.assertEqual(status, 200)

    def test_query_token_sets_cookie(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("GET", "/?t=test-token")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 302)
            cookie = resp.getheader("Set-Cookie") or ""
            # per-PORT name: cookies ignore the port, so two boards on one host
            # sharing a name would sign each other out
            self.assertIn(sprintd.board_cookie_name(self.port) + "=test-token",
                          cookie)
        finally:
            conn.close()
        # the cookie alone authenticates
        status, _ = self.get("/api/board", token=None,
                             headers={"Cookie": "%s=test-token"
                                                % sprintd.board_cookie_name(self.port)})
        self.assertEqual(status, 200)
        # ...and so does the legacy name, so an already-signed-in browser is
        # not logged out by the upgrade
        status, _ = self.get("/api/board", token=None,
                             headers={"Cookie": "%s=test-token" % sprintd.COOKIE_NAME})
        self.assertEqual(status, 200)

    def test_handshake_keeps_the_other_query_params(self):
        """?t= is consumed by the cookie handshake; ?theme=/?mock= must survive."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("GET", "/?t=test-token&theme=dark&mock=1")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 302)
            loc = resp.getheader("Location")
            self.assertNotIn("t=test-token", loc)
            self.assertIn("theme=dark", loc)
            self.assertIn("mock=1", loc)
        finally:
            conn.close()

    def test_placeholder_page_when_web_missing(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        if not os.path.isdir(sprintd.web_dir()):
            self.assertIn(b"sprintd placeholder", body)


class TestCards(Base):
    def test_create_with_image_writes_attachment_before_event(self):
        card = self.new_card("header overlaps\nsecond line", images=[PNG_B64])
        self.assertEqual(card["num"], 1)
        self.assertEqual(card["state"], "queued")
        self.assertEqual(card["title"], "header overlaps")

        status, detail = self.get("/api/cards/1")
        self.assertEqual(status, 200)
        atts = detail["attachments"]
        self.assertEqual(len(atts), 1)
        att = atts[0]
        self.assertEqual(att["mime"], "image/png")
        self.assertTrue(os.path.isfile(att["path"]),
                        "attachment must exist on disk: %s" % att["path"])
        self.assertTrue(os.path.basename(att["path"]).startswith(att["sha256"]))
        with open(att["path"], "rb") as fh:
            self.assertEqual(fh.read(), base64.b64decode(PNG_B64))

        # the referencing event carries the same ref
        submitted = [e for e in detail["timeline"] if e["kind"] == "submitted"][0]
        self.assertEqual(submitted["payload"]["attachments"][0]["sha256"], att["sha256"])

        # and it is servable
        status, blob = self.get(att["url"])
        self.assertEqual(status, 200)
        self.assertEqual(blob, base64.b64decode(PNG_B64))

    def test_content_addressed_dedup(self):
        c1 = self.new_card("one", images=[PNG_B64])
        c2 = self.new_card("two", images=["data:image/png;base64," + PNG_B64])
        _, d1 = self.get("/api/cards/%d" % c1["num"])
        _, d2 = self.get("/api/cards/%d" % c2["num"])
        self.assertEqual(d1["attachments"][0]["sha256"], d2["attachments"][0]["sha256"])
        self.assertEqual(len(os.listdir(self.app.attach_dir)), 1)

    def test_image_only_card_and_empty_rejected(self):
        card = self.new_card("", images=[PNG_B64])
        self.assertEqual(card["title"], "(image only)")
        status, body = self.post("/api/cards", {"text": "  ", "images": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "empty_submission")

    def test_non_image_attachment_rejected(self):
        status, body = self.post(
            "/api/cards",
            {"text": "x", "images": [base64.b64encode(b"not an image").decode()]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_image")

    def test_upload_cap(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=20)
        try:
            body = b'{"text":"' + b"x" * (sprintd.DEFAULT_MAX_UPLOAD + 1024) + b'"}'
            conn.request("POST", "/api/cards", body=body, headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json"})
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 413)
            self.assertEqual(payload["error"], "too_large")
        finally:
            conn.close()

    def test_idempotency_key_replays(self):
        hdr = {"Idempotency-Key": "abc-123"}
        s1, c1 = self.req("POST", "/api/cards", {"text": "same"}, headers=hdr)
        s2, c2 = self.req("POST", "/api/cards", {"text": "same"}, headers=hdr)
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(c1["num"], c2["num"])
        _, board = self.get("/api/board")
        self.assertEqual(len(board["cards"]), 1)

    def test_actions_pin_cancel_duplicate(self):
        card = self.new_card("pin me")
        num = card["num"]
        status, body = self.post("/api/cards/%d/action" % num, {"action": "pin"})
        self.assertEqual(status, 200)
        self.assertTrue(body["card"]["pinned"])
        other = self.new_card("original")
        status, body = self.post("/api/cards/%d/action" % num,
                                 {"action": "duplicate_of", "dup_of": other["num"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["card"]["state"], "duplicate")
        self.assertEqual(body["card"]["dup_of"], other["num"])
        status, body = self.post("/api/cards/%d/action" % other["num"],
                                 {"action": "cancel"})
        self.assertEqual(status, 200)
        self.assertEqual(body["card"]["state"], "canceled")


class TestStateMachine(Base):
    def test_legal_path(self):
        num = self.new_card()["num"]
        self.assertEqual(self.state_of(num), "queued")
        for state in ("triaging", "in_progress", "blocked"):
            body = {"state": state}
            if state == "blocked":
                body["reason"] = "ci_red"
            status, _ = self.post("/api/cards/%d/state" % num, body)
            self.assertEqual(status, 200, state)
            self.assertEqual(self.state_of(num), state)
        status, _ = self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        self.assertEqual(status, 200)
        # every transition left a state event behind
        kinds = self.kinds_for(num)
        self.assertGreaterEqual(kinds.count("state"), 5)

    def test_illegal_transition_409(self):
        num = self.new_card()["num"]
        self.post("/api/cards/%d/action" % num, {"action": "cancel"})
        self.assertEqual(self.state_of(num), "canceled")
        status, body = self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], "illegal_transition")
        self.assertEqual(body["from_state"], "canceled")
        self.assertEqual(body["to_state"], "in_progress")
        self.assertEqual(self.state_of(num), "canceled")

    def test_completed_is_terminal(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, _ = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/cards/%d/verdict" % num, {"verdict": "approve"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "integrating")
        status, _ = self.post("/api/cards/%d/integrated" % num, {"ok": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "completed")
        status, body = self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        self.assertEqual(status, 409, body)

    def test_ready_cannot_jump_straight_to_completed(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(self.state_of(num), "ready")
        # /integrated is only for cards the session is actually merging
        status, body = self.post("/api/cards/%d/integrated" % num, {"ok": True})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], "not_integrating")
        self.assertEqual(self.state_of(num), "ready")
        self.assertNotIn("completed", sprintd.TRANSITIONS["ready"],
                         "Done must mean the branch landed")

    def test_ready_not_reachable_via_state_endpoint(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/state" % num, {"state": "ready"})
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"], "evidence_required")
        self.assertEqual(self.state_of(num), "in_progress")

    def test_blocked_requires_reason(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/state" % num, {"state": "blocked"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "reason")
        self.assertEqual(self.state_of(num), "in_progress")

    def test_unknown_card_404(self):
        status, body = self.get("/api/cards/999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "no_such_card")


class TestEvidenceGate(Base):
    def test_empty_packet_422_names_every_missing_field(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": {}})
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"], "evidence_incomplete")
        for field in ("claim", "diffstat", "branch", "test_cmd", "test_result",
                      "validate", "ui_change"):
            self.assertIn(field, body["missing"])
        self.assertEqual(self.state_of(num), "in_progress")
        # rejection is on the timeline so the worker can read why
        _, detail = self.get("/api/cards/%d" % num)
        rejections = [e for e in detail["timeline"]
                      if e["kind"] == "evidence" and not e["payload"].get("ok")]
        self.assertEqual(len(rejections), 1)
        self.assertIn("claim", rejections[0]["payload"]["missing"])

    def test_prose_test_result_rejected_counts_required(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        packet = dict(GOOD_PACKET, test_result="tests pass")
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": packet})
        self.assertEqual(status, 422, body)
        self.assertEqual(body["missing"], ["test_result"])

    def test_ui_change_requires_screenshots(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        packet = dict(GOOD_PACKET, ui_change=True)
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": packet})
        self.assertEqual(status, 422, body)
        self.assertEqual(body["missing"], ["screenshots"])
        packet["screenshots"] = ["deadbeef"]
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": packet})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "ready")

    def test_422_then_200(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/ready" % num,
                                 {"packet": {"claim": "did it"}})
        self.assertEqual(status, 422)
        self.assertIn("test_result", body["missing"])
        self.assertNotIn("claim", body["missing"])
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "ready")
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["evidence"]["packet"]["claim"], GOOD_PACKET["claim"])
        self.assertEqual(len(detail["evidence_history"]), 1)

    def test_validate_steps_are_required(self):
        """The human must be able to confirm the fix without reading code."""
        num = self.new_card()["num"]
        self.to_in_progress(num)

        no_validate = dict(GOOD_PACKET)
        no_validate.pop("validate")
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": no_validate})
        self.assertEqual(status, 422, body)
        self.assertEqual(body["missing"], ["validate"])
        self.assertEqual(self.state_of(num), "in_progress")

        for bad in ("", "   ", [], [""], ["  "], ["ok step", ""], "\n",
                    None, 5, {"step": "x"}, [1, 2]):
            packet = dict(GOOD_PACKET, validate=bad)
            status, body = self.post("/api/cards/%d/ready" % num, {"packet": packet})
            self.assertEqual(status, 422, "validate=%r must be rejected" % (bad,))
            self.assertIn("validate", body["missing"])
            self.assertEqual(self.state_of(num), "in_progress")

        # named on the timeline so the worker sees exactly why
        _, detail = self.get("/api/cards/%d" % num)
        rejections = [e for e in detail["timeline"]
                      if e["kind"] == "evidence" and not e["payload"].get("ok")]
        self.assertTrue(all("validate" in e["payload"]["missing"] for e in rejections))

    def test_validate_accepts_a_string_or_a_list_of_steps(self):
        one = self.new_card("a")["num"]
        self.to_in_progress(one)
        packet = dict(GOOD_PACKET,
                      validate="Run `make lint` — it exits 0 where it used to exit 1.")
        status, body = self.post("/api/cards/%d/ready" % one, {"packet": packet})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(one), "ready")

        two = self.new_card("b")["num"]
        self.to_in_progress(two)
        packet = dict(GOOD_PACKET, validate=["Open /board.", "Click Approve.",
                                             "The card says merging…"])
        status, body = self.post("/api/cards/%d/ready" % two, {"packet": packet})
        self.assertEqual(status, 200, body)
        _, detail = self.get("/api/cards/%d" % two)
        self.assertEqual(len(detail["evidence"]["packet"]["validate"]), 3)

    def test_verdict_approve_and_bounce_escalation(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        status, body = self.post("/api/cards/%d/verdict" % num,
                                 {"verdict": "bounce", "notes": "still overlaps"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cards"][0]["bounce_count"], 1)
        self.assertFalse(body["cards"][0]["escalate"])
        self.assertEqual(self.state_of(num), "in_progress")

        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        status, body = self.post("/api/cards/%d/verdict" % num, {"verdict": "bounce"})
        self.assertEqual(status, 200)
        self.assertEqual(body["cards"][0]["bounce_count"], 2)
        self.assertTrue(body["cards"][0]["escalate"])
        _, detail = self.get("/api/cards/%d" % num)
        escalations = [e for e in detail["timeline"]
                       if e["kind"] == "verdict" and e["payload"].get("escalate")]
        self.assertEqual(len(escalations), 1)

        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        status, _ = self.post("/api/cards/%d/verdict" % num, {"verdict": "approve"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "integrating")
        status, _ = self.post("/api/cards/%d/integrated" % num, {"ok": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "completed")

    def test_verdict_on_non_ready_card_409(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/verdict" % num, {"verdict": "approve"})
        self.assertEqual(status, 409, body)


class TestIntegrating(Base):
    def _approved(self, text="merge me"):
        num = self.new_card(text)["num"]
        self.to_in_progress(num)
        status, _ = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200)
        status, body = self.post("/api/cards/%d/verdict" % num, {"verdict": "approve"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "integrating")
        return num

    def test_approve_flips_to_integrating_not_completed(self):
        num = self._approved()
        _, board = self.get("/api/board")
        card = {c["num"]: c for c in board["cards"]}[num]
        self.assertEqual(card["state"], "integrating")
        self.assertEqual(board["counts"].get("integrating"), 1)
        self.assertIn("integrating", board["states"])
        self.assertEqual(board["column_of"]["integrating"], "ready",
                         "merging cards stay in the Ready column")
        self.assertEqual(board["column_of"]["completed"], "done")

    def test_integration_success_completes(self):
        num = self._approved()
        status, body = self.post("/api/cards/%d/integrated" % num,
                                 {"ok": True, "reason": "merged to main"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cards"][0]["state"], "completed")
        self.assertEqual(self.state_of(num), "completed")

    def test_integration_failure_returns_to_in_progress_without_bouncing(self):
        num = self._approved()
        _, before = self.get("/api/cards/%d" % num)
        self.assertEqual(before["card"]["bounce_count"], 0)

        status, body = self.post("/api/cards/%d/integrated" % num,
                                 {"ok": False, "reason": "rebase conflict in web/app.css"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "in_progress")

        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["bounce_count"], 0,
                         "integration failure is NOT a review bounce")
        errors = [e for e in detail["timeline"] if e["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("rebase conflict in web/app.css", errors[0]["payload"]["text"])
        self.assertEqual(errors[0]["payload"]["reason"],
                         "rebase conflict in web/app.css")

        # a real bounce still increments, so the two paths stay distinguishable
        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        status, body = self.post("/api/cards/%d/verdict" % num, {"verdict": "bounce"})
        self.assertEqual(status, 200)
        self.assertEqual(body["cards"][0]["bounce_count"], 1)

    def test_integration_failure_requires_a_reason(self):
        num = self._approved()
        status, body = self.post("/api/cards/%d/integrated" % num, {"ok": False})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "reason")
        self.assertEqual(self.state_of(num), "integrating")
        status, body = self.post("/api/cards/%d/integrated" % num, {"reason": "x"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "ok")

    def test_integrating_is_not_terminal(self):
        num = self._approved()
        # chat still lands
        status, _ = self.post("/api/cards/%d/chat" % num, {"text": "any luck merging?"})
        self.assertEqual(status, 201)
        self.assertEqual(self.state_of(num), "integrating")
        # and the user can still cancel it
        status, body = self.post("/api/cards/%d/action" % num, {"action": "cancel"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["state"], "canceled")

    def test_integrated_rejects_non_integrating_cards(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/integrated" % num, {"ok": True})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], "not_integrating")
        self.assertEqual(body["from_state"], "in_progress")
        self.assertEqual(self.state_of(num), "in_progress")


class TestQuestions(Base):
    def test_question_answer_and_duplicate_answer_409(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/question" % num,
                                 {"text": "Which header — the sticky one or the page one?",
                                  "options": ["sticky", "page"]})
        self.assertEqual(status, 201, body)
        qid = body["question"]["id"]
        self.assertEqual(self.state_of(num), "needs_you")

        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["open_question"]["id"], qid)
        self.assertEqual(detail["card"]["open_question"]["options"], ["sticky", "page"])

        status, body = self.post("/api/cards/%d/answer" % num,
                                 {"question_id": qid, "text": "sticky"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "in_progress")

        status, body = self.post("/api/cards/%d/answer" % num,
                                 {"question_id": qid, "text": "sticky again"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], "already_answered")
        self.assertEqual(self.state_of(num), "in_progress")

        kinds = self.kinds_for(num)
        self.assertEqual(kinds.count("question"), 1)
        self.assertEqual(kinds.count("answer"), 1)

    def test_answer_unknown_question_404(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/answer" % num,
                                 {"question_id": 4242, "text": "hi"})
        self.assertEqual(status, 404, body)

    def test_chat_and_worker_events(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, _ = self.post("/api/cards/%d/chat" % num, {"text": "also check dark mode"})
        self.assertEqual(status, 201)
        status, _ = self.post("/api/cards/%d/events" % num,
                              {"kind": "progress", "payload": {"text": "reading the css"}})
        self.assertEqual(status, 201)
        status, body = self.post("/api/cards/%d/events" % num, {"kind": "bogus"})
        self.assertEqual(status, 400, body)
        kinds = self.kinds_for(num)
        self.assertEqual(kinds.count("chat"), 1)
        self.assertEqual(kinds.count("progress"), 1)


class TestEventsCursor(Base):
    def test_seq_ordering_and_drain(self):
        n1 = self.new_card("one")["num"]
        n2 = self.new_card("two")["num"]
        self.to_in_progress(n1)
        self.post("/api/cards/%d/chat" % n2, {"text": "hello"})
        self.post("/api/sidebar", {"text": "why is #%d blocked?" % n1, "actor": "user"})

        status, body = self.get("/api/events?after=0&limit=1000")
        self.assertEqual(status, 200)
        seqs = [e["seq"] for e in body["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        self.assertEqual(seqs[-1], body["head"])

        # paged drain matches the whole-log read exactly once
        drained, cursor = [], 0
        while True:
            status, page = self.get("/api/events?after=%d&limit=3" % cursor)
            self.assertEqual(status, 200)
            if not page["events"]:
                break
            self.assertLessEqual(len(page["events"]), 3)
            drained.extend(page["events"])
            cursor = page["next"]
        self.assertEqual([e["seq"] for e in drained], seqs)
        self.assertEqual(cursor, body["head"])

        # sprint-level events carry card_num NULL and show in the sidebar
        sidebar = [e for e in drained if e["card_num"] is None and e["kind"] == "chat"]
        self.assertEqual(len(sidebar), 1)
        status, side = self.get("/api/sidebar")
        self.assertEqual(status, 200)
        self.assertEqual([e["seq"] for e in side["events"]],
                         [e["seq"] for e in sidebar])

        # card timelines are a projection of the same log
        _, detail = self.get("/api/cards/%d" % n1)
        card_seqs = [e["seq"] for e in detail["timeline"]]
        self.assertEqual(card_seqs, sorted(card_seqs))
        self.assertTrue(set(card_seqs).issubset(set(seqs)))

    def test_cursor_roundtrip_and_session_liveness(self):
        self.new_card("one")
        status, board = self.get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(board["session"]["status"], "online")
        head = board["seq"]
        status, _ = self.post("/api/cursors/orchestrator", {"seq": head})
        self.assertEqual(status, 200)
        status, cur = self.get("/api/cursors/orchestrator")
        self.assertEqual(cur["seq"], head)
        _, board = self.get("/api/board")
        self.assertEqual(board["session"]["pending"], 0)

    def test_every_read_surface_exposes_the_drain_cursor(self):
        """Per-message status is derived from (event seq vs drain cursor), so
        every surface the UI reads has to carry the cursor -- board, card
        detail and the events poll alike."""
        card = self.new_card("cursor please")
        num = card["num"]
        for path in ("/api/board", "/api/cards/%d" % num, "/api/events?after=0"):
            status, body = self.get(path)
            self.assertEqual(status, 200, body)
            self.assertEqual(body.get("cursor"), 0, "%s: cursor missing/wrong" % path)
        _, board = self.get("/api/board")
        self.assertEqual(board["session"]["cursor"], 0)

        head = self.app.max_seq()
        self.post("/api/cursors/orchestrator", {"seq": head})
        for path in ("/api/board", "/api/cards/%d" % num, "/api/events?after=0"):
            status, body = self.get(path)
            self.assertEqual(body.get("cursor"), head, "%s did not follow the cursor" % path)
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["session"]["cursor"], head)
        self.assertEqual(detail["session"]["status"], "online")

    def test_a_sent_message_gets_a_seq_and_only_then_is_seen(self):
        """The two honest facts behind the UI's ticks: the POST hands back the
        seq that means 'landed', and only a cursor at/past that seq means the
        session actually read it. Sidebar and card chat both."""
        card = self.new_card("chat with me")
        num = card["num"]
        status, side = self.post("/api/sidebar", {"text": "hello session", "actor": "user"})
        self.assertEqual(status, 201, side)
        status, chat = self.post("/api/cards/%d/chat" % num, {"text": "hello agent"})
        self.assertEqual(status, 201, chat)

        side_seq, chat_seq = side["event"]["seq"], chat["event"]["seq"]
        for seq in (side_seq, chat_seq):
            self.assertIsInstance(seq, int)
            self.assertGreater(seq, 0)
        # landed means durable: both are readable back out of the log
        _, log = self.get("/api/events?after=0&limit=1000")
        by_seq = {e["seq"]: e for e in log["events"]}
        self.assertEqual(by_seq[side_seq]["payload"]["text"], "hello session")
        self.assertEqual(by_seq[chat_seq]["payload"]["text"], "hello agent")

        # not seen yet -- the session has drained nothing
        self.assertLess(log["cursor"], side_seq)

        # the session drains only as far as the sidebar line
        self.post("/api/cursors/orchestrator", {"seq": side_seq})
        _, log = self.get("/api/events?after=0&limit=1000")
        self.assertGreaterEqual(log["cursor"], side_seq, "sidebar line has been read")
        self.assertLess(log["cursor"], chat_seq, "card chat line has NOT been read yet")

    def test_cursor_post_wakes_the_streams_immediately(self):
        """A cursor move appends no event, so a stream parked on the condition
        only learns about it because set_cursor notifies. Without the notify the
        waiter sits there until its own timeout -- 'session is on it' would show
        up seconds late, which is exactly the lag this card is about."""
        elapsed = []

        def watcher():
            started = time.time()
            with self.app.cond:
                self.app.cond.wait(timeout=6.0)
            elapsed.append(time.time() - started)

        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        time.sleep(0.2)
        self.post("/api/cursors/orchestrator", {"seq": 1})
        t.join(timeout=8.0)
        self.assertTrue(elapsed, "watcher never returned")
        self.assertLess(elapsed[0], 1.5,
                        "set_cursor must notify() the waiters, not let them time out")

    def test_wait_cli_exit_codes(self):
        info = {"pid": os.getpid(), "port": self.port, "host": self.host,
                "token": "test-token", "project_root": self.project_root,
                "started_at": time.time()}
        sprintd.write_server_json(
            os.path.join(self.app.data_dir, sprintd.SERVER_JSON), info)
        self.new_card("something happened")
        head = self.app.max_seq()

        rc = sprintd.main(["--project-root", self.project_root, "wait",
                           "--after", "0", "--timeout", "5"])
        self.assertEqual(rc, 0, "events past 0 exist -> exit 0")

        t0 = time.time()
        rc = sprintd.main(["--project-root", self.project_root, "wait",
                           "--after", str(head), "--timeout", "1", "--poll", "0.1"])
        self.assertEqual(rc, 2, "no events -> exit 2 (relaunch me)")
        self.assertGreaterEqual(time.time() - t0, 0.9)

        empty = os.path.join(self.tmp, "no-server")
        os.makedirs(empty)
        rc = sprintd.main(["--project-root", empty, "wait", "--after", "0",
                           "--timeout", "1"])
        self.assertEqual(rc, 3, "unreachable -> exit 3")

    def test_wait_wakes_on_new_event(self):
        info = {"pid": os.getpid(), "port": self.port, "host": self.host,
                "token": "test-token", "project_root": self.project_root,
                "started_at": time.time()}
        sprintd.write_server_json(
            os.path.join(self.app.data_dir, sprintd.SERVER_JSON), info)
        head = self.app.max_seq()
        result = {}

        def waiter():
            result["rc"] = sprintd.main(
                ["--project-root", self.project_root, "wait", "--after", str(head),
                 "--timeout", "10", "--poll", "0.1"])

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.3)
        self.new_card("late arrival")
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["rc"], 0)


class TestSessionOffline(Base):
    SESSION_OFFLINE = 0.4

    def test_board_reports_session_offline_when_cursor_stalls(self):
        self.new_card("queue me")
        time.sleep(0.6)
        status, board = self.get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(board["session"]["status"], "offline")
        self.assertGreater(board["session"]["pending"], 0)
        # submissions are still accepted while offline
        card = self.new_card("still accepted")
        self.assertEqual(card["state"], "queued")
        # cursor movement brings it back
        self.post("/api/cursors/orchestrator", {"seq": self.app.max_seq()})
        _, board = self.get("/api/board")
        self.assertEqual(board["session"]["status"], "online")


class TestWaiterHeartbeat(Base):
    """The session's waiter polling /api/events IS the session being attached.

    Before this, liveness came only from the drain cursor, so an orchestrator
    that was mid-dispatch (cursor legitimately minutes behind, waiter polling the
    whole time) was reported `offline` and the board raised a banner that read
    like the backend had restarted. It never had.
    """
    SESSION_OFFLINE = 0.4       # cursor counts as "behind" fast
    WAITER_ONLINE = 0.3
    WAITER_GONE = 1.5
    WAITER_PERSIST = 0.0        # persist every hit so the disk path is exercised

    def waiter_poll(self, after=0):
        return self.get("/api/events?after=%d&limit=1" % after,
                        headers={sprintd.WAITER_HEADER: "sprintd-wait"})

    def test_waiter_hit_records_a_sighting(self):
        self.new_card("one")
        _, board = self.get("/api/board")
        self.assertIsNone(board["session"]["seconds_since_waiter"],
                          "no waiter has polled yet -- do not invent one")
        self.assertFalse(board["session"]["waiter_polling"])

        status, _ = self.waiter_poll()
        self.assertEqual(status, 200)
        _, board = self.get("/api/board")
        self.assertIsNotNone(board["session"]["waiter_seen_at"])
        self.assertLess(board["session"]["seconds_since_waiter"], 1.0)
        self.assertTrue(board["session"]["waiter_polling"])

    def test_waiter_sighting_is_persisted_not_just_in_memory(self):
        """A restarted server must not forget the session was here a second ago."""
        self.waiter_poll()
        row = self.app.q1("SELECT * FROM cursors WHERE name=?", (sprintd.WAITER_CURSOR,))
        self.assertIsNotNone(row, "waiter sighting never reached the db")
        self.app._waiter_seen_at = None          # simulate a cold start
        self.assertIsNotNone(self.app.waiter_seen_at())

    def test_a_browser_polling_events_never_fakes_liveness(self):
        """The board's own UI falls back to polling /api/events. That is a
        browser, not the session -- counting it would be fake liveness."""
        self.new_card("queue me")
        self.get("/api/events?after=0")          # no waiter header: a browser
        time.sleep(0.6)
        self.get("/api/events?after=0")
        _, board = self.get("/api/board")
        self.assertIsNone(board["session"]["waiter_seen_at"])
        self.assertEqual(board["session"]["status"], "offline")

    def test_busy_not_offline_while_the_waiter_is_alive(self):
        """The reported bug, end to end: cursor stalls, waiter keeps polling."""
        self.new_card("dispatch me")
        self.waiter_poll()
        time.sleep(0.6)                          # cursor now stale past SESSION_OFFLINE
        self.waiter_poll()                       # ...but the waiter is right here
        _, board = self.get("/api/board")
        sess = board["session"]
        self.assertEqual(sess["status"], "busy")
        self.assertTrue(sess["online"], "busy must not read as offline to the UI")
        self.assertEqual(sess["note"], "session is on it — catching up")
        self.assertGreater(sess["pending"], 0)
        self.assertGreater(sess["seconds_since_cursor_move"], self.SESSION_OFFLINE)

    def test_busy_decays_to_offline_only_once_the_waiter_is_gone(self):
        self.new_card("dispatch me")
        self.waiter_poll()
        time.sleep(0.6)
        _, board = self.get("/api/board")
        self.assertEqual(board["session"]["status"], "busy")
        time.sleep(1.1)                          # total > WAITER_GONE
        _, board = self.get("/api/board")
        self.assertEqual(board["session"]["status"], "offline")
        self.assertFalse(board["session"]["online"])
        self.assertEqual(board["session"]["note"], "session offline — items will queue")

    def test_a_caught_up_session_is_online_even_with_no_waiter(self):
        """Nothing pending means nothing is behind: no banner, no busy dot."""
        self.new_card("one")
        self.post("/api/cursors/orchestrator", {"seq": self.app.max_seq()})
        time.sleep(0.6)
        _, board = self.get("/api/board")
        self.assertEqual(board["session"]["status"], "online")
        self.assertEqual(board["session"]["pending"], 0)

    def test_posting_the_drain_cursor_counts_as_a_sighting(self):
        self.post("/api/cursors/orchestrator", {"seq": 0})
        _, board = self.get("/api/board")
        self.assertIsNotNone(board["session"]["waiter_seen_at"])

    def test_waiter_query_param_works_for_plain_curl_drains(self):
        status, _ = self.get("/api/events?after=0&waiter=1")
        self.assertEqual(status, 200)
        _, board = self.get("/api/board")
        self.assertIsNotNone(board["session"]["waiter_seen_at"])

    def test_an_unauthenticated_hit_cannot_mark_the_session_alive(self):
        self.new_card("queue me")
        status, _ = self.get("/api/events?after=0", token=None,
                             headers={sprintd.WAITER_HEADER: "forged"})
        self.assertEqual(status, 401)
        _, board = self.get("/api/board")
        self.assertIsNone(board["session"]["waiter_seen_at"])


class TestWaiterThresholdsAreTunable(Base):
    """The thresholds are env-tunable so tests (and a slow machine) can move
    them without editing code."""

    def test_env_overrides_are_read(self):
        keys = {"SPRINT_SESSION_OFFLINE_SECONDS": "7",
                "SPRINT_WAITER_ONLINE_SECONDS": "3",
                "SPRINT_WAITER_GONE_SECONDS": "11"}
        old = {k: os.environ.get(k) for k in keys}
        os.environ.update(keys)
        try:
            root = os.path.join(self.tmp, "envproj")
            os.makedirs(root)
            app = sprintd.App(root, log=self.logfh, token="t")
            try:
                self.assertEqual(app.session_offline_seconds, 7.0)
                self.assertEqual(app.waiter_online_seconds, 3.0)
                self.assertEqual(app.waiter_gone_seconds, 11.0)
                live = app.session_liveness()
                self.assertEqual(live["offline_after_seconds"], 7.0)
                self.assertEqual(live["waiter_gone_seconds"], 11.0)
            finally:
                app.close()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestSilenceTimer(Base):
    SILENCE_SECONDS = 2.0
    SILENCE_TICK = 0.2
    START_BACKGROUND = True

    def _wait_for_silent(self, num, timeout=12.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, detail = self.get("/api/cards/%d" % num)
            events = [e for e in detail["timeline"] if e["kind"] == "agent_silent"]
            if events:
                return events
            time.sleep(0.1)
        return []

    def _silent_events(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e for e in detail["timeline"] if e["kind"] == "agent_silent"]

    def _wait_for_silent_count(self, num, want, timeout=12.0):
        deadline = time.time() + timeout
        events = self._silent_events(num)
        while time.time() < deadline and len(events) < want:
            time.sleep(0.1)
            events = self._silent_events(num)
        return events

    def test_agent_silent_fires_once_and_rearms(self):
        quiet = self.new_card("quiet card")["num"]
        longjob = self.new_card("long job")["num"]
        self.to_in_progress(quiet)
        self.to_in_progress(longjob)
        status, body = self.post("/api/cards/%d/events" % longjob,
                                 {"kind": "note", "long_running": True,
                                  "payload": {"text": "running the full suite, ~20 min"}})
        self.assertEqual(status, 201, body)
        self.assertTrue(body["card"]["long_running"])

        events = self._wait_for_silent(quiet)
        self.assertEqual(len(events), 1, "exactly one agent_silent per episode")
        self.assertEqual(events[0]["actor"], "server")
        self.assertGreaterEqual(events[0]["payload"]["silent_for_seconds"], 2.0)

        # no repeat while still silent
        time.sleep(self.SILENCE_SECONDS + 0.6)
        self.assertEqual(len(self._silent_events(quiet)), 1)

        # long_running suppresses the timer entirely
        self.assertEqual(self._silent_events(longjob), [])

        # the card is flagged silent on the board
        _, board = self.get("/api/board")
        by_num = {c["num"]: c for c in board["cards"]}
        self.assertTrue(by_num[quiet]["silent"])
        self.assertFalse(by_num[longjob]["silent"])

        # new worker activity re-arms; a second episode fires a second event
        status, _ = self.post("/api/cards/%d/events" % quiet,
                              {"kind": "progress", "payload": {"text": "alive, sorry"}})
        self.assertEqual(status, 201)
        _, board = self.get("/api/board")
        self.assertFalse({c["num"]: c for c in board["cards"]}[quiet]["silent"])
        deadline = time.time() + 12
        while time.time() < deadline and len(self._silent_events(quiet)) < 2:
            time.sleep(0.1)
        self.assertEqual(len(self._silent_events(quiet)), 2,
                         "worker activity re-arms the timer")

    def test_no_silence_for_non_in_progress_cards(self):
        num = self.new_card("just queued")["num"]
        time.sleep(self.SILENCE_SECONDS + 0.8)
        self.assertEqual(self._silent_events(num), [])

    # -- one agent, several cards --------------------------------------------
    #
    # The clock is the AGENT's, not the card's: an agent posting progress on
    # one of its cards is not silent on the others.

    def assign(self, num, agent):
        status, body = self.post("/api/cards/%d/assign" % num,
                                 {"agent_name": agent, "worktree": "/tmp/wt",
                                  "branch": "sprint/" + agent})
        self.assertEqual(status, 200, body)

    def test_activity_on_one_card_keeps_the_agents_other_cards_quiet(self):
        a = self.new_card("card A")["num"]
        b = self.new_card("card B")["num"]
        for num in (a, b):
            self.assign(num, "sprint-batch-1")
            self.to_in_progress(num)

        # keep posting on A only, well past the threshold
        deadline = time.time() + self.SILENCE_SECONDS * 2.5
        while time.time() < deadline:
            status, _ = self.post("/api/cards/%d/events" % a,
                                  {"kind": "progress", "payload": {"text": "still on it"}})
            self.assertEqual(status, 201)
            time.sleep(0.3)

        self.assertEqual(self._silent_events(b), [],
                         "B must not amber while its own agent is visibly working on A")
        self.assertEqual(self._silent_events(a), [])
        _, board = self.get("/api/board")
        by_num = {c["num"]: c for c in board["cards"]}
        self.assertFalse(by_num[b]["silent"])
        self.assertFalse(by_num[a]["silent"])

    def test_both_cards_amber_once_the_agent_itself_goes_quiet(self):
        a = self.new_card("card A")["num"]
        b = self.new_card("card B")["num"]
        for num in (a, b):
            self.assign(num, "sprint-batch-2")
            self.to_in_progress(num)
        self.post("/api/cards/%d/events" % a, {"kind": "progress", "payload": {"text": "one line"}})

        self.assertEqual(len(self._wait_for_silent(a)), 1)
        self.assertEqual(len(self._wait_for_silent(b)), 1,
                         "a truly quiet agent still ambers every card it holds")

        # and one event per episode, per card — re-arm semantics unchanged
        time.sleep(self.SILENCE_SECONDS + 0.6)
        self.assertEqual(len(self._silent_events(a)), 1)
        self.assertEqual(len(self._silent_events(b)), 1)

        # a line on A re-arms BOTH cards, and both fire again
        self.post("/api/cards/%d/events" % a, {"kind": "progress", "payload": {"text": "back"}})
        _, board = self.get("/api/board")
        by_num = {c["num"]: c for c in board["cards"]}
        self.assertFalse(by_num[a]["silent"])
        self.assertFalse(by_num[b]["silent"], "the agent spoke — B is not silent either")
        self.assertEqual(len(self._wait_for_silent_count(a, 2)), 2)
        self.assertEqual(len(self._wait_for_silent_count(b, 2)), 2)

    def test_a_single_card_agent_is_unchanged(self):
        num = self.new_card("solo")["num"]
        self.assign(num, "sprint-card-solo")
        self.to_in_progress(num)
        self.assertEqual(len(self._wait_for_silent(num)), 1)

    def test_another_agents_activity_does_not_cover_for_you(self):
        mine = self.new_card("mine")["num"]
        theirs = self.new_card("theirs")["num"]
        self.assign(mine, "sprint-card-a")
        self.assign(theirs, "sprint-card-b")
        for num in (mine, theirs):
            self.to_in_progress(num)
        deadline = time.time() + self.SILENCE_SECONDS * 2.0
        while time.time() < deadline:
            self.post("/api/cards/%d/events" % theirs,
                      {"kind": "progress", "payload": {"text": "busy over here"}})
            time.sleep(0.3)
        self.assertEqual(len(self._wait_for_silent(mine)), 1,
                         "a different agent's chatter must not cover for a quiet one")


class SweepBase(Base):
    """Cards parked in a state somebody owes an action on.

    Ages are faked by rewinding the card's own events rather than by sleeping:
    a ten-minute rule tested with a ten-minute threshold is the real rule, and
    a test that sleeps through it isn't a test anyone will run.
    """

    def backdate(self, num, seconds):
        with self.app.lock:
            self.app.conn.execute("UPDATE events SET ts=ts-? WHERE card_num=?",
                                  (seconds, num))
            self.app.conn.execute("UPDATE cards SET updated_at=updated_at-? WHERE num=?",
                                  (seconds, num))

    def stuck_events(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e for e in detail["timeline"] if e["kind"] == "stuck"]

    def board_card(self, num):
        _, board = self.get("/api/board")
        return {c["num"]: c for c in board["cards"]}[num]

    # -- fixtures, one per parked state ----------------------------------

    def a_queued_card(self):
        return self.new_card("nobody has picked this up")["num"]

    def a_blocked_card(self):
        num = self.new_card("walled off")["num"]
        status, _ = self.post("/api/cards/%d/state" % num,
                              {"state": "blocked", "reason": "ci_red"})
        self.assertEqual(status, 200)
        return num

    def a_needs_you_card(self):
        num = self.new_card("has a question")["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/question" % num,
                                 {"text": "sqlite or a file lock?"})
        self.assertEqual(status, 201, body)
        self.assertEqual(self.state_of(num), "needs_you")
        return num

    def a_ready_card(self):
        num = self.new_card("waiting on a verdict")["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200, body)
        return num

    def an_integrating_card(self):
        num = self.a_ready_card()
        status, body = self.post("/api/cards/%d/verdict" % num, {"verdict": "approve"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "integrating")
        return num


class TestStaleSweep(SweepBase):
    """The five rules, at their real production thresholds."""

    def test_integrating_past_ten_minutes_is_stuck(self):
        """Today's failure, exactly: approved, then forgotten at 'merging'."""
        num = self.an_integrating_card()
        self.backdate(num, 11 * 60)
        self.assertEqual(self.app.sweep_stuck(), 1)

        events = self.stuck_events(num)
        self.assertEqual(len(events), 1, "one opening notice")
        ev = events[0]
        self.assertEqual(ev["actor"], "server")
        self.assertEqual(ev["payload"]["state"], "integrating")
        self.assertEqual(ev["payload"]["threshold_seconds"], 600.0)
        self.assertGreaterEqual(ev["payload"]["stuck_for_seconds"], 660)
        self.assertIn("still merging", ev["payload"]["text"])
        self.assertIn("11m", ev["payload"]["text"])
        self.assertIsNone(ev["payload"].get("reply_to"),
                          "a server event has no human waiting behind it")

    def test_queued_with_nobody_on_it_past_fifteen_minutes_is_stuck(self):
        num = self.a_queued_card()
        self.backdate(num, 16 * 60)
        self.assertEqual(self.app.sweep_stuck(), 1)
        p = self.stuck_events(num)[0]["payload"]
        self.assertEqual(p["state"], "queued")
        self.assertEqual(p["threshold_seconds"], 900.0)
        self.assertIn("dispatch it", p["text"])

    def test_blocked_past_thirty_minutes_is_stuck(self):
        num = self.a_blocked_card()
        self.backdate(num, 31 * 60)
        self.assertEqual(self.app.sweep_stuck(), 1)
        p = self.stuck_events(num)[0]["payload"]
        self.assertEqual(p["state"], "blocked")
        self.assertEqual(p["threshold_seconds"], 1800.0)
        self.assertIn("re-checking", p["text"])

    def test_needs_you_unanswered_past_thirty_minutes_is_stuck(self):
        num = self.a_needs_you_card()
        self.backdate(num, 31 * 60)
        self.assertEqual(self.app.sweep_stuck(), 1)
        p = self.stuck_events(num)[0]["payload"]
        self.assertEqual(p["state"], "needs_you")
        self.assertEqual(p["threshold_seconds"], 1800.0)
        self.assertIn("waiting on you", p["text"])

    def test_ready_past_a_day_is_stuck(self):
        num = self.a_ready_card()
        self.backdate(num, 25 * 3600)
        self.assertEqual(self.app.sweep_stuck(), 1)
        p = self.stuck_events(num)[0]["payload"]
        self.assertEqual(p["state"], "ready")
        self.assertEqual(p["threshold_seconds"], 86400.0)
        self.assertIn("verdict", p["text"])
        self.assertIn("1d", p["text"])

    def test_ready_under_a_day_is_not_stuck_yet(self):
        """The thresholds are per-state, not one global clock: 23h of `ready`
        is fine where 11m of `integrating` is not."""
        num = self.a_ready_card()
        self.backdate(num, 23 * 3600)
        self.assertEqual(self.app.sweep_stuck(), 0)
        self.assertEqual(self.stuck_events(num), [])

    def test_healthy_cards_are_left_alone(self):
        """Every card the sweep must NOT touch, in one place."""
        fresh_queued = self.a_queued_card()          # young
        fresh_integrating = self.an_integrating_card()

        working = self.new_card("an agent is on it")["num"]
        self.to_in_progress(working)                 # in_progress is not swept
        self.backdate(working, 6 * 3600)

        held = self.new_card("not dispatched yet", hold=True)["num"]
        self.backdate(held, 6 * 3600)                # held is deliberate, not stale

        done = self.an_integrating_card()
        status, _ = self.post("/api/cards/%d/integrated" % done, {"ok": True})
        self.assertEqual(status, 200)
        self.backdate(done, 6 * 3600)                # terminal states are over

        picked_up = self.a_queued_card()             # queued but assigned == mid-pickup
        status, _ = self.post("/api/cards/%d/assign" % picked_up,
                              {"agent_name": "sprint-card-x", "worktree": "/tmp/wt",
                               "branch": "sprint/x"})
        self.assertEqual(status, 200)
        with self.app.lock:                          # assign also moves it to triaging
            self.app.conn.execute("UPDATE cards SET state='queued' WHERE num=?", (picked_up,))
        self.backdate(picked_up, 6 * 3600)

        self.assertEqual(self.app.sweep_stuck(), 0)
        for num in (fresh_queued, fresh_integrating, working, held, done, picked_up):
            self.assertEqual(self.stuck_events(num), [], "#%d should be healthy" % num)

    def test_a_note_on_a_blocked_card_restarts_its_recheck_clock(self):
        """`blocked` is the one re-check rule: somebody looking at the wall and
        saying so is exactly the action the reminder was asking for."""
        num = self.a_blocked_card()
        self.backdate(num, 31 * 60)
        status, _ = self.post("/api/cards/%d/events" % num,
                              {"kind": "note", "payload": {"text": "ci still red"}})
        self.assertEqual(status, 201)
        self.assertEqual(self.app.sweep_stuck(), 0)
        self.assertEqual(self.stuck_events(num), [])

    def test_the_board_ambers_a_stuck_card_until_it_moves(self):
        num = self.an_integrating_card()
        self.assertFalse(self.board_card(num)["stuck"])
        self.backdate(num, 11 * 60)
        self.app.sweep_stuck()
        self.assertTrue(self.board_card(num)["stuck"])
        _, detail = self.get("/api/cards/%d" % num)
        self.assertTrue(detail["card"]["stuck"])

        status, _ = self.post("/api/cards/%d/integrated" % num, {"ok": True})
        self.assertEqual(status, 200)
        self.assertFalse(self.board_card(num)["stuck"],
                         "moving the card clears the flag")

    def test_a_reminder_does_not_count_as_activity(self):
        """Otherwise the card face reads 'just now' in amber — the age and the
        colour contradicting each other on one line."""
        num = self.an_integrating_card()
        self.backdate(num, 11 * 60)
        before = self.board_card(num)["last_activity_at"]
        self.app.sweep_stuck()
        after = self.board_card(num)
        self.assertTrue(after["stuck"])
        self.assertEqual(after["last_activity_at"], before,
                         "the sweep's own reminder is not activity")
        self.assertEqual(after["last_event"]["kind"], "stuck",
                         "it is still the latest event, just not activity")

    def test_thresholds_are_env_tunable(self):
        """The SPRINT_SWEEP_* knobs the tests (and a ten-second demo) rely on."""
        names = {state: envname for state, envname, _d in sprintd.STUCK_RULES}
        self.assertEqual(sorted(names.values()), sorted([
            "SPRINT_SWEEP_BLOCKED_SECONDS", "SPRINT_SWEEP_INTEGRATING_SECONDS",
            "SPRINT_SWEEP_NEEDS_YOU_SECONDS", "SPRINT_SWEEP_QUEUED_SECONDS",
            "SPRINT_SWEEP_READY_SECONDS"]))
        saved = {k: os.environ.get(k) for k in list(names.values()) + ["SPRINT_SWEEP_TICK"]}
        try:
            for envname in names.values():
                os.environ[envname] = "7"
            os.environ["SPRINT_SWEEP_TICK"] = "0.25"
            app = sprintd.App(self.project_root, data_dir=os.path.join(self.tmp, "envdir"),
                              token="t", log=self.logfh)
            try:
                self.assertEqual(app.sweep_tick, 0.25)
                for state in names:
                    self.assertEqual(app.sweep_thresholds[state], 7.0, state)
            finally:
                app.close()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestStuckBackoff(SweepBase):
    """One notice, three backing-off reminders, then silence — until it moves."""

    SWEEP_BACKOFF = (600.0, 1800.0, 5400.0)
    SWEEP_MAX_REMINDERS = 3

    def _age_last_stuck(self, num, seconds):
        """Pretend the last reminder was `seconds` ago."""
        rows = self.app.q("SELECT seq FROM events WHERE card_num=? AND kind='stuck' "
                          "ORDER BY seq DESC LIMIT 1", (num,))
        self.assertTrue(rows, "no stuck event to age")
        with self.app.lock:
            self.app.conn.execute("UPDATE events SET ts=ts-? WHERE seq=?",
                                  (seconds, rows[0]["seq"]))

    def test_reminders_back_off_then_stop_at_three(self):
        num = self.an_integrating_card()
        self.backdate(num, 11 * 60)

        self.assertEqual(self.app.sweep_stuck(), 1)          # the opening notice
        self.assertEqual(len(self.stuck_events(num)), 1)

        # A second sweep a moment later says nothing: the ladder starts at 10m.
        self.assertEqual(self.app.sweep_stuck(), 0)
        self._age_last_stuck(num, 9 * 60)
        self.assertEqual(self.app.sweep_stuck(), 0, "9m < the 10m first gap")

        for gap, want in ((10 * 60, 2), (30 * 60, 3), (90 * 60, 4)):
            self._age_last_stuck(num, gap)
            self.assertEqual(self.app.sweep_stuck(), 1, "gap %ds should fire" % gap)
            self.assertEqual(len(self.stuck_events(num)), want)

        # Cap reached: one notice + three reminders, and then it shuts up
        # however long the card sits there.
        for _ in range(3):
            self._age_last_stuck(num, 24 * 3600)
            self.assertEqual(self.app.sweep_stuck(), 0, "capped at 3 reminders")
        self.assertEqual(len(self.stuck_events(num)), 4)
        self.assertEqual([e["payload"]["reminder"] for e in self.stuck_events(num)],
                         [0, 1, 2, 3])

    def test_a_state_change_re_arms_the_episode(self):
        num = self.an_integrating_card()
        self.backdate(num, 11 * 60)
        for _ in range(4):
            self.app.sweep_stuck()
            self._age_last_stuck(num, 24 * 3600)
        self.assertEqual(len(self.stuck_events(num)), 4)
        self.assertEqual(self.app.sweep_stuck(), 0, "episode is spent")

        # The session finally lands it, the user re-opens it, it stalls again:
        # a fresh episode, counted from zero.
        status, _ = self.post("/api/cards/%d/integrated" % num,
                              {"ok": False, "reason": "rebase conflict"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "in_progress")
        status, _ = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/cards/%d/verdict" % num, {"verdict": "approve"})
        self.assertEqual(status, 200)
        self.backdate(num, 11 * 60)

        self.assertEqual(self.app.sweep_stuck(), 1, "a state change re-arms it")
        self.assertEqual(self.stuck_events(num)[-1]["payload"]["reminder"], 0)


class TestSweepThreadFiresOnItsOwn(SweepBase):
    """The rule the user actually asked for: nobody has to run anything."""

    SWEEP_TICK = 0.1
    SWEEP_THRESHOLDS = {"integrating": 0.5, "queued": 0.5, "blocked": 0.5,
                        "needs_you": 0.5, "ready": 0.5}
    START_BACKGROUND = True

    def test_a_forgotten_merge_flags_itself(self):
        num = self.an_integrating_card()
        deadline = time.time() + 12
        while time.time() < deadline and not self.stuck_events(num):
            time.sleep(0.1)
        events = self.stuck_events(num)
        self.assertTrue(events, "the sweep thread never fired")
        self.assertEqual(events[0]["payload"]["state"], "integrating")
        self.assertEqual(events[0]["payload"]["threshold_seconds"], 0.5)
        self.assertTrue(self.board_card(num)["stuck"])


class TestHoldMode(Base):
    def test_per_card_hold_flag(self):
        card = self.new_card("hold this one", hold=True)
        self.assertEqual(card["state"], "held")
        status, body = self.post("/api/cards/%d/action" % card["num"],
                                 {"action": "release"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["state"], "queued")

    def test_sprint_hold_mode_then_go(self):
        status, body = self.post("/api/sprint",
                                 {"action": "set_hold_mode", "hold_mode": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["sprint"]["hold_mode"])

        nums = [self.new_card("css nit %d" % i)["num"] for i in range(3)]
        _, board = self.get("/api/board")
        self.assertTrue(board["hold_mode"])
        self.assertEqual(board["counts"].get("held"), 3)
        self.assertIsNone(board["counts"].get("queued"))

        status, body = self.post("/api/sprint",
                                 {"action": "set_hold_mode", "hold_mode": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["sprint"]["hold_mode"])
        for num in nums:
            status, _ = self.post("/api/cards/%d/action" % num, {"action": "release"})
            self.assertEqual(status, 200)
        _, board = self.get("/api/board")
        self.assertEqual(board["counts"].get("queued"), 3)
        # positions are assigned in card order
        positions = {c["num"]: c["queue_position"] for c in board["cards"]}
        self.assertEqual([positions[n] for n in nums], [1, 2, 3])

        # after the flip, new cards queue again
        self.assertEqual(self.new_card("fresh")["state"], "queued")

    def test_hold_action_on_queued_card(self):
        num = self.new_card("queued then held")["num"]
        status, body = self.post("/api/cards/%d/action" % num, {"action": "hold"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["state"], "held")

    def test_sprint_open_close(self):
        self.new_card("opens the sprint implicitly")
        status, body = self.post("/api/sprint", {"action": "open"})
        self.assertEqual(status, 200)
        self.assertFalse(body["created"], "a sprint is already open")
        status, body = self.post("/api/sprint", {"action": "close"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(body["sprint"]["closed_at"])
        status, body = self.post("/api/sprint", {"action": "open", "title": "round two"})
        self.assertEqual(status, 200)
        self.assertTrue(body["created"])
        status, body = self.get("/api/sprints")
        self.assertEqual(len(body["sprints"]), 2)


class TestBatches(Base):
    def _batch_of_three(self):
        nums = [self.new_card("css nit %d" % i)["num"] for i in range(3)]
        status, body = self.post("/api/batches",
                                 {"card_nums": nums, "agent_name": "sprint-batch-1",
                                  "branch": "sprint/batch-1",
                                  "worktree": "/tmp/wt-batch-1"})
        self.assertEqual(status, 201, body)
        for num in nums:
            self.to_in_progress(num)
        return nums, body["batch"]

    def test_batch_shares_agent_badge(self):
        nums, batch = self._batch_of_three()
        self.assertEqual(batch["cards"], nums)
        _, board = self.get("/api/board")
        for card in board["cards"]:
            self.assertEqual(card["agent_name"], "sprint-batch-1")
            self.assertEqual(card["batch_id"], batch["id"])
        self.assertEqual(len(board["batches"]), 1)

    def test_batch_ready_requires_per_card_then_flips_all(self):
        nums, _ = self._batch_of_three()
        lead = nums[0]

        status, body = self.post("/api/cards/%d/ready" % lead,
                                 {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 422, body)
        self.assertIn("per_card", body["missing"])
        for num in nums:
            self.assertEqual(self.state_of(num), "in_progress")

        partial = dict(GOOD_PACKET,
                       per_card=[{"card_num": nums[0], "claim": "fixed a"},
                                 {"card_num": nums[1], "claim": "fixed b"}])
        status, body = self.post("/api/cards/%d/ready" % lead, {"packet": partial})
        self.assertEqual(status, 422, body)
        self.assertIn("per_card[#%d]" % nums[2], body["missing"])

        blank_claim = dict(GOOD_PACKET,
                           per_card=[{"card_num": n, "claim": ""} for n in nums])
        status, body = self.post("/api/cards/%d/ready" % lead, {"packet": blank_claim})
        self.assertEqual(status, 422, body)
        self.assertIn("per_card[#%d].claim" % nums[0], body["missing"])

        full = dict(GOOD_PACKET,
                    per_card=[{"card_num": n, "claim": "fixed #%d" % n} for n in nums])
        status, body = self.post("/api/cards/%d/ready" % lead, {"packet": full})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(body["cards"]), sorted(nums))
        for num in nums:
            self.assertEqual(self.state_of(num), "ready", "#%d must flip too" % num)

    def test_batch_wholesale_approve_and_member_bounce(self):
        nums, _ = self._batch_of_three()
        full = dict(GOOD_PACKET,
                    per_card=[{"card_num": n, "claim": "fixed #%d" % n} for n in nums])
        status, _ = self.post("/api/cards/%d/ready" % nums[0], {"packet": full})
        self.assertEqual(status, 200)

        # one member bounces back to the agent, the rest stay ready
        status, body = self.post("/api/cards/%d/verdict" % nums[1],
                                 {"verdict": "bounce", "notes": "still 2px off"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(nums[1]), "in_progress")
        self.assertEqual(self.state_of(nums[0]), "ready")

        # bounce cannot be applied batch-wide
        status, body = self.post("/api/cards/%d/verdict" % nums[0],
                                 {"verdict": "bounce", "scope": "batch"})
        self.assertEqual(status, 400, body)

        # approving the batch wholesale needs every member ready
        status, body = self.post("/api/cards/%d/verdict" % nums[0],
                                 {"verdict": "approve", "scope": "batch"})
        self.assertEqual(status, 409, body)

        status, _ = self.post("/api/cards/%d/ready" % nums[1], {"packet": full})
        self.assertEqual(status, 200)
        status, body = self.post("/api/cards/%d/verdict" % nums[0],
                                 {"verdict": "approve", "scope": "batch"})
        self.assertEqual(status, 200, body)
        for num in nums:
            self.assertEqual(self.state_of(num), "integrating",
                             "approved members wait on the real merge")

        # one merge, reported across every member card
        status, body = self.post("/api/cards/%d/integrated" % nums[0],
                                 {"ok": True, "card_nums": nums})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(c["num"] for c in body["cards"]), sorted(nums))
        for num in nums:
            self.assertEqual(self.state_of(num), "completed")

    def test_batch_integration_failure_returns_every_member(self):
        nums, _ = self._batch_of_three()
        full = dict(GOOD_PACKET,
                    per_card=[{"card_num": n, "claim": "fixed #%d" % n} for n in nums])
        self.post("/api/cards/%d/ready" % nums[0], {"packet": full})
        status, _ = self.post("/api/cards/%d/verdict" % nums[0],
                              {"verdict": "approve", "scope": "batch"})
        self.assertEqual(status, 200)

        status, body = self.post("/api/cards/%d/integrated" % nums[0],
                                 {"ok": False, "reason": "gate failed: 3 fail",
                                  "card_nums": nums})
        self.assertEqual(status, 200, body)
        for num in nums:
            self.assertEqual(self.state_of(num), "in_progress")
            _, detail = self.get("/api/cards/%d" % num)
            self.assertEqual(detail["card"]["bounce_count"], 0)
            self.assertEqual([e["kind"] for e in detail["timeline"]].count("error"), 1)

    def test_batch_integrated_is_all_or_nothing(self):
        nums, _ = self._batch_of_three()
        full = dict(GOOD_PACKET,
                    per_card=[{"card_num": n, "claim": "fixed #%d" % n} for n in nums])
        self.post("/api/cards/%d/ready" % nums[0], {"packet": full})
        self.post("/api/cards/%d/verdict" % nums[0],
                  {"verdict": "approve", "scope": "batch"})
        # take one member out of integrating
        self.post("/api/cards/%d/action" % nums[2], {"action": "cancel"})
        status, body = self.post("/api/cards/%d/integrated" % nums[0],
                                 {"ok": True, "card_nums": nums})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["card"], nums[2])
        for num in nums[:2]:
            self.assertEqual(self.state_of(num), "integrating",
                             "no member may be half-completed")

    def test_board_carries_what_awaiting_review_groups_on(self):
        """The Awaiting-review list groups cards into work units client-side, so
        the board payload has to say -- on every ready card, with no second
        fetch -- which batch it belongs to, which agent carried it, which branch
        it is on, and what its OWN claim inside a shared batch packet is."""
        nums, batch = self._batch_of_three()
        full = dict(GOOD_PACKET,
                    per_card=[{"card_num": n, "claim": "fixed #%d" % n} for n in nums])
        self.post("/api/cards/%d/ready" % nums[0], {"packet": full})

        # a singleton on its own branch, same sprint
        solo = self.new_card("its own thing")["num"]
        self.post("/api/cards/%d/assign" % solo,
                  {"agent_name": "sprint-card-%d" % solo, "worktree": "/tmp/wt-solo",
                   "branch": "sprint/card-%d" % solo})
        self.post("/api/cards/%d/state" % solo, {"state": "in_progress"})
        self.post("/api/cards/%d/ready" % solo, {"packet": dict(GOOD_PACKET)})

        status, board = self.get("/api/board")
        self.assertEqual(status, 200, board)
        cards = {c["num"]: c for c in board["cards"]}
        for num in nums:
            c = cards[num]
            self.assertEqual(c["state"], "ready")
            self.assertEqual(c["batch_id"], batch["id"], "the work unit's key")
            self.assertEqual(c["agent_name"], "sprint-batch-1")
            self.assertEqual(c["branch"], "sprint/batch-1", "the work unit's name")
            packet = c["evidence"]["packet"]
            mine = [e for e in packet["per_card"] if e["card_num"] == num]
            self.assertEqual(mine[0]["claim"], "fixed #%d" % num,
                             "a member row leads with its own claim, not the branch's")

        c = cards[solo]
        self.assertIsNone(c["batch_id"], "a singleton is a work unit of one")
        self.assertEqual(c["agent_name"], "sprint-card-%d" % solo)
        self.assertEqual(c["branch"], "sprint/card-%d" % solo)
        self.assertNotIn("per_card", c["evidence"]["packet"])


class StreamReader:
    """Read an SSE stream line by line. Shared by every test that reads frames."""

    def _open_stream(self, path="/api/stream?after=0", headers=None, timeout=8.0):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        hdrs = {"Authorization": "Bearer test-token", "Accept": "text/event-stream"}
        hdrs.update(headers or {})
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        self.addCleanup(conn.close)
        return conn, resp

    def _read_lines(self, resp, want, timeout=8.0):
        """Read lines until `want(line)` is true; returns the collected lines."""
        deadline = time.time() + timeout
        lines = []
        while time.time() < deadline:
            try:
                line = resp.fp.readline()
            except (socket.timeout, OSError):
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\n")
            lines.append(text)
            if want(text):
                return lines
        return lines

    def _frames(self, lines, name):
        """Every `data:` payload belonging to an `event: <name>` frame."""
        out, kind = [], None
        for text in lines:
            if text.startswith("event:"):
                kind = text.split(":", 1)[1].strip()
            elif text.startswith("data:"):
                if kind == name:
                    out.append(json.loads(text[5:].strip()))
                kind = None
            elif not text.strip():
                continue
        return out


class TestStream(StreamReader, Base):
    SSE_HEARTBEAT = 0.4

    def test_stream_delivers_events_and_heartbeats(self):
        card = self.new_card("stream me")
        _conn, resp = self._open_stream()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type"))
        def is_submitted(text):
            return text.startswith("data:") and '"kind":"submitted"' in text

        lines = self._read_lines(resp, is_submitted)
        submitted = [t for t in lines if is_submitted(t)]
        self.assertTrue(submitted, lines)
        payload = json.loads(submitted[0][5:].strip())
        self.assertEqual(payload["card_num"], card["num"])
        self.assertTrue(any(t.startswith("id:") for t in lines), lines)
        beat = self._read_lines(resp, lambda t: t.startswith(": heartbeat"), timeout=6)
        self.assertTrue(any(t.startswith(": heartbeat") for t in beat), beat)

    def test_holding_the_stream_open_is_proof_the_session_is_attached(self):
        """`sprintd tail` sits on this stream instead of polling, so the stream
        has to count as liveness the way the waiter's polling does -- but only
        when it says so. A browser's EventSource cannot send the header, and
        counting it would be exactly the fake green the marker exists to stop."""
        self.new_card("liveness")
        has_event = lambda t: t.startswith("data:") and '"seq"' in t  # noqa: E731

        _conn, resp = self._open_stream()                     # a browser
        self._read_lines(resp, has_event)
        _, board = self.get("/api/board")
        self.assertIsNone(board["session"]["waiter_seen_at"],
                          "an unmarked stream is a browser, not the session")

        _conn2, resp2 = self._open_stream(
            headers={sprintd.WAITER_HEADER: "sprintd-tail"})
        self._read_lines(resp2, has_event)
        _, board = self.get("/api/board")
        self.assertIsNotNone(board["session"]["waiter_seen_at"])
        self.assertTrue(board["session"]["waiter_polling"])

    def test_a_quiet_stream_keeps_refreshing_the_sighting(self):
        """Marking only at connect time would let a session that is listening
        perfectly well read as offline 30s later. The sighting refreshes every
        tick, with nothing on the wire but heartbeats."""
        self.new_card("quiet")
        _conn, resp = self._open_stream(
            headers={sprintd.WAITER_HEADER: "sprintd-tail"})
        self._read_lines(resp, lambda t: t.startswith("data:") and '"seq"' in t)
        _, board = self.get("/api/board")
        first = board["session"]["waiter_seen_at"]
        self.assertIsNotNone(first)
        self._read_lines(resp, lambda t: t.startswith(": heartbeat"), timeout=6)
        _, board = self.get("/api/board")
        self.assertGreater(board["session"]["waiter_seen_at"], first,
                           "a stream that is still open is still the session")

    def test_event_frames_are_id_plus_data_only(self):
        """Event frames: `id:` = seq, `data:` = one event JSON, and no `event:`
        line -- a named type would never reach the browser's default message
        handler. The only named frames on this stream are `hello` and `cursor`,
        neither of which is an event and neither of which carries an `id:`
        (they must never become Last-Event-ID)."""
        self.new_card("framing")
        _conn, resp = self._open_stream()
        lines = self._read_lines(resp, lambda t: t.startswith("data:") and '"seq"' in t)
        named = [t for t in lines if t.startswith("event:")]
        self.assertTrue(all(t.strip() in ("event: cursor", "event: hello") for t in named),
                        lines)

        # walk frames: a `cursor` frame is named + un-id'd, an event frame is
        # id'd + anonymous.
        kind, ident, seen_event = None, None, False
        for text in lines:
            if text.startswith("event:"):
                kind = text.split(":", 1)[1].strip()
            elif text.startswith("id:"):
                ident = int(text[3:].strip())
            elif text.startswith("data:"):
                payload = json.loads(text[5:].strip())
                if kind in ("cursor", "hello"):
                    self.assertIsNone(ident, "a %s frame must not carry an id" % kind)
                    self.assertIn("cursor", payload)
                else:
                    seen_event = True
                    self.assertIsNotNone(ident, "an event frame must carry its seq as id")
                    self.assertEqual(ident, payload["seq"])
                    for key in ("seq", "card_num", "ts", "actor", "kind", "payload"):
                        self.assertIn(key, payload)
                kind, ident = None, None
        self.assertTrue(seen_event, lines)

    def test_stream_opens_with_the_drain_cursor(self):
        """The browser has to know where the session is the moment it connects,
        or every message it already sent would read 'landed' forever."""
        self.new_card("hello")
        self.post("/api/cursors/orchestrator", {"seq": self.app.max_seq()})
        _conn, resp = self._open_stream()
        lines = self._read_lines(resp, lambda t: t.startswith("data:") and '"cursor"' in t)
        frames = [t for t in lines if t.startswith("data:") and '"cursor"' in t]
        self.assertTrue(frames, lines)
        payload = json.loads(frames[0][5:].strip())
        self.assertEqual(payload["cursor"], self.app.max_seq())

    def test_cursor_move_is_pushed_live(self):
        """This is what flips a sent message from 'landed' to 'session is on
        it' without a reload: the cursor moving past its seq."""
        _conn, resp = self._open_stream()
        self._read_lines(resp, lambda t: t.startswith("data:") and '"cursor"' in t)

        status, sent = self.post("/api/sidebar", {"text": "you there?", "actor": "user"})
        self.assertEqual(status, 201, sent)
        seq = sent["event"]["seq"]

        # the message is in the log but the session has not read it yet
        self.assertLess(self.app.cursor_seq(), seq)
        self.post("/api/cursors/orchestrator", {"seq": seq})

        def is_seen(text):
            if not (text.startswith("data:") and '"cursor"' in text):
                return False
            return json.loads(text[5:].strip())["cursor"] >= seq

        lines = self._read_lines(resp, is_seen)
        self.assertTrue(any(is_seen(t) for t in lines),
                        "the cursor move must reach the browser: %r" % (lines,))

    def test_last_event_id_resumes(self):
        self.new_card("first")
        head = self.app.max_seq()
        self.new_card("second")
        _conn, resp = self._open_stream(path="/api/stream",
                                        headers={"Last-Event-ID": str(head)})
        self.assertEqual(resp.status, 200)
        lines = self._read_lines(resp, lambda t: t.startswith("id:"))
        ids = [int(t[3:].strip()) for t in lines if t.startswith("id:")]
        self.assertTrue(ids, lines)
        self.assertGreater(ids[0], head, "must not replay events before Last-Event-ID")


class TestServerGeneration(StreamReader, Base):
    """A restart must be DETECTABLE by an open tab.

    The user's report: chat messages silently failed to send from a tab that
    had been open across a backend restart -- nothing landed server-side and
    nothing on screen said so. The tab could not tell "the server is quiet"
    from "the server I was talking to no longer exists". `generation` is that
    one bit: one id per server PROCESS, on every surface a live tab touches.
    """

    SSE_HEARTBEAT = 0.4

    def test_healthz_carries_a_generation(self):
        status, body = self.get("/healthz", token=None)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("generation"), body)
        self.assertIsInstance(body["generation"], str)

    def test_generation_is_stable_within_one_process(self):
        gens = {self.get("/healthz", token=None)[1]["generation"] for _ in range(3)}
        self.new_card("does not change the generation")
        gens.add(self.get("/healthz", token=None)[1]["generation"])
        self.assertEqual(len(gens), 1, gens)
        self.assertEqual(gens.pop(), self.app.generation)

    def test_a_second_process_gets_a_different_generation(self):
        """Same data dir, same token, new process -- and the tab can tell."""
        other = sprintd.App(self.project_root, log=self.logfh, token="test-token")
        self.addCleanup(other.close)
        self.assertNotEqual(other.generation, self.app.generation)

    def test_board_and_events_carry_the_generation(self):
        """The polling fallback learns about a restart too, not just SSE."""
        for path in ("/api/board", "/api/events?after=0"):
            status, body = self.get(path)
            self.assertEqual(status, 200, body)
            self.assertEqual(body.get("generation"), self.app.generation, path)

    def test_stream_opens_with_a_hello_carrying_the_generation(self):
        self.new_card("hello frame")
        _conn, resp = self._open_stream()
        lines = self._read_lines(resp, lambda t: t.startswith("data:") and '"generation"' in t)
        hellos = self._frames(lines, "hello")
        self.assertTrue(hellos, lines)
        self.assertEqual(hellos[0]["generation"], self.app.generation)
        # It is the FIRST frame: a tab must not have to wait for traffic to
        # find out which server it is attached to.
        named = [t for t in lines if t.startswith("event:")]
        self.assertEqual(named[0].strip(), "event: hello", lines)
        # and it says where the log is, so the tab can resume from its cursor
        self.assertIn("cursor", hellos[0])
        self.assertIn("head", hellos[0])

    def test_cursor_frames_carry_the_generation_too(self):
        """The cursor is the most frequent frame on the wire; a tab that missed
        the hello still learns within a second."""
        _conn, resp = self._open_stream()
        # past the hello (which also carries a cursor) to the cursor frame itself
        lines = self._read_lines(
            resp, lambda t: t.startswith("data:") and '"cursor"' in t
            and '"started_at"' not in t)
        cursors = self._frames(lines, "cursor")
        self.assertTrue(cursors, lines)
        for frame in cursors:
            self.assertEqual(frame["generation"], self.app.generation)

    def test_the_log_survives_the_restart_the_generation_reports(self):
        """Why resuming from the cursor is safe: seq is durable, so a tab that
        reconnects after a restart picks up exactly where it stopped hearing.
        """
        num = self.new_card("before the restart")["num"]
        before_seq = self.app.max_seq()
        self.app.close()
        again = sprintd.App(self.project_root, log=self.logfh, token="test-token")
        self.addCleanup(again.close)
        self.assertNotEqual(again.generation, self.app.generation)
        self.assertEqual(again.max_seq(), before_seq)
        again.card_chat(num, "after the restart", actor="user")
        fresh = again.events_after(before_seq, 50)
        self.assertEqual([e["kind"] for e in fresh], ["chat"])
        self.assertGreater(fresh[0]["seq"], before_seq)


class TestReplyToRouting(Base):
    """Every user-originated event says, in machine-readable form, where the
    answer belongs -- `"sidebar"` or `"card:<num>"`. No orchestrator should
    have to infer routing from operating-doc prose, from which endpoint was
    hit, or from the shape of a payload.

    Deliberately a biconditional: stamped on EVERY user event, stripped from
    every worker/session/server event. `reply_to` present means "a human said
    this and is waiting".
    """

    def user_events(self):
        return [e for e in self.get("/api/events?after=0&limit=500")[1]["events"]
                if e["actor"] == "user"]

    def test_the_four_user_surfaces_all_carry_reply_to(self):
        num = self.new_card("route my replies")["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/question" % num, {"text": "which one?"})
        status, _ = self.post("/api/cards/%d/answer" % num, {"text": "the first"})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/cards/%d/chat" % num, {"text": "a message"})
        self.assertEqual(status, 201)
        self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        status, _ = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/cards/%d/verdict" % num,
                              {"verdict": "bounce", "notes": "not quite"})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/sidebar", {"text": "hey", "actor": "user"})
        self.assertEqual(status, 201)

        by_kind = {}
        for ev in self.user_events():
            by_kind.setdefault(ev["kind"], []).append(ev)
        for kind in ("chat", "answer", "verdict", "submitted"):
            self.assertIn(kind, by_kind, by_kind)

        card_ref = "card:%d" % num
        for ev in self.user_events():
            want = "sidebar" if ev["card_num"] is None else "card:%d" % ev["card_num"]
            self.assertEqual(ev["payload"].get("reply_to"), want,
                             "%s event routes wrong: %r" % (ev["kind"], ev["payload"]))
        # the two shapes, both present in this run
        refs = {e["payload"]["reply_to"] for e in self.user_events()}
        self.assertEqual(refs, {card_ref, "sidebar"})

    def test_sidebar_chat_says_sidebar(self):
        self.post("/api/sidebar", {"text": "why is #3 blocked?", "actor": "user"})
        ev = self.user_events()[-1]
        self.assertIsNone(ev["card_num"])
        self.assertEqual(ev["payload"]["reply_to"], "sidebar")

    def test_card_actions_are_user_events_too(self):
        """Total means total: pin, hold, cancel, reopen -- if a human did it on
        a card, the reply goes to that card."""
        num = self.new_card("act on me")["num"]
        self.post("/api/cards/%d/action" % num, {"action": "hold"})
        self.post("/api/cards/%d/action" % num, {"action": "release"})
        acted = [e for e in self.user_events() if e["card_num"] == num]
        self.assertTrue(acted)
        for ev in acted:
            self.assertEqual(ev["payload"]["reply_to"], "card:%d" % num)

    def test_worker_session_and_server_events_carry_none(self):
        num = self.new_card("quiet please")["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/events" % num,
                  {"kind": "progress", "payload": {"text": "step one"}})
        self.post("/api/cards/%d/question" % num, {"text": "which one?"})
        self.post("/api/sidebar", {"text": "on it", "actor": "session"})
        rows = self.get("/api/events?after=0&limit=500")[1]["events"]
        others = [e for e in rows if e["actor"] != "user"]
        self.assertTrue(any(e["actor"] == "worker" for e in others), others)
        self.assertTrue(any(e["actor"] == "session" for e in others), others)
        self.assertTrue(any(e["actor"] == "server" for e in others), others)
        for ev in others:
            self.assertNotIn("reply_to", ev["payload"],
                             "%s/%s must not claim a reply route" % (ev["actor"], ev["kind"]))

    def test_a_worker_cannot_forge_reply_to(self):
        """The server is the only writer of this field. A worker that sends one
        gets it stripped -- otherwise `reply_to` would only be as trustworthy
        as the least careful agent."""
        num = self.new_card("forgery")["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/events" % num,
                  {"kind": "note", "payload": {"text": "hi", "reply_to": "sidebar"}})
        ev = self.get("/api/cards/%d" % num)[1]["timeline"][-1]
        self.assertEqual(ev["actor"], "worker")
        self.assertNotIn("reply_to", ev["payload"])

    def test_reply_to_is_on_the_card_timeline_and_the_drain(self):
        """Both readers see the same field: the drain is what an orchestrator
        reads, the timeline is what the drawer reads."""
        num = self.new_card("both readers")["num"]
        self.post("/api/cards/%d/chat" % num, {"text": "hello there"})
        drained = [e for e in self.get("/api/events?after=0&limit=500")[1]["events"]
                   if e["kind"] == "chat"]
        timeline = [e for e in self.get("/api/cards/%d" % num)[1]["timeline"]
                    if e["kind"] == "chat"]
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0]["payload"]["reply_to"], "card:%d" % num)
        self.assertEqual(timeline[0]["payload"]["reply_to"], "card:%d" % num)


class TestRetryIsIdempotent(Base):
    """A send that failed VISIBLY has to be retryable safely.

    The failure a user cannot tell apart from a lost message is the slow one
    that actually landed: the server took it, the answer never made it back.
    The UI retries with the SAME Idempotency-Key, so that case replays instead
    of writing the message twice.
    """

    def send(self, path, body, key):
        return self.req("POST", path, body, headers={"Idempotency-Key": key})

    def raw(self, path, body, key):
        """Same POST, but keep the response headers -- we want the replay flag."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10.0)
        try:
            conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                         headers={"Authorization": "Bearer test-token",
                                  "Content-Type": "application/json",
                                  "Idempotency-Key": key})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, dict(resp.getheaders()), json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

    def test_retrying_a_card_chat_does_not_duplicate_it(self):
        num = self.new_card("retry me")["num"]
        s1, b1 = self.send("/api/cards/%d/chat" % num, {"text": "did that land?"}, "k-chat")
        s2, b2 = self.send("/api/cards/%d/chat" % num, {"text": "did that land?"}, "k-chat")
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(b1["event"]["seq"], b2["event"]["seq"])
        chats = [e for e in self.get("/api/cards/%d" % num)[1]["timeline"]
                 if e["kind"] == "chat"]
        self.assertEqual(len(chats), 1, chats)

    def test_a_replay_is_labelled_as_one(self):
        num = self.new_card("labelled")["num"]
        self.raw("/api/cards/%d/chat" % num, {"text": "one"}, "k-label")
        status, headers, _ = self.raw("/api/cards/%d/chat" % num, {"text": "one"}, "k-label")
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("Idempotent-Replay"), "true")

    def test_retrying_a_sidebar_line_does_not_duplicate_it(self):
        s1, b1 = self.send("/api/sidebar", {"text": "you there?", "actor": "user"}, "k-side")
        s2, b2 = self.send("/api/sidebar", {"text": "you there?", "actor": "user"}, "k-side")
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(b1["event"]["seq"], b2["event"]["seq"])
        lines = [e for e in self.get("/api/events?after=0&limit=500")[1]["events"]
                 if e["kind"] == "chat" and e["card_num"] is None]
        self.assertEqual(len(lines), 1, lines)

    def test_retrying_an_answer_replays_instead_of_409ing(self):
        """Without a stable key the retry would hit the second-answer 409 and
        read as 'your answer was lost' when it had in fact landed."""
        num = self.new_card("answer me")["num"]
        self.to_in_progress(num)
        status, q = self.post("/api/cards/%d/question" % num, {"text": "which?"})
        self.assertEqual(status, 201, q)
        qid = q["question"]["id"]
        body = {"question_id": qid, "text": "the first"}
        s1, b1 = self.send("/api/cards/%d/answer" % num, body, "k-ans")
        s2, b2 = self.send("/api/cards/%d/answer" % num, body, "k-ans")
        self.assertEqual((s1, s2), (200, 200), (b1, b2))
        answers = [e for e in self.get("/api/cards/%d" % num)[1]["timeline"]
                   if e["kind"] == "answer"]
        self.assertEqual(len(answers), 1, answers)
        self.assertEqual(self.state_of(num), "in_progress")

    def test_retrying_a_verdict_replays_instead_of_409ing(self):
        num = self.new_card("sign me off")["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        body = {"verdict": "approve"}
        s1, _ = self.send("/api/cards/%d/verdict" % num, body, "k-verdict")
        s2, _ = self.send("/api/cards/%d/verdict" % num, body, "k-verdict")
        self.assertEqual((s1, s2), (200, 200))
        verdicts = [e for e in self.get("/api/cards/%d" % num)[1]["timeline"]
                    if e["kind"] == "verdict"]
        self.assertEqual(len(verdicts), 1, verdicts)
        self.assertEqual(self.state_of(num), "integrating")

    def test_a_different_key_is_a_different_message(self):
        """The guard is the key, not the text: sending the same words twice on
        purpose still writes twice."""
        num = self.new_card("say it twice")["num"]
        self.send("/api/cards/%d/chat" % num, {"text": "ping"}, "k-one")
        self.send("/api/cards/%d/chat" % num, {"text": "ping"}, "k-two")
        chats = [e for e in self.get("/api/cards/%d" % num)[1]["timeline"]
                 if e["kind"] == "chat"]
        self.assertEqual(len(chats), 2, chats)


class TestBoardPayloadForTheUI(Base):
    """The board is the UI's whole contract -- every field the card face reads
    has to be on it, with no client-side reconstruction."""

    def board(self):
        status, board = self.get("/api/board")
        self.assertEqual(status, 200, board)
        return board

    def card_on_board(self, num):
        for c in self.board()["cards"]:
            if c["num"] == num:
                return c
        self.fail("#%d is not on the board" % num)

    def test_board_carries_sidebar_thread(self):
        self.post("/api/sidebar", {"text": "why is #1 blocked?", "actor": "user"})
        self.post("/api/sidebar", {"text": "CI is red; nothing you type fixes it.",
                                   "actor": "session"})
        self.new_card("a card, not sidebar noise")
        board = self.board()
        self.assertIn("sidebar", board)
        lines = board["sidebar"]
        self.assertEqual([e["payload"]["text"] for e in lines][-2:],
                         ["why is #1 blocked?", "CI is red; nothing you type fixes it."])
        for e in lines:
            self.assertIsNone(e["card_num"], "sidebar is sprint-level only")
            self.assertIn(e["actor"], ("user", "session"))
        self.assertEqual([e["seq"] for e in lines], sorted(e["seq"] for e in lines))

    def test_board_sidebar_excludes_worker_telemetry(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/events" % num,
                  {"kind": "progress", "payload": {"text": "halfway"}})
        texts = [e["payload"].get("text") for e in self.board()["sidebar"]]
        self.assertNotIn("halfway", texts)

    def test_board_card_has_question_evidence_reason_error_and_column(self):
        blocked = self.new_card("blocked one")["num"]
        self.to_in_progress(blocked)
        self.post("/api/cards/%d/state" % blocked,
                  {"state": "blocked", "reason": "ci_red: main is red"})
        c = self.card_on_board(blocked)
        self.assertEqual(c["reason"], "ci_red: main is red")
        self.assertEqual(c["column"], "blocked")

        asked = self.new_card("asks something")["num"]
        self.to_in_progress(asked)
        self.post("/api/cards/%d/question" % asked,
                  {"text": "slug or uuid?", "options": ["slug", "uuid"]})
        c = self.card_on_board(asked)
        self.assertEqual(c["question"]["text"], "slug or uuid?")
        self.assertEqual(c["question"], c["open_question"])
        self.assertEqual(c["column"], "needs_you")

        ready = self.new_card("ready one")["num"]
        self.to_in_progress(ready)
        self.post("/api/cards/%d/ready" % ready, {"packet": dict(GOOD_PACKET)})
        c = self.card_on_board(ready)
        self.assertEqual(c["evidence"]["packet"]["claim"], GOOD_PACKET["claim"])
        self.assertEqual(c["column"], "ready", "ready cards render in Ready")

        self.post("/api/cards/%d/verdict" % ready, {"verdict": "approve"})
        self.assertEqual(self.card_on_board(ready)["column"], "ready",
                         "integrating stays in Ready as 'merging', never Done")

        failed = self.new_card("dies")["num"]
        self.to_in_progress(failed)
        self.post("/api/cards/%d/events" % failed,
                  {"kind": "error", "payload": {"text": "agent died: worktree vanished"}})
        self.post("/api/cards/%d/state" % failed, {"state": "in_progress"})
        self.app.transition(failed, "failed", "session", reason="agent died")
        c = self.card_on_board(failed)
        self.assertIn("worktree vanished", c["error"])
        self.assertEqual(c["column"], "in_progress")

    def test_board_column_of_covers_every_state(self):
        board = self.board()
        for state in board["states"]:
            self.assertIn(state, board["column_of"])
        self.assertEqual(board["column_of"]["integrating"], "ready")
        self.assertEqual(board["column_of"]["completed"], "done")

    def test_every_batch_member_shows_the_shared_packet(self):
        """One batch, one packet -- but each member card is asking for its own
        verdict, so each one has to show the user what to check."""
        nums = [self.new_card("m%d" % i)["num"] for i in range(3)]
        self.post("/api/batches", {"card_nums": nums, "agent_name": "sprint-batch-1",
                                   "branch": "sprint/batch-1"})
        for n in nums:
            self.to_in_progress(n)
        packet = dict(GOOD_PACKET, per_card=[
            {"card_num": n, "claim": "member %d fixed" % n} for n in nums])
        status, _ = self.post("/api/cards/%d/ready" % nums[0], {"packet": packet})
        self.assertEqual(status, 200)
        for n in nums:
            card = [c for c in self.get("/api/board")[1]["cards"] if c["num"] == n][0]
            self.assertIsNotNone(card["evidence"], "#%d has nothing to review" % n)
            self.assertEqual(card["evidence"]["packet"]["claim"], GOOD_PACKET["claim"])
            _, detail = self.get("/api/cards/%d" % n)
            self.assertEqual(len(detail["evidence"]["packet"]["per_card"]), 3)
        self.assertFalse(self.get("/api/cards/%d" % nums[0])[1]["evidence"]["shared"])
        self.assertTrue(self.get("/api/cards/%d" % nums[1])[1]["evidence"]["shared"],
                        "a member flags that the packet came from a sibling")

    def test_card_detail_shape(self):
        num = self.new_card("shape me", images=[PNG_B64])["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(status, 200, detail)
        for key in ("card", "timeline", "evidence", "attachments"):
            self.assertIn(key, detail)
        self.assertEqual(detail["card"]["num"], num)
        self.assertEqual(detail["events"], detail["timeline"])
        self.assertEqual(detail["evidence"]["packet"]["claim"], GOOD_PACKET["claim"])
        self.assertEqual(len(detail["attachments"]), 1)


class TestAttachmentUrls(Base):
    """Every attachment ref the browser sees must be loadable as-is."""

    def test_submitted_attachment_ref_carries_a_working_url(self):
        num = self.new_card("look", images=[PNG_B64])["num"]
        _, detail = self.get("/api/cards/%d" % num)
        att = detail["attachments"][0]
        self.assertEqual(att["url"], "/api/attachments/%s.png" % att["sha256"])
        status, blob = self.get(att["url"])
        self.assertEqual(status, 200)
        self.assertEqual(blob, base64.b64decode(PNG_B64))

    def test_data_url_images_are_accepted(self):
        num = self.new_card("pasted", images=["data:image/png;base64," + PNG_B64])["num"]
        _, detail = self.get("/api/cards/%d" % num)
        att = detail["attachments"][0]
        self.assertEqual(att["mime"], "image/png")
        self.assertEqual(self.get(att["url"])[0], 200)

    def test_evidence_screenshot_paths_become_urls(self):
        shot = os.path.join(self.tmp, "after-light.png")
        with open(shot, "wb") as fh:
            fh.write(base64.b64decode(PNG_B64))
        num = self.new_card("ui work")["num"]
        self.to_in_progress(num)
        packet = dict(GOOD_PACKET, ui_change=True, screenshots=[shot])
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": packet})
        self.assertEqual(status, 200, body)
        _, detail = self.get("/api/cards/%d" % num)
        ref = detail["evidence"]["packet"]["screenshots"][0]
        self.assertIsInstance(ref, dict)
        self.assertEqual(ref["name"], "after-light.png")
        self.assertTrue(ref["url"].startswith("/api/attachments/"))
        self.assertEqual(self.get(ref["url"])[0], 200,
                         "a worker's file path must be servable to the browser")

    def test_per_card_screenshots_are_ingested_too(self):
        shot = os.path.join(self.tmp, "member.png")
        with open(shot, "wb") as fh:
            fh.write(base64.b64decode(PNG_B64))
        nums = [self.new_card("m%d" % i)["num"] for i in range(2)]
        self.post("/api/batches", {"card_nums": nums, "agent_name": "sprint-batch-1",
                                   "branch": "sprint/batch-1"})
        for n in nums:
            self.to_in_progress(n)
        packet = dict(GOOD_PACKET, ui_change=True, screenshots=[shot], per_card=[
            {"card_num": nums[0], "claim": "one", "screenshots": [shot]},
            {"card_num": nums[1], "claim": "two"},
        ])
        status, body = self.post("/api/cards/%d/ready" % nums[0], {"packet": packet})
        self.assertEqual(status, 200, body)
        _, detail = self.get("/api/cards/%d" % nums[0])
        ref = detail["evidence"]["packet"]["per_card"][0]["screenshots"][0]
        self.assertEqual(self.get(ref["url"])[0], 200)

    def test_unresolvable_screenshot_ref_does_not_pretend_to_be_a_url(self):
        num = self.new_card("no such file")["num"]
        self.to_in_progress(num)
        packet = dict(GOOD_PACKET, ui_change=True,
                      screenshots=["/nope/does-not-exist.png"])
        status, _ = self.post("/api/cards/%d/ready" % num, {"packet": packet})
        self.assertEqual(status, 200)
        _, detail = self.get("/api/cards/%d" % num)
        ref = detail["evidence"]["packet"]["screenshots"][0]
        self.assertIsNone(ref.get("url"))
        self.assertEqual(ref["name"], "does-not-exist.png")


class TestSessionOnlyStates(Base):
    """Only the brain may declare a card dead -- but it MUST have a way to."""

    def test_session_can_mark_failed_with_a_reason(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/state" % num,
                                 {"state": "failed", "actor": "session",
                                  "reason": "agent_gone: worktree pruned mid-run"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "failed")

    def test_failed_requires_a_reason(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/state" % num,
                                 {"state": "failed", "actor": "session"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "reason")

    def test_worker_cannot_declare_itself_failed(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/state" % num,
                                 {"state": "failed", "reason": "I give up"})
        self.assertEqual(status, 400, body)
        self.assertNotIn("failed", body["message"].split("must be one of")[1])
        self.assertEqual(self.state_of(num), "in_progress")

    def test_session_can_mark_stale(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, _ = self.post("/api/cards/%d/state" % num,
                              {"state": "stale", "actor": "session"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "stale")


class TestRetryAction(Base):
    def test_retry_requeues_a_failed_card_and_drops_the_dead_agent(self):
        num = self.new_card("agent dies here")["num"]
        self.post("/api/cards/%d/assign" % num,
                  {"agent_name": "sprint-card-%d" % num, "worktree": "/tmp/wt",
                   "branch": "sprint/card"})
        self.to_in_progress(num)
        self.app.transition(num, "failed", "session", reason="agent died")

        status, body = self.post("/api/cards/%d/action" % num, {"action": "retry"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "queued")

        _, detail = self.get("/api/cards/%d" % num)
        self.assertIsNone(detail["card"]["agent_name"],
                          "a retry gets a FRESH agent; never message the dead one")
        self.assertIsNone(detail["card"]["worktree"])
        notes = [e for e in detail["timeline"]
                 if e["kind"] == "note" and e["payload"].get("retry")]
        self.assertEqual(len(notes), 1)
        self.assertIn("fresh agent", notes[0]["payload"]["text"])
        states = [e["payload"]["to"] for e in detail["timeline"] if e["kind"] == "state"]
        self.assertEqual(states[-1], "queued")

    def test_retry_on_a_healthy_card_is_409(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/action" % num, {"action": "retry"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], "not_retryable")
        self.assertEqual(self.state_of(num), "in_progress")

    def test_retry_works_on_a_stale_card(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        self.app.transition(num, "stale", "session", reason="session gap")
        status, _ = self.post("/api/cards/%d/action" % num, {"action": "retry"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "queued")

    def test_unknown_action_names_the_real_ones(self):
        num = self.new_card()["num"]
        status, body = self.post("/api/cards/%d/action" % num, {"action": "yolo"})
        self.assertEqual(status, 400, body)
        self.assertIn("retry", body["message"])


class TestAssignStartsTheWork(Base):
    """Assignment is the start of work, not a label on a queued card."""

    LONG = ("once it's assigned, it should move to in progress, right?  and then "
            "the card title should get updated to a nice condensed version")

    def assign(self, num, **extra):
        body = {"agent_name": "sprint-card-%d" % num,
                "worktree": "/tmp/wt-%d" % num, "branch": "sprint/card-%d" % num}
        body.update(extra)
        return self.post("/api/cards/%d/assign" % num, body)

    def states_of(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e["payload"]["to"] for e in detail["timeline"] if e["kind"] == "state"]

    def notes_of(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e["payload"]["text"] for e in detail["timeline"] if e["kind"] == "note"]

    def test_assign_flips_a_queued_card_to_triaging(self):
        num = self.new_card()["num"]
        self.assertEqual(self.state_of(num), "queued")
        status, body = self.assign(num)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["state"], "triaging")
        self.assertEqual(self.state_of(num), "triaging")

    def test_assign_emits_a_plain_english_state_event(self):
        num = self.new_card()["num"]
        self.assign(num)
        _, detail = self.get("/api/cards/%d" % num)
        st = [e for e in detail["timeline"] if e["kind"] == "state"][-1]
        self.assertEqual(st["payload"]["from"], "queued")
        self.assertEqual(st["payload"]["to"], "triaging")
        self.assertIn("assigned to sprint-card-%d" % num, st["payload"]["text"])
        self.assertIn("picking it up", st["payload"]["text"])
        # the assignment note is still there too
        self.assertIn("assigned to sprint-card-%d" % num, self.notes_of(num))
        # and the card lands in the In progress column, not Queued
        self.assertEqual(detail["card"]["column"], "in_progress")

    def test_assign_does_not_regress_a_card_already_moving(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        before = self.states_of(num)
        status, body = self.assign(num)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["state"], "in_progress")
        self.assertEqual(self.states_of(num), before,
                         "assign must not append a state event to a moving card")
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["agent_name"], "sprint-card-%d" % num)

    def test_assign_on_a_held_card_records_but_leaves_it_held(self):
        num = self.new_card(hold=True)["num"]
        self.assertEqual(self.state_of(num), "held")
        status, body = self.assign(num)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "held")
        self.assertEqual(body["card"]["worktree"], "/tmp/wt-%d" % num)

    def test_title_at_assign_renames_the_face_not_the_submission(self):
        num = self.new_card(self.LONG)["num"]
        status, body = self.assign(num, title="Assign should start the work")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["title"], "Assign should start the work")
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["title"], "Assign should start the work")
        self.assertEqual(detail["card"]["body"], self.LONG)
        self.assertIn("titled: Assign should start the work", self.notes_of(num))
        sub = [e for e in detail["timeline"] if e["kind"] == "submitted"][0]
        self.assertEqual(sub["payload"]["text"], self.LONG,
                         "the user's own words are append-only")

    def test_title_at_state_renames_the_face_not_the_submission(self):
        num = self.new_card(self.LONG)["num"]
        raw = self.get("/api/cards/%d" % num)[1]["card"]["title"]
        status, body = self.post("/api/cards/%d/state" % num,
                                 {"state": "triaging",
                                  "title": "Condense card titles at triage"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["state"], "triaging")
        self.assertEqual(body["card"]["title"], "Condense card titles at triage")
        self.assertNotEqual(body["card"]["title"], raw)
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["body"], self.LONG)
        self.assertIn("titled: Condense card titles at triage", self.notes_of(num))

    def test_title_is_optional_everywhere(self):
        num = self.new_card(self.LONG)["num"]
        raw = self.get("/api/cards/%d" % num)[1]["card"]["title"]
        self.assign(num)
        self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["title"], raw)
        self.assertEqual([n for n in self.notes_of(num) if n.startswith("titled:")], [])

    def test_title_is_one_line_collapsed_and_capped(self):
        num = self.new_card()["num"]
        self.post("/api/cards/%d/state" % num,
                  {"state": "triaging", "title": "  two   lines\nbecome one  "})
        self.assertEqual(self.get("/api/cards/%d" % num)[1]["card"]["title"],
                         "two lines become one")
        self.post("/api/cards/%d/state" % num,
                  {"state": "in_progress", "title": "x" * 200})
        title = self.get("/api/cards/%d" % num)[1]["card"]["title"]
        self.assertEqual(len(title), 90)
        self.assertTrue(title.endswith("…"))

    def test_an_empty_title_is_refused_and_the_state_does_not_move(self):
        num = self.new_card()["num"]
        for bad in ("", "   ", 17):
            status, body = self.post("/api/cards/%d/state" % num,
                                     {"state": "triaging", "title": bad})
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"], "bad_title")
            self.assertEqual(body["field"], "title")
            self.assertEqual(self.state_of(num), "queued",
                             "a bad title must not half-apply the transition")
        status, body = self.assign(num, title="")
        self.assertEqual(status, 400, body)
        self.assertEqual(self.state_of(num), "queued")
        self.assertIsNone(self.get("/api/cards/%d" % num)[1]["card"]["agent_name"])

    def test_retitling_to_the_same_words_is_a_no_op(self):
        num = self.new_card(self.LONG)["num"]
        self.assign(num, title="Assign should start the work")
        self.post("/api/cards/%d/state" % num,
                  {"state": "in_progress", "title": "Assign should start the work"})
        self.assertEqual(
            [n for n in self.notes_of(num) if n.startswith("titled:")],
            ["titled: Assign should start the work"])


class TestReopenIsTheUsersUndo(Base):
    """User verbatim: "you should never move a card to closed. I lost it."
    Closing is the user's call -- and getting it back must not need a DBA."""

    def close_completed(self, num):
        self.to_in_progress(num)
        status, _ = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200)
        self.post("/api/cards/%d/verdict" % num, {"verdict": "approve"})
        status, _ = self.post("/api/cards/%d/integrated" % num, {"ok": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "completed")

    def test_reopen_brings_back_a_canceled_card(self):
        num = self.new_card("I still want this")["num"]
        self.post("/api/cards/%d/action" % num, {"action": "cancel"})
        self.assertEqual(self.state_of(num), "canceled")
        status, body = self.post("/api/cards/%d/action" % num, {"action": "reopen"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["state"], "queued")
        _, detail = self.get("/api/cards/%d" % num)
        st = [e for e in detail["timeline"] if e["kind"] == "state"][-1]
        self.assertEqual(st["payload"]["from"], "canceled")
        self.assertEqual(st["payload"]["to"], "queued")
        self.assertIn("reopened", st["payload"]["text"])
        self.assertEqual(st["payload"]["reopened_from"], "canceled")

    def test_reopen_brings_back_a_completed_card(self):
        num = self.new_card("closed too early")["num"]
        self.close_completed(num)
        status, body = self.post("/api/cards/%d/action" % num, {"action": "reopen"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "queued")
        _, detail = self.get("/api/cards/%d" % num)
        st = [e for e in detail["timeline"] if e["kind"] == "state"][-1]
        self.assertEqual(st["payload"]["reopened_from"], "completed")
        self.assertEqual(detail["card"]["column"], "queued")

    def test_reopen_brings_back_a_duplicate(self):
        keep = self.new_card("the original")["num"]
        num = self.new_card("marked dup by mistake")["num"]
        self.post("/api/cards/%d/action" % num, {"action": "duplicate_of", "dup_of": keep})
        self.assertEqual(self.state_of(num), "duplicate")
        status, _ = self.post("/api/cards/%d/action" % num, {"action": "reopen"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "queued")

    def test_reopen_on_a_live_card_is_409(self):
        num = self.new_card()["num"]
        for _ in range(1):
            status, body = self.post("/api/cards/%d/action" % num, {"action": "reopen"})
            self.assertEqual(status, 409, body)
            self.assertEqual(body["error"], "not_reopenable")
            self.assertEqual(self.state_of(num), "queued")
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/action" % num, {"action": "reopen"})
        self.assertEqual(status, 409, body)
        self.assertEqual(self.state_of(num), "in_progress")

    def test_unknown_action_message_names_reopen(self):
        num = self.new_card()["num"]
        status, body = self.post("/api/cards/%d/action" % num, {"action": "yolo"})
        self.assertEqual(status, 400, body)
        self.assertIn("reopen", body["message"])

    def test_a_completed_card_still_cannot_walk_back_into_the_work(self):
        num = self.new_card()["num"]
        self.close_completed(num)
        for state in ("in_progress", "triaging", "ready"):
            status, _ = self.post("/api/cards/%d/state" % num, {"state": state})
            self.assertIn(status, (409, 422))
            self.assertEqual(self.state_of(num), "completed")


class TestPinIsExplicit(Base):
    def test_pin_takes_an_explicit_value(self):
        num = self.new_card()["num"]
        self.post("/api/cards/%d/action" % num, {"action": "pin", "pinned": True})
        self.assertTrue(self.get("/api/cards/%d" % num)[1]["card"]["pinned"])
        # Repeating the same intent is idempotent, not a toggle.
        self.post("/api/cards/%d/action" % num, {"action": "pin", "pinned": True})
        self.assertTrue(self.get("/api/cards/%d" % num)[1]["card"]["pinned"])
        self.post("/api/cards/%d/action" % num, {"action": "pin", "pinned": False})
        self.assertFalse(self.get("/api/cards/%d" % num)[1]["card"]["pinned"])

    def test_unpin_action_name_works(self):
        num = self.new_card()["num"]
        self.post("/api/cards/%d/action" % num, {"action": "pin", "pinned": True})
        self.post("/api/cards/%d/action" % num, {"action": "unpin"})
        self.assertFalse(self.get("/api/cards/%d" % num)[1]["card"]["pinned"])


class TestChatImages(Base):
    """Pasting a screenshot into a card's chat is a first-class message.

    Same attachment path as dropping work on the board: sniffed, deduped,
    capped, and referenced from the event so both the browser (url) and the
    agent (absolute path) get what they need out of one timeline read.
    """

    JPEG_B64 = base64.b64encode(
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 40 + b"\xff\xd9").decode()

    def chat(self, num, body):
        return self.post("/api/cards/%d/chat" % num, body)

    def test_chat_with_images_stores_them_and_refs_them_in_the_event(self):
        num = self.new_card("a card")["num"]
        status, res = self.chat(num, {"text": "looks like this", "images": [PNG_B64]})
        self.assertEqual(status, 201, res)

        atts = res["event"]["payload"]["attachments"]
        self.assertEqual(len(atts), 1)
        att = atts[0]
        self.assertEqual(att["mime"], "image/png")
        self.assertEqual(att["url"], "/api/attachments/%s.png" % att["sha256"])
        self.assertTrue(os.path.isfile(att["path"]),
                        "chat attachment must be on disk: %s" % att["path"])
        self.assertTrue(os.path.isabs(att["path"]),
                        "the agent gets an absolute path to Read")

        # servable as-is
        status, blob = self.get(att["url"])
        self.assertEqual(status, 200)
        self.assertEqual(blob, base64.b64decode(PNG_B64))

        # and visible in the timeline read, both on the event and on the card
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(status, 200)
        chat_ev = [e for e in detail["timeline"] if e["kind"] == "chat"][0]
        self.assertEqual(chat_ev["payload"]["attachments"][0]["sha256"], att["sha256"])
        self.assertEqual(chat_ev["payload"]["text"], "looks like this")
        self.assertIn(att["sha256"], [a["sha256"] for a in detail["attachments"]])

    def test_chat_images_are_content_addressed_and_deduped(self):
        num = self.new_card("a card", images=[PNG_B64])["num"]
        _, res = self.chat(num, {"text": "again", "images": [PNG_B64]})
        _, res2 = self.chat(num, {"text": "and again",
                                  "images": ["data:image/png;base64," + PNG_B64]})
        shas = {res["event"]["payload"]["attachments"][0]["sha256"],
                res2["event"]["payload"]["attachments"][0]["sha256"]}
        self.assertEqual(len(shas), 1)
        self.assertEqual(len(os.listdir(self.app.attach_dir)), 1,
                         "the same bytes are stored exactly once")
        # the card's attachment roll-up does not repeat one image per event
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(len(detail["attachments"]), 1)

    def test_image_only_chat_is_a_complete_message(self):
        num = self.new_card("a card")["num"]
        status, res = self.chat(num, {"images": [PNG_B64]})
        self.assertEqual(status, 201, res)
        payload = res["event"]["payload"]
        self.assertEqual(len(payload["attachments"]), 1)
        self.assertTrue(payload["text"].strip(),
                        "an image-only line still needs a skim line")

    def test_empty_chat_is_rejected(self):
        num = self.new_card("a card")["num"]
        status, body = self.chat(num, {"text": "  ", "images": []})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "empty_submission")

    def test_non_image_chat_attachment_is_rejected(self):
        num = self.new_card("a card")["num"]
        status, body = self.chat(
            num, {"text": "here", "images": [base64.b64encode(b"not an image").decode()]})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "bad_image")
        self.assertEqual(os.listdir(self.app.attach_dir), [],
                         "a rejected attachment leaves nothing behind")
        # and the rejected line never reached the timeline
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual([e for e in detail["timeline"] if e["kind"] == "chat"], [])

    def test_jpeg_chat_attachment_is_accepted(self):
        num = self.new_card("a card")["num"]
        status, res = self.chat(num, {"images": ["data:image/jpeg;base64," + self.JPEG_B64]})
        self.assertEqual(status, 201, res)
        att = res["event"]["payload"]["attachments"][0]
        self.assertEqual(att["mime"], "image/jpeg")
        self.assertTrue(att["url"].endswith(".jpg"))

    def test_oversize_chat_attachment_is_a_413_and_stores_nothing(self):
        num = self.new_card("a card")["num"]
        self.app.max_upload = 512          # tiny cap, so the test stays cheap
        big = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + os.urandom(2048)).decode()
        status, body = self.chat(num, {"text": "big one", "images": [big]})
        self.assertEqual(status, 413, body)
        self.assertEqual(body["error"], "too_large")
        self.assertEqual(os.listdir(self.app.attach_dir), [])

    def test_chat_to_a_missing_card_stores_nothing(self):
        status, body = self.chat(9999, {"text": "hi", "images": [PNG_B64]})
        self.assertEqual(status, 404, body)
        self.assertEqual(os.listdir(self.app.attach_dir), [],
                         "we check the card exists before writing bytes")

    def test_text_only_chat_still_works_and_carries_no_attachments(self):
        num = self.new_card("a card")["num"]
        status, res = self.chat(num, {"text": "just words"})
        self.assertEqual(status, 201, res)
        self.assertNotIn("attachments", res["event"]["payload"])

    def test_sidebar_takes_images_too(self):
        status, res = self.post("/api/sidebar",
                                {"text": "see this", "images": [PNG_B64], "actor": "user"})
        self.assertEqual(status, 201, res)
        att = res["event"]["payload"]["attachments"][0]
        self.assertEqual(att["url"], "/api/attachments/%s.png" % att["sha256"])
        self.assertEqual(self.get(att["url"])[0], 200)
        # and it comes back on the board's sidebar thread
        line = self.get("/api/board")[1]["sidebar"][-1]
        self.assertEqual(line["payload"]["attachments"][0]["sha256"], att["sha256"])

    def test_image_only_sidebar_line_is_valid_and_empty_is_not(self):
        status, res = self.post("/api/sidebar", {"images": [PNG_B64], "actor": "user"})
        self.assertEqual(status, 201, res)
        self.assertTrue(res["event"]["payload"]["text"].strip())
        status, body = self.post("/api/sidebar", {"text": "   ", "actor": "user"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "missing_field")

    def test_non_image_sidebar_attachment_is_rejected(self):
        status, body = self.post(
            "/api/sidebar",
            {"text": "x", "images": [base64.b64encode(b"nope").decode()], "actor": "user"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "bad_image")

    def test_images_must_be_a_list(self):
        num = self.new_card("a card")["num"]
        status, body = self.chat(num, {"text": "x", "images": PNG_B64})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "bad_image")


class TestEventPayloadContract(Base):
    """One field set, every event: `text` always; state carries from/to."""

    def test_every_event_kind_carries_text(self):
        num = self.new_card("submit me", images=[PNG_B64])["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/events" % num,
                  {"kind": "progress", "payload": {"text": "step one"}})
        self.post("/api/cards/%d/events" % num,
                  {"kind": "note", "payload": {"note": "aliased field"}})
        self.post("/api/cards/%d/question" % num, {"text": "which one?"})
        self.post("/api/cards/%d/answer" % num, {"text": "the first"})
        self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.post("/api/cards/%d/verdict" % num,
                  {"verdict": "bounce", "notes": "not quite"})
        self.post("/api/sidebar", {"text": "hello", "actor": "user"})

        rows = self.get("/api/events?after=0&limit=500")[1]["events"]
        kinds = set()
        for ev in rows:
            kinds.add(ev["kind"])
            text = ev["payload"].get("text")
            self.assertTrue(isinstance(text, str) and text.strip(),
                            "%s event has no text: %r" % (ev["kind"], ev["payload"]))
        self.assertTrue({"submitted", "state", "progress", "note", "question",
                         "answer", "evidence", "verdict", "chat"} <= kinds, kinds)

    def test_state_events_carry_from_and_to(self):
        num = self.new_card()["num"]
        self.to_in_progress(num)
        rows = [e for e in self.get("/api/cards/%d" % num)[1]["timeline"]
                if e["kind"] == "state"]
        self.assertEqual([e["payload"]["to"] for e in rows],
                         ["queued", "triaging", "in_progress"])
        self.assertEqual(rows[-1]["payload"]["from"], "triaging")
        self.assertNotIn("new_state", rows[-1]["payload"])


class TestDoctorAndLifecycle(Base):
    def test_doctor_appends_git_info_exclude(self):
        os.makedirs(os.path.join(self.project_root, ".git", "info"))
        exclude = os.path.join(self.project_root, ".git", "info", "exclude")
        with open(exclude, "w", encoding="utf-8") as fh:
            fh.write("# git ls-files --others --exclude-from=.git/info/exclude\n")
        rc = sprintd.main(["--project-root", self.project_root, "doctor"])
        self.assertIn(rc, (0, 1))
        with open(exclude, "r", encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(".sprint/", body)
        # idempotent
        sprintd.main(["--project-root", self.project_root, "doctor"])
        with open(exclude, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().count(".sprint/"), 1)

    def test_gitignore_untouched(self):
        os.makedirs(os.path.join(self.project_root, ".git", "info"))
        gitignore = os.path.join(self.project_root, ".gitignore")
        with open(gitignore, "w", encoding="utf-8") as fh:
            fh.write("node_modules\n")
        sprintd.main(["--project-root", self.project_root, "doctor"])
        with open(gitignore, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "node_modules\n")

    def test_status_reports_running_server(self):
        info = {"pid": os.getpid(), "port": self.port, "host": self.host,
                "token": "test-token", "project_root": self.project_root,
                "started_at": time.time()}
        sprintd.write_server_json(
            os.path.join(self.app.data_dir, sprintd.SERVER_JSON), info)
        state, health = sprintd.probe_server(info)
        self.assertEqual(state, "healthy", health)
        rc = sprintd.main(["--project-root", self.project_root, "status", "--json"])
        self.assertEqual(rc, 0)

        bad = dict(info, token="not-the-token")
        self.assertEqual(sprintd.probe_server(bad)[0], "wrong_token")
        dead = dict(info, port=1)
        self.assertEqual(sprintd.probe_server(dead)[0], "dead")

    def test_status_without_server(self):
        empty = os.path.join(self.tmp, "nothing")
        os.makedirs(empty)
        rc = sprintd.main(["--project-root", empty, "status"])
        self.assertEqual(rc, 1)

    def test_stale_pidfile_is_cleaned(self):
        path = os.path.join(self.app.data_dir, sprintd.SERVER_JSON)
        sprintd.write_server_json(path, {
            "pid": 2 ** 22, "port": 1, "token": "x",
            "project_root": self.project_root, "started_at": 0})
        rc = sprintd.main(["--project-root", self.project_root, "stop"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(path), "stale server.json must be removed")

    def test_web_dir_resolves_next_to_the_script(self):
        self.assertEqual(sprintd.web_dir(),
                         os.path.join(os.path.dirname(os.path.dirname(SPRINTD_PATH)),
                                      "web"))

    def test_default_port_constant(self):
        self.assertEqual(sprintd.DEFAULT_PORT, 8377)
        self.assertEqual(sprintd.DEFAULT_MAX_UPLOAD, 25 * 1024 * 1024)
        self.assertEqual(sprintd.DEFAULT_SILENCE_SECONDS, 300.0)


class TestStartStopSubprocess(unittest.TestCase):
    """Exercises the real CLI: daemonized start, idempotency, stale pid, stop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)
        self.server_json = os.path.join(self.root, ".sprint", sprintd.SERVER_JSON)
        self.port = self._free_port()
        self.assertNotEqual(self.port, sprintd.DEFAULT_PORT)
        self.addCleanup(self._kill_leftovers)

    def _free_port(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def _kill_leftovers(self):
        info = sprintd.read_server_json(self.server_json)
        if info and isinstance(info.get("pid"), int):
            try:
                os.kill(info["pid"], 15)
            except OSError:
                pass

    def _run(self, *argv, timeout=60):
        import subprocess
        return subprocess.run(
            [sys.executable, SPRINTD_PATH, "--project-root", self.root] + list(argv),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

    def _healthz(self, token=None):
        try:
            return sprintd.http_get("127.0.0.1", self.port, "/healthz", token,
                                    timeout=3.0)[0]
        except (OSError, http.client.HTTPException):
            return None

    def test_start_is_idempotent_and_stop_cleans_up(self):
        r = self._run("start", "--port", str(self.port), "--token", "cli-token",
                      "--no-tailscale")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("http://127.0.0.1:%d/?t=cli-token" % self.port, out)
        self.assertEqual(self._healthz(), 200)

        info = sprintd.read_server_json(self.server_json)
        self.assertEqual(info["port"], self.port)
        self.assertEqual(info["token"], "cli-token")
        self.assertEqual(os.path.realpath(info["project_root"]),
                         os.path.realpath(self.root))
        self.assertEqual(info["hosts"], ["127.0.0.1"])
        self.assertEqual(oct(os.stat(self.server_json).st_mode)[-3:], "600")

        # second start: no new process, still exit 0
        r2 = self._run("start", "--port", str(self.port), "--no-tailscale")
        self.assertEqual(r2.returncode, 0, r2.stderr.decode())
        self.assertIn("already running", r2.stdout.decode())
        self.assertEqual(sprintd.read_server_json(self.server_json)["pid"], info["pid"])

        r3 = self._run("status", "--json")
        self.assertEqual(r3.returncode, 0)
        self.assertTrue(json.loads(r3.stdout.decode())["running"])

        r4 = self._run("stop")
        self.assertEqual(r4.returncode, 0, r4.stderr.decode())
        self.assertFalse(os.path.exists(self.server_json))
        self.assertIsNone(self._healthz())

    def test_recycled_pid_after_power_loss_is_recovered(self):
        os.makedirs(os.path.dirname(self.server_json), exist_ok=True)
        # pid 1 is alive but is definitely not sprintd (PID recycling case)
        sprintd.write_server_json(self.server_json, {
            "pid": 1, "port": self.port, "host": "127.0.0.1",
            "token": "survives-reboot", "project_root": self.root,
            "started_at": 0})
        r = self._run("start", "--no-tailscale")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        # port and token are reused so the URL survives the outage
        self.assertIn("http://127.0.0.1:%d/?t=survives-reboot" % self.port,
                      r.stdout.decode())
        self.assertEqual(self._healthz(), 200)
        info = sprintd.read_server_json(self.server_json)
        self.assertNotEqual(info["pid"], 1)
        status, _ = sprintd.http_get("127.0.0.1", self.port, "/api/board",
                                     "survives-reboot")
        self.assertEqual(status, 200)
        self.assertEqual(self._run("stop").returncode, 0)

    def test_token_survives_stop_and_start(self):
        """`stop` deletes server.json -- which used to be the only copy of the
        token, so every restart logged the browser out and killed the printed
        URL. The token now lives in its own file and outlives the server."""
        token_file = os.path.join(self.root, ".sprint", sprintd.TOKEN_FILENAME)

        r = self._run("start", "--port", str(self.port), "--no-tailscale")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        first = sprintd.read_server_json(self.server_json)["token"]
        self.assertTrue(os.path.exists(token_file))
        self.assertEqual(oct(os.stat(token_file).st_mode)[-3:], "600")
        self.assertEqual(sprintd.read_token_file(token_file), first)

        self.assertEqual(self._run("stop").returncode, 0)
        self.assertFalse(os.path.exists(self.server_json))
        self.assertTrue(os.path.exists(token_file), "stop must not take the token with it")

        r2 = self._run("start", "--port", str(self.port), "--no-tailscale")
        self.assertEqual(r2.returncode, 0, r2.stderr.decode())
        self.assertEqual(sprintd.read_server_json(self.server_json)["token"], first)
        # the URL printed the first time still works, cookie and all
        self.assertIn("http://127.0.0.1:%d/?t=%s" % (self.port, first), r2.stdout.decode())
        status, _ = sprintd.http_get("127.0.0.1", self.port, "/api/board", first)
        self.assertEqual(status, 200)
        self.assertEqual(self._run("stop").returncode, 0)

    def test_new_token_rotates_and_invalidates_the_old_one(self):
        token_file = os.path.join(self.root, ".sprint", sprintd.TOKEN_FILENAME)
        r = self._run("start", "--port", str(self.port), "--no-tailscale")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        first = sprintd.read_server_json(self.server_json)["token"]
        self.assertEqual(self._run("stop").returncode, 0)

        r2 = self._run("start", "--port", str(self.port), "--no-tailscale", "--new-token")
        self.assertEqual(r2.returncode, 0, r2.stderr.decode())
        second = sprintd.read_server_json(self.server_json)["token"]
        self.assertNotEqual(second, first)
        self.assertEqual(sprintd.read_token_file(token_file), second)
        self.assertEqual(sprintd.http_get("127.0.0.1", self.port, "/api/board", second)[0], 200)
        self.assertEqual(sprintd.http_get("127.0.0.1", self.port, "/api/board", first)[0], 401)
        self.assertEqual(self._run("stop").returncode, 0)

    def test_explicit_token_beats_the_stored_one(self):
        r = self._run("start", "--port", str(self.port), "--no-tailscale",
                      "--token", "chosen-by-hand")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(sprintd.read_server_json(self.server_json)["token"],
                         "chosen-by-hand")
        self.assertEqual(self._run("stop").returncode, 0)
        # and it is what gets reused on the next plain start
        r2 = self._run("start", "--port", str(self.port), "--no-tailscale")
        self.assertEqual(r2.returncode, 0, r2.stderr.decode())
        self.assertEqual(sprintd.read_server_json(self.server_json)["token"],
                         "chosen-by-hand")
        self.assertEqual(self._run("stop").returncode, 0)

    def test_wait_polling_marks_the_session_alive(self):
        """`sprintd wait` is the session's ingress primitive; its polling is what
        the board now reads as 'the session is attached'."""
        r = self._run("start", "--port", str(self.port), "--no-tailscale",
                      "--token", "cli-token")
        self.assertEqual(r.returncode, 0, r.stderr.decode())

        def session(tok):
            status, raw = sprintd.http_get("127.0.0.1", self.port, "/api/board", tok)
            self.assertEqual(status, 200)
            return json.loads(raw.decode())["session"]

        self.assertIsNone(session("cli-token")["waiter_seen_at"])
        # one wait that times out with nothing pending: still a heartbeat
        w = self._run("wait", "--after", "0", "--timeout", "0.4", "--poll", "0.1")
        self.assertIn(w.returncode, (0, 2), w.stderr.decode())
        sess = session("cli-token")
        self.assertIsNotNone(sess["waiter_seen_at"])
        self.assertTrue(sess["waiter_polling"])
        self.assertEqual(self._run("stop").returncode, 0)


class TestTailLine(unittest.TestCase):
    """The line shape itself: fixed keys, always present, clipped text."""

    def test_line_is_the_same_shape_for_every_event(self):
        line = sprintd.tail_line({
            "seq": 12, "card_num": 5, "ts": 1.0, "actor": "user", "kind": "chat",
            "payload": {"text": "hello", "reply_to": "card:5", "detail": "long…"}})
        self.assertEqual(list(line), ["seq", "card", "actor", "kind",
                                      "reply_to", "text"])
        self.assertEqual(line, {"seq": 12, "card": 5, "actor": "user",
                                "kind": "chat", "reply_to": "card:5",
                                "text": "hello"})

    def test_keys_are_present_even_when_empty(self):
        """A monitor reads a field without first checking it exists."""
        line = sprintd.tail_line({"seq": 3, "card_num": None, "actor": "server",
                                  "kind": "note", "payload": {"text": "sprint opened"}})
        self.assertIsNone(line["card"])
        self.assertIsNone(line["reply_to"], "only humans get a reply_to")
        line = sprintd.tail_line({"seq": 4, "actor": "worker", "kind": "progress",
                                  "payload": None})
        self.assertEqual(line["text"], "")
        self.assertIsNone(line["reply_to"])

    def test_text_is_one_clipped_line(self):
        line = sprintd.tail_line({"seq": 1, "card_num": 1, "actor": "user",
                                  "kind": "chat",
                                  "payload": {"text": "x" * 400}})
        self.assertEqual(len(line["text"]), sprintd.TAIL_TEXT_MAX)
        self.assertTrue(line["text"].endswith("…"))
        line = sprintd.tail_line({"seq": 2, "card_num": 1, "actor": "user",
                                  "kind": "chat",
                                  "payload": {"text": "first\nsecond\nthird"}})
        self.assertEqual(line["text"], "first", "one event is one line, always")

    def test_a_forged_reply_to_is_still_only_a_string(self):
        line = sprintd.tail_line({"seq": 9, "card_num": 1, "actor": "worker",
                                  "kind": "note",
                                  "payload": {"text": "hi", "reply_to": {"a": 1}}})
        self.assertIsNone(line["reply_to"])


class TestTail(unittest.TestCase):
    """`sprintd tail` -- the quiet ingress.

    The session used to relaunch a 60s long-poll forever, which is a wake per
    minute whether or not anything happened. tail holds one stream open and
    prints exactly one line per real event: one wake per event, zero idle
    wakes. It runs under a streaming monitor, so it must never exit on its
    own and must never print anything a monitor would wake on pointlessly.
    """

    HEARTBEAT = 0.3          # server-side SSE heartbeat, so beats are frequent

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-tail-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)
        self.server_json = os.path.join(self.root, ".sprint", sprintd.SERVER_JSON)
        self.port = self._free_port()
        self.assertNotEqual(self.port, sprintd.DEFAULT_PORT,
                            "never touch the real board's port")
        self.token = "tail-token"
        self.tails = []
        self.addCleanup(self._kill_leftovers)
        self.addCleanup(self._kill_tails)
        self.start_server()
        self.assertEqual(self.api("POST", "/api/sprint", {"action": "open"})[0], 200)

    # -- server ---------------------------------------------------------

    def _free_port(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def _kill_leftovers(self):
        info = sprintd.read_server_json(self.server_json)
        if info and isinstance(info.get("pid"), int):
            try:
                os.kill(info["pid"], 15)
            except OSError:
                pass

    def _kill_tails(self):
        for proc, _lines, _noise in self.tails:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass

    def _sprintd(self, *argv, timeout=60):
        import subprocess
        env = dict(os.environ, SPRINT_SSE_HEARTBEAT=str(self.HEARTBEAT),
                   **getattr(self, "extra_env", {}))
        return subprocess.run(
            [sys.executable, SPRINTD_PATH, "--project-root", self.root] + list(argv),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)

    def _healthz(self):
        try:
            return sprintd.http_get("127.0.0.1", self.port, "/healthz",
                                    timeout=2.0)[0]
        except (OSError, http.client.HTTPException):
            return None

    def _await(self, want, timeout=15.0, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = want()
            if got:
                return got
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % (what() if callable(what) else what))

    def start_server(self):
        r = self._sprintd("start", "--port", str(self.port), "--token", self.token,
                          "--no-tailscale")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self._await(lambda: self._healthz() == 200, what="the server to come up")

    def stop_server(self):
        r = self._sprintd("stop")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self._await(lambda: self._healthz() is None, what="the server to go away")

    def generation(self):
        status, raw = sprintd.http_get("127.0.0.1", self.port, "/healthz")
        self.assertEqual(status, 200)
        return json.loads(raw.decode())["generation"]

    def api(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10.0)
        try:
            headers = {"Authorization": "Bearer " + self.token,
                       "Accept": "application/json"}
            payload = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                payload = json.dumps(body).encode("utf-8")
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8") or "{}")
        finally:
            conn.close()

    # -- events the tests post -------------------------------------------

    def user_says(self, text):
        status, _ = self.api("POST", "/api/sidebar", {"text": text, "actor": "user"})
        self.assertIn(status, (200, 201))

    def session_says(self, text):
        status, _ = self.api("POST", "/api/sidebar", {"text": text, "actor": "session"})
        self.assertIn(status, (200, 201))

    def new_card(self, text):
        status, card = self.api("POST", "/api/cards", {"text": text})
        self.assertEqual(status, 201, card)
        return card

    def head(self):
        return self.api("GET", "/healthz")[1]["seq"]

    # -- the tail process -------------------------------------------------

    def tail(self, *argv):
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, SPRINTD_PATH, "--project-root", self.root, "tail",
             "--port", str(self.port), "--token", self.token] + list(argv),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines, noise = [], []

        def pump(stream, sink):
            for raw in stream:
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    sink.append(text)

        for stream, sink in ((proc.stdout, lines), (proc.stderr, noise)):
            threading.Thread(target=pump, args=(stream, sink), daemon=True).start()
        self.tails.append((proc, lines, noise))
        return proc, lines, noise

    def wait_lines(self, lines, count, timeout=20.0):
        self._await(lambda: len(lines) >= count, timeout=timeout,
                    what=lambda: "%d tail line(s); got %r" % (count, lines))
        return [json.loads(t) for t in list(lines)]

    def parsed(self, lines):
        return [json.loads(t) for t in list(lines)]

    # -- tests ------------------------------------------------------------

    def test_one_line_per_event_carrying_the_routing_key(self):
        """The whole product of this command: one compact line per event, and
        `reply_to` on it, so a session knows where its answer belongs without
        a second round trip."""
        _proc, lines, noise = self.tail("--after", str(self.head()))
        card = self.new_card("a card the user dropped")
        self.user_says("and a sidebar message")
        got = self.wait_lines(lines, 3)          # submitted, state, chat

        by_kind = {ev.get("kind"): ev for ev in got if "kind" in ev}
        self.assertEqual(by_kind["submitted"]["card"], card["num"])
        self.assertEqual(by_kind["submitted"]["actor"], "user")
        self.assertEqual(by_kind["submitted"]["reply_to"], "card:%d" % card["num"])
        self.assertEqual(by_kind["submitted"]["text"], "a card the user dropped")
        self.assertEqual(by_kind["chat"]["reply_to"], "sidebar")
        self.assertIsNone(by_kind["state"]["reply_to"],
                          "a server event has no human waiting behind it")

        seqs = [ev["seq"] for ev in got]
        self.assertEqual(seqs, sorted(seqs), "events arrive in log order")
        self.assertEqual(len(seqs), len(set(seqs)), "one line per event, exactly")

    def test_default_tail_prints_the_sweep(self):
        """A `stuck` event is an ordinary event on the ordinary stream: the
        session's default tail wakes on it with no special casing. `--user-only`
        does not show it, on purpose — that filter is "a human is waiting", and
        the whole point of the sweep is that no human is."""
        self.stop_server()
        self.extra_env = {"SPRINT_SWEEP_TICK": "0.2",
                          "SPRINT_SWEEP_QUEUED_SECONDS": "1"}
        self.start_server()
        self.assertEqual(self.api("POST", "/api/sprint", {"action": "open"})[0], 200)

        _proc, lines, _noise = self.tail("--after", str(self.head()))
        _uproc, ulines, _unoise = self.tail("--after", str(self.head()), "--user-only")
        card = self.new_card("nobody will dispatch this")

        got = self._await(
            lambda: [ev for ev in self.parsed(lines) if ev.get("kind") == "stuck"],
            timeout=25.0, what="a stuck line on the default tail")
        self.assertEqual(got[0]["card"], card["num"])
        self.assertEqual(got[0]["actor"], "server")
        self.assertIn("dispatch it", got[0]["text"])
        self.assertIsNone(got[0]["reply_to"])
        self.assertEqual([ev for ev in self.parsed(ulines) if ev.get("kind") == "stuck"],
                         [], "--user-only is a human filter, and this is the server")

    def test_user_only_prints_only_what_a_human_wrote(self):
        """The filter the session actually runs: the events it must answer."""
        _proc, lines, noise = self.tail("--after", str(self.head()), "--user-only")
        self.session_says("a reply the session posted itself")
        self.new_card("a card, which is a user event")   # + a server state event
        self.user_says("the human again")
        got = self.wait_lines(lines, 2)
        self.assertTrue(all(ev["actor"] == "user" for ev in got), got)
        self.assertEqual([ev["text"] for ev in got],
                         ["a card, which is a user event", "the human again"])

    def test_after_catches_up_from_the_cursor_before_streaming(self):
        """Restarting a monitor must not replay the whole sprint, and must not
        lose what landed while it was down."""
        self.user_says("before the cursor")
        cursor = self.head()
        self.user_says("landed while the monitor was down")
        _proc, lines, noise = self.tail("--after", str(cursor), "--user-only")
        self.user_says("landed while it was watching")
        got = self.wait_lines(lines, 2)
        self.assertEqual([ev["text"] for ev in got],
                         ["landed while the monitor was down",
                          "landed while it was watching"])
        self.assertTrue(all(ev["seq"] > cursor for ev in got), got)

    def test_heartbeats_and_cursor_moves_print_nothing(self):
        """The reason this command exists. A heartbeat that reached stdout
        would wake the monitor every 15s forever -- the exact idle churn the
        long-poll waiter was costing."""
        _proc, lines, noise = self.tail("--after", str(self.head()))
        self.user_says("the only real event")
        self.wait_lines(lines, 1)
        # many heartbeats, plus the single most frequent frame on the wire
        for seq in range(1, 4):
            self.assertEqual(
                self.api("POST", "/api/cursors/orchestrator", {"seq": seq})[0], 200)
            time.sleep(0.5)
        self.assertEqual(len(lines), 1, self.parsed(lines))

    def test_a_dropped_connection_resumes_silently_without_duplicates(self):
        """The stream drops (idle timeout here; a flaky socket in life). The
        reconnect is bookkeeping, not news: nothing extra reaches stdout, and
        nothing already printed is printed twice."""
        # a read timeout well under the heartbeat: the stream drops constantly
        _proc, lines, noise = self.tail("--after", str(self.head()), "--read-timeout", "0.15")
        self.user_says("one")
        self.wait_lines(lines, 1)
        time.sleep(2.0)                          # several forced reconnects
        self.user_says("two")
        got = self.wait_lines(lines, 2)
        self.assertEqual([ev["text"] for ev in got], ["one", "two"])
        self.assertFalse([ev for ev in got if "restart" in ev],
                         "same server, same generation -- no restart line")
        self.assertEqual(noise, [], "reconnect chatter on stderr is still chatter")

    def test_a_server_restart_says_so_once_and_resumes_from_seq(self):
        """A restarted backend is the one piece of transport news the session
        does need: same port, new process. Everything after it must arrive
        exactly once, from where the tail left off."""
        _proc, lines, noise = self.tail("--after", str(self.head()),
                                 "--unreachable-after", "600")
        self.user_says("before the restart")
        self.wait_lines(lines, 1)
        before = self.generation()

        self.stop_server()
        self.start_server()
        self.assertNotEqual(self.generation(), before, "restart must be a new process")
        self.user_says("after the restart")

        got = self.wait_lines(lines, 3)
        restarts = [ev for ev in got if ev.get("restart")]
        self.assertEqual(len(restarts), 1, got)
        self.assertEqual(restarts[0]["generation"], self.generation())
        texts = [ev.get("text") for ev in got if "seq" in ev]
        self.assertEqual(texts, ["before the restart", "after the restart"])
        seqs = [ev["seq"] for ev in got if "seq" in ev]
        self.assertEqual(len(seqs), len(set(seqs)), "resume must not replay")

    def test_unreachable_is_said_once_and_the_tail_keeps_going(self):
        """It lives under a monitor: dying is not an option, and neither is
        screaming once a second while the server is down."""
        proc, lines, noise = self.tail("--after", str(self.head()),
                                "--unreachable-after", "1", "--read-timeout", "2")
        self.user_says("still up")
        self.wait_lines(lines, 1)

        self.stop_server()
        self._await(lambda: any(json.loads(t).get("error") for t in list(lines)),
                    timeout=20, what="the unreachable line")
        time.sleep(3.0)                          # keep it down, stay quiet
        errors = [ev for ev in self.parsed(lines) if ev.get("error")]
        self.assertEqual(len(errors), 1, self.parsed(lines))
        self.assertEqual(errors[0], {"error": "unreachable"})
        self.assertEqual(noise, [], "one line on stdout, nothing anywhere else")
        self.assertIsNone(proc.poll(), "tail must never exit on its own")

        self.start_server()
        self.user_says("and we are back")
        got = self.wait_lines(lines, 3)
        self.assertEqual(got[-1]["text"], "and we are back")
        self.assertIsNone(proc.poll())

    def test_tail_is_the_sessions_proof_of_life(self):
        """Sitting on the stream replaces the waiter's polling, so it has to
        buy the same thing: a board that does not tell the user 'session
        offline' while the session is right there listening."""
        _proc, lines, noise = self.tail("--after", str(self.head()))
        self.user_says("wake up")
        self.wait_lines(lines, 1)
        session = self._await(
            lambda: self.api("GET", "/api/board")[1]["session"].get("waiter_seen_at")
            and self.api("GET", "/api/board")[1]["session"],
            what="the board to see the tail as the session")
        self.assertTrue(session["waiter_polling"])
        self.assertNotEqual(session["status"], "offline")


class TestEventDetail(Base):
    """Skim then dig in: every event keeps a one-line `text`, and an OPTIONAL
    `detail` carries the long version (a test log, the reasoning) alongside it."""

    LOG = ("$ python3 tests/test_sprintd.py\n"
           "test_detail_rides_through ... ok\n"
           "----------------------------------------------------------------------\n"
           "Ran 118 tests in 4.21s\n\nOK")

    def event(self, num, payload, kind="progress"):
        return self.post("/api/cards/%d/events" % num, {"kind": kind, "payload": payload})

    def progress_events(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e for e in detail["timeline"] if e["kind"] == "progress"]

    def test_detail_rides_through_events_board_and_card_reads(self):
        num = self.new_card("run the suite")["num"]
        self.to_in_progress(num)
        status, body = self.event(num, {"text": "suite green — 118 pass, 0 fail",
                                        "detail": self.LOG})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["event"]["payload"]["detail"], self.LOG)

        ev = self.progress_events(num)[-1]
        self.assertEqual(ev["payload"]["text"], "suite green — 118 pass, 0 fail")
        self.assertEqual(ev["payload"]["detail"], self.LOG,
                         "the drawer timeline is where you dig in — detail has to survive")

        card = [c for c in self.get("/api/board")[1]["cards"] if c["num"] == num][0]
        self.assertEqual(card["last_event"]["payload"]["text"],
                         "suite green — 118 pass, 0 fail")
        self.assertEqual(card["last_event"]["payload"]["detail"], self.LOG)

        drained = self.get("/api/events?after=0&limit=500")[1]["events"]
        self.assertTrue(any(e["payload"].get("detail") == self.LOG for e in drained),
                        "the session drains the same payload the browser sees")

    def test_non_string_detail_is_a_400_and_lands_nothing(self):
        num = self.new_card("bad detail")["num"]
        self.to_in_progress(num)
        for bad in ({"lines": ["a"]}, ["a", "b"], 17, True):
            status, body = self.event(num, {"text": "one-liner", "detail": bad})
            self.assertEqual(status, 400, (bad, body))
            self.assertEqual(body["error"], "bad_detail")
            self.assertEqual(body["field"], "detail")
        self.assertEqual(self.progress_events(num), [],
                         "a rejected event must not be half-written to the log")

    def test_empty_detail_is_the_same_as_no_detail(self):
        num = self.new_card("nothing to expand")["num"]
        self.to_in_progress(num)
        for empty in ("", "   \n  ", None):
            status, body = self.event(num, {"text": "no long version", "detail": empty})
            self.assertEqual(status, 201, body)
            self.assertNotIn("detail", body["event"]["payload"],
                             "an empty detail must not render an empty expander")

    def test_detail_only_payload_still_gets_a_one_line_text(self):
        """The card face renders `text`. A detail-only post must not put a
        20-line blob there."""
        num = self.new_card("detail only")["num"]
        self.to_in_progress(num)
        status, body = self.event(num, {"detail": self.LOG})
        self.assertEqual(status, 201, body)
        p = body["event"]["payload"]
        self.assertEqual(p["text"], "$ python3 tests/test_sprintd.py")
        self.assertEqual(p["detail"], self.LOG)

        long_line = "x" * 400
        _, body = self.event(num, {"detail": long_line})
        p = body["event"]["payload"]
        self.assertEqual(len(p["text"]), sprintd.ONE_LINER_MAX)
        self.assertTrue(p["text"].endswith("…"))
        self.assertEqual(p["detail"], long_line)

    def test_any_worker_kind_can_carry_detail(self):
        num = self.new_card("kinds")["num"]
        self.to_in_progress(num)
        for kind in ("progress", "chat", "note", "error"):
            status, body = self.event(num, {"text": kind + " line",
                                            "detail": "expanded\n" + kind}, kind=kind)
            self.assertEqual(status, 201, body)
            self.assertEqual(body["event"]["payload"]["detail"], "expanded\n" + kind)

    def test_sidebar_lines_can_carry_detail_too(self):
        """The session answers in one line and parks the working underneath."""
        status, body = self.post("/api/sidebar", {
            "actor": "session",
            "text": "#12 and #14 are blocked on the same red CI job",
            "detail": "#12 — waiting on build 4471 (lint)\n#14 — same job, queued behind it",
        })
        self.assertEqual(status, 201, body)
        thread = self.get("/api/board")[1]["sidebar"]
        line = thread[-1]
        self.assertEqual(line["payload"]["text"],
                         "#12 and #14 are blocked on the same red CI job")
        self.assertIn("build 4471", line["payload"]["detail"])

        status, body = self.post("/api/sidebar", {"actor": "session", "text": "hi",
                                                  "detail": ["not", "a", "string"]})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "detail")

    def test_detail_and_long_running_coexist(self):
        num = self.new_card("long job")["num"]
        self.to_in_progress(num)
        status, body = self.event(num, {"text": "full suite, ~10min",
                                        "detail": "make test\nmake lint",
                                        "long_running": True})
        self.assertEqual(status, 201, body)
        self.assertTrue(body["card"]["long_running"])
        self.assertEqual(body["event"]["payload"]["detail"], "make test\nmake lint")


class TestSprintPostHelper(Base):
    """bin/sprint-post is how a worker actually writes these. The one-liner is
    never rejected for being long -- it gets truncated, loudly."""

    SPRINT_POST = os.path.join(os.path.dirname(HERE), "bin", "sprint-post")

    def run_post(self, *argv):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN="test-token")
        return subprocess.run([sys.executable, self.SPRINT_POST] + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)

    def last_payload(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return detail["timeline"][-1]["payload"]

    def test_detail_flag_posts_text_plus_detail(self):
        num = self.new_card("helper")["num"]
        self.to_in_progress(num)
        r = self.run_post(num, "progress", "fix applied, running tests",
                          "--detail", "ran: make test\nsaw: 118 pass, 0 fail")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        p = self.last_payload(num)
        self.assertEqual(p["text"], "fix applied, running tests")
        self.assertEqual(p["detail"], "ran: make test\nsaw: 118 pass, 0 fail")

    def test_detail_file_reads_a_captured_log(self):
        num = self.new_card("helper file")["num"]
        self.to_in_progress(num)
        log = os.path.join(self.tmp, "test.log")
        body = "Ran 118 tests in 4.2s\n\nOK\n"
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(body)
        r = self.run_post(num, "progress", "suite green", "--detail-file", log)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        p = self.last_payload(num)
        self.assertEqual(p["text"], "suite green")
        self.assertEqual(p["detail"], body.rstrip())

    def test_detail_and_detail_file_together_is_a_named_failure(self):
        num = self.new_card("both")["num"]
        r = self.run_post(num, "progress", "x", "--detail", "a", "--detail-file", "/tmp/nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("detail", r.stderr.decode())

    def test_unreadable_detail_file_names_the_field(self):
        num = self.new_card("missing file")["num"]
        r = self.run_post(num, "progress", "x", "--detail-file",
                          os.path.join(self.tmp, "not-here.log"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("detail-file", r.stderr.decode())

    def test_empty_detail_file_sends_no_detail(self):
        num = self.new_card("empty file")["num"]
        self.to_in_progress(num)
        log = os.path.join(self.tmp, "empty.log")
        open(log, "w").close()
        r = self.run_post(num, "progress", "nothing to expand", "--detail-file", log)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertNotIn("detail", self.last_payload(num))

    def test_long_text_is_truncated_with_a_notice_and_kept_in_detail(self):
        num = self.new_card("long one-liner")["num"]
        self.to_in_progress(num)
        long_text = "the fix is " + ("really " * 40) + "long"
        r = self.run_post(num, "progress", long_text)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("one line", r.stderr.decode(),
                      "a truncated one-liner has to say so, not silently clip")
        p = self.last_payload(num)
        self.assertEqual(len(p["text"]), 140)
        self.assertTrue(p["text"].endswith("…"))
        self.assertEqual(p["detail"], long_text,
                         "nothing the worker wrote may be thrown away")

    def test_multiline_text_keeps_the_first_line_and_loses_nothing(self):
        num = self.new_card("multiline")["num"]
        self.to_in_progress(num)
        r = self.run_post(num, "progress", "headline\nand a second line",
                          "--detail", "my own detail")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        p = self.last_payload(num)
        self.assertEqual(p["text"], "headline")
        self.assertEqual(p["detail"], "headline\nand a second line\n\nmy own detail")

    def test_a_normal_one_liner_is_untouched_and_quiet(self):
        num = self.new_card("plain")["num"]
        self.to_in_progress(num)
        r = self.run_post(num, "progress", "read the CSS, found the misaligned flex item")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(r.stderr.decode(), "")
        p = self.last_payload(num)
        self.assertEqual(p["text"], "read the CSS, found the misaligned flex item")
        self.assertNotIn("detail", p)


class RegistryBase(unittest.TestCase):
    """Every registry/hub test points $SPRINT_REGISTRY at a temp file, so the
    real ~/.sprint on this machine is never read, written, or pruned."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-reg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.registry = os.path.join(self.tmp, "hubstate", "registry.json")
        self._old_env = os.environ.get("SPRINT_REGISTRY")
        os.environ["SPRINT_REGISTRY"] = self.registry
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("SPRINT_REGISTRY", None)
        else:
            os.environ["SPRINT_REGISTRY"] = self._old_env

    def entry(self, name, port, pid=None, started_at=None, root=None, data_dir=None):
        root = root or os.path.join(self.tmp, name)
        os.makedirs(root, exist_ok=True)
        return sprintd.registry_entry(root, port, "127.0.0.1", data_dir=data_dir,
                                      pid=pid if pid is not None else os.getpid(),
                                      started_at=started_at)


class TestRegistry(RegistryBase):
    def test_env_override_keeps_the_real_home_registry_untouched(self):
        self.assertEqual(sprintd.registry_path(), os.path.abspath(self.registry))
        home_reg = os.path.join(os.path.expanduser("~"), ".sprint", "registry.json")
        before = os.path.exists(home_reg)
        before_mtime = os.path.getmtime(home_reg) if before else None
        sprintd.registry_register(self.entry("alpha", 9101))
        self.assertTrue(os.path.exists(self.registry))
        self.assertEqual(os.path.exists(home_reg), before)
        if before:
            self.assertEqual(os.path.getmtime(home_reg), before_mtime)

    def test_explicit_argument_beats_the_env_override(self):
        other = os.path.join(self.tmp, "other", "reg.json")
        self.assertEqual(sprintd.registry_path(other), os.path.abspath(other))
        sprintd.registry_register(self.entry("beta", 9102), other)
        self.assertTrue(os.path.exists(other))
        self.assertFalse(os.path.exists(self.registry))

    def test_hub_state_lives_beside_the_registry(self):
        """One override has to move the whole machine-wide state -- otherwise a
        test hub would read (or clobber) the real hub's token."""
        self.assertEqual(os.path.dirname(sprintd.hub_token_path()),
                         os.path.dirname(os.path.abspath(self.registry)))
        self.assertEqual(os.path.dirname(sprintd.hub_json_path()),
                         os.path.dirname(os.path.abspath(self.registry)))

    def test_register_writes_one_row_per_project_root(self):
        e = self.entry("alpha", 9101)
        sprintd.registry_register(e)
        rows = sprintd.read_registry()
        self.assertEqual(list(rows), [e["project_root"]])
        self.assertEqual(rows[e["project_root"]]["name"], "alpha")
        self.assertEqual(rows[e["project_root"]]["port"], 9101)

    def test_restarting_the_same_board_updates_its_row_not_adds_one(self):
        """One Claude session == one sprint == one project root: a restart is
        the same board, never a second row on the hub."""
        root = os.path.join(self.tmp, "alpha")
        sprintd.registry_register(self.entry("alpha", 9101, pid=111, root=root))
        sprintd.registry_register(self.entry("alpha", 9109, pid=222, root=root))
        rows = sprintd.read_registry()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[os.path.realpath(root)]["port"], 9109)
        self.assertEqual(rows[os.path.realpath(root)]["pid"], 222)

    def test_two_projects_are_two_rows(self):
        sprintd.registry_register(self.entry("alpha", 9101))
        sprintd.registry_register(self.entry("beta", 9102))
        self.assertEqual(len(sprintd.read_registry()), 2)

    def test_unregister_removes_only_that_row(self):
        a = self.entry("alpha", 9101)
        b = self.entry("beta", 9102)
        sprintd.registry_register(a)
        sprintd.registry_register(b)
        self.assertTrue(sprintd.registry_unregister(a["project_root"]))
        rows = sprintd.read_registry()
        self.assertEqual(list(rows), [b["project_root"]])
        # removing something already gone is a no-op, not an error
        self.assertFalse(sprintd.registry_unregister(a["project_root"]))

    def test_a_dying_server_cannot_delete_the_row_a_newer_one_just_wrote(self):
        root = os.path.join(self.tmp, "alpha")
        sprintd.registry_register(self.entry("alpha", 9101, pid=111, root=root))
        sprintd.registry_register(self.entry("alpha", 9109, pid=222, root=root))
        self.assertFalse(sprintd.registry_unregister(root, pid=111))
        self.assertEqual(len(sprintd.read_registry()), 1)
        self.assertTrue(sprintd.registry_unregister(root, pid=222))
        self.assertEqual(sprintd.read_registry(), {})

    def test_write_is_atomic_and_0600_with_no_tmp_left_behind(self):
        sprintd.registry_register(self.entry("alpha", 9101))
        self.assertEqual(oct(os.stat(self.registry).st_mode)[-3:], "600")
        leftovers = [f for f in os.listdir(os.path.dirname(self.registry))
                     if ".tmp" in f]
        self.assertEqual(leftovers, [], "temp files must be renamed, not left")

    def test_a_reader_never_sees_a_half_written_registry(self):
        """The rename-based write is what makes the hub safe to poll while two
        boards are starting: a reader sees the old file or the new one."""
        sprintd.registry_register(self.entry("alpha", 9101))
        stop = threading.Event()
        seen = []

        def reader():
            while not stop.is_set():
                seen.append(len(sprintd.read_registry()))

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for i in range(60):
                sprintd.registry_register(self.entry("beta", 9200 + i))
                sprintd.registry_unregister(os.path.join(self.tmp, "beta"))
        finally:
            stop.set()
            t.join(timeout=5)
        self.assertGreater(len(seen), 10, "reader thread never ran")
        self.assertNotIn(0, seen,
                         "a torn/truncated read would have shown zero entries")

    def test_a_corrupt_registry_reads_as_empty_instead_of_exploding(self):
        os.makedirs(os.path.dirname(self.registry), exist_ok=True)
        with open(self.registry, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertEqual(sprintd.read_registry(), {})
        # ...and the next register repairs it
        sprintd.registry_register(self.entry("alpha", 9101))
        self.assertEqual(len(sprintd.read_registry()), 1)

    def test_missing_registry_is_simply_empty(self):
        self.assertEqual(sprintd.read_registry(), {})

    def test_touch_merges_fields_into_an_existing_row_only(self):
        a = self.entry("alpha", 9101)
        sprintd.registry_register(a)
        sprintd.registry_touch(a["project_root"], {"last_seen": 12345.0})
        self.assertEqual(sprintd.read_registry()[a["project_root"]]["last_seen"],
                         12345.0)
        sprintd.registry_touch("/nope/not/here", {"last_seen": 1.0})
        self.assertEqual(len(sprintd.read_registry()), 1)

    def test_a_fresh_start_clears_the_stale_last_seen(self):
        a = self.entry("alpha", 9101)
        sprintd.registry_register(a)
        sprintd.registry_touch(a["project_root"], {"last_seen": 12345.0})
        sprintd.registry_register(self.entry("alpha", 9101))
        self.assertNotIn("last_seen",
                         sprintd.read_registry()[a["project_root"]])


class TestClickThroughFromTheHub(Base):
    """The bounce, verbatim: "I clicked your live link. then I tried to click
    into the russ board and it said {"error":"unauthorized"...}".

    Cookies are scoped by HOST and ignore the PORT, so every board on one
    machine shares a cookie jar -- which is the whole situation the hub exists
    for. The board used to prefer that ambient cookie over the `?t=` in the
    link it was just handed, so a valid hub link 401'd as soon as you were
    signed into any OTHER board on the same machine.
    """

    HTML_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8")

    def raw(self, path, headers=None, method="GET"):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body, dict(resp.getheaders())
        finally:
            conn.close()

    def test_a_valid_link_signs_you_in_even_holding_another_boards_cookie(self):
        status, _, headers = self.raw(
            "/?t=test-token",
            {"Cookie": "%s=a-different-boards-token" % sprintd.COOKIE_NAME,
             "Accept": self.HTML_ACCEPT})
        self.assertEqual(status, 302, "this is the exact bounce: it used to 401")
        self.assertIn(sprintd.board_cookie_name(self.port) + "=test-token",
                      headers.get("Set-Cookie") or "")
        self.assertEqual(headers.get("Location"), "/")

    def test_the_query_token_outranks_a_stale_cookie_on_the_api_too(self):
        status, _, _ = self.raw(
            "/api/board?t=test-token",
            {"Cookie": "%s=stale" % sprintd.board_cookie_name(self.port)})
        self.assertEqual(status, 200)

    def test_a_wrong_query_token_still_fails_even_with_a_good_cookie_present(self):
        """Precedence is 'any credential may match', not 'the last one wins' --
        a bad ?t= must not lock out a browser that IS signed in."""
        status, _, _ = self.raw(
            "/api/board?t=nope",
            {"Cookie": "%s=test-token" % sprintd.board_cookie_name(self.port)})
        self.assertEqual(status, 200)

    def test_two_boards_on_one_host_do_not_sign_each_other_out(self):
        """The premise of the whole hub: several sprints, one machine."""
        other_root = os.path.join(self.tmp, "other")
        os.makedirs(other_root, exist_ok=True)
        other = sprintd.App(other_root, log=self.logfh, token="other-token")
        httpd = sprintd.make_server(other, "127.0.0.1", 0)
        other_port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

        def shutdown():
            httpd.shutdown()
            httpd.server_close()
            other.close()

        self.addCleanup(shutdown)
        self.assertNotEqual(sprintd.board_cookie_name(self.port),
                            sprintd.board_cookie_name(other_port),
                            "each board needs its own slot in the browser jar")
        # signed into BOTH at once: one jar, two cookies, neither clobbers
        jar = "%s=test-token; %s=other-token" % (
            sprintd.board_cookie_name(self.port),
            sprintd.board_cookie_name(other_port))
        status, _, _ = self.raw("/api/board", {"Cookie": jar})
        self.assertEqual(status, 200)
        conn = http.client.HTTPConnection("127.0.0.1", other_port, timeout=10)
        try:
            conn.request("GET", "/api/board", headers={"Cookie": jar})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()

    def test_a_person_who_lands_unauthorized_gets_words_not_json(self):
        status, body, headers = self.raw("/", {"Accept": self.HTML_ACCEPT})
        self.assertEqual(status, 401)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        page = body.decode()
        self.assertIn("This board needs its link", page)
        self.assertNotIn('{"error"', page)
        self.assertNotIn("test-token", page, "a 401 page must never echo a token")

    def test_a_rotated_token_explains_itself_instead_of_stranding_you(self):
        status, body, _ = self.raw(
            "/?t=the-old-rotated-one", {"Accept": self.HTML_ACCEPT})
        self.assertEqual(status, 401)
        self.assertIn("new-token", body.decode())
        self.assertIn("sprintd hub", body.decode(),
                      "point a stuck user at the page that has every live link")

    def test_the_api_still_gets_json_not_a_web_page(self):
        for path in ("/api/board", "/api/cards/1"):
            status, body, headers = self.raw(path, {"Accept": self.HTML_ACCEPT})
            self.assertEqual(status, 401, path)
            self.assertIn("application/json", headers.get("Content-Type", ""), path)
            self.assertEqual(json.loads(body.decode())["error"], "unauthorized")

    def test_a_scripted_client_still_gets_json(self):
        status, _, headers = self.raw("/", {"Accept": "application/json"})
        self.assertEqual(status, 401)
        self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_healthz_is_still_open_and_unchanged(self):
        status, body, _ = self.raw("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode())["ok"])


class HubBase(RegistryBase):
    """Spins real boards (real HTTP server, real sqlite, temp data dir) and
    points a real HubApp at them through a temp registry."""

    def setUp(self):
        super().setUp()
        self.boards = {}

    def board(self, name, token=None, write_token=True):
        token = token or ("%s-token" % name)
        root = os.path.join(self.tmp, name)
        os.makedirs(root, exist_ok=True)
        logfh = open(os.path.join(self.tmp, "%s.log" % name), "a", encoding="utf-8")
        self.addCleanup(logfh.close)
        app = sprintd.App(root, log=logfh, token=token,
                          session_offline_seconds=90.0)
        httpd = sprintd.make_server(app, "127.0.0.1", 0)
        port = httpd.server_address[1]
        self.assertNotEqual(port, sprintd.DEFAULT_PORT)
        threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

        def shutdown():
            httpd.shutdown()
            httpd.server_close()
            app.close()

        self.addCleanup(shutdown)
        if write_token:
            # exactly what `sprintd start` writes; it is where the hub reads from
            sprintd.write_token_file(os.path.join(app.data_dir, "token"), token)
        b = {"app": app, "port": port, "token": token, "root": root,
             "data_dir": app.data_dir, "name": name}
        self.boards[name] = b
        sprintd.registry_register(sprintd.registry_entry(
            root, port, "127.0.0.1", data_dir=app.data_dir))
        return b

    def bpost(self, b, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", b["port"], timeout=10)
        try:
            conn.request("POST", path,
                         body=json.dumps(body or {}).encode("utf-8"),
                         headers={"Authorization": "Bearer " + b["token"],
                                  "Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

    def card(self, b, text="something"):
        status, card = self.bpost(b, "/api/cards", {"text": text})
        self.assertEqual(status, 201, card)
        return card["num"]

    def card_in_progress(self, b, text="working"):
        num = self.card(b, text)
        self.bpost(b, "/api/cards/%d/state" % num, {"state": "triaging"})
        self.bpost(b, "/api/cards/%d/state" % num, {"state": "in_progress"})
        return num

    def card_needs_you(self, b, text="stuck", question="which one?"):
        num = self.card_in_progress(b, text)
        status, body = self.bpost(b, "/api/cards/%d/question" % num, {"text": question})
        self.assertIn(status, (200, 201), body)
        return num

    def card_ready(self, b, text="done"):
        num = self.card_in_progress(b, text)
        packet = dict(GOOD_PACKET)
        status, body = self.bpost(b, "/api/cards/%d/ready" % num, {"packet": packet})
        self.assertEqual(status, 200, body)
        return num

    def rows(self, hub):
        return {r["name"]: r for r in hub.refresh()["sprints"]}


class TestHubAggregation(HubBase):
    def test_counts_every_board_separately(self):
        a = self.board("alpha")
        b = self.board("beta")
        self.card_needs_you(a, "a1")
        self.card_needs_you(a, "a2")
        self.card_in_progress(a, "a3")
        self.card_ready(b, "b1")
        self.card(b, "b2")           # sits in queued

        hub = sprintd.HubApp(token="hub-token")
        rows = self.rows(hub)
        self.assertEqual(set(rows), {"alpha", "beta"})
        self.assertEqual((rows["alpha"]["needs_you"], rows["alpha"]["ready"],
                          rows["alpha"]["in_motion"]), (2, 0, 1))
        self.assertEqual((rows["beta"]["needs_you"], rows["beta"]["ready"],
                          rows["beta"]["in_motion"]), (0, 1, 0))
        self.assertEqual(rows["beta"]["queued"], 1)
        self.assertTrue(rows["alpha"]["reachable"])
        self.assertEqual(rows["alpha"]["status"], "live")

    def test_the_link_carries_that_board_s_own_token(self):
        a = self.board("alpha", token="alpha-secret")
        hub = sprintd.HubApp(token="hub-token")
        url = self.rows(hub)["alpha"]["url"]
        self.assertEqual(url, "http://127.0.0.1:%d/?t=alpha-secret" % a["port"])
        # and it really signs you in: the board takes it as the query token
        status, _ = sprintd.http_get("127.0.0.1", a["port"], "/api/board",
                                     "alpha-secret")
        self.assertEqual(status, 200)

    def test_needs_you_sorts_to_the_top_with_the_oldest_question_s_age(self):
        quiet = self.board("quiet")
        waiting = self.board("waiting")
        self.card_ready(quiet)
        num = self.card_needs_you(waiting, "old one", "answer me")
        self.card_needs_you(waiting, "new one", "and me")
        # backdate the FIRST question by 22 minutes: "stuck 22m" is the oldest
        # thing waiting on the user, not the newest.
        waiting["app"].conn.execute(
            "UPDATE questions SET created_at=created_at-1320 WHERE card_num=?", (num,))
        waiting["app"].conn.commit()

        hub = sprintd.HubApp(token="hub-token")
        snap = hub.refresh()
        self.assertEqual([r["name"] for r in snap["sprints"]], ["waiting", "quiet"])
        top = snap["sprints"][0]
        self.assertEqual(top["stuck_card"], num)
        self.assertGreater(top["stuck_seconds"], 1300)
        self.assertLess(top["stuck_seconds"], 1400)
        self.assertEqual(snap["needs_you_total"], 2)

    def test_the_longest_wait_sorts_above_a_shorter_one(self):
        first = self.board("newer")
        second = self.board("older")
        self.card_needs_you(first)
        num = self.card_needs_you(second)
        second["app"].conn.execute(
            "UPDATE questions SET created_at=created_at-600 WHERE card_num=?", (num,))
        second["app"].conn.commit()
        hub = sprintd.HubApp(token="hub-token")
        self.assertEqual([r["name"] for r in hub.refresh()["sprints"]],
                         ["older", "newer"])

    def test_liveness_and_last_activity_come_from_the_board_itself(self):
        a = self.board("alpha")
        self.card(a, "hello")
        hub = sprintd.HubApp(token="hub-token")
        row = self.rows(hub)["alpha"]
        self.assertIn(row["session"], ("online", "busy", "offline"))
        self.assertIsNotNone(row["last_activity_at"])
        self.assertIn("#1", row["last_activity"])

    def test_an_empty_registry_is_an_empty_page_not_an_error(self):
        hub = sprintd.HubApp(token="hub-token")
        snap = hub.refresh()
        self.assertEqual(snap["sprints"], [])
        self.assertEqual(snap["count"], 0)
        self.assertEqual(snap["needs_you_total"], 0)


class TestHubDeadBoards(HubBase):
    def test_a_dead_board_is_greyed_and_kept_not_dropped(self):
        alive = self.board("alive")
        self.card(alive)
        # a board that went away: pid gone, nothing on the port, seen 4m ago
        dead_root = os.path.join(self.tmp, "gone")
        os.makedirs(dead_root, exist_ok=True)
        sprintd.registry_register(sprintd.registry_entry(
            dead_root, self._closed_port(), "127.0.0.1", pid=999999,
            started_at=sprintd.now() - 240))

        hub = sprintd.HubApp(token="hub-token", prune_seconds=900.0)
        snap = hub.refresh()
        names = [r["name"] for r in snap["sprints"]]
        self.assertEqual(names, ["alive", "gone"], "dead boards sink, never vanish")
        dead = snap["sprints"][-1]
        self.assertFalse(dead["reachable"])
        self.assertEqual(dead["status"], "unreachable")
        self.assertIsNone(dead["url"], "no sign-in link to a board that is gone")
        self.assertFalse(dead["pid_alive"])
        # still on file, so the row keeps rendering with "last seen Xm"
        self.assertIn(os.path.realpath(dead_root), sprintd.read_registry())

    def test_a_long_dead_board_is_pruned_from_the_registry(self):
        dead_root = os.path.join(self.tmp, "ancient")
        os.makedirs(dead_root, exist_ok=True)
        sprintd.registry_register(sprintd.registry_entry(
            dead_root, self._closed_port(), "127.0.0.1", pid=999999,
            started_at=sprintd.now() - 5000))
        hub = sprintd.HubApp(token="hub-token", prune_seconds=900.0)
        snap = hub.refresh()
        self.assertEqual(snap["sprints"], [])
        self.assertEqual(sprintd.read_registry(), {},
                         "the file is advisory: the hub prunes what it proved dead")

    def test_a_live_pid_is_never_pruned_however_old_the_entry(self):
        """Pruning needs BOTH proofs. A board whose process is alive but is
        mid-restart (port briefly closed) must survive the sweep."""
        root = os.path.join(self.tmp, "restarting")
        os.makedirs(root, exist_ok=True)
        sprintd.registry_register(sprintd.registry_entry(
            root, self._closed_port(), "127.0.0.1", pid=os.getpid(),
            started_at=sprintd.now() - 99999))
        hub = sprintd.HubApp(token="hub-token", prune_seconds=1.0)
        snap = hub.refresh()
        self.assertEqual(len(snap["sprints"]), 1)
        self.assertTrue(snap["sprints"][0]["pid_alive"])
        self.assertIn(os.path.realpath(root), sprintd.read_registry())

    def test_a_port_stolen_by_another_project_is_not_reported_as_that_board(self):
        """Ports get recycled. The hub health-checks project_root before it
        believes a row -- otherwise it would show one project's counts under
        another project's name."""
        real = self.board("real")
        impostor_root = os.path.join(self.tmp, "impostor")
        os.makedirs(impostor_root, exist_ok=True)
        sprintd.registry_register(sprintd.registry_entry(
            impostor_root, real["port"], "127.0.0.1", pid=999999))
        hub = sprintd.HubApp(token="hub-token", prune_seconds=99999.0)
        row = self.rows(hub)["impostor"]
        self.assertFalse(row["reachable"])
        self.assertIn("another project", row["detail"])

    def test_a_board_with_no_token_on_disk_says_so_instead_of_lying(self):
        b = self.board("tokenless", write_token=False)
        os.remove(os.path.join(b["data_dir"], "token")) if os.path.exists(
            os.path.join(b["data_dir"], "token")) else None
        hub = sprintd.HubApp(token="hub-token")
        row = self.rows(hub)["tokenless"]
        self.assertEqual(row["status"], "no_token")
        self.assertFalse(row["reachable"])

    def test_a_stale_token_on_disk_is_reported_not_silently_zeroed(self):
        b = self.board("rotated")
        sprintd.write_token_file(os.path.join(b["data_dir"], "token"), "the-old-one")
        hub = sprintd.HubApp(token="hub-token")
        row = self.rows(hub)["rotated"]
        self.assertEqual(row["status"], "unauthorized")
        self.assertEqual(row["needs_you"], 0)
        self.assertIsNone(row["url"])

    def test_a_successful_poll_stamps_last_seen_on_the_registry(self):
        self.board("alpha")
        hub = sprintd.HubApp(token="hub-token")
        hub.refresh()
        row = list(sprintd.read_registry().values())[0]
        self.assertIn("last_seen", row)
        self.assertLess(sprintd.now() - row["last_seen"], 30)

    def _closed_port(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        return port


class TestHubServer(HubBase):
    """The hub page and its API, over real HTTP."""

    def setUp(self):
        super().setUp()
        self.hub = sprintd.HubApp(token="hub-secret", poll_seconds=60.0)
        self.httpd = sprintd.make_hub_server(self.hub, "127.0.0.1", 0)
        self.hub_port = self.httpd.server_address[1]
        self.assertNotEqual(self.hub_port, sprintd.DEFAULT_HUB_PORT)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

        def shutdown():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.hub.close()

        self.addCleanup(shutdown)

    def hget(self, path, token="hub-secret", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub_port, timeout=10)
        try:
            hdrs = {"Accept": "application/json"}
            if token:
                hdrs["Authorization"] = "Bearer " + token
            hdrs.update(headers or {})
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, raw, dict(resp.getheaders())
        finally:
            conn.close()

    def test_healthz_needs_no_token_and_identifies_itself_as_the_hub(self):
        status, raw, _ = self.hget("/healthz", token=None)
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        self.assertTrue(body["hub"])
        self.assertEqual(os.path.realpath(body["registry"]),
                         os.path.realpath(self.registry))

    def test_the_page_and_the_api_both_require_the_hub_token(self):
        """The hub links into token-guarded boards -- it is a keyring, so it
        cannot be the one unauthenticated surface."""
        for path in ("/", "/api/hub"):
            status, raw, _ = self.hget(path, token=None)
            self.assertEqual(status, 401, path)
            self.assertIn("unauthorized", raw.decode())
        status, _, _ = self.hget("/api/hub", token="not-the-hub-token")
        self.assertEqual(status, 401)

    def test_query_token_sets_a_cookie_and_redirects(self):
        status, _, headers = self.hget("/?t=hub-secret", token=None)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")
        cookie = headers.get("Set-Cookie") or ""
        self.assertIn(sprintd.HUB_COOKIE_NAME + "=hub-secret", cookie)
        self.assertIn("HttpOnly", cookie)
        # the cookie alone is enough from then on
        status, raw, _ = self.hget(
            "/api/hub", token=None,
            headers={"Cookie": "%s=hub-secret" % sprintd.HUB_COOKIE_NAME})
        self.assertEqual(status, 200)
        self.assertIn("sprints", json.loads(raw.decode()))

    def test_the_hub_cookie_does_not_collide_with_a_board_cookie(self):
        """Cookies ignore the port, so a hub on the same host that reused the
        board's cookie name would sign every open board out."""
        self.assertNotEqual(sprintd.HUB_COOKIE_NAME, sprintd.COOKIE_NAME)
        status, _, _ = self.hget(
            "/api/hub", token=None,
            headers={"Cookie": "%s=hub-secret" % sprintd.COOKIE_NAME})
        self.assertEqual(status, 401, "a board cookie must not open the hub")

    def test_a_wrong_query_token_does_not_hand_out_a_cookie(self):
        status, _, headers = self.hget("/?t=guessing", token=None)
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_the_page_is_self_contained_html(self):
        status, raw, headers = self.hget("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        page = raw.decode()
        self.assertIn("sprint boards on this machine", page)
        for marker in ("http://", "https://", "//cdn", "<link"):
            if marker in ("http://", "https://"):
                self.assertNotIn('src="' + marker, page)
                self.assertNotIn('href="' + marker, page)
            else:
                self.assertNotIn(marker, page)

    def test_the_api_serves_the_rows_the_page_renders(self):
        a = self.board("alpha")
        self.card_needs_you(a)
        status, raw, _ = self.hget("/api/hub?fresh=1")
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["needs_you_total"], 1)
        self.assertEqual(body["sprints"][0]["name"], "alpha")
        self.assertEqual(body["sprints"][0]["port"], a["port"])

    def test_a_cached_snapshot_is_served_between_polls(self):
        self.board("alpha")
        first = self.hub.get()
        second = self.hub.get()
        self.assertEqual(first["server_time"], second["server_time"])
        self.assertNotEqual(self.hub.refresh()["server_time"], first["server_time"])

    def test_unknown_paths_404(self):
        status, _, _ = self.hget("/nope")
        self.assertEqual(status, 404)

    def test_the_link_the_page_renders_actually_signs_you_in(self):
        """End to end on the reported path: read the href out of the hub's own
        API, follow it exactly as a browser would -- carrying the cookie of a
        DIFFERENT board on the same host -- and land on the board, not on
        `{"error":"unauthorized"}`."""
        a = self.board("alpha", token="alpha-secret")
        self.board("beta", token="beta-secret")
        status, raw, _ = self.hget("/api/hub?fresh=1")
        self.assertEqual(status, 200)
        rows = {r["name"]: r for r in json.loads(raw.decode())["sprints"]}
        url = rows["alpha"]["url"]
        self.assertIn("?t=alpha-secret", url)

        # the browser already holds beta's cookie for this host
        jar = "%s=beta-secret" % sprintd.board_cookie_name(rows["beta"]["port"])
        conn = http.client.HTTPConnection("127.0.0.1", a["port"], timeout=10)
        try:
            conn.request("GET", "/?t=alpha-secret",
                         headers={"Cookie": jar,
                                  "Accept": "text/html,*/*;q=0.8"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 302, "the bounce: this used to 401")
            set_cookie = resp.getheader("Set-Cookie") or ""
        finally:
            conn.close()
        self.assertIn(sprintd.board_cookie_name(a["port"]) + "=alpha-secret",
                      set_cookie)
        # ...and the follow-up request with both cookies is signed in
        jar2 = jar + "; " + set_cookie.split(";")[0]
        conn = http.client.HTTPConnection("127.0.0.1", a["port"], timeout=10)
        try:
            conn.request("GET", "/api/board", headers={"Cookie": jar2})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()

    def test_a_stale_hub_cookie_cannot_lock_you_out_of_a_good_hub_link(self):
        status, _, headers = self.hget(
            "/?t=hub-secret", token=None,
            headers={"Cookie": "%s=rotated-away" % sprintd.HUB_COOKIE_NAME,
                     "Accept": "text/html,*/*;q=0.8"})
        self.assertEqual(status, 302)
        self.assertIn(sprintd.HUB_COOKIE_NAME + "=hub-secret",
                      headers.get("Set-Cookie") or "")

    def test_the_hub_also_answers_a_person_in_words(self):
        status, raw, headers = self.hget(
            "/", token=None, headers={"Accept": "text/html,*/*;q=0.8"})
        self.assertEqual(status, 401)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        page = raw.decode()
        self.assertIn("This page needs its link", page)
        self.assertNotIn("hub-secret", page)


class TestHubCli(unittest.TestCase):
    """The real CLI, end to end: two daemonized boards register themselves, the
    hub lists them both, and --stop cleans up."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-hubcli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.registry = os.path.join(self.tmp, "hubstate", "registry.json")
        self.env = dict(os.environ, SPRINT_REGISTRY=self.registry)
        self.roots = {}
        self.addCleanup(self._kill_leftovers)

    def _free_port(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def _run(self, *argv, timeout=60):
        import subprocess
        return subprocess.run([sys.executable, SPRINTD_PATH] + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env, timeout=timeout)

    def _kill_leftovers(self):
        for root in list(self.roots.values()):
            self._run("--project-root", root, "stop")
        self._run("hub", "--stop")

    def start_board(self, name, token):
        root = os.path.join(self.tmp, name)
        os.makedirs(root, exist_ok=True)
        self.roots[name] = root
        port = self._free_port()
        r = self._run("--project-root", root, "start", "--port", port,
                      "--no-tailscale", "--token", token)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        return root, port

    def test_boards_register_themselves_and_the_hub_lists_them(self):
        root_a, port_a = self.start_board("alpha", "alpha-tok")
        root_b, port_b = self.start_board("beta", "beta-tok")

        reg = sprintd.read_registry(self.registry)
        self.assertEqual(sorted(r["name"] for r in reg.values()), ["alpha", "beta"])
        self.assertEqual(oct(os.stat(self.registry).st_mode)[-3:], "600")

        hub_port = self._free_port()
        r = self._run("hub", "--port", hub_port, "--no-tailscale", "--token", "hub-tok")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("http://127.0.0.1:%d/?t=hub-tok" % hub_port, r.stdout.decode())

        status, raw = sprintd.http_get("127.0.0.1", hub_port, "/api/hub?fresh=1",
                                       "hub-tok", timeout=10.0)
        self.assertEqual(status, 200)
        body = json.loads(raw.decode())
        rows = {r["name"]: r for r in body["sprints"]}
        self.assertEqual(sorted(rows), ["alpha", "beta"])
        self.assertTrue(all(r["reachable"] for r in rows.values()))
        self.assertEqual(rows["alpha"]["url"],
                         "http://127.0.0.1:%d/?t=alpha-tok" % port_a)
        self.assertEqual(rows["beta"]["url"],
                         "http://127.0.0.1:%d/?t=beta-tok" % port_b)

        # a second `hub` reuses the running one instead of fighting for the port
        r2 = self._run("hub", "--port", hub_port, "--no-tailscale")
        self.assertEqual(r2.returncode, 0, r2.stderr.decode())
        self.assertIn("already running", r2.stdout.decode())

        # stopping a board takes its row off the hub
        self._run("--project-root", root_b, "stop")
        self.assertEqual(sorted(n["name"] for n in
                                sprintd.read_registry(self.registry).values()),
                         ["alpha"])
        status, raw = sprintd.http_get("127.0.0.1", hub_port, "/api/hub?fresh=1",
                                       "hub-tok", timeout=10.0)
        self.assertEqual([r["name"] for r in json.loads(raw.decode())["sprints"]],
                         ["alpha"])

        # hub token persisted beside the registry, 0600, and reused next time
        tok_file = os.path.join(os.path.dirname(self.registry), "hub-token")
        self.assertEqual(sprintd.read_token_file(tok_file), "hub-tok")
        self.assertEqual(oct(os.stat(tok_file).st_mode)[-3:], "600")

        r3 = self._run("hub", "--stop")
        self.assertEqual(r3.returncode, 0, r3.stderr.decode())
        try:
            sprintd.http_get("127.0.0.1", hub_port, "/healthz", timeout=2.0)
            self.fail("hub still listening after --stop")
        except (OSError, http.client.HTTPException):
            pass

    def test_hub_stop_when_nothing_is_running_is_a_clean_no_op(self):
        r = self._run("hub", "--stop")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("not running", r.stdout.decode())

    def test_the_registry_env_override_keeps_the_hub_off_the_real_home_dir(self):
        self.start_board("alpha", "alpha-tok")
        self.assertTrue(os.path.exists(self.registry))
        home_reg = os.path.join(os.path.expanduser("~"), ".sprint", "registry.json")
        if os.path.exists(home_reg):
            self.assertNotIn(os.path.realpath(os.path.join(self.tmp, "alpha")),
                             sprintd.read_registry(home_reg))


class SiblingsBase(HubBase):
    """`GET /api/siblings` — the same machine-wide picture as the hub, but read
    from INSIDE one board so its title can become a switcher.

    Everything runs against a temp registry (RegistryBase) so the real
    ~/.sprint on this machine is never read or written.
    """

    def board(self, name, token=None, write_token=True):
        b = super().board(name, token=token, write_token=write_token)
        # what `sprintd start` stamps once the socket is really bound
        b["app"].port = b["port"]
        b["app"].hosts = ["127.0.0.1"]
        return b

    def siblings(self, b, fresh=True, token="__own__"):
        path = "/api/siblings" + ("?fresh=1" if fresh else "")
        tok = b["token"] if token == "__own__" else token
        status, raw = sprintd.http_get("127.0.0.1", b["port"], path, tok)
        body = json.loads(raw.decode("utf-8")) if raw else {}
        return status, body

    def by_name(self, body):
        return {r["name"]: r for r in body["sprints"]}

    def _closed_port(self):
        """A port nothing is listening on — a board that went away."""
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()


class TestSiblingsEndpoint(SiblingsBase):
    def test_lists_every_live_sprint_with_this_one_flagged_self(self):
        a = self.board("alpha")
        self.board("beta")
        status, body = self.siblings(a)
        self.assertEqual(status, 200, body)
        rows = self.by_name(body)
        self.assertEqual(set(rows), {"alpha", "beta"})
        self.assertEqual(body["count"], 2)
        self.assertTrue(rows["alpha"]["self"], "the board answering is itself in the list")
        self.assertFalse(rows["beta"]["self"])
        self.assertTrue(rows["alpha"]["alive"] and rows["beta"]["alive"])
        # ...and the same question asked from the OTHER board flips the flag
        _, other = self.siblings(self.boards["beta"])
        self.assertTrue(self.by_name(other)["beta"]["self"])
        self.assertFalse(self.by_name(other)["alpha"]["self"])

    def test_a_single_board_lists_only_itself_so_the_title_stays_plain(self):
        a = self.board("solo")
        status, body = self.siblings(a)
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertTrue(body["sprints"][0]["self"])
        self.assertEqual(body["needs_you_elsewhere"], 0)

    def test_the_counts_are_each_board_s_own(self):
        a = self.board("alpha")
        b = self.board("beta")
        self.card_needs_you(a, "a1")
        self.card_needs_you(b, "b1")
        self.card_needs_you(b, "b2")
        self.card_ready(b, "b3")
        self.card_in_progress(b, "b4")
        rows = self.by_name(self.siblings(a)[1])
        self.assertEqual(rows["alpha"]["needs_you"], 1)
        self.assertEqual((rows["beta"]["needs_you"], rows["beta"]["ready"],
                          rows["beta"]["in_motion"]), (2, 1, 1))

    def test_needs_you_elsewhere_ignores_this_board_s_own_pile(self):
        """The dot on the title means ANOTHER sprint wants you — this board's
        own needs-you cards are already on the page behind the title."""
        a = self.board("alpha")
        b = self.board("beta")
        self.card_needs_you(a, "mine")
        self.assertEqual(self.siblings(a)[1]["needs_you_elsewhere"], 0)
        self.card_needs_you(b, "theirs")
        self.assertEqual(self.siblings(a)[1]["needs_you_elsewhere"], 1)
        # and from beta's side it is alpha's card that counts
        self.assertEqual(self.siblings(b)[1]["needs_you_elsewhere"], 1)

    def test_the_sibling_waiting_on_you_sorts_first_after_this_one(self):
        a = self.board("alpha")
        self.board("quiet")
        waiting = self.board("waiting")
        self.card_needs_you(waiting)
        names = [r["name"] for r in self.siblings(a)[1]["sprints"]]
        self.assertEqual(names, ["alpha", "waiting", "quiet"])

    def test_each_row_links_with_that_board_s_own_token(self):
        a = self.board("alpha", token="alpha-secret")
        b = self.board("beta", token="beta-secret")
        rows = self.by_name(self.siblings(a)[1])
        self.assertEqual(rows["beta"]["url"],
                         "http://127.0.0.1:%d/?t=beta-secret" % b["port"])
        self.assertEqual(rows["alpha"]["url"],
                         "http://127.0.0.1:%d/?t=alpha-secret" % a["port"])
        # the link really signs you in to that board
        status, _ = sprintd.http_get("127.0.0.1", b["port"], "/api/board", "beta-secret")
        self.assertEqual(status, 200)

    def test_a_dead_registry_row_is_left_out_of_the_menu(self):
        """The hub greys a dead board because its job is to say it died. A
        dropdown exists to be clicked: a row you cannot navigate to is noise."""
        a = self.board("alpha")
        dead_root = os.path.join(self.tmp, "gone")
        os.makedirs(dead_root, exist_ok=True)
        sprintd.registry_register(sprintd.registry_entry(
            dead_root, self._closed_port(), "127.0.0.1", pid=999999,
            started_at=sprintd.now() - 240))
        body = self.siblings(a)[1]
        self.assertEqual([r["name"] for r in body["sprints"]], ["alpha"])
        # ...and it is NOT pruned from the registry: only the hub prunes
        self.assertIn(os.path.realpath(dead_root), sprintd.read_registry())

    def test_a_port_stolen_by_another_project_is_never_offered(self):
        """Ports get recycled. Without the /healthz project_root guard the menu
        would offer one project's board wearing another project's name."""
        a = self.board("alpha", token="a-shared-token")
        self.card_needs_you(a, "alpha's own question")
        # A stale row for a project that is gone, still pointing at a port that
        # something else now owns -- and (worst case) holding a token that port
        # accepts, so nothing downstream of the guard would notice.
        impostor_root = os.path.join(self.tmp, "impostor")
        os.makedirs(os.path.join(impostor_root, ".sprint"), exist_ok=True)
        sprintd.write_token_file(
            os.path.join(impostor_root, ".sprint", "token"), "a-shared-token")
        sprintd.registry_register(sprintd.registry_entry(
            impostor_root, a["port"], "127.0.0.1", pid=999999))
        body = self.siblings(a)[1]
        self.assertEqual([r["name"] for r in body["sprints"]], ["alpha"],
                         "the /healthz project_root check is what rejects this row")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["needs_you_elsewhere"], 0,
                         "alpha's own question must never count as a sibling's")

    def test_a_board_missing_from_the_registry_still_lists_itself(self):
        a = self.board("alpha")
        sprintd.registry_unregister(os.path.realpath(a["root"]))
        body = self.siblings(a)[1]
        self.assertEqual(body["count"], 1)
        row = body["sprints"][0]
        self.assertTrue(row["self"])
        self.assertEqual(row["name"], "alpha")
        self.assertEqual(row["url"], "http://127.0.0.1:%d/?t=alpha-token" % a["port"])

    def test_it_needs_the_board_s_token(self):
        a = self.board("alpha")
        status, _ = self.siblings(a, token=None)
        self.assertEqual(status, 401, "the sibling list hands out other boards' tokens")
        status, _ = self.siblings(a, token="not-the-token")
        self.assertEqual(status, 401)

    def test_the_snapshot_is_cached_and_fresh_bypasses_the_cache(self):
        """A dozen tabs polling every 30s must not become a dozen health-check
        sweeps of every board on the machine."""
        a = self.board("alpha")
        b = self.board("beta")
        self.assertEqual(self.by_name(self.siblings(a, fresh=False)[1])["beta"]["needs_you"], 0)
        self.card_needs_you(b, "new question")
        cached = self.by_name(self.siblings(a, fresh=False)[1])
        self.assertEqual(cached["beta"]["needs_you"], 0, "served from the 10s cache")
        fresh = self.by_name(self.siblings(a, fresh=True)[1])
        self.assertEqual(fresh["beta"]["needs_you"], 1)

    def test_the_cache_ttl_is_tunable_and_expires(self):
        a = self.board("alpha")
        b = self.board("beta")
        a["app"].siblings_ttl = 0.05
        self.siblings(a, fresh=False)
        self.card_needs_you(b, "later")
        time.sleep(0.2)
        self.assertEqual(self.by_name(self.siblings(a, fresh=False)[1])["beta"]["needs_you"], 1)


class TestBoardRollupIsSharedWithTheHub(SiblingsBase):
    def test_the_hub_row_and_the_sibling_row_agree_on_every_count(self):
        """One counter, two callers: the dropdown and the hub can never drift
        apart on what "2 need you" means."""
        a = self.board("alpha")
        b = self.board("beta")
        self.card_needs_you(b, "b1")
        self.card_needs_you(b, "b2")
        self.card_ready(b, "b3")
        self.card_in_progress(b, "b4")
        self.card(b, "b5")
        hub_row = {r["name"]: r for r in sprintd.HubApp(token="hub-tok").refresh()["sprints"]}["beta"]
        sib_row = self.by_name(self.siblings(a)[1])["beta"]
        for field in ("needs_you", "ready", "in_motion", "queued", "blocked"):
            self.assertEqual(sib_row[field], hub_row[field], field)

    def test_the_self_row_matches_what_the_hub_sees_over_http(self):
        a = self.board("alpha")
        self.card_needs_you(a, "a1")
        self.card_ready(a, "a2")
        hub_row = {r["name"]: r for r in sprintd.HubApp(token="hub-tok").refresh()["sprints"]}["alpha"]
        self_row = self.by_name(self.siblings(a)[1])["alpha"]
        self.assertTrue(self_row["self"])
        for field in ("needs_you", "ready", "in_motion", "queued", "blocked"):
            self.assertEqual(self_row[field], hub_row[field], field)


if __name__ == "__main__":
    unittest.main(verbosity=2)
