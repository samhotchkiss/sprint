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
    SSE_HEARTBEAT = 15.0
    START_BACKGROUND = False

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
            sse_heartbeat=self.SSE_HEARTBEAT,
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
            self.assertIn(sprintd.COOKIE_NAME + "=test-token", cookie)
        finally:
            conn.close()
        # the cookie alone authenticates
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


class TestStream(Base):
    SSE_HEARTBEAT = 0.4

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

    def test_event_frames_are_id_plus_data_only(self):
        """Event frames: `id:` = seq, `data:` = one event JSON, and no `event:`
        line -- a named type would never reach the browser's default message
        handler. The ONLY named frame on this stream is `cursor`, which is not
        an event and therefore carries no `id:` (it must never become
        Last-Event-ID)."""
        self.new_card("framing")
        _conn, resp = self._open_stream()
        lines = self._read_lines(resp, lambda t: t.startswith("data:") and '"seq"' in t)
        named = [t for t in lines if t.startswith("event:")]
        self.assertTrue(all(t.strip() == "event: cursor" for t in named), lines)

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
                if kind == "cursor":
                    self.assertIsNone(ident, "a cursor frame must not carry an id")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
