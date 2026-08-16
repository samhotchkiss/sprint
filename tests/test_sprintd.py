#!/usr/bin/env python3
"""Tests for bin/sprintd. Stdlib unittest only.

Every test spins a real HTTP server on 127.0.0.1 with an ephemeral port and a
temp data dir. Port 8377 (the real default) is never touched.
"""

import base64
import datetime
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
import html.parser as html_parser
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

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # EVERY board test points machine-wide state at a temp dir, not just
        # the registry ones: an account limit is read off a shared file beside
        # the registry on every board read, and a suite that read the real
        # ~/.sprint would either invent a banner from the developer's own live
        # limit or write one into it.
        self.registry = os.path.join(self.tmp, "hubstate", "registry.json")
        self._old_registry_env = os.environ.get("SPRINT_REGISTRY")
        os.environ["SPRINT_REGISTRY"] = self.registry
        self.addCleanup(self._restore_registry_env)
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

    def _restore_registry_env(self):
        if self._old_registry_env is None:
            os.environ.pop("SPRINT_REGISTRY", None)
        else:
            os.environ["SPRINT_REGISTRY"] = self._old_registry_env

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
        env = dict(os.environ, SPRINT_SSE_HEARTBEAT=str(self.HEARTBEAT))
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

    # -- self-echo suppression (card #39) ---------------------------------

    def test_the_session_is_not_woken_by_its_own_posts(self):
        """The session's standing tail must never wake it on its own writing.

        Reported live: every sidebar reply the orchestrator posted came straight
        back down its own ingress and woke it to read what it had just said.
        Heartbeats and cursor frames were already suppressed for exactly this
        reason; the session's own echo is the same class of non-event.
        """
        _proc, lines, noise = self.tail("--after", str(self.head()))
        self.session_says("a reply the session posted itself")
        self.user_says("and then the human said something")
        got = self.wait_lines(lines, 1)
        # The user line is the FIRST thing on stdout: the session's own post
        # never appeared, it was not merely printed later.
        self.assertEqual([ev["text"] for ev in got],
                         ["and then the human said something"])
        self.assertTrue(all(ev["actor"] != "session" for ev in got), got)
        self.assertEqual(noise, [])

    def test_include_self_restores_the_raw_log(self):
        """Suppression is a default, not a hole: --include-self prints them."""
        _proc, lines, noise = self.tail("--after", str(self.head()),
                                        "--include-self")
        self.session_says("a reply the session posted itself")
        self.user_says("and then the human said something")
        got = self.wait_lines(lines, 2)
        self.assertEqual([ev["actor"] for ev in got], ["session", "user"])
        self.assertEqual([ev["text"] for ev in got],
                         ["a reply the session posted itself",
                          "and then the human said something"])
        # ...and the biconditional still holds on the line: only the human's
        # carries a routing key.
        self.assertIsNone(got[0]["reply_to"])
        self.assertEqual(got[1]["reply_to"], "sidebar")

    def test_worker_and_server_events_still_wake_the_session(self):
        """Only the session's OWN voice is dropped. A worker's progress and the
        server's own state lines are exactly what the tail exists to carry."""
        _proc, lines, noise = self.tail("--after", str(self.head()))
        card = self.new_card("something to work")
        self.api("POST", "/api/cards/%d/events" % card["num"],
                 {"kind": "progress", "actor": "worker",
                  "payload": {"text": "worker reporting in"}})
        got = self.wait_lines(lines, 3)      # submitted, state (server), progress
        self.assertEqual(sorted({ev["actor"] for ev in got}),
                         ["server", "user", "worker"])


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


class TestPhases(Base):
    """A phase is what the agent says it is DOING, on its own clock. It is a
    projection of the event log, so a card that moved on can't still be
    'testing'."""

    def phase(self, num, phase, expect=None, kind="progress"):
        payload = {"text": "phase: %s" % phase, "phase": phase}
        if expect is not None:
            payload["expected_seconds"] = expect
        return self.post("/api/cards/%d/events" % num, {"kind": kind, "payload": payload})

    def card_of(self, num):
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(status, 200, detail)
        return detail["card"]

    def board_card(self, num):
        status, board = self.get("/api/board")
        self.assertEqual(status, 200, board)
        return {c["num"]: c for c in board["cards"]}[num]

    def test_a_phase_event_shows_up_on_the_card_and_the_board(self):
        num = self.new_card("phase me")["num"]
        self.to_in_progress(num)
        before = self.card_of(num)
        self.assertIsNone(before["phase"])
        self.assertIsNone(before["phase_since"])
        self.assertIsNone(before["phase_expected_seconds"])

        status, body = self.phase(num, "testing", expect=300)
        self.assertEqual(status, 201, body)

        for card in (self.card_of(num), self.board_card(num), body["card"]):
            self.assertEqual(card["phase"], "testing")
            self.assertEqual(card["phase_expected_seconds"], 300.0)
            self.assertIsNotNone(card["phase_since"])
            self.assertGreater(card["phase_since"], 0)

    def test_the_latest_phase_wins_and_plain_events_do_not_clear_it(self):
        num = self.new_card("many phases")["num"]
        self.to_in_progress(num)
        self.phase(num, "reading")
        self.phase(num, "coding", expect=600)
        self.post("/api/cards/%d/events" % num,
                  {"kind": "progress", "payload": {"text": "found the bug"}})
        card = self.card_of(num)
        self.assertEqual(card["phase"], "coding",
                         "a plain progress line is not the end of a phase")
        self.assertEqual(card["phase_expected_seconds"], 600.0)

    def test_a_state_change_clears_the_phase(self):
        num = self.new_card("phase then move")["num"]
        self.to_in_progress(num)
        self.phase(num, "assembling packet", expect=120)
        self.assertEqual(self.card_of(num)["phase"], "assembling packet")

        status, _ = self.post("/api/cards/%d/state" % num,
                              {"state": "blocked", "reason": "ci_red"})
        self.assertEqual(status, 200)
        card = self.card_of(num)
        self.assertIsNone(card["phase"], "a card that moved on is not still packing")
        self.assertIsNone(card["phase_since"])
        self.assertIsNone(card["phase_expected_seconds"])

    def test_an_expectation_without_a_phase_carries_no_clock(self):
        num = self.new_card("no phase")["num"]
        self.to_in_progress(num)
        status, _ = self.post("/api/cards/%d/events" % num,
                              {"kind": "progress",
                               "payload": {"text": "x", "expected_seconds": 300}})
        self.assertEqual(status, 201)
        self.assertIsNone(self.card_of(num)["phase"])

    def test_junk_phases_are_named_400s_not_empty_chips(self):
        num = self.new_card("junk")["num"]
        self.to_in_progress(num)
        for bad in ("", "   ", 7, None):
            status, body = self.post("/api/cards/%d/events" % num,
                                     {"kind": "progress",
                                      "payload": {"text": "x", "phase": bad}})
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"], "bad_phase")
        for bad in (0, -5, "soon", 25 * 3600, True):
            status, body = self.phase(num, "testing", expect=bad)
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"], "bad_expected_seconds")
        self.assertIsNone(self.card_of(num)["phase"], "nothing junk was recorded")

    def test_a_long_phase_is_clipped_to_a_chip(self):
        num = self.new_card("long phase")["num"]
        self.to_in_progress(num)
        self.phase(num, "capturing evidence " * 10)
        card = self.card_of(num)
        self.assertLessEqual(len(card["phase"]), 40)
        self.assertTrue(card["phase"].startswith("capturing evidence"))


class TestPhaseAndSilence(Base):
    """The falsification pair: the SAME quiet stretch, one card shielded by a
    phase that claimed it and one not. If the shield stopped working, the first
    assertion below is the one that fails."""

    SILENCE_SECONDS = 2.0
    SILENCE_TICK = 0.2
    START_BACKGROUND = True

    def silent_events(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e for e in detail["timeline"] if e["kind"] == "agent_silent"]

    def wait_for_silent(self, num, timeout=12.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.silent_events(num)
            if events:
                return events
            time.sleep(0.1)
        return []

    def phase(self, num, phase, expect=None):
        payload = {"text": "phase: %s" % phase, "phase": phase}
        if expect is not None:
            payload["expected_seconds"] = expect
        status, body = self.post("/api/cards/%d/events" % num,
                                 {"kind": "progress", "payload": payload})
        self.assertEqual(status, 201, body)

    def test_a_declared_phase_holds_the_timer_and_a_bare_line_does_not(self):
        shielded = self.new_card("declared a long phase")["num"]
        control = self.new_card("just said something")["num"]
        for num in (shielded, control):
            self.post("/api/cards/%d/assign" % num,
                      {"agent_name": "sprint-card-%d" % num, "worktree": "/tmp/wt",
                       "branch": "sprint/card-%d" % num})
            self.to_in_progress(num)

        # Same instant, same silence afterwards. The only difference is that one
        # of them said how long it would be quiet for.
        self.phase(shielded, "testing", expect=8)
        self.post("/api/cards/%d/events" % control,
                  {"kind": "progress", "payload": {"text": "running the tests"}})

        # Past the 2s threshold: the control ambers, the shielded card does not.
        self.assertEqual(len(self.wait_for_silent(control)), 1,
                         "the control card proves the timer is armed and firing")
        self.assertEqual(self.silent_events(shielded), [],
                         "a phase inside the time it claimed is not silence")
        _, board = self.get("/api/board")
        by_num = {c["num"]: c for c in board["cards"]}
        self.assertFalse(by_num[shielded]["silent"])
        self.assertTrue(by_num[control]["silent"])

    def test_the_shield_lasts_exactly_as_long_as_the_claim(self):
        num = self.new_card("overran its phase")["num"]
        self.to_in_progress(num)
        self.phase(num, "testing", expect=3)
        time.sleep(2.4)
        self.assertEqual(self.silent_events(num), [], "still inside the claim")
        # ...and once the claim runs out with no new word, it ambers as usual.
        self.assertEqual(len(self.wait_for_silent(num)), 1,
                         "an expectation that ran out is exactly what amber is for")
        self.assertTrue(self.get("/api/cards/%d" % num)[1]["card"]["silent"])

    def test_a_phase_with_no_expectation_shields_nothing(self):
        num = self.new_card("no expectation")["num"]
        self.to_in_progress(num)
        self.phase(num, "coding")
        self.assertEqual(len(self.wait_for_silent(num)), 1,
                         "the default clock is the promise you didn't make")

    def test_a_new_phase_re_arms_the_shield(self):
        num = self.new_card("phase after phase")["num"]
        self.to_in_progress(num)
        self.phase(num, "coding")
        self.assertEqual(len(self.wait_for_silent(num)), 1)
        self.phase(num, "testing", expect=8)
        _, board = self.get("/api/board")
        self.assertFalse({c["num"]: c for c in board["cards"]}[num]["silent"])
        time.sleep(self.SILENCE_SECONDS + 1.0)
        self.assertEqual(len(self.silent_events(num)), 1,
                         "declaring a phase is an act of liveness")


class TestSprintPostPhaseHelper(Base):
    """`sprint-post 42 phase "testing" --expect 5m` — sugar over a progress
    event, validated before it ever hits the network."""

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

    def working_card(self, text="phase helper"):
        num = self.new_card(text)["num"]
        self.to_in_progress(num)
        return num

    def test_phase_posts_a_progress_event_carrying_the_phase(self):
        num = self.working_card()
        r = self.run_post(num, "phase", "capturing evidence")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        p = self.last_payload(num)
        self.assertEqual(p["phase"], "capturing evidence")
        self.assertNotIn("expected_seconds", p)
        self.assertEqual(p["text"], "phase: capturing evidence")
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["timeline"][-1]["kind"], "progress",
                         "phase is sugar, not a new event kind on the wire")
        self.assertEqual(detail["card"]["phase"], "capturing evidence")

    def test_expect_takes_seconds_or_a_unit(self):
        for raw, want in (("300", 300), ("90s", 90), ("5m", 300), ("1h", 3600)):
            num = self.working_card("expect " + raw)
            r = self.run_post(num, "phase", "testing", "--expect", raw)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            p = self.last_payload(num)
            self.assertEqual(p["expected_seconds"], want)
            self.assertIn("expect ~", p["text"],
                          "the timeline line says the expectation out loud")
            self.assertEqual(self.get("/api/cards/%d" % num)[1]["card"]
                             ["phase_expected_seconds"], float(want))

    def test_a_bad_expectation_is_a_named_client_side_failure(self):
        num = self.working_card()
        for bad in ("soon", "0", "-30", "25h", ""):
            r = self.run_post(num, "phase", "testing", "--expect", bad)
            self.assertEqual(r.returncode, 2, "%r should not post" % bad)
            self.assertIn("expect", r.stderr.decode())
        _, detail = self.get("/api/cards/%d" % num)
        self.assertNotIn("phase", detail["timeline"][-1]["payload"],
                         "nothing reached the server")

    def test_expect_without_a_phase_is_rejected(self):
        num = self.working_card()
        r = self.run_post(num, "progress", "running tests", "--expect", "300")
        self.assertEqual(r.returncode, 2)
        self.assertIn("expect", r.stderr.decode())

    def test_a_phase_with_no_name_names_the_missing_field(self):
        num = self.working_card()
        r = self.run_post(num, "phase")
        self.assertEqual(r.returncode, 2)
        self.assertIn("text", r.stderr.decode())

    def test_a_long_phase_is_clipped_before_it_is_sent(self):
        num = self.working_card()
        r = self.run_post(num, "phase", "testing " * 20)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertLessEqual(len(self.last_payload(num)["phase"]), 40)

    def test_it_tells_you_what_the_card_now_says(self):
        num = self.working_card()
        r = self.run_post(num, "phase", "testing", "--expect", "5m")
        out = r.stdout.decode()
        self.assertIn("testing", out)
        self.assertIn("5m", out)


# ===========================================================================
# Reports — markdown / HTML documents as first-class attachments (card #33)
# ===========================================================================

REPORT_MD = """# Findings: the drawer scrim

The scrim reads **too dark** at 980px. Three things are true:

- the token is `--scrim`
- it is used in exactly one place
- nothing else reads it

## Numbers

| width | opacity | verdict |
|---|---|---|
| 1440 | .62 | fine |
| 980  | .62 | too dark |

> The Fold is the case that matters.

```css
.scrim { background: var(--scrim); }
```

See [the spec](https://example.com/spec) for the rest.
"""

# The whole safety story in one fixture: every way an author could try to get
# markup out of a .md file. None of it may reach the browser as markup.
HOSTILE_MD = """# Hostile report

<script>window.__pwned = 1;</script>

<img src=x onerror="window.__pwned = 2">

<iframe src="javascript:alert(1)"></iframe>

An [innocent link](https://example.com) beside a [bad one](javascript:alert(1))
and an ![image](javascript:alert(2)).

<div onclick="alert(3)">a div</div>

`<script>inline</script>`
"""

HOSTILE_HTML = ("<!doctype html><html><head><title>Author page</title></head>"
                "<body><h1>Author page</h1><script>window.__pwned=3;</script>"
                "</body></html>")


class ReportBase(Base):
    """Reports ride the same content-addressed store an image does. What is
    different is the sniff (text has no magic bytes) and the render (markdown is
    turned into HTML by us; author HTML never is)."""

    def write_doc(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def working_card(self, text="a card that gets a report"):
        num = self.new_card(text)["num"]
        self.to_in_progress(num)
        return num

    def post_report(self, num, path, text="here are the findings", kind="chat"):
        status, body = self.post("/api/cards/%d/events" % num,
                                 {"kind": kind,
                                  "payload": {"text": text, "reports": [path]}})
        self.assertEqual(status, 201, body)
        return body

    def last_payload(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return detail["timeline"][-1]["payload"]

    def only_report(self, num):
        docs = [a for a in (self.last_payload(num).get("attachments") or [])
                if a.get("doc")]
        self.assertEqual(len(docs), 1, docs)
        return docs[0]

    def raw(self, path, token="test-token"):
        """A raw fetch that keeps the headers — the CSP on author HTML is the
        point of the test, and `req` throws headers away."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer " + token})
            resp = conn.getresponse()
            return resp.status, resp.read(), dict(resp.getheaders())
        finally:
            conn.close()


class TestReportAccept(ReportBase):
    """What counts as a report, and what does not."""

    def test_markdown_attaches_and_carries_a_title(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("findings.md", REPORT_MD))
        ref = self.only_report(num)
        self.assertEqual(ref["doc"], "md")
        self.assertEqual(ref["name"], "findings.md")
        self.assertEqual(ref["mime"], "text/markdown; charset=utf-8")
        # the skim line is what the document calls itself, not its filename
        self.assertEqual(ref["title"], "Findings: the drawer scrim")
        self.assertRegex(ref["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(ref["url"].endswith(".md"), ref["url"])
        self.assertTrue(os.path.isfile(ref["path"]))

    def test_html_attaches_and_titles_from_its_title_tag(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("page.html", HOSTILE_HTML))
        ref = self.only_report(num)
        self.assertEqual(ref["doc"], "html")
        self.assertEqual(ref["title"], "Author page")
        self.assertEqual(ref["mime"], "text/html; charset=utf-8")

    def test_the_title_keeps_a_card_number_in_it(self):
        """Markers that WRAP the line get stripped; a `#` inside the words does
        not. "how card #33 shipped" is a title about card #33."""
        num = self.working_card()
        self.post_report(num, self.write_doc(
            "t.md", "# Reports — how card #33 shipped\n\nbody"))
        self.assertEqual(self.only_report(num)["title"],
                         "Reports — how card #33 shipped")

    def test_the_title_drops_a_trailing_closing_hash(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("t.md", "## Findings ##\n\nbody"))
        self.assertEqual(self.only_report(num)["title"], "Findings")

    def test_a_report_with_no_heading_falls_back_to_its_first_line(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("plain.md", "just a sentence\n\nand more"))
        self.assertEqual(self.only_report(num)["title"], "just a sentence")

    def test_same_bytes_are_stored_once(self):
        num = self.working_card()
        a = self.write_doc("one.md", REPORT_MD)
        b = self.write_doc("two.md", REPORT_MD)
        self.post_report(num, a)
        first = self.only_report(num)
        self.post_report(num, b)
        second = self.only_report(num)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["path"], second["path"])

    def test_images_door_still_refuses_text(self):
        """`images:` stays png/jpeg only — nothing that ever worked starts
        accepting documents because reports arrived."""
        status, body = self.post("/api/cards", {
            "images": [base64.b64encode(REPORT_MD.encode()).decode()]})
        self.assertEqual(status, 400, body)

    def test_a_wrong_extension_is_not_a_report(self):
        num = self.working_card()
        path = self.write_doc("notes.txt", "# not a report")
        status, body = self.post("/api/cards/%d/events" % num,
                                 {"kind": "chat",
                                  "payload": {"text": "x", "reports": [path]}})
        # an unusable path degrades to a name, never to a stored document
        self.assertEqual(status, 201, body)
        docs = [a for a in (self.last_payload(num).get("attachments") or [])
                if a.get("doc")]
        self.assertEqual(docs, [])

    def test_extension_is_the_whole_sniff_on_the_direct_door(self):
        status, body = self.post("/api/cards",
                                 {"text": "x", "reports": [{"name": "notes.txt",
                                                            "text": "# hi"}]})
        self.assertEqual(status, 400, body)
        self.assertIn("md", json.dumps(body))

    def test_a_binary_wearing_a_md_name_is_refused(self):
        status, body = self.post("/api/cards", {"text": "x", "reports": [
            {"name": "sneaky.md",
             "data": base64.b64encode(b"MZ\x00\x00\x90binary").decode()}]})
        self.assertEqual(status, 400, body)
        self.assertIn("NUL", json.dumps(body))

    def test_invalid_utf8_is_refused(self):
        status, body = self.post("/api/cards", {"text": "x", "reports": [
            {"name": "bad.md", "data": base64.b64encode(b"\xff\xfe\xfd\xfc").decode()}]})
        self.assertEqual(status, 400, body)
        self.assertIn("UTF-8", json.dumps(body))

    def test_an_empty_report_is_refused(self):
        status, body = self.post("/api/cards", {"text": "x", "reports": [
            {"name": "empty.md", "text": ""}]})
        self.assertEqual(status, 400, body)

    def test_size_cap_is_enforced_with_a_413(self):
        big = "x" * (sprintd.REPORT_MAX_BYTES + 1)
        status, body = self.post("/api/cards", {"text": "x", "reports": [
            {"name": "huge.md", "text": big}]})
        self.assertEqual(status, 413, body)
        self.assertIn("exceed", json.dumps(body))

    def test_just_under_the_cap_is_accepted(self):
        ok = "x" * (sprintd.REPORT_MAX_BYTES - 16)
        status, body = self.post("/api/cards", {"text": "x", "reports": [
            {"name": "big.md", "text": ok}]})
        self.assertEqual(status, 201, body)

    def test_a_report_alone_is_a_submission(self):
        """A document with no words is still work dropped on the board — the
        card's title becomes the document's."""
        status, card = self.post("/api/cards", {"reports": [
            {"name": "findings.md", "text": REPORT_MD}]})
        self.assertEqual(status, 201, card)
        self.assertEqual(card["title"], "Findings: the drawer scrim")

    def test_a_report_lands_on_the_sidebar_too(self):
        status, body = self.post("/api/sidebar", {
            "text": "wrote this up", "actor": "session",
            "reports": [{"name": "notes.md", "text": "# Notes\n\nbody"}]})
        self.assertEqual(status, 201, body)
        _, board = self.get("/api/board")
        docs = []
        for ev in board["sidebar"]:
            docs += [a for a in (ev["payload"].get("attachments") or []) if a.get("doc")]
        self.assertEqual(len(docs), 1, docs)


class TestReportRenderIsInert(ReportBase):
    """FALSIFICATION TARGET.

    The claim under test is not "we sanitize markdown" — it is that no author
    markup is ever TREATED as markup. Markdown is rendered out of text that was
    escaped before a single tag existed, so a `<script>` in a .md comes back as
    the characters `<script>`. Break `esc` and every assertion below fails."""

    def rendered(self, text, name="hostile.md"):
        status, card = self.post("/api/cards", {"text": "x", "reports": [
            {"name": name, "text": text}]})
        self.assertEqual(status, 201, card)
        ref = [a for a in card["attachments"] if a.get("doc")][0]
        status, detail = self.get("/api/reports/%s.md" % ref["sha256"])
        self.assertEqual(status, 200, detail)
        return detail["html"]

    def test_a_script_tag_in_a_md_renders_as_characters_not_a_script(self):
        html = self.rendered(HOSTILE_MD)
        # the words survive — the report is still readable
        self.assertIn("window.__pwned", html)
        # ...but no browser will ever run them
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("</script", html.lower())
        self.assertIn("&lt;script&gt;", html)

    def parse(self, html):
        """Every tag and attribute the browser would actually SEE.

        Substring assertions are not enough here: `onerror=` legitimately
        appears inside `&lt;img src=x onerror=&quot;…&quot;&gt;`, which is inert
        text. The honest question is structural — what markup does a parser find
        — so this is the assertion that cannot be satisfied by accident."""
        seen = []

        class P(html_parser.HTMLParser):
            def handle_starttag(_self, tag, attrs):
                seen.append((tag, dict(attrs)))
            handle_startendtag = handle_starttag

        P(convert_charrefs=True).feed(html)
        return seen

    # Everything the renderer is allowed to emit. Author markup is not on it,
    # and cannot get on it: the text is escaped before a tag exists.
    ALLOWED_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr",
                    "ul", "ol", "li", "pre", "code", "blockquote",
                    "table", "thead", "tbody", "tr", "th", "td",
                    "a", "img", "strong", "em", "del"}

    def test_a_script_tag_never_becomes_a_tag(self):
        tags = [t for t, _ in self.parse(self.rendered(HOSTILE_MD))]
        self.assertNotIn("script", tags)
        self.assertNotIn("iframe", tags)
        self.assertNotIn("div", tags)

    def test_only_our_own_tags_are_ever_emitted(self):
        for tag, _ in self.parse(self.rendered(HOSTILE_MD)):
            self.assertIn(tag, self.ALLOWED_TAGS, "renderer emitted <%s>" % tag)

    def test_no_event_handler_attribute_survives_as_an_attribute(self):
        for tag, attrs in self.parse(self.rendered(HOSTILE_MD)):
            for name in attrs:
                self.assertFalse(name.startswith("on"),
                                 "<%s> carries %s" % (tag, name))

    def test_no_attribute_value_is_ever_a_script_url(self):
        for tag, attrs in self.parse(self.rendered(HOSTILE_MD)):
            for name, value in attrs.items():
                v = (value or "").strip().lower().replace("\\t", "").replace("\\n", "")
                self.assertFalse(v.startswith("javascript:"), "<%s %s>" % (tag, name))
                self.assertFalse(v.startswith("vbscript:"), "<%s %s>" % (tag, name))
                self.assertFalse(v.startswith("data:text/html"), "<%s %s>" % (tag, name))

    def test_the_hostile_markup_is_still_READABLE_as_text(self):
        """Escaping is not deletion. A report about XSS has to be able to say
        the word `<script>` and have you see it."""
        html = self.rendered(HOSTILE_MD)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;iframe", html)
        self.assertIn("window.__pwned", html)

    def test_a_javascript_link_is_dropped_but_its_words_are_kept(self):
        html = self.rendered(HOSTILE_MD)
        hrefs = [a.get("href") for t, a in self.parse(html) if t == "a"]
        self.assertEqual(hrefs, ["https://example.com"])
        # the bad link's WORDS survive, without the stray bracket
        self.assertIn("beside a bad one", html)
        self.assertNotIn("bad one)", html)
        srcs = [a.get("src") for t, a in self.parse(html) if t == "img"]
        self.assertEqual(srcs, [])
        self.assertIn("and an image.", html)

    def test_a_code_span_containing_a_tag_is_still_inert(self):
        html = self.rendered(HOSTILE_MD)
        self.assertIn("<code>&lt;script&gt;inline&lt;/script&gt;</code>", html)

    def test_an_ampersand_is_escaped_exactly_once(self):
        html = self.rendered("a & b, and &lt; too")
        self.assertIn("a &amp; b", html)
        self.assertIn("&amp;lt;", html)

    def test_every_link_we_do_emit_is_defanged(self):
        html = self.rendered("[x](https://example.com)")
        self.assertIn('rel="noreferrer noopener nofollow"', html)
        self.assertIn('target="_blank"', html)


class TestReportRenderShape(ReportBase):
    """The renderer is small on purpose, but it has to actually render a report:
    a document that comes back as one paragraph is not readable."""

    def html_of(self, text):
        status, card = self.post("/api/cards", {"text": "x", "reports": [
            {"name": "doc.md", "text": text}]})
        self.assertEqual(status, 201, card)
        ref = [a for a in card["attachments"] if a.get("doc")][0]
        _, detail = self.get("/api/reports/%s.md" % ref["sha256"])
        return detail["html"]

    def test_the_whole_subset_renders(self):
        html = self.html_of(REPORT_MD)
        self.assertIn("<h1>Findings: the drawer scrim</h1>", html)
        self.assertIn("<h2>Numbers</h2>", html)
        self.assertIn("<strong>too dark</strong>", html)
        self.assertIn("<code>--scrim</code>", html)
        self.assertIn("<ul>", html)
        self.assertIn("<li>the token is <code>--scrim</code></li>", html)
        self.assertIn("<table>", html)
        self.assertIn("<th>width</th>", html)
        self.assertIn("<td>too dark</td>", html)
        self.assertIn("<blockquote>", html)
        self.assertIn('<pre><code class="lang-css">', html)
        self.assertIn('<a href="https://example.com/spec"', html)

    def test_nested_lists_nest(self):
        html = self.html_of("- outer\n    - inner\n- back")
        self.assertIn("<ul>", html)
        self.assertGreaterEqual(html.count("<ul>"), 2)
        self.assertIn("inner", html)

    def test_an_ordered_list_is_ordered(self):
        html = self.html_of("1. first\n2. second")
        self.assertIn("<ol>", html)
        self.assertIn("<li>first</li>", html)

    def test_a_fenced_block_is_never_re_scanned_for_markup(self):
        html = self.html_of("```\n**not bold** and <b>not bold</b>\n```")
        self.assertNotIn("<strong>", html)
        self.assertIn("&lt;b&gt;", html)

    def test_a_wrapped_list_item_stays_ONE_item(self):
        """A real report wraps its prose. Before this, a wrapped item closed the
        list and the next number restarted at 1."""
        html = self.html_of(
            "1. **First.** a line that keeps going\n"
            "   onto a second source line\n"
            "2. **Second.** and this one\n"
            "   also wraps\n")
        self.assertEqual(html.count("<ol>"), 1, html)
        self.assertEqual(html.count("<li>"), 2, html)
        self.assertIn("onto a second source line</li>", html)

    def test_a_wrapped_paragraph_is_one_paragraph_not_a_stack_of_breaks(self):
        """The author's 80-column wrap is not a design decision about the rail,
        which is 480px wide."""
        html = self.html_of("one line\nand its continuation\nand a third")
        self.assertIn("<p>one line and its continuation and a third</p>", html)
        self.assertNotIn("<br>", html)

    def test_two_trailing_spaces_still_mean_a_hard_break(self):
        html = self.html_of("first line  \nsecond line")
        self.assertIn("first line<br>second line", html)

    def test_a_trailing_backslash_is_a_hard_break_too(self):
        html = self.html_of("first line\\\nsecond line")
        self.assertIn("first line<br>second line", html)

    def test_a_paragraph_still_stops_at_the_next_block(self):
        html = self.html_of("some prose\n## A heading\nmore prose")
        self.assertIn("<p>some prose</p>", html)
        self.assertIn("<h2>A heading</h2>", html)

    def test_control_bytes_are_refused(self):
        """\x00 and \x01 are the renderer's own placeholders — a document must
        never be able to carry them."""
        for bad in (b"# ok\x00", b"# ok\x01"):
            status, body = self.post("/api/cards", {"text": "x", "reports": [
                {"name": "c.md", "data": base64.b64encode(bad).decode()}]})
            self.assertEqual(status, 400, body)

    def test_tabs_and_newlines_are_still_fine(self):
        status, body = self.post("/api/cards", {"text": "x", "reports": [
            {"name": "t.md", "text": "# ok\n\n\tindented\r\n"}]})
        self.assertEqual(status, 201, body)

    def test_a_horizontal_rule(self):
        self.assertIn("<hr>", self.html_of("above\n\n---\n\nbelow"))


class TestReportServing(ReportBase):
    """How the bytes come back — and why author HTML can never touch the board."""

    def stored(self, name, text):
        status, card = self.post("/api/cards", {"text": "x", "reports": [
            {"name": name, "text": text}]})
        self.assertEqual(status, 201, card)
        return [a for a in card["attachments"] if a.get("doc")][0]

    def test_markdown_comes_back_as_markdown(self):
        ref = self.stored("doc.md", REPORT_MD)
        status, blob, headers = self.raw(ref["url"])
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers.get("Content-Type", ""))
        self.assertIn(b"# Findings", blob)

    def test_author_html_is_served_under_a_sandbox_csp(self):
        ref = self.stored("page.html", HOSTILE_HTML)
        status, blob, headers = self.raw(ref["url"])
        self.assertEqual(status, 200)
        csp = headers.get("Content-Security-Policy", "")
        # `sandbox` with no allow-list: unique opaque origin, scripts off. Even a
        # direct navigation to this URL cannot read a cookie or reach the API.
        self.assertIn("sandbox", csp)
        self.assertNotIn("allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_markdown_gets_no_needless_csp_but_is_not_html(self):
        ref = self.stored("doc.md", REPORT_MD)
        _, _, headers = self.raw(ref["url"])
        self.assertNotIn("text/html", headers.get("Content-Type", ""))

    def test_author_html_is_never_inlined_into_the_boards_dom(self):
        ref = self.stored("page.html", HOSTILE_HTML)
        status, detail = self.get("/api/reports/%s.html" % ref["sha256"])
        self.assertEqual(status, 200, detail)
        self.assertTrue(detail["sandboxed"])
        self.assertIn("raw_url", detail)
        # the ONE thing that must not be there: pre-rendered markup to innerHTML
        self.assertNotIn("html", detail)

    def test_serving_needs_the_token(self):
        ref = self.stored("doc.md", REPORT_MD)
        status, _, _ = self.raw(ref["url"], token="wrong")
        self.assertEqual(status, 401)

    def test_an_unknown_report_is_a_404(self):
        status, _ = self.get("/api/reports/%s.md" % ("a" * 64))
        self.assertEqual(status, 404)


class TestReportsIndex(ReportBase):
    """The library, and the ONE number the header link reads.

    User ruling, verbatim: "link should only appear once there's a report
    within the sprint" — so the default scope is the OPEN sprint and nothing
    else."""

    def test_an_empty_sprint_has_no_reports_and_says_so_on_the_board(self):
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 0)
        _, index = self.get("/api/reports")
        self.assertEqual(index["count"], 0)
        self.assertEqual(index["reports"], [])

    def test_the_index_names_the_source_card_author_and_date(self):
        num = self.working_card("the card that owns the report")
        self.post("/api/cards/%d/state" % num, {"state": "in_progress",
                                                "title": "Drawer scrim too dark"})
        self.post_report(num, self.write_doc("findings.md", REPORT_MD))
        _, index = self.get("/api/reports")
        self.assertEqual(index["count"], 1)
        row = index["reports"][0]
        self.assertEqual(row["title"], "Findings: the drawer scrim")
        self.assertEqual(row["card_num"], num)
        self.assertEqual(row["card_title"], "Drawer scrim too dark")
        self.assertEqual(row["actor"], "worker")
        self.assertTrue(row["ts"])
        self.assertTrue(row["view_url"].startswith("#/report/"))

    def test_the_board_count_matches_the_index(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("a.md", "# A\n\nbody"))
        self.post_report(num, self.write_doc("b.md", "# B\n\nbody"))
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 2)

    def test_one_document_counts_once_however_often_it_is_posted(self):
        num = self.working_card()
        path = self.write_doc("same.md", "# Same\n\nbody")
        self.post_report(num, path)
        self.post_report(num, path, text="posting it again")
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 1)

    def test_the_index_is_scoped_to_the_OPEN_sprint(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("old.md", "# Last sprint\n\nbody"))
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 1)

        status, _ = self.post("/api/sprint", {"action": "close"})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/sprint", {"action": "open", "title": "round two"})
        self.assertEqual(status, 200)

        # a fresh sprint: the link must NOT be there, and the library is empty
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 0)
        _, index = self.get("/api/reports")
        self.assertEqual(index["count"], 0)

        # history is still reachable — it is just never what decides the link
        _, allof = self.get("/api/reports?scope=all")
        self.assertEqual(allof["count"], 1)
        self.assertEqual(allof["reports"][0]["title"], "Last sprint")

    def test_a_report_in_the_new_sprint_brings_the_link_back(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("old.md", "# Last sprint\n\nbody"))
        self.post("/api/sprint", {"action": "close"})
        self.post("/api/sprint", {"action": "open", "title": "round two"})
        fresh = self.working_card("a new card in the new sprint")
        self.post_report(fresh, self.write_doc("new.md", "# This sprint\n\nbody"))
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 1)
        _, index = self.get("/api/reports")
        self.assertEqual([r["title"] for r in index["reports"]], ["This sprint"])

    def test_newest_first(self):
        num = self.working_card()
        self.post_report(num, self.write_doc("a.md", "# First\n\nbody"))
        time.sleep(0.01)
        self.post_report(num, self.write_doc("b.md", "# Second\n\nbody"))
        _, index = self.get("/api/reports")
        self.assertEqual([r["title"] for r in index["reports"]], ["Second", "First"])

    def test_the_index_needs_the_token(self):
        status, _ = self.get("/api/reports", token=None)
        self.assertEqual(status, 401)


class TestReportsInAnEvidencePacket(ReportBase):
    """A packet is a document envelope as much as a screenshot one."""

    def test_a_packet_reports_field_is_ingested_and_indexed(self):
        num = self.working_card()
        path = self.write_doc("findings.md", REPORT_MD)
        packet = dict(GOOD_PACKET)
        packet["reports"] = [path]
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": packet})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "ready")

        _, detail = self.get("/api/cards/%d" % num)
        refs = detail["evidence"]["packet"]["reports"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["doc"], "md")
        self.assertEqual(refs[0]["title"], "Findings: the drawer scrim")

        _, index = self.get("/api/reports")
        self.assertEqual(index["count"], 1)
        self.assertEqual(index["reports"][0]["card_num"], num)

    def test_a_packet_without_reports_still_works(self):
        num = self.working_card()
        status, body = self.post("/api/cards/%d/ready" % num, {"packet": dict(GOOD_PACKET)})
        self.assertEqual(status, 200, body)
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 0)


class TestSprintPostReportFlag(ReportBase):
    """`sprint-post <num> chat "summary" --report path.md` — the worker's door."""

    SPRINT_POST = os.path.join(os.path.dirname(HERE), "bin", "sprint-post")
    SPRINT_READY = os.path.join(os.path.dirname(HERE), "bin", "sprint-ready")

    def run_post(self, *argv):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN="test-token")
        return subprocess.run([sys.executable, self.SPRINT_POST] + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)

    def run_ready(self, *argv):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN="test-token")
        return subprocess.run([sys.executable, self.SPRINT_READY] + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)

    def test_the_flag_attaches_the_document(self):
        num = self.working_card()
        path = self.write_doc("findings.md", REPORT_MD)
        r = self.run_post(num, "chat", "findings are in the report", "--report", path)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("1 report", r.stdout.decode())
        ref = self.only_report(num)
        self.assertEqual(ref["title"], "Findings: the drawer scrim")
        _, board = self.get("/api/board")
        self.assertEqual(board["reports"], 1)

    def test_the_flag_repeats(self):
        num = self.working_card()
        a = self.write_doc("a.md", "# A\n\nbody")
        b = self.write_doc("b.html", "<title>B</title><p>body</p>")
        r = self.run_post(num, "chat", "two of them", "--report", a, "--report", b)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        docs = [x for x in self.last_payload(num)["attachments"] if x.get("doc")]
        self.assertEqual(sorted(d["doc"] for d in docs), ["html", "md"])

    def test_a_missing_file_fails_before_the_network(self):
        num = self.working_card()
        r = self.run_post(num, "chat", "x", "--report", os.path.join(self.tmp, "nope.md"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("report", r.stderr.decode())
        self.assertIn("no such file", r.stderr.decode())

    def test_a_wrong_extension_fails_by_name(self):
        num = self.working_card()
        r = self.run_post(num, "chat", "x", "--report", self.write_doc("n.txt", "hi"))
        self.assertEqual(r.returncode, 2)
        self.assertIn(".md", r.stderr.decode())

    def test_an_oversized_report_fails_before_the_network(self):
        num = self.working_card()
        path = self.write_doc("huge.md", "x" * (2 * 1024 * 1024 + 4))
        r = self.run_post(num, "chat", "x", "--report", path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("ceiling", r.stderr.decode())

    def test_a_phase_refuses_a_report(self):
        """A phase says what you are doing right now. A document is not that."""
        num = self.working_card()
        r = self.run_post(num, "phase", "testing", "--report",
                          self.write_doc("a.md", "# A"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("report", r.stderr.decode())

    def test_sprint_ready_carries_reports_through(self):
        num = self.working_card()
        packet = dict(GOOD_PACKET)
        packet["reports"] = [self.write_doc("findings.md", REPORT_MD)]
        pfile = os.path.join(self.tmp, "packet.json")
        with open(pfile, "w", encoding="utf-8") as fh:
            json.dump(packet, fh)
        r = self.run_ready(num, pfile)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        _, index = self.get("/api/reports")
        self.assertEqual(index["count"], 1)

    def test_sprint_ready_names_a_bad_report_entry(self):
        num = self.working_card()
        packet = dict(GOOD_PACKET)
        packet["reports"] = ["/tmp/notes.txt"]
        pfile = os.path.join(self.tmp, "packet.json")
        with open(pfile, "w", encoding="utf-8") as fh:
            json.dump(packet, fh)
        r = self.run_ready(num, pfile)
        self.assertEqual(r.returncode, 2)
        self.assertIn("reports", r.stderr.decode())


class TestMarkdownIsTheRecommendedFormat(ReportBase):
    """User verbatim on the bounce: "let's also advise agents that md reports
    are preferable to html. our html rendering isn't great."

    HTML support does NOT go away — a document that arrives already-HTML still
    has a home. What changes is what an agent is told: every surface an agent
    reads says write .md, and both helpers say it out loud when an .html report
    goes by. A notice on stderr, never an error: the report still posts."""

    SPRINT_POST = os.path.join(os.path.dirname(HERE), "bin", "sprint-post")
    SPRINT_READY = os.path.join(os.path.dirname(HERE), "bin", "sprint-ready")
    ROOT = os.path.dirname(HERE)

    def run_post(self, *argv):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN="test-token")
        return subprocess.run([sys.executable, self.SPRINT_POST] + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)

    def run_ready(self, *argv):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN="test-token")
        return subprocess.run([sys.executable, self.SPRINT_READY] + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)

    def read_repo_file(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    # --- the nudge is a notice, not a gate -------------------------------

    def test_an_html_report_still_posts(self):
        num = self.working_card()
        path = self.write_doc("legacy.html", "<title>Legacy</title><p>body</p>")
        r = self.run_post(num, "chat", "an already-HTML document", "--report", path)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(self.only_report(num)["doc"], "html")

    def test_an_html_report_prints_a_notice_naming_md(self):
        num = self.working_card()
        path = self.write_doc("legacy.html", "<title>Legacy</title><p>body</p>")
        err = self.run_post(num, "chat", "x", "--report", path).stderr.decode()
        self.assertIn("legacy.html", err)
        self.assertIn(".md", err)
        self.assertNotIn("missing required field", err)

    def test_a_markdown_report_is_not_nagged(self):
        num = self.working_card()
        r = self.run_post(num, "chat", "x", "--report",
                          self.write_doc("findings.md", REPORT_MD))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertNotIn("Prefer .md", r.stderr.decode())

    def test_sprint_ready_notices_an_html_report_but_accepts_it(self):
        num = self.working_card()
        packet = dict(GOOD_PACKET)
        packet["reports"] = [self.write_doc("legacy.html",
                                            "<title>Legacy</title><p>body</p>")]
        pfile = os.path.join(self.tmp, "packet.json")
        with open(pfile, "w", encoding="utf-8") as fh:
            json.dump(packet, fh)
        r = self.run_ready(num, pfile)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn(".md", r.stderr.decode())
        _, index = self.get("/api/reports")
        self.assertEqual(index["count"], 1)

    # --- every surface an agent reads says it ----------------------------

    def test_the_helpers_own_help_recommends_md(self):
        for helper in ("sprint-post", "sprint-ready"):
            with open(os.path.join(self.ROOT, "bin", helper), encoding="utf-8") as fh:
                doc = fh.read()
            self.assertIn("refer .md", doc,
                          "%s should tell an agent to prefer .md" % helper)

    def test_the_worker_definition_recommends_md(self):
        doc = self.read_repo_file("agents", "sprint-worker.md")
        self.assertIn("sandboxed", doc)
        self.assertIn("Write `.md`", doc)

    def test_the_spec_and_readme_record_the_preference(self):
        spec = self.read_repo_file("SPEC.md")
        self.assertIn("md reports are preferable to html", spec)
        readme = self.read_repo_file("README.md")
        self.assertIn("sandboxed", readme)
# --------------------------------------------------------------------------
# Killed-agent detection (#42)
#
# The incident: three workers were killed mid-flight by a provider usage limit
# and their cards sat in In motion for hours. `agent_silent` had already said
# "quiet" and had nothing further to say. These tests are about the ending.
# --------------------------------------------------------------------------


class WorkerGoneBase(Base):
    """Thresholds in seconds so a twenty-minute rule is actually testable, and
    the sweep driven by hand so nothing here depends on a timer racing."""

    GONE = 1.0
    DEAD = 2.0
    LONGRUN_FACTOR = 6.0

    def setUp(self):
        super().setUp()
        self.app.worker_gone_seconds = self.GONE
        self.app.worker_dead_seconds = self.DEAD
        self.app.worker_longrun_factor = self.LONGRUN_FACTOR

    # -- fixtures ---------------------------------------------------------

    def working_card(self, text="an agent is on this", agent=None, state="in_progress"):
        """A card with an agent, a worktree and a branch, mid-flight."""
        num = self.new_card(text)["num"]
        agent = agent or "sprint-card-%d" % num
        status, _ = self.post("/api/cards/%d/assign" % num,
                              {"agent_name": agent,
                               "worktree": "/tmp/wt/%s" % agent,
                               "branch": "sprint/card-%d" % num})
        self.assertEqual(status, 200)
        if state == "in_progress":
            status, _ = self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
            self.assertEqual(status, 200)
        return num

    def say_something(self, num, text="still here"):
        status, _ = self.post("/api/cards/%d/events" % num,
                              {"kind": "progress", "payload": {"text": text}})
        self.assertEqual(status, 201)

    def go_quiet(self, num, seconds):
        """Backdate every event on this card so its agent has been quiet that
        long. Cheaper and far more deterministic than sleeping."""
        with self.app.lock:
            self.app.conn.execute(
                "UPDATE events SET ts=ts-? WHERE card_num=?", (seconds, num))
            self.app.conn.execute(
                "UPDATE cards SET updated_at=updated_at-? WHERE num=?", (seconds, num))

    def stuck_events(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e for e in detail["timeline"] if e["kind"] == "stuck"]

    def gone_notices(self, num):
        return [e for e in self.stuck_events(num)
                if e["payload"].get("rule") == "worker_gone"]

    def card(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return detail["card"]


class TestWorkerGoneTiming(WorkerGoneBase):
    def test_the_notice_lands_at_the_first_interval_and_failed_at_the_second(self):
        num = self.working_card()
        self.say_something(num, "reading the card")

        # Inside the first interval: nothing at all. A quiet agent is a working
        # agent until proven otherwise.
        self.go_quiet(num, 0.5)
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        self.assertEqual(self.gone_notices(num), [])
        self.assertEqual(self.state_of(num), "in_progress")

        # Past the first interval: the board says out loud that the worker may
        # be dead, and names it. The card does NOT move yet.
        self.go_quiet(num, 1.0)          # 1.5s quiet, threshold 1.0
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        notices = self.gone_notices(num)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["actor"], "server")
        self.assertEqual(notices[0]["payload"]["stage"], "notice")
        self.assertEqual(notices[0]["payload"]["agent_name"], "sprint-card-%d" % num)
        self.assertIn("sprint-card-%d" % num, notices[0]["payload"]["text"])
        self.assertEqual(self.state_of(num), "in_progress",
                         "the first interval is a warning, not a verdict")

        # Past the second: it stops guessing.
        self.go_quiet(num, 1.0)          # 2.5s quiet, threshold 2.0
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        card = self.card(num)
        self.assertEqual(card["state"], "failed")
        self.assertRegex(card["reason"], r"^worker gone: no events for ")
        self.assertRegex(card["error"], r"^worker gone: no events for ")

    def test_a_worker_that_comes_back_resets_the_clock(self):
        num = self.working_card()
        self.go_quiet(num, 5.0)
        self.say_something(num, "sorry — long build")
        self.assertEqual(self.app.sweep_worker_gone(), 0,
                         "a live worker is never declared dead")
        self.assertEqual(self.state_of(num), "in_progress")

    def test_one_agents_activity_covers_every_card_it_holds(self):
        """The clock is the AGENT's. While it is posting on card A it is
        demonstrably not dead on card B."""
        a = self.working_card("card A", agent="sprint-batch-9")
        b = self.working_card("card B", agent="sprint-batch-9")
        self.go_quiet(a, 5.0)
        self.go_quiet(b, 5.0)
        self.say_something(a, "working through the batch")
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        self.assertEqual(self.state_of(b), "in_progress")

    def test_the_notice_fires_once_per_episode(self):
        num = self.working_card()
        self.go_quiet(num, 1.5)
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        self.assertEqual(self.app.sweep_worker_gone(), 0, "no second notice")
        self.assertEqual(len(self.gone_notices(num)), 1)

    def test_an_agent_that_never_said_a_word_still_gets_caught(self):
        """Killed at dispatch: assigned, never posted. The clock falls back to
        the card's last transition, which is exactly the incident shape."""
        num = self.working_card("never spoke")
        self.go_quiet(num, 2.5)
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        self.assertEqual(self.state_of(num), "failed")

    def test_a_triaging_card_counts_too(self):
        num = self.working_card("died while triaging", state="triaging")
        self.assertEqual(self.state_of(num), "triaging")
        self.go_quiet(num, 2.5)
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        self.assertEqual(self.state_of(num), "failed")


class TestWorkerGoneGuards(WorkerGoneBase):
    """Declaring a working agent dead is worse than waiting, so every one of
    these is a bias toward leaving the card alone."""

    def test_a_card_with_no_agent_is_never_declared_dead(self):
        num = self.new_card("nobody on it")["num"]
        self.post("/api/cards/%d/state" % num, {"state": "triaging"})
        self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        self.go_quiet(num, 60.0)
        # Both halves: the predicate says why, and the sweep's own query agrees.
        # They are separate guards and either one alone would let this through.
        self.assertEqual(self.app.worker_gone_exempt(self.app.card_row(num)),
                         "no_agent")
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        self.assertEqual(self.state_of(num), "in_progress")
        self.assertEqual(self.gone_notices(num), [])

    def test_an_external_agent_card_is_never_declared_dead(self):
        """Somebody else's process on somebody else's clock. The board has no
        standing to call it dead and no way to restart it."""
        num = self.working_card("run by something else")
        self.go_quiet(num, 60.0)
        # Before the flag, this card WOULD be escalated — which is what makes
        # the flag the thing under test rather than an accident of the fixture.
        self.assertIsNone(self.app.worker_gone_exempt(self.app.card_row(num)))
        status, body = self.post("/api/cards/%d/action" % num,
                                 {"action": "external_agent",
                                  "note": "a cron job owns this one"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["card"]["external_agent"])
        self.go_quiet(num, 60.0)
        self.assertEqual(self.app.worker_gone_exempt(self.app.card_row(num)),
                         "external_agent")
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        self.assertEqual(self.state_of(num), "in_progress")

    def test_a_flag_this_database_has_never_heard_of_degrades_to_false(self):
        """`card_flag` is how a guard survives a column that landed in another
        change and has not reached this database yet — absence must read as
        "no such flag", never as a crash on every sweep tick."""
        num = self.working_card("nothing exotic about this one")
        row = self.app.card_row(num)
        self.assertFalse(self.app.card_flag(row, "no_such_column_anywhere"))
        self.assertNotIn("no_such_column_anywhere", self.app.columns("cards"))
        self.assertIn("external_agent", self.app.columns("cards"))

    def test_a_long_running_card_is_safe_inside_its_window_and_not_outside_it(self):
        num = self.working_card("full suite, ~30 min")
        status, body = self.post("/api/cards/%d/events" % num,
                                 {"kind": "note", "long_running": True,
                                  "payload": {"text": "running the full suite"}})
        self.assertEqual(status, 201, body)
        self.assertTrue(body["card"]["long_running"])

        # Well past the ordinary window, comfortably inside the stretched one.
        self.go_quiet(num, 5.0)          # ordinary fail is 2.0s; stretched is 12.0s
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        self.assertEqual(self.state_of(num), "in_progress")

        # ...but the window ENDS. A flag is not a permanent exemption; an agent
        # killed mid-suite is the exact shape this rule exists for.
        self.go_quiet(num, 10.0)         # 15s quiet vs a 12s stretched threshold
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        self.assertEqual(self.state_of(num), "failed")

    def test_a_live_phase_claim_holds_the_rule_off(self):
        num = self.working_card("declared a long phase")
        status, _ = self.post("/api/cards/%d/events" % num,
                              {"kind": "progress",
                               "payload": {"text": "phase: testing", "phase": "testing",
                                           "expected_seconds": 600}})
        self.assertEqual(status, 201)
        self.go_quiet(num, 5.0)
        self.assertEqual(self.app.worker_gone_exempt(self.app.card_row(num)), "phase")
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        # ...and once the claim runs out, the rule applies as usual.
        self.go_quiet(num, 700.0)
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        self.assertEqual(self.state_of(num), "failed")

    def test_a_needs_you_card_with_an_open_question_is_not_a_dead_worker(self):
        """A worker parked on a question is SUPPOSED to be silent — it ended its
        turn and the user has the ball. The needs_you sweep rule already nags
        the right person about that."""
        num = self.working_card("asked something")
        status, _ = self.post("/api/cards/%d/question" % num,
                              {"text": "which of these did you mean?"})
        self.assertEqual(status, 201)
        self.assertEqual(self.state_of(num), "needs_you")
        self.go_quiet(num, 60.0)
        self.assertEqual(self.app.worker_gone_exempt(self.app.card_row(num)),
                         "open_question")
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        self.assertEqual(self.state_of(num), "needs_you")

    def test_a_ready_card_is_not_the_workers_problem(self):
        num = self.working_card("finished and waiting on a verdict")
        status, body = self.post("/api/cards/%d/ready" % num,
                                 {"packet": dict(GOOD_PACKET,
                                                 branch="sprint/card-%d" % num)})
        self.assertEqual(status, 200, body)
        self.go_quiet(num, 60.0)
        self.assertEqual(self.app.sweep_worker_gone(), 0)
        self.assertEqual(self.state_of(num), "ready",
                         "a ready card is waiting on the USER, not on its agent")


class TestWorkerGoneFailedPreservesEverything(WorkerGoneBase):
    def failed_card_with_history(self):
        num = self.working_card("has a real history")
        self.say_something(num, "read the CSS, found the misaligned flex item")
        status, body = self.post("/api/cards/%d/ready" % num,
                                 {"packet": dict(GOOD_PACKET,
                                                 branch="sprint/card-%d" % num)})
        self.assertEqual(status, 200, body)
        status, _ = self.post("/api/cards/%d/verdict" % num,
                              {"verdict": "bounce", "notes": "still overlaps at 980px"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "in_progress")
        self.go_quiet(num, 30.0)
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        return num

    def test_failed_keeps_the_timeline_evidence_branch_and_worktree(self):
        num = self.failed_card_with_history()
        _, detail = self.get("/api/cards/%d" % num)
        card = detail["card"]
        self.assertEqual(card["state"], "failed")
        self.assertEqual(card["branch"], "sprint/card-%d" % num)
        self.assertEqual(card["worktree"], "/tmp/wt/sprint-card-%d" % num)
        self.assertEqual(card["agent_name"], "sprint-card-%d" % num)
        self.assertEqual(card["evidence"]["packet"]["claim"], GOOD_PACKET["claim"],
                         "the evidence a Retry needs as its brief is still there")
        texts = [str((e.get("payload") or {}).get("text", "")) for e in detail["timeline"]]
        self.assertTrue(any("misaligned flex item" in t for t in texts),
                        "the whole timeline survives — it IS the retry brief")
        self.assertTrue(any(e["kind"] == "evidence" for e in detail["timeline"]))
        self.assertTrue(any(e["kind"] == "verdict" for e in detail["timeline"]))

    def test_retry_from_a_worker_gone_failure_requeues_with_the_timeline_intact(self):
        num = self.failed_card_with_history()
        before = len(self.get("/api/cards/%d" % num)[1]["timeline"])
        status, body = self.post("/api/cards/%d/action" % num, {"action": "retry"})
        self.assertEqual(status, 200, body)
        _, detail = self.get("/api/cards/%d" % num)
        card = detail["card"]
        self.assertEqual(card["state"], "queued")
        self.assertIsNone(card["agent_name"], "nobody SendMessages a dead agent")
        self.assertIsNone(card["worktree"])
        self.assertEqual(card["branch"], "sprint/card-%d" % num,
                         "the branch is where the dead agent's commits are")
        self.assertEqual(card["evidence"]["packet"]["claim"], GOOD_PACKET["claim"])
        self.assertGreater(len(detail["timeline"]), before)
        retry = [e for e in detail["timeline"] if (e["payload"] or {}).get("retry")]
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["payload"]["previous_agent"], "sprint-card-%d" % num)

    def test_the_failed_card_shows_the_machine_reason_and_a_retry(self):
        """What the user actually sees: the face carries the reason, and the
        card is in the one state whose menu offers Retry."""
        num = self.failed_card_with_history()
        _, board = self.get("/api/board")
        card = {c["num"]: c for c in board["cards"]}[num]
        self.assertEqual(card["state"], "failed")
        self.assertIn("worker gone", card["error"])
        self.assertIn("no events for", card["error"])


class TestWorkerGoneIsWired(WorkerGoneBase):
    """The rule is only worth anything if the timer thread actually runs it."""

    START_BACKGROUND = True

    def setUp(self):
        super().setUp()

    def test_the_background_sweep_escalates_without_anyone_calling_it(self):
        # the sweep thread is already running with the real (long) thresholds
        # baked in at construction; re-point them and let the tick do the work
        self.app.sweep_tick = 0.2
        num = self.working_card("nobody is going to call the sweep by hand")
        self.go_quiet(num, 30.0)
        deadline = time.time() + 20
        while time.time() < deadline and self.state_of(num) != "failed":
            time.sleep(0.2)
        self.assertEqual(self.state_of(num), "failed",
                         "the sweep loop must run this rule, not just define it")

    def test_the_board_ambers_a_card_whose_worker_may_be_gone(self):
        num = self.working_card("about to go quiet")
        self.go_quiet(num, 1.5)
        self.assertEqual(self.app.sweep_worker_gone(), 1)
        _, board = self.get("/api/board")
        self.assertTrue({c["num"]: c for c in board["cards"]}[num]["stuck"],
                        "the age text is the one thing on a face already about "
                        "time passing — that is where the amber goes")


# --------------------------------------------------------------------------
# Model at dispatch (#41)
# --------------------------------------------------------------------------


class TestDispatchModel(Base):
    def assign(self, num, **extra):
        body = {"agent_name": "sprint-card-%d" % num, "worktree": "/tmp/wt",
                "branch": "sprint/card-%d" % num}
        body.update(extra)
        status, out = self.post("/api/cards/%d/assign" % num, body)
        self.assertEqual(status, 200, out)
        return out

    def test_assign_records_the_model_and_the_board_says_what_the_default_is(self):
        num = self.new_card("dispatched after a fallback")["num"]
        self.assign(num, model="opus")
        card = self.get("/api/cards/%d" % num)[1]["card"]
        self.assertEqual(card["model"], "opus")
        self.assertEqual(card["default_model"], sprintd.DEFAULT_MODEL)
        _, board = self.get("/api/board")
        self.assertEqual(board["default_model"], sprintd.DEFAULT_MODEL)
        self.assertEqual({c["num"]: c for c in board["cards"]}[num]["model"], "opus")

    def test_a_card_dispatched_on_the_default_carries_nothing_to_shout_about(self):
        num = self.new_card("ordinary dispatch")["num"]
        self.assign(num, model=sprintd.DEFAULT_MODEL)
        card = self.get("/api/cards/%d" % num)[1]["card"]
        self.assertEqual(card["model"], card["default_model"],
                         "the face shows the model only when it is the exception")

    def test_assign_without_a_model_leaves_it_unset_and_never_clears_it(self):
        num = self.new_card("model set once, then a plain re-assign")["num"]
        self.assign(num)
        self.assertIsNone(self.get("/api/cards/%d" % num)[1]["card"]["model"])
        self.assign(num, model="sonnet")
        self.assign(num, agent_name="sprint-card-%d" % num)
        self.assertEqual(self.get("/api/cards/%d" % num)[1]["card"]["model"], "sonnet")

    def test_the_fallback_is_readable_in_the_timeline(self):
        num = self.new_card("fable ran out")["num"]
        self.assign(num, model="opus")
        _, detail = self.get("/api/cards/%d" % num)
        notes = [e for e in detail["timeline"]
                 if e["kind"] == "note" and (e["payload"] or {}).get("model")]
        self.assertEqual(len(notes), 1)
        self.assertIn("on opus", notes[0]["payload"]["text"])

    def test_a_junk_model_is_a_named_400(self):
        num = self.new_card("junk")["num"]
        status, body = self.post("/api/cards/%d/assign" % num,
                                 {"agent_name": "a", "worktree": "/tmp", "branch": "b",
                                  "model": 7})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "model")

    def test_a_board_that_predates_the_column_gets_it_on_open(self):
        """CREATE TABLE IF NOT EXISTS never migrates a live database. A board
        that has been up since before this shipped must not 500 on every read."""
        old = os.path.join(self.tmp, "old-project")
        os.makedirs(os.path.join(old, ".sprint"))
        db = os.path.join(old, ".sprint", "sprint.db")
        import sqlite3 as _sq
        conn = _sq.connect(db)
        # The schema as it was before `model` existed — the real shape of a
        # board that has been up since before this shipped.
        old_schema = sprintd.SCHEMA.replace("  model TEXT,\n", "")
        # The guard is that the replace actually LANDED, so it names the exact
        # line it removed. A bare "model TEXT" also matches the `limits` table's
        # own `model` column, which has nothing to do with this migration.
        self.assertNotIn("  model TEXT,", old_schema)
        conn.executescript(old_schema)
        conn.execute("INSERT INTO sprints(opened_at) VALUES(1.0)")
        conn.execute("INSERT INTO cards(sprint_id, state, title, body, created_at, "
                     "updated_at) VALUES(1,'queued','old card','body',1.0,1.0)")
        conn.commit()
        conn.close()
        before = {r[1] for r in _sq.connect(db).execute("PRAGMA table_info(cards)")}
        self.assertNotIn("model", before)

        app = sprintd.App(old, token="t", log=self.logfh)
        self.addCleanup(app.close)
        self.assertIn("model", app.columns("cards"))
        self.assertIsNone(app.card_json(app.card_row(1), brief=True)["model"])


class TestSprintRecover(Base):
    """`sprint-recover 41 42` — the "inspect before you redo anything" step,
    as one command instead of five. Seeded against a real git repo, because
    the whole value of the thing is that it reports what git actually says."""

    SPRINT_RECOVER = os.path.join(os.path.dirname(HERE), "bin", "sprint-recover")

    def run_recover(self, *argv, token="test-token"):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN=token)
        return subprocess.run([sys.executable, self.SPRINT_RECOVER]
                              + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=90)

    def git(self, cwd, *args):
        import subprocess
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        r = subprocess.run(("git", "-C", cwd) + args, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        return r.stdout.decode()

    def seeded_worktree(self, name, commits=1, dirty=True):
        """A real repo with a `main`, a branch off it, N commits on the branch
        and (optionally) an uncommitted edit — the exact state a killed agent
        leaves behind."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        self.git(path, "init", "-q", "-b", "main")
        with open(os.path.join(path, "README"), "w") as fh:
            fh.write("base\n")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-qm", "base")
        self.git(path, "checkout", "-qb", name)
        for i in range(commits):
            with open(os.path.join(path, "work-%d.txt" % i), "w") as fh:
                fh.write("committed work %d\n" % i)
            self.git(path, "add", "-A")
            self.git(path, "commit", "-qm", "half-finished thing %d" % i)
        if dirty:
            with open(os.path.join(path, "scratch.txt"), "w") as fh:
                fh.write("uncommitted work the dead agent left\n")
        return path

    def dead_card(self, worktree, branch, text="the agent on this died"):
        num = self.new_card(text)["num"]
        status, _ = self.post("/api/cards/%d/assign" % num,
                              {"agent_name": "sprint-card-%d" % num,
                               "worktree": worktree, "branch": branch,
                               "model": "opus"})
        self.assertEqual(status, 200)
        self.post("/api/cards/%d/state" % num, {"state": "in_progress"})
        self.post("/api/cards/%d/events" % num,
                  {"kind": "progress",
                   "payload": {"text": "read the sweep, found the missing guard"}})
        return num

    def test_it_reports_state_branch_worktree_commits_and_dirtiness(self):
        wt = self.seeded_worktree("sprint-card-1", commits=2, dirty=True)
        num = self.dead_card(wt, "sprint-card-1")
        r = self.run_recover(num)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("#%d" % num, out)
        self.assertIn("state: in_progress", out)
        self.assertIn("agent: sprint-card-%d" % num, out)
        self.assertIn("model: opus", out)
        self.assertIn("branch:   sprint-card-1", out)
        self.assertIn(wt, out)
        self.assertIn("commits ahead of main: 2", out)
        self.assertIn("half-finished thing 1", out)
        self.assertIn("dirty: YES", out)
        self.assertIn("scratch.txt", out)
        self.assertIn("read the sweep, found the missing guard", out,
                      "the timeline IS the brief")
        self.assertIn("inspect the branch and the worktree", out,
                      "the point of the header is the instruction, not the data")

    def test_a_clean_branch_with_nothing_on_it_says_so_plainly(self):
        wt = self.seeded_worktree("sprint-card-2", commits=0, dirty=False)
        num = self.dead_card(wt, "sprint-card-2")
        out = self.run_recover(num).stdout.decode()
        self.assertIn("committed NOTHING", out)
        self.assertIn("dirty: no", out)

    def test_a_worktree_that_is_gone_is_a_finding_not_a_crash(self):
        num = self.dead_card(os.path.join(self.tmp, "pruned-already"),
                             "sprint-card-3")
        r = self.run_recover(num)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("GONE", r.stdout.decode())

    def test_several_cards_in_one_go(self):
        a = self.dead_card(self.seeded_worktree("wt-a"), "wt-a", "card A")
        b = self.dead_card(self.seeded_worktree("wt-b"), "wt-b", "card B")
        out = self.run_recover(a, b).stdout.decode()
        self.assertIn("#%d, #%d" % (a, b), out)
        self.assertIn("#%d —" % a, out)
        self.assertIn("#%d —" % b, out)

    def test_the_last_ten_lines_are_the_last_ten(self):
        num = self.dead_card(self.seeded_worktree("wt-c"), "wt-c")
        for i in range(15):
            self.post("/api/cards/%d/events" % num,
                      {"kind": "progress", "payload": {"text": "step number %d" % i}})
        out = self.run_recover(num).stdout.decode()
        self.assertIn("step number 14", out)
        self.assertNotIn("step number 3", out, "only the last ten")

    def test_it_never_writes_anything_to_the_card(self):
        num = self.dead_card(self.seeded_worktree("wt-d"), "wt-d")
        before = self.get("/api/cards/%d" % num)[1]["timeline"]
        self.run_recover(num)
        after = self.get("/api/cards/%d" % num)[1]["timeline"]
        self.assertEqual(len(before), len(after),
                         "safe to run on a live board")

    def test_it_fails_named_without_a_card_number(self):
        r = self.run_recover()
        self.assertEqual(r.returncode, 2)
        self.assertIn("card number", r.stderr.decode())

    def test_a_bad_token_is_a_clear_failure_not_a_traceback(self):
        num = self.dead_card(self.seeded_worktree("wt-e"), "wt-e")
        r = self.run_recover(num, token="wrong")
        self.assertEqual(r.returncode, 2)
        self.assertIn("401", r.stderr.decode())
        self.assertNotIn("Traceback", r.stderr.decode())


# --------------------------------------------------------------------------
# Ops batch (#36-#40): bulk import, actor attribution, ops cards, external
# agents, and the additive-column migration.
# --------------------------------------------------------------------------


class TestBulkCreate(Base):
    """Importing N issues must never flood the board against intent.

    User's live incident: importing a list meant N POSTs, and 14 cards were
    created against intent before there was any chance to say stop -- then 14
    hand-written cancels to undo it. So a bulk import lands HELD (the preview
    IS the hold), all-or-nothing, under a cap.
    """

    def bulk(self, items, **body):
        payload = {"items": items}
        payload.update(body)
        return self.post("/api/cards/bulk", payload)

    def test_bulk_holds_by_default_so_nothing_dispatches(self):
        status, body = self.bulk(["first issue", {"text": "second issue"},
                                  {"text": "third issue"}])
        self.assertEqual(status, 201, body)
        self.assertEqual(body["count"], 3)
        self.assertTrue(body["hold"], "silence means hold -- this is the point")
        self.assertEqual(body["state"], "held")
        for num in body["card_nums"]:
            self.assertEqual(self.state_of(num), "held")
        # ...and nothing is dispatchable: the queue is empty.
        _, board = self.get("/api/board")
        queued = [c for c in board["cards"] if c["state"] == "queued"]
        self.assertEqual(queued, [], "a bulk import must never reach the queue")

    def test_hold_false_is_explicit_opt_in(self):
        status, body = self.bulk(["go now", "and this one"], hold=False)
        self.assertEqual(status, 201, body)
        self.assertFalse(body["hold"])
        for num in body["card_nums"]:
            self.assertEqual(self.state_of(num), "queued")

    def test_over_the_cap_is_a_413_and_creates_nothing(self):
        before = len(self.get("/api/board")[1]["cards"])
        status, body = self.bulk(["issue %d" % i
                                  for i in range(sprintd.BULK_MAX + 1)])
        self.assertEqual(status, 413, body)
        self.assertEqual(body["error"], "too_many")
        self.assertEqual(body["max"], sprintd.BULK_MAX)
        self.assertEqual(len(self.get("/api/board")[1]["cards"]), before)

    def test_at_the_cap_is_allowed(self):
        status, body = self.bulk(["issue %d" % i for i in range(sprintd.BULK_MAX)])
        self.assertEqual(status, 201, body)
        self.assertEqual(body["count"], sprintd.BULK_MAX)

    def test_one_bad_item_creates_nothing_at_all(self):
        """Atomicity is the difference between an import and a mess: a half
        landed import is the same flood-then-clean-up problem, smaller."""
        before = len(self.get("/api/board")[1]["cards"])
        status, body = self.bulk(["fine", "also fine", {"text": "   "},
                                  "would have been fine"])
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "empty_submission")
        self.assertEqual(body["index"], 2)
        after = self.get("/api/board")[1]["cards"]
        self.assertEqual(len(after), before,
                         "not one card may survive a rejected import")

    def test_empty_items_is_a_named_field_error(self):
        status, body = self.post("/api/cards/bulk", {"items": []})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "items")

    def test_bulk_action_cancels_the_whole_import_in_one_call(self):
        """The undo the user had to hand-write fourteen times."""
        _, made = self.bulk(["a", "b", "c", "d"])
        nums = made["card_nums"]
        status, body = self.post("/api/cards/bulk-action",
                                 {"card_nums": nums, "action": "cancel"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["applied"], nums)
        self.assertEqual(body["failed"], [])
        for num in nums:
            self.assertEqual(self.state_of(num), "canceled")

    def test_bulk_action_releases_a_held_import(self):
        _, made = self.bulk(["a", "b"])
        status, body = self.post("/api/cards/bulk-action",
                                 {"card_nums": made["card_nums"],
                                  "action": "release"})
        self.assertEqual(status, 200, body)
        for num in made["card_nums"]:
            self.assertEqual(self.state_of(num), "queued")

    def test_an_unknown_card_stops_the_whole_bulk_action(self):
        _, made = self.bulk(["a", "b"])
        status, body = self.post("/api/cards/bulk-action",
                                 {"card_nums": made["card_nums"] + [99999],
                                  "action": "cancel"})
        self.assertEqual(status, 404, body)
        for num in made["card_nums"]:
            self.assertEqual(self.state_of(num), "held",
                             "a typo in the list moves nothing")

    def test_one_illegal_move_does_not_abandon_the_rest(self):
        """One card that cannot move is not a reason to leave twelve held."""
        _, made = self.bulk(["a", "b", "c"], hold=False)
        first = made["card_nums"][0]
        self.assertEqual(self.post("/api/cards/%d/action" % first,
                                   {"action": "cancel"})[0], 200)
        # Re-park the pile. The canceled one cannot go back on hold (only the
        # user's `reopen` moves a closed card); the other two must still park.
        status, body = self.post("/api/cards/bulk-action",
                                 {"card_nums": made["card_nums"],
                                  "action": "hold"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"])
        self.assertEqual([f["card_num"] for f in body["failed"]], [first])
        self.assertEqual(body["failed"][0]["error"], "illegal_transition")
        self.assertEqual(body["applied"], made["card_nums"][1:])
        self.assertEqual(self.state_of(first), "canceled")
        for num in made["card_nums"][1:]:
            self.assertEqual(self.state_of(num), "held")

    def test_cancelling_an_already_cancelled_card_is_a_no_op(self):
        """Re-running the undo must be safe: the whole point is one call
        instead of fourteen, and a retry after a partial failure is normal."""
        _, made = self.bulk(["a", "b"])
        nums = made["card_nums"]
        self.assertEqual(self.post("/api/cards/bulk-action",
                                   {"card_nums": nums, "action": "cancel"})[0], 200)
        status, body = self.post("/api/cards/bulk-action",
                                 {"card_nums": nums, "action": "cancel"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["applied"], nums)

    def test_bulk_action_rejects_verbs_outside_the_flood_control_set(self):
        _, made = self.bulk(["a"])
        status, body = self.post("/api/cards/bulk-action",
                                 {"card_nums": made["card_nums"],
                                  "action": "retry"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "bad_action")

    def test_bulk_cards_carry_their_text_and_a_submitted_event(self):
        _, made = self.bulk([{"text": "the mailbox reprocess never finished"}])
        num = made["card_nums"][0]
        _, detail = self.get("/api/cards/%d" % num)
        submitted = [e for e in detail["timeline"] if e["kind"] == "submitted"]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["payload"]["text"],
                         "the mailbox reprocess never finished")
        self.assertTrue(submitted[0]["payload"]["hold"])
        self.assertEqual(submitted[0]["payload"]["bulk"], 1)


class TestActorAttribution(Base):
    """Who a card says wrote it.

    User's report: cards the SESSION created over the API showed their
    `submitted` events as the user's -- so `reply_to` claimed a human was
    waiting behind the session's own writing. A bearer-holding script may now
    say who it is; a browser never can, whatever it puts in the body.
    """

    # Headers a browser sets on every fetch and page JS cannot remove. Any one
    # of them is enough on its own.
    BROWSERISH = ({"Origin": "http://127.0.0.1:8377"},
                  {"Sec-Fetch-Site": "same-origin"},
                  {"Sec-Fetch-Mode": "cors"},
                  {"Cookie": "sprint_token_8377=test-token"})

    def submitted_actor(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        ev = [e for e in detail["timeline"] if e["kind"] == "submitted"][0]
        return ev["actor"], ev["payload"].get("reply_to")

    def test_a_script_may_say_it_is_the_session(self):
        status, card = self.post("/api/cards",
                                 {"text": "filed by the session", "actor": "session"})
        self.assertEqual(status, 201, card)
        actor, reply_to = self.submitted_actor(card["num"])
        self.assertEqual(actor, "session")
        self.assertIsNone(reply_to,
                          "nobody is waiting behind the session's own writing")

    def test_a_browser_can_never_claim_to_be_the_session(self):
        """FALSIFICATION: this is the whole security property. Every browser
        marker, one at a time, and the claim is dropped every time."""
        for headers in self.BROWSERISH:
            with self.subTest(headers=headers):
                status, card = self.post(
                    "/api/cards",
                    {"text": "typed by a person", "actor": "session"},
                    headers=headers)
                self.assertEqual(status, 201, card)
                actor, reply_to = self.submitted_actor(card["num"])
                self.assertEqual(actor, "user",
                                 "a browser is the user's hands: %r" % headers)
                self.assertEqual(reply_to, "card:%d" % card["num"])

    def test_no_claim_still_means_user(self):
        """Fails CLOSED: silence is never an upgrade."""
        card = self.new_card("plain submission")
        actor, reply_to = self.submitted_actor(card["num"])
        self.assertEqual(actor, "user")
        self.assertEqual(reply_to, "card:%d" % card["num"])

    def test_the_server_voice_is_never_borrowable(self):
        """`server` is the board's own voice -- agent_silent, stuck. No client
        may wear it, bearer token or not."""
        status, card = self.post("/api/cards",
                                 {"text": "pretending", "actor": "server"})
        self.assertEqual(status, 201, card)
        self.assertEqual(self.submitted_actor(card["num"])[0], "user")

    def test_bulk_import_carries_the_actor_too(self):
        status, body = self.post("/api/cards/bulk",
                                 {"items": ["one", "two"], "actor": "session"})
        self.assertEqual(status, 201, body)
        for num in body["card_nums"]:
            self.assertEqual(self.submitted_actor(num)[0], "session")

    def test_a_browser_bulk_import_is_still_the_user(self):
        status, body = self.post("/api/cards/bulk",
                                 {"items": ["one"], "actor": "session"},
                                 headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 201, body)
        self.assertEqual(self.submitted_actor(body["card_nums"][0])[0], "user")

    def test_the_user_still_owns_verdicts_on_a_session_authored_card(self):
        """Attribution changes who WROTE the card, never who it belongs to. A
        human talking on a session-authored card is still a human waiting."""
        _, card = self.post("/api/cards",
                            {"text": "filed by the session", "actor": "session"})
        num = card["num"]
        status, _ = self.post("/api/cards/%d/chat" % num,
                              {"text": "what is the status here?"},
                              headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 201)
        _, detail = self.get("/api/cards/%d" % num)
        chat = [e for e in detail["timeline"] if e["kind"] == "chat"][-1]
        self.assertEqual(chat["actor"], "user")
        self.assertEqual(chat["payload"]["reply_to"], "card:%d" % num)

    def test_a_browser_cannot_post_worker_telemetry_either(self):
        card = self.new_card("a card")
        self.to_in_progress(card["num"])
        status, body = self.post("/api/cards/%d/chat" % card["num"],
                                 {"text": "on it", "actor": "worker"},
                                 headers={"Origin": "http://127.0.0.1:8377"})
        self.assertEqual(status, 201, body)
        _, detail = self.get("/api/cards/%d" % card["num"])
        chat = [e for e in detail["timeline"] if e["kind"] == "chat"][-1]
        self.assertEqual(chat["actor"], "user")


class TestOpsCards(Base):
    """Non-code work is first-class, with its own shape of proof.

    A mailbox reprocess has no diff, no branch and no preview. Demanding them
    made ops cards either liars or second-class, so `work_kind: "ops"` SWAPS
    the required set -- claim, validate, readback -- rather than relaxing it.
    """

    OPS_PACKET = {
        "work_kind": "ops",
        "claim": "Reprocessed the stuck mailbox backlog.",
        "readback": "$ russ mail reprocess --since 2026-08-14\n"
                    "processed=412 skipped=0 errors=0\nqueue depth now 0",
        "validate": ["Run `russ mail stats`.",
                     "Confirm the queued count reads 0, not 412."],
    }

    def ready_card(self):
        num = self.new_card("reprocess the stuck mailbox")["num"]
        self.to_in_progress(num)
        return num

    def submit(self, num, packet):
        return self.post("/api/cards/%d/ready" % num, {"packet": packet})

    def test_an_ops_packet_is_accepted_without_diff_branch_or_tests(self):
        num = self.ready_card()
        status, body = self.submit(num, dict(self.OPS_PACKET))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(num), "ready")
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["work_kind"], "ops")
        self.assertIn("processed=412",
                      detail["card"]["evidence"]["packet"]["readback"])

    def test_an_ops_packet_without_a_readback_is_rejected(self):
        """The one thing ops work owes: what actually came back."""
        num = self.ready_card()
        packet = dict(self.OPS_PACKET)
        packet.pop("readback")
        status, body = self.submit(num, packet)
        self.assertEqual(status, 422, body)
        self.assertEqual(body["missing"], ["readback"])
        self.assertEqual(self.state_of(num), "in_progress")

    def test_an_ops_packet_still_owes_a_claim_and_validate_steps(self):
        num = self.ready_card()
        packet = dict(self.OPS_PACKET)
        packet.pop("claim")
        packet.pop("validate")
        status, body = self.submit(num, packet)
        self.assertEqual(status, 422, body)
        self.assertEqual(sorted(body["missing"]), ["claim", "validate"])

    def test_a_blank_readback_is_not_a_readback(self):
        num = self.ready_card()
        for empty in ("", "   \n  ", [], ["", "  "], 17, None):
            with self.subTest(readback=empty):
                packet = dict(self.OPS_PACKET, readback=empty)
                status, body = self.submit(num, packet)
                self.assertEqual(status, 422, body)
                self.assertIn("readback", body["missing"])

    def test_a_readback_may_arrive_as_lines(self):
        num = self.ready_card()
        packet = dict(self.OPS_PACKET,
                      readback=["$ russ mail reprocess", "processed=412"])
        status, body = self.submit(num, packet)
        self.assertEqual(status, 200, body)
        _, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["evidence"]["packet"]["readback"],
                         "$ russ mail reprocess\nprocessed=412")

    def test_test_result_is_optional_for_ops_but_still_needs_counts(self):
        num = self.ready_card()
        status, body = self.submit(num, dict(self.OPS_PACKET,
                                             test_result="tests pass"))
        self.assertEqual(status, 422, body)
        self.assertEqual(body["missing"], ["test_result"])
        status, body = self.submit(num, dict(self.OPS_PACKET,
                                             test_cmd="make check",
                                             test_result="3 pass, 0 fail"))
        self.assertEqual(status, 200, body)

    def test_an_ops_packet_that_claims_a_ui_change_still_owes_screenshots(self):
        num = self.ready_card()
        status, body = self.submit(num, dict(self.OPS_PACKET, ui_change=True))
        self.assertEqual(status, 422, body)
        self.assertEqual(body["missing"], ["screenshots"])

    def test_a_code_card_may_not_skip_the_code_fields(self):
        """The gate is swapped, not weakened: default work still owes it all."""
        num = self.ready_card()
        packet = dict(self.OPS_PACKET)
        packet.pop("work_kind")
        status, body = self.submit(num, packet)
        self.assertEqual(status, 422, body)
        self.assertEqual(sorted(body["missing"]),
                         ["branch", "diffstat", "test_cmd", "test_result",
                          "ui_change"])

    def test_assign_may_omit_the_worktree_and_branch_for_ops(self):
        num = self.new_card("reprocess the mailbox")["num"]
        status, body = self.post("/api/cards/%d/assign" % num,
                                 {"agent_name": "russ-ops", "work_kind": "ops"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["card"]["work_kind"], "ops")
        self.assertIsNone(body["card"]["worktree"])
        self.assertIsNone(body["card"]["branch"])
        self.assertEqual(body["card"]["state"], "triaging")

    def test_the_card_remembers_it_is_ops_across_a_bounce(self):
        """A second packet after a bounce is judged by the same rules."""
        num = self.new_card("reprocess the mailbox")["num"]
        self.assertEqual(self.post("/api/cards/%d/assign" % num,
                                   {"agent_name": "russ-ops",
                                    "work_kind": "ops"})[0], 200)
        self.assertEqual(self.post("/api/cards/%d/state" % num,
                                   {"state": "in_progress"})[0], 200)
        bare = dict(self.OPS_PACKET)
        bare.pop("work_kind")            # the packet forgot; the card did not
        status, body = self.submit(num, bare)
        self.assertEqual(status, 200, body)

    def test_a_nonsense_work_kind_on_assign_is_named(self):
        num = self.new_card("something")["num"]
        status, body = self.post("/api/cards/%d/assign" % num,
                                 {"agent_name": "a", "work_kind": "vibes"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "work_kind")


class TestExternalAgentsAndLongRunning(Base):
    """The silence timer, for work it cannot watch.

    `long_running` was worker-only, so when the session ran work as an ordinary
    background agent -- which emits no worker telemetry at all -- nothing could
    turn the clock off and the card re-ambered every five minutes for its whole
    build. Two card actions fix it, and the session's own notes now count as
    activity, because posting one means it just went and looked.
    """

    SILENCE_SECONDS = 1.0
    SILENCE_TICK = 0.15
    START_BACKGROUND = True

    def silent_events(self, num):
        _, detail = self.get("/api/cards/%d" % num)
        return [e for e in detail["timeline"] if e["kind"] == "agent_silent"]

    def await_silence(self, num, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = self.silent_events(num)
            if got:
                return got
            time.sleep(0.1)
        return []

    def test_the_session_can_mark_a_card_long_running(self):
        num = self.new_card("a slow migration")["num"]
        self.to_in_progress(num)
        status, body = self.post("/api/cards/%d/action" % num,
                                 {"action": "long_running", "actor": "session",
                                  "note": "the full suite takes ~40 minutes"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["card"]["long_running"])
        time.sleep(2.0)
        self.assertEqual(self.silent_events(num), [],
                         "a declared long job must not amber")
        _, detail = self.get("/api/cards/%d" % num)
        note = [e for e in detail["timeline"] if e["kind"] == "note"][-1]
        self.assertIn("40 minutes", note["payload"]["text"])

    def test_long_running_can_be_cleared_again(self):
        num = self.new_card("a slow migration")["num"]
        self.to_in_progress(num)
        self.post("/api/cards/%d/action" % num,
                  {"action": "long_running", "actor": "session"})
        status, body = self.post("/api/cards/%d/action" % num,
                                 {"action": "long_running", "value": False,
                                  "actor": "session"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["card"]["long_running"])
        self.assertTrue(self.await_silence(num), "the clock runs again")

    def test_an_external_agent_never_ambers(self):
        """It emits no worker telemetry, so the clock can never be satisfied --
        so it does not run. The session owns checking on it."""
        outside = self.new_card("built by an ordinary background agent")["num"]
        inside = self.new_card("built by a sprint worker")["num"]
        self.to_in_progress(outside)
        self.to_in_progress(inside)
        status, body = self.post("/api/cards/%d/action" % outside,
                                 {"action": "external_agent", "actor": "session",
                                  "note": "russ-codex seat 4"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["card"]["external_agent"])

        self.assertTrue(self.await_silence(inside),
                        "an ordinary card must still amber")
        self.assertEqual(self.silent_events(outside), [])

    def test_a_session_note_counts_as_activity(self):
        """The session investigating a card IS the check agent_silent asked
        for; nagging it again five minutes later is nagging it about work it
        already did."""
        num = self.new_card("a card the session is watching")["num"]
        self.to_in_progress(num)
        self.assertTrue(self.await_silence(num))
        before = len(self.silent_events(num))
        status, _ = self.post("/api/cards/%d/events" % num,
                              {"kind": "note", "actor": "session",
                               "payload": {"text": "checked the seat — it is "
                                                   "mid-build, log is moving"}})
        self.assertEqual(status, 201)
        time.sleep(0.5)
        self.assertEqual(len(self.silent_events(num)), before,
                         "the note reset the clock")
        # ...and it is only a reprieve, not a mute: silence resumes after it.
        deadline = time.time() + 10.0
        while time.time() < deadline and len(self.silent_events(num)) <= before:
            time.sleep(0.1)
        self.assertGreater(len(self.silent_events(num)), before)

    def test_a_session_note_counts_on_an_ASSIGNED_card_too(self):
        """The clock keys on the AGENT, so the agent-wide branch of the
        baseline has to agree with the per-card one."""
        num = self.new_card("assigned to an outside seat")["num"]
        self.assertEqual(self.post("/api/cards/%d/assign" % num,
                                   {"agent_name": "russ-codex-4"})[0], 200)
        self.assertEqual(self.post("/api/cards/%d/state" % num,
                                   {"state": "in_progress"})[0], 200)
        self.assertTrue(self.await_silence(num))
        before = len(self.silent_events(num))
        self.assertEqual(self.post("/api/cards/%d/events" % num,
                                   {"kind": "note", "actor": "session",
                                    "payload": {"text": "pinged the seat, "
                                                        "it is alive"}})[0], 201)
        time.sleep(0.5)
        self.assertEqual(len(self.silent_events(num)), before,
                         "a session note on an assigned card resets the clock")
        # The proof the clock RESET rather than merely stayed quiet: the next
        # episode arms off the note, so silence fires again after it.
        deadline = time.time() + 10.0
        while time.time() < deadline and len(self.silent_events(num)) <= before:
            time.sleep(0.1)
        self.assertGreater(len(self.silent_events(num)), before,
                           "the note re-armed the episode")

    def test_a_server_reminder_never_counts_as_activity(self):
        """The sweep's own noise must not reset the clock it is complaining
        about -- that is how a nag becomes permanent silence."""
        num = self.new_card("quiet")["num"]
        self.to_in_progress(num)
        self.assertTrue(self.await_silence(num))
        ts, _seq = self.app.card_baseline(num)
        _, detail = self.get("/api/cards/%d" % num)
        server_events = [e for e in detail["timeline"] if e["actor"] == "server"]
        self.assertTrue(server_events)
        self.assertLess(ts, max(e["ts"] for e in server_events))

    def test_the_board_may_not_set_either_flag(self):
        """These turn the silence timer OFF. The user has no way to know
        whether an agent is legitimately quiet, so it is not their switch."""
        num = self.new_card("a card")["num"]
        self.to_in_progress(num)
        for action in ("long_running", "external_agent"):
            with self.subTest(action=action):
                status, body = self.post(
                    "/api/cards/%d/action" % num, {"action": action},
                    headers={"Sec-Fetch-Site": "same-origin"})
                self.assertEqual(status, 403, body)
                self.assertEqual(body["error"], "session_only")
        _, detail = self.get("/api/cards/%d" % num)
        self.assertFalse(detail["card"]["long_running"])
        self.assertFalse(detail["card"]["external_agent"])

    def test_retry_forgets_both_flags(self):
        """A fresh agent starts on a fresh clock."""
        num = self.new_card("a card")["num"]
        self.to_in_progress(num)
        for action in ("long_running", "external_agent"):
            self.post("/api/cards/%d/action" % num,
                      {"action": action, "actor": "session"})
        self.assertEqual(self.post("/api/cards/%d/state" % num,
                                   {"state": "failed", "actor": "session",
                                    "reason": "agent died"})[0], 200)
        status, body = self.post("/api/cards/%d/action" % num, {"action": "retry"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["card"]["long_running"])
        self.assertFalse(body["card"]["external_agent"])

    def test_assign_can_declare_an_external_agent_up_front(self):
        num = self.new_card("built outside")["num"]
        status, body = self.post("/api/cards/%d/assign" % num,
                                 {"agent_name": "russ-codex-4",
                                  "external_agent": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["card"]["external_agent"])

    def test_a_bad_action_names_the_whole_enum(self):
        num = self.new_card("a card")["num"]
        status, body = self.post("/api/cards/%d/action" % num,
                                 {"action": "teleport"})
        self.assertEqual(status, 400, body)
        self.assertIn("long_running", body["message"])
        self.assertIn("external_agent", body["message"])


class TestMigratedColumns(unittest.TestCase):
    """A board that has been running since before a column landed must open."""

    def test_an_old_database_gains_the_new_columns(self):
        tmp = tempfile.mkdtemp(prefix="sprintd-migrate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "project")
        os.makedirs(root)
        app = sprintd.App(root, token="t")
        # Rewind: drop the columns the way an older sprintd would never have
        # had them, then reopen and read a card through the normal path.
        app.conn.execute("ALTER TABLE cards DROP COLUMN external_agent")
        app.conn.execute("ALTER TABLE cards DROP COLUMN work_kind")
        app.conn.execute(
            "INSERT INTO cards(sprint_id, state, title, body, created_at, updated_at) "
            "VALUES(NULL,'queued','old','old card',1.0,1.0)")
        app.close()

        app2 = sprintd.App(root, token="t")
        self.addCleanup(app2.close)
        card = app2.card_json(app2.card_row(1))
        self.assertFalse(card["external_agent"])
        self.assertEqual(card["work_kind"], "code")
class TestSettings(Base):
    """.sprint/config.json — the board's dispatch policy.

    The server stores and validates it and does nothing else with it: the
    SESSION reads it at dispatch. Which is exactly why an unknown key has to
    be a loud 400 — a typo that is quietly accepted reads back, hours later,
    as "the defaults are fine".
    """

    def settings(self):
        status, body = self.get("/api/settings")
        self.assertEqual(status, 200, body)
        return body

    def put(self, patch, **kw):
        return self.req("PUT", "/api/settings", patch, **kw)

    # -- read -----------------------------------------------------------

    def test_defaults_before_anything_is_written(self):
        body = self.settings()
        w = body["settings"]["worker"]
        self.assertEqual(w["model_policy"], "lowest_feasible")
        self.assertEqual(w["default_executor"], "subagent")
        self.assertEqual(w["concurrency"], 3)
        self.assertIn("claude", w["executors"])
        self.assertEqual(body["defaults"]["worker"]["model_policy"], "lowest_feasible")
        # the panel's dropdowns come from the server, not a list in JS
        self.assertEqual(body["choices"]["model_policy"],
                         ["lowest_feasible", "always_opus", "always_sonnet"])

    def test_settings_need_the_token(self):
        status, _ = self.get("/api/settings", token=None)
        self.assertEqual(status, 401)
        status, _ = self.put({"worker": {"concurrency": 5}}, token=None)
        self.assertEqual(status, 401)

    # -- write ----------------------------------------------------------

    def test_write_persists_to_config_json_and_reads_back(self):
        status, body = self.put({"worker": {
            "model_policy": "always_sonnet",
            "concurrency": 5,
            "executors": {"grok": {"kind": "tmux", "command": "grok",
                                   "session": "sprint-workers"},
                          "claude": {"kind": "subagent"}},
            "default_executor": "grok",
        }})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["settings"]["worker"]["default_executor"], "grok")

        path = os.path.join(self.project_root, ".sprint", "config.json")
        self.assertTrue(os.path.isfile(path), "config.json was written")
        with open(path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["worker"]["concurrency"], 5)
        self.assertEqual(on_disk["worker"]["executors"]["grok"]["command"], "grok")

        # ...and a fresh read comes back with what we wrote
        again = self.settings()["settings"]["worker"]
        self.assertEqual(again["model_policy"], "always_sonnet")
        self.assertEqual(again["executors"]["grok"]["kind"], "tmux")

    def test_a_partial_write_leaves_everything_else_alone(self):
        self.put({"worker": {"concurrency": 7}})
        self.put({"worker": {"model_policy": "always_opus"}})
        w = self.settings()["settings"]["worker"]
        self.assertEqual(w["concurrency"], 7)
        self.assertEqual(w["model_policy"], "always_opus")

    def test_post_writes_the_same_as_put(self):
        status, body = self.post("/api/settings", {"worker": {"concurrency": 4}})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.settings()["settings"]["worker"]["concurrency"], 4)

    def test_a_change_lands_on_the_event_log_for_the_session_to_see(self):
        before = self.get("/api/events?after=0&limit=500")[1]["head"]
        self.put({"worker": {"concurrency": 6}})
        status, page = self.get("/api/events?after=%d&limit=50" % before)
        self.assertEqual(status, 200)
        notes = [e for e in page["events"] if e["kind"] == "note"
                 and e["payload"].get("settings")]
        self.assertEqual(len(notes), 1, page["events"])
        note = notes[0]
        self.assertEqual(note["actor"], "server")
        self.assertIn("next dispatch", note["payload"]["text"])
        # a server event is never a human waiting for a reply
        self.assertNotIn("reply_to", note["payload"])
        # ...and it is not sidebar chatter either
        self.assertEqual([e for e in self.get("/api/sidebar")[1]["events"]
                          if e["payload"].get("settings")], [])

    def test_a_write_that_changes_nothing_writes_no_event(self):
        self.put({"worker": {"concurrency": 5}})
        head = self.get("/api/events?after=0&limit=500")[1]["head"]
        status, _ = self.put({"worker": {"concurrency": 5}})
        self.assertEqual(status, 200)
        self.assertEqual(self.get("/api/events?after=0&limit=500")[1]["head"], head)

    # -- validation ------------------------------------------------------

    def test_unknown_key_is_rejected_and_named(self):
        status, body = self.put({"worker": {"model_polciy": "always_opus"}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "unknown_key")
        self.assertEqual(body["field"], "worker.model_polciy")
        # ...and nothing was written
        self.assertFalse(os.path.isfile(
            os.path.join(self.project_root, ".sprint", "config.json")))

    def test_unknown_top_level_section_is_rejected(self):
        status, body = self.put({"orchestrator": {"model": "opus"}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "unknown_key")
        self.assertEqual(body["field"], "orchestrator")

    def test_bad_enum_is_rejected(self):
        status, body = self.put({"worker": {"model_policy": "haiku_always"}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "worker.model_policy")
        self.assertIn("lowest_feasible", body["message"])

    def test_concurrency_must_be_a_sane_whole_number(self):
        for bad in ("3", 0, -1, 999, 2.5, True):
            status, body = self.put({"worker": {"concurrency": bad}})
            self.assertEqual(status, 400, "%r should be refused: %s" % (bad, body))
            self.assertEqual(body["field"], "worker.concurrency")

    def test_a_tmux_executor_without_a_command_is_refused(self):
        status, body = self.put({"worker": {"executors": {"grok": {"kind": "tmux"}}}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "worker.executors.grok.command")

    def test_an_unknown_executor_kind_is_refused(self):
        status, body = self.put({"worker": {
            "executors": {"grok": {"kind": "ssh", "command": "grok"}}}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "worker.executors.grok.kind")

    def test_an_unknown_field_inside_an_executor_is_refused(self):
        status, body = self.put({"worker": {
            "executors": {"grok": {"kind": "tmux", "command": "grok",
                                   "windo": "sprint"}}}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "unknown_key")
        self.assertIn("windo", body["field"])

    def test_default_executor_must_be_one_that_exists(self):
        status, body = self.put({"worker": {"default_executor": "grok"}})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "worker.default_executor")
        # ...but declaring it in the same request is fine
        status, body = self.put({"worker": {
            "executors": {"grok": {"kind": "tmux", "command": "grok"}},
            "default_executor": "grok"}})
        self.assertEqual(status, 200, body)

    def test_a_tmux_executor_gets_a_default_session_name(self):
        self.put({"worker": {"executors": {"grok": {"kind": "tmux", "command": "grok"}}}})
        w = self.settings()["settings"]["worker"]
        self.assertEqual(w["executors"]["grok"]["session"], "sprint-workers")

    def test_a_hand_edited_broken_file_falls_back_to_defaults(self):
        path = os.path.join(self.project_root, ".sprint", "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json,, }")
        w = self.settings()["settings"]["worker"]
        self.assertEqual(w["model_policy"], "lowest_feasible")
        self.assertEqual(w["concurrency"], 3)

    def test_a_hand_edited_file_is_picked_up_without_a_restart(self):
        self.put({"worker": {"concurrency": 4}})
        path = os.path.join(self.project_root, ".sprint", "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"worker": {"concurrency": 9,
                                  "model_policy": "always_opus"}}, fh)
        w = self.settings()["settings"]["worker"]
        self.assertEqual(w["concurrency"], 9)
        self.assertEqual(w["model_policy"], "always_opus")

    def test_put_to_anything_else_is_a_404(self):
        status, body = self.req("PUT", "/api/board", {})
        self.assertEqual(status, 404, body)


class TestPerCardExecutor(Base):
    """A card can name its own executor and model at assign time.

    User's scope call, verbatim: "Peer per card — mix grok-via-tmux and claude
    subagents". So the choice lives on the card, not on the board, and the
    board's settings are only what an unset card falls back to.
    """

    def declare_grok(self):
        status, body = self.req("PUT", "/api/settings", {"worker": {"executors": {
            "grok": {"kind": "tmux", "command": "grok", "session": "sprint-workers"},
            "claude": {"kind": "subagent"}}}})
        self.assertEqual(status, 200, body)

    def card_of(self, num):
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(status, 200, detail)
        return detail["card"]

    def board_card(self, num):
        status, board = self.get("/api/board")
        self.assertEqual(status, 200, board)
        return next(c for c in board["cards"] if c["num"] == num)

    def test_assign_records_executor_and_model(self):
        self.declare_grok()
        card = self.new_card("make the header calm")
        status, body = self.post("/api/cards/%d/assign" % card["num"], {
            "agent_name": "sprint-card-%d" % card["num"],
            "worktree": "/tmp/wt", "branch": "sprint/card-x",
            "executor": "grok", "model": "grok-4"})
        self.assertEqual(status, 200, body)

        for got in (self.card_of(card["num"]), self.board_card(card["num"])):
            self.assertEqual(got["executor"], "grok")
            self.assertEqual(got["model"], "grok-4")
            self.assertEqual(got["dispatch"]["kind"], "tmux")
            self.assertEqual(got["dispatch"]["command"], "grok")
            self.assertEqual(got["dispatch"]["session"], "sprint-workers")
            self.assertFalse(got["dispatch"]["is_default"])

    def test_the_timeline_says_how_it_was_dispatched(self):
        self.declare_grok()
        card = self.new_card("something for grok")
        self.post("/api/cards/%d/assign" % card["num"], {
            "agent_name": "sprint-card-9", "executor": "grok", "model": "grok-4"})
        status, detail = self.get("/api/cards/%d" % card["num"])
        self.assertEqual(status, 200)
        notes = [e for e in detail["timeline"] if e["kind"] == "note"
                 and "assigned to" in (e["payload"].get("text") or "")]
        self.assertTrue(notes)
        self.assertIn("grok · tmux · grok-4", notes[-1]["payload"]["text"])

    def test_defaults_apply_when_the_card_says_nothing(self):
        card = self.new_card("ordinary work")
        self.post("/api/cards/%d/assign" % card["num"],
                  {"agent_name": "sprint-card-1"})
        got = self.card_of(card["num"])
        self.assertIsNone(got["executor"])
        self.assertIsNone(got["model"])
        # resolved from the board's policy: lowest feasible == sonnet
        self.assertEqual(got["dispatch"]["executor"], "subagent")
        self.assertEqual(got["dispatch"]["kind"], "subagent")
        self.assertEqual(got["dispatch"]["model"], "sonnet")
        self.assertTrue(got["dispatch"]["is_default"],
                        "a card on the defaults carries no executor tag")

    def test_the_model_policy_moves_the_default_model(self):
        card = self.new_card("ordinary work")
        self.post("/api/cards/%d/assign" % card["num"], {"agent_name": "a"})
        self.assertEqual(self.card_of(card["num"])["dispatch"]["model"], "sonnet")
        self.req("PUT", "/api/settings", {"worker": {"model_policy": "always_opus"}})
        self.assertEqual(self.card_of(card["num"])["dispatch"]["model"], "opus")

    def test_the_default_executor_moves_an_unset_card(self):
        self.declare_grok()
        card = self.new_card("ordinary work")
        self.post("/api/cards/%d/assign" % card["num"], {"agent_name": "a"})
        self.req("PUT", "/api/settings", {"worker": {"default_executor": "grok"}})
        got = self.card_of(card["num"])
        self.assertEqual(got["dispatch"]["executor"], "grok")
        self.assertEqual(got["dispatch"]["kind"], "tmux")
        # still no per-card choice, so still nothing to tag
        self.assertTrue(got["dispatch"]["is_default"])

    def test_an_undeclared_executor_is_refused_at_assign(self):
        card = self.new_card("who runs this")
        status, body = self.post("/api/cards/%d/assign" % card["num"],
                                 {"agent_name": "a", "executor": "grok"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "executor")
        self.assertIsNone(self.card_of(card["num"])["executor"])

    def test_a_bad_model_is_refused(self):
        card = self.new_card("who runs this")
        for bad in ("", "   ", 7, "x" * 61):
            status, body = self.post("/api/cards/%d/assign" % card["num"],
                                     {"agent_name": "a", "model": bad})
            self.assertEqual(status, 400, "%r should be refused: %s" % (bad, body))
            self.assertEqual(body["field"], "model")

    def test_assigning_again_never_clears_a_recorded_choice(self):
        self.declare_grok()
        card = self.new_card("keep it")
        self.post("/api/cards/%d/assign" % card["num"],
                  {"agent_name": "a", "executor": "grok", "model": "grok-4"})
        self.post("/api/cards/%d/assign" % card["num"], {"worktree": "/tmp/wt2"})
        got = self.card_of(card["num"])
        self.assertEqual(got["executor"], "grok")
        self.assertEqual(got["model"], "grok-4")

    def test_the_board_ships_the_settings_the_faces_need(self):
        self.declare_grok()
        status, board = self.get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(board["settings"]["worker"]["executors"]["grok"]["kind"], "tmux")

    def test_an_existing_board_gains_the_columns(self):
        """A board that predates this feature must not need a fresh DB."""
        import sqlite3 as _sqlite3
        db = os.path.join(self.tmp, "old.db")
        conn = _sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE cards (num INTEGER PRIMARY KEY AUTOINCREMENT, sprint_id "
            "INTEGER, state TEXT NOT NULL, title TEXT, body TEXT, batch_id INTEGER, "
            "agent_name TEXT, worktree TEXT, branch TEXT, bounce_count INTEGER NOT "
            "NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0, dup_of INTEGER, "
            "long_running INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL);")
        conn.execute("INSERT INTO cards(sprint_id, state, title, body, created_at, "
                     "updated_at) VALUES(1,'queued','old','old',1,1)")
        conn.commit()
        conn.close()
        old_root = os.path.join(self.tmp, "oldproject")
        os.makedirs(os.path.join(old_root, ".sprint"))
        shutil.copy(db, os.path.join(old_root, ".sprint", "sprint.db"))
        app = sprintd.App(old_root, log=self.logfh, token="test-token")
        self.addCleanup(app.close)
        cols = {r["name"] for r in app.conn.execute("PRAGMA table_info(cards)")}
        self.assertIn("executor", cols)
        self.assertIn("model", cols)
        self.assertIsNone(app.card_json(app.card_row(1), brief=True)["executor"])
class TestSprintIsNamedAfterTheProject(Base):
    """A board's title defaults to the PROJECT, never the literal "sprint".

    User report: the russ board's header read "sprint" — which says nothing at
    all when three boards are open in three tmux windows, and is the one string
    every auto-opened sprint shared.
    """

    def test_auto_opened_sprint_takes_the_project_basename(self):
        self.app.open_sprint(create=True)
        status, board = self.get("/api/board")
        self.assertEqual(status, 200, board)
        self.assertEqual(board["sprint"]["title"], "project")   # basename of project_root

    def test_open_without_a_title_takes_the_project_basename(self):
        status, res = self.post("/api/sprint", {"action": "open"})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["sprint"]["title"], "project")

    def test_an_explicit_title_still_wins(self):
        status, res = self.post("/api/sprint", {"action": "open", "title": "Billing week"})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["sprint"]["title"], "Billing week")

    def test_a_board_still_carrying_the_old_literal_default_is_renamed_once(self):
        """Every board opened before this change is sitting under "sprint"."""
        self.app.open_sprint(create=True)
        with self.app.lock:
            self.app.conn.execute("UPDATE sprints SET title='sprint'")
        self.assertEqual(self.get("/api/board")[1]["sprint"]["title"], "sprint")
        self.app.name_untitled_sprint()
        self.assertEqual(self.get("/api/board")[1]["sprint"]["title"], "project")

    def test_a_title_somebody_chose_is_never_touched(self):
        self.post("/api/sprint", {"action": "open", "title": "Billing week"})
        self.app.name_untitled_sprint()
        self.assertEqual(self.get("/api/board")[1]["sprint"]["title"], "Billing week")

    def test_the_registry_name_is_unchanged_by_any_of_this(self):
        """The switcher labels boards from the registry; this only names sprints."""
        entry = sprintd.registry_entry(self.project_root, 8399, "127.0.0.1")
        self.assertEqual(entry["name"], "project")


class TestApiVersionIsPublished(Base):
    """A page can tell it is talking to a server older than itself."""

    def test_healthz_and_board_both_carry_the_api_version(self):
        status, health = self.get("/healthz", token=None)
        self.assertEqual(status, 200, health)
        self.assertEqual(health["api_version"], sprintd.API_VERSION)
        status, board = self.get("/api/board")
        self.assertEqual(status, 200, board)
        self.assertEqual(board["api_version"], sprintd.API_VERSION)

    def test_the_version_is_an_integer_that_only_goes_up(self):
        self.assertIsInstance(sprintd.API_VERSION, int)
        self.assertGreaterEqual(sprintd.API_VERSION, 2)


class TestPortChoice(unittest.TestCase):
    """`sprintd start` must never need --port: not on a restart, and not when
    somebody else's board is already sitting on the default."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-port-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)

    def hold(self, port=0):
        """Occupy a port for the length of the test and return it."""
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_port_file_round_trips(self):
        p = os.path.join(self.tmp, "port")
        sprintd.write_port_file(p, 8378)
        self.assertEqual(sprintd.read_port_file(p), 8378)

    def test_a_junk_or_missing_port_file_reads_as_nothing(self):
        p = os.path.join(self.tmp, "port")
        self.assertIsNone(sprintd.read_port_file(p))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("not a port\n")
        self.assertIsNone(sprintd.read_port_file(p))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("99999\n")
        self.assertIsNone(sprintd.read_port_file(p))

    def test_a_free_port_is_simply_taken(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        self.assertEqual(sprintd.choose_port(free, self.root), free)

    def test_a_held_port_moves_up_to_the_next_free_one(self):
        held = self.hold()
        got = sprintd.choose_port(held, self.root)
        self.assertNotEqual(got, held)
        self.assertGreater(got, held)
        self.assertTrue(sprintd.port_is_free(got))

    def test_an_explicit_port_is_obeyed_even_when_it_is_taken(self):
        """--port means that port. Silently moving would be the worse bug."""
        held = self.hold()
        self.assertEqual(sprintd.choose_port(held, self.root, explicit=True), held)

    def test_it_says_which_port_it_moved_to_and_why(self):
        held = self.hold()
        said = []
        got = sprintd.choose_port(held, self.root, say=said.append)
        self.assertEqual(len(said), 1)
        self.assertIn(str(held), said[0])
        self.assertIn(str(got), said[0])


class TestPortSurvivesStopAndStart(unittest.TestCase):
    """The live failure this fixes: the russ board had been serving on 8378 for
    weeks; a restart tried the compiled default 8377, found ANOTHER project's
    board there, and died with "cannot bind 127.0.0.1:8377"."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sprintd-portcli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)
        self.server_json = os.path.join(self.root, ".sprint", sprintd.SERVER_JSON)
        self.port_file = os.path.join(self.root, ".sprint", sprintd.PORT_FILENAME)
        self.registry = os.path.join(self.tmp, "registry.json")
        self.addCleanup(self._kill_leftovers)

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
            [sys.executable, SPRINTD_PATH, "--project-root", self.root,
             "--registry", self.registry] + list(argv),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

    def _free_port(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def test_restart_reuses_the_port_without_being_told(self):
        port = self._free_port()
        r = self._run("start", "--port", str(port), "--token", "keeps-its-port",
                      "--no-tailscale")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(sprintd.read_port_file(self.port_file), port)

        self.assertEqual(self._run("stop").returncode, 0)
        self.assertFalse(os.path.exists(self.server_json))
        self.assertTrue(os.path.exists(self.port_file),
                        "stop must not take the port with it")

        # no --port this time: the board must come back where it was
        r2 = self._run("start", "--no-tailscale")
        self.assertEqual(r2.returncode, 0, r2.stderr.decode())
        self.assertIn("http://127.0.0.1:%d/?t=keeps-its-port" % port, r2.stdout.decode())
        self.assertEqual(sprintd.read_server_json(self.server_json)["port"], port)
        self.assertEqual(self._run("stop").returncode, 0)

    def test_a_default_port_held_by_another_project_does_not_stop_the_start(self):
        """No recorded port, and something else on the one we would have used:
        take the next free port and say so, instead of refusing to start."""
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        held = s.getsockname()[1]
        self.addCleanup(s.close)

        self.assertIsNone(sprintd.read_port_file(self.port_file))
        # --port is deliberately NOT passed; the preferred port is seeded the
        # way a fresh board seeds it, through the port file.
        os.makedirs(os.path.dirname(self.port_file), exist_ok=True)
        sprintd.write_port_file(self.port_file, held)
        r = self._run("start", "--no-tailscale", "--token", "moves-over")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        landed = sprintd.read_server_json(self.server_json)["port"]
        self.assertNotEqual(landed, held)
        self.assertEqual(sprintd.read_port_file(self.port_file), landed)
        self.assertIn("is held by", r.stderr.decode())
        self.assertEqual(sprintd.http_get("127.0.0.1", landed, "/healthz")[0], 200)
        self.assertEqual(self._run("stop").returncode, 0)


# --------------------------------------------------------------------------
# Decision requests (#50) -- an agent asking for a DECISION, not a verdict.
#
# User's rule, verbatim: "needs you is where we talk through things. review
# means the session genuinely thinks the card is 100% complete. needs you is
# that the card is waiting for my input before it can keep moving forward."
#
# So the two handoffs must land in different places and stay that way: a
# question with artifacts is `needs_you`, never `ready`, and the gate is
# untouched by any of it.
# --------------------------------------------------------------------------


class DecisionBase(Base):
    def working_card(self, text="which header do you want"):
        num = self.new_card(text)["num"]
        self.to_in_progress(num)
        return num

    def write_png(self, name="mock.png"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(PNG_B64))
        return path

    def question_of(self, num):
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(status, 200, detail)
        return detail["card"]["question"]


class TestDecisionRequest(DecisionBase):
    def test_a_decision_request_lands_needs_you_not_ready(self):
        num = self.working_card()
        status, body = self.post("/api/cards/%d/question" % num, {
            "text": "Which of these three headers?",
            "options": ["A", "B", "C"],
            "artifacts": {"url": "http://127.0.0.1:8450/preview",
                          "attachments": [self.write_png()],
                          "notes": "All three keep the 44px targets."},
        })
        self.assertEqual(status, 201, body)
        self.assertEqual(self.state_of(num), "needs_you")
        self.assertNotEqual(self.state_of(num), "ready")

    def test_artifacts_survive_the_round_trip(self):
        num = self.working_card()
        png = self.write_png()
        self.post("/api/cards/%d/question" % num, {
            "text": "Pick one",
            "artifacts": {"url": "http://127.0.0.1:8450/preview",
                          "attachments": [png],
                          "notes": "  context  "},
        })
        arts = self.question_of(num)["artifacts"]
        self.assertEqual(arts["url"], "http://127.0.0.1:8450/preview")
        self.assertEqual(arts["notes"], "context")
        self.assertEqual(len(arts["attachments"]), 1)
        att = arts["attachments"][0]
        # ...ingested exactly like a packet's screenshot: content-addressed,
        # on disk, and servable back to the browser.
        self.assertRegex(att["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(os.path.isfile(att["path"]))
        status, blob = self.get(att["url"])
        self.assertEqual(status, 200)
        self.assertEqual(blob, base64.b64decode(PNG_B64))

    def test_the_question_event_carries_them_too(self):
        """Answering must not erase what you were asked to look at."""
        num = self.working_card()
        self.post("/api/cards/%d/question" % num,
                  {"text": "Pick one", "artifacts": {"notes": "two options below"}})
        _, detail = self.get("/api/cards/%d" % num)
        q = [e for e in detail["timeline"] if e["kind"] == "question"][-1]
        self.assertEqual(q["payload"]["artifacts"]["notes"], "two options below")

    def test_answering_a_decision_request_unblocks_the_card(self):
        num = self.working_card()
        self.post("/api/cards/%d/question" % num,
                  {"text": "Pick one", "options": ["A", "B"],
                   "artifacts": {"url": "http://127.0.0.1:8450/x"}})
        qid = self.question_of(num)["id"]
        status, _ = self.post("/api/cards/%d/answer" % num,
                              {"question_id": qid, "text": "B"})
        self.assertEqual(status, 200)
        self.assertEqual(self.state_of(num), "in_progress")

    def test_a_plain_question_still_has_no_artifacts(self):
        num = self.working_card()
        self.post("/api/cards/%d/question" % num, {"text": "what colour?"})
        self.assertIsNone(self.question_of(num)["artifacts"])
        self.assertEqual(self.state_of(num), "needs_you")

    def test_a_malformed_artifacts_payload_names_itself(self):
        num = self.working_card()
        for bad, field in (
            ("not an object", "artifacts"),
            ({}, "artifacts"),
            ({"url": "ftp://nope"}, "artifacts.url"),
            ({"url": ""}, "artifacts.url"),
            ({"notes": 7}, "artifacts.notes"),
            ({"attachments": "shot.png"}, "artifacts.attachments"),
        ):
            status, body = self.post("/api/cards/%d/question" % num,
                                     {"text": "hm", "artifacts": bad})
            self.assertEqual(status, 400, (bad, body))
            self.assertEqual(body.get("field"), field, (bad, body))
        # ...and none of those left the card waiting on a question
        self.assertEqual(self.state_of(num), "in_progress")

    def test_the_gate_is_untouched_by_any_of_this(self):
        """A decision request is not a back door into `ready`."""
        num = self.working_card()
        self.post("/api/cards/%d/question" % num,
                  {"text": "Pick one", "artifacts": {"notes": "x"}})
        status, body = self.post("/api/cards/%d/state" % num, {"state": "ready"})
        self.assertEqual(status, 422, body)

    def test_an_older_board_gets_the_column_added(self):
        """CREATE TABLE IF NOT EXISTS does nothing to an existing table."""
        self.assertIn(("questions", "artifacts", "TEXT"),
                      [(t, c, d) for t, c, d in sprintd.App.ADDED_COLUMNS])


class TestSprintAskArtifactFlags(DecisionBase):
    """bin/sprint-ask is how a worker actually posts one."""

    SPRINT_ASK = os.path.join(os.path.dirname(HERE), "bin", "sprint-ask")

    def run_ask(self, *argv):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN="test-token")
        return subprocess.run([sys.executable, self.SPRINT_ASK] + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)

    def test_the_flags_post_a_decision_request(self):
        num = self.working_card()
        png = self.write_png()
        r = self.run_ask(num, "Which header?", "--options", '["A","B"]',
                         "--url", "http://127.0.0.1:8450/preview",
                         "--attach", png, "--notes", "B costs a request")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("decision request", out)
        self.assertIn("needs_you", out)
        self.assertEqual(self.state_of(num), "needs_you")
        arts = self.question_of(num)["artifacts"]
        self.assertEqual(arts["url"], "http://127.0.0.1:8450/preview")
        self.assertEqual(arts["notes"], "B costs a request")
        self.assertEqual(len(arts["attachments"]), 1)

    def test_a_plain_ask_is_exactly_what_it_was(self):
        num = self.working_card()
        r = self.run_ask(num, "what colour?")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertNotIn("decision request", r.stdout.decode())
        self.assertIsNone(self.question_of(num)["artifacts"])

    def test_a_missing_attachment_fails_before_the_network(self):
        num = self.working_card()
        r = self.run_ask(num, "pick", "--attach", os.path.join(self.tmp, "nope.png"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("attach", r.stderr.decode())
        self.assertIn("no such file", r.stderr.decode())
        self.assertEqual(self.state_of(num), "in_progress")

    def test_a_relative_attachment_path_fails_by_name(self):
        num = self.working_card()
        r = self.run_ask(num, "pick", "--attach", "shot.png")
        self.assertEqual(r.returncode, 2)
        self.assertIn("absolute", r.stderr.decode())

    def test_a_non_http_url_fails_by_name(self):
        num = self.working_card()
        r = self.run_ask(num, "pick", "--url", "/tmp/preview")
        self.assertEqual(r.returncode, 2)
        self.assertIn("url", r.stderr.decode())

    def test_the_worker_contract_states_the_rule(self):
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "agents", "sprint-worker.md"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("needs you is where we talk through things", doc)
        self.assertIn("--attach", doc)
        with open(os.path.join(root, "SPEC.md"), encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn("needs you is where we talk through things", spec)
        with open(os.path.join(root, "skills", "sprint", "SKILL.md"), encoding="utf-8") as fh:
            skill = fh.read()
        self.assertIn("decision request", skill)


class TestSprintReadyDecisionNotice(DecisionBase):
    """The cheap guard: a packet that is really a question says so on stderr.

    Advisory, deliberately. The packet has already passed every real rule; this
    is judgment, and a gate made of judgment is a gate that blocks good work.
    """

    SPRINT_READY = os.path.join(os.path.dirname(HERE), "bin", "sprint-ready")

    def run_ready(self, num, packet):
        import subprocess
        path = os.path.join(self.tmp, "packet-%s.json" % num)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(packet, fh)
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN="test-token")
        return subprocess.run([sys.executable, self.SPRINT_READY, str(num), path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=60)

    def test_a_packet_carrying_options_is_named_and_still_posts(self):
        num = self.working_card()
        packet = dict(GOOD_PACKET)
        packet["options"] = ["A", "B"]
        r = self.run_ready(num, packet)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        err = r.stderr.decode()
        self.assertIn("options", err)
        self.assertIn("sprint-ask", err)
        self.assertEqual(self.state_of(num), "ready")   # advisory, not a gate

    def test_a_validate_step_that_is_a_question_is_named(self):
        num = self.working_card()
        packet = dict(GOOD_PACKET)
        packet["validate"] = ["Which of these two layouts do you want?"]
        err = self.run_ready(num, packet).stderr.decode()
        self.assertIn("validate", err)
        self.assertIn("sprint-ask", err)

    def test_a_claim_that_is_a_question_is_named(self):
        num = self.working_card()
        packet = dict(GOOD_PACKET)
        packet["claim"] = "Should the header be sticky?"
        err = self.run_ready(num, packet).stderr.decode()
        self.assertIn("claim", err)

    def test_an_ordinary_packet_is_not_nagged(self):
        num = self.working_card()
        r = self.run_ready(num, dict(GOOD_PACKET))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertNotIn("sprint-ask", r.stderr.decode())
        self.assertEqual(self.state_of(num), "ready")


# --------------------------------------------------------------------------
# Clickable links (#54). User verbatim: "make links clickable and should auto
# open in a new tab." The browser half lives in web/util.js; this is the half
# the server renders -- markdown reports.
# --------------------------------------------------------------------------


class TestBareUrlsAutolink(Base):
    def test_a_bare_url_in_a_report_becomes_a_new_tab_link(self):
        html = sprintd.render_markdown("see http://127.0.0.1:8450/preview for it")
        self.assertIn('href="http://127.0.0.1:8450/preview"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noreferrer noopener nofollow"', html)

    def test_trailing_punctuation_is_the_sentence_not_the_url(self):
        html = sprintd.render_markdown("open http://example.com/x.")
        self.assertIn('href="http://example.com/x"', html)

    def test_a_markdown_link_is_not_double_wrapped(self):
        html = sprintd.render_markdown("[the preview](http://example.com/x)")
        self.assertEqual(html.count("<a "), 1)
        self.assertIn(">the preview<", html)

    def test_a_url_in_inline_code_stays_text(self):
        html = sprintd.render_markdown("run `curl http://example.com/x`")
        self.assertNotIn("<a ", html)

    def test_a_url_in_a_fenced_block_stays_text(self):
        html = sprintd.render_markdown("```\ncurl http://example.com/x\n```")
        self.assertNotIn("<a ", html)

    def test_a_javascript_url_is_not_linked(self):
        html = sprintd.render_markdown("javascript:alert(1) and data:text/html,x")
        self.assertNotIn("<a ", html)
# Provider limit windows (card #47)
#
# The incident: three workers killed at once by a usage limit whose message
# said when it would end, nothing captured that sentence, and the cards sat
# open for hours. These tests are about the three things that failure needed —
# record the window, show it, and fire exactly ONE resume signal when it ends.
# --------------------------------------------------------------------------


class TestResetTimeParsing(unittest.TestCase):
    """`parse_reset_time` — the one parser the board and `sprint-limit` share.

    Everything is pinned to a fixed reference instant, because "the next
    occurrence of 11:50pm" is a different answer at 11:49 and at 11:51 and a
    test that reads the wall clock would be right twice a day.
    """

    # 2026-08-16 23:52:00 local — two minutes PAST 11:50pm, which is the exact
    # moment the real kill message arrives and the exact moment naive parsing
    # gets it wrong.
    REF = datetime.datetime(2026, 8, 16, 23, 52, 0).timestamp()

    def at(self, value, ref=None):
        return datetime.datetime.fromtimestamp(
            sprintd.parse_reset_time(value, ref=self.REF if ref is None else ref))

    def test_a_clock_time_means_the_next_time_it_comes_round(self):
        # 11:50pm, read at 11:52pm, is TOMORROW. Reading it as today would put
        # the reset two minutes in the past and clear the window instantly.
        self.assertEqual(self.at("11:50pm"),
                         datetime.datetime(2026, 8, 17, 23, 50))
        # ...and the same time read BEFORE it happens is today.
        ref = datetime.datetime(2026, 8, 16, 20, 0, 0).timestamp()
        self.assertEqual(self.at("11:50pm", ref=ref),
                         datetime.datetime(2026, 8, 16, 23, 50))

    def test_the_shapes_a_human_actually_types(self):
        self.assertEqual(self.at("11:50 PM"), datetime.datetime(2026, 8, 17, 23, 50))
        self.assertEqual(self.at("11:50p.m."), datetime.datetime(2026, 8, 17, 23, 50))
        self.assertEqual(self.at("9pm"), datetime.datetime(2026, 8, 17, 21, 0))
        self.assertEqual(self.at("23:50"), datetime.datetime(2026, 8, 17, 23, 50))
        # midnight and noon are the two that off-by-twelve bugs live in
        self.assertEqual(self.at("12am"), datetime.datetime(2026, 8, 17, 0, 0))
        self.assertEqual(self.at("12pm"), datetime.datetime(2026, 8, 17, 12, 0))

    def test_iso_and_epoch(self):
        self.assertEqual(self.at("2026-08-17T06:30:00"),
                         datetime.datetime(2026, 8, 17, 6, 30))
        # an offset is honoured rather than ignored
        self.assertEqual(sprintd.parse_reset_time("2026-08-17T06:30:00+00:00"),
                         datetime.datetime(2026, 8, 17, 6, 30,
                                           tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(sprintd.parse_reset_time("2026-08-17T06:30:00Z"),
                         datetime.datetime(2026, 8, 17, 6, 30,
                                           tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(sprintd.parse_reset_time(1786945800), 1786945800.0)
        self.assertEqual(sprintd.parse_reset_time("1786945800"), 1786945800.0)
        # milliseconds, because something will eventually send them
        self.assertEqual(sprintd.parse_reset_time(1786945800000), 1786945800.0)

    def test_the_providers_own_parenthetical_zone(self):
        """The kill message can be pasted verbatim, zone and all."""
        utc = sprintd.parse_reset_time("11:50pm (UTC)", ref=self.REF)
        self.assertEqual(
            datetime.datetime.fromtimestamp(utc, datetime.timezone.utc),
            datetime.datetime(2026, 8, 17, 23, 50, tzinfo=datetime.timezone.utc))
        # a zone nobody has heard of is a 400, never a silent local reading
        with self.assertRaises(sprintd.ApiError) as ctx:
            sprintd.parse_reset_time("11:50pm (Bogus/Zone)", ref=self.REF)
        self.assertEqual(ctx.exception.status, 400)

    def test_nonsense_is_a_named_400_not_a_guess(self):
        for bad in ("gibberish", "", "  ", "25:00", "13:70", "13pm", None, True, []):
            with self.assertRaises(sprintd.ApiError) as ctx:
                sprintd.parse_reset_time(bad, ref=self.REF)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.extra.get("field"), "resets_at")

    def test_clock_label_is_the_words_the_board_uses(self):
        ts = datetime.datetime(2026, 8, 17, 23, 50).timestamp()
        self.assertEqual(sprintd.clock_label(ts), "11:50pm")
        self.assertEqual(
            sprintd.clock_label(datetime.datetime(2026, 8, 17, 0, 5).timestamp()),
            "12:05am")
        self.assertEqual(
            sprintd.clock_label(datetime.datetime(2026, 8, 17, 12, 0).timestamp()),
            "12:00pm")


class LimitBase(Base):
    def declare(self, model="fable", resets="11:50pm", **kw):
        body = {"model": model, "resets_at": resets}
        body.update(kw)
        status, out = self.post("/api/limits", body)
        self.assertIn(status, (200, 201), out)
        return status, out

    def limit_events(self, kind=None):
        status, body = self.get("/api/events?after=0&limit=2000")
        self.assertEqual(status, 200, body)
        return [e for e in body["events"]
                if e["kind"] in ("limit_declared", "limit_cleared")
                and (kind is None or e["kind"] == kind)]


class TestLimitWindows(LimitBase):
    def test_declare_read_and_clear(self):
        status, out = self.declare(source="kill message", note="three agents died")
        self.assertEqual(status, 201)
        self.assertTrue(out["created"])
        lim = out["limit"]
        self.assertEqual(lim["model"], "fable")
        self.assertTrue(lim["active"])
        self.assertGreater(lim["resets_at"], sprintd.now())
        self.assertEqual(lim["source"], "kill message")

        status, body = self.get("/api/limits")
        self.assertEqual(status, 200, body)
        self.assertEqual([l["id"] for l in body["active"]], [lim["id"]])
        self.assertEqual(body["recent"], [])

        # declaring it is a fact on the log, in words
        declared = self.limit_events("limit_declared")
        self.assertEqual(len(declared), 1)
        self.assertIsNone(declared[0]["card_num"])
        self.assertEqual(declared[0]["actor"], "server")
        self.assertIn("rate-limited until", declared[0]["payload"]["text"])

        status, out = self.post("/api/limits/%d/clear" % lim["id"])
        self.assertEqual(status, 200, out)
        self.assertTrue(out["cleared"])
        self.assertFalse(out["limit"]["active"])
        cleared = self.limit_events("limit_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["payload"]["reason"], "cleared_early")
        self.assertEqual(cleared[0]["payload"]["model"], "fable")

        status, body = self.get("/api/limits")
        self.assertEqual(body["active"], [])
        self.assertEqual([l["id"] for l in body["recent"]], [lim["id"]])

    def test_redeclaring_the_same_window_updates_it(self):
        """The session sees the same kill message on the second and third dead
        agent. That must not become three lines on the board."""
        _, first = self.declare()
        _, second = self.declare(note="and a third agent")
        self.assertFalse(second["created"])
        self.assertEqual(second["limit"]["id"], first["limit"]["id"])
        self.assertEqual(second["limit"]["note"], "and a third agent")
        status, body = self.get("/api/limits")
        self.assertEqual(len(body["active"]), 1)
        # ...and the unchanged re-declaration says nothing new on the log
        self.assertEqual(len(self.limit_events("limit_declared")), 1)

    def test_a_corrected_reset_time_moves_the_window_it_corrects(self):
        _, first = self.declare(resets="11:50pm")
        _, second = self.declare(resets=sprintd.now() + 3600)
        self.assertEqual(second["limit"]["id"], first["limit"]["id"])
        self.assertNotEqual(second["limit"]["resets_at"], first["limit"]["resets_at"])
        status, body = self.get("/api/limits")
        self.assertEqual(len(body["active"]), 1)
        # a real change IS worth saying out loud
        self.assertEqual(len(self.limit_events("limit_declared")), 2)

    def test_two_models_are_two_windows(self):
        self.declare(model="fable")
        self.declare(model="opus")
        status, body = self.get("/api/limits")
        self.assertEqual(sorted(l["model"] for l in body["active"]), ["fable", "opus"])

    def test_a_passed_window_is_inactive_with_no_job_having_run(self):
        """Activeness is COMPUTED. Nothing here runs the sweep — the background
        threads are off in this fixture — and the window is still over, because
        a board that was asleep across 11:50pm has to come back up knowing."""
        self.assertIsNone(self.app._sweep_thread)
        _, out = self.declare(resets=sprintd.now() - 10)
        lim = out["limit"]
        self.assertFalse(lim["active"])
        self.assertIsNone(lim["cleared_at"])      # nothing wrote anything down

        status, body = self.get("/api/limits")
        self.assertEqual(body["active"], [])
        self.assertEqual([l["id"] for l in body["recent"]], [lim["id"]])
        status, board = self.get("/api/board")
        self.assertEqual(board["limits"], [])
        # and the resume signal has NOT been faked by the read path
        self.assertEqual(self.limit_events("limit_cleared"), [])

    def test_the_board_payload_carries_the_open_windows(self):
        status, board = self.get("/api/board")
        self.assertEqual(board["limits"], [])
        _, out = self.declare()
        status, board = self.get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual([l["id"] for l in board["limits"]], [out["limit"]["id"]])
        self.assertEqual(board["limits"][0]["model"], "fable")
        # the line the UI writes needs both halves of the sentence
        self.assertTrue(board["limits"][0]["resets_at_label"])
        self.assertEqual(board["default_model"], "fable")

    def test_bad_declarations_are_named_400s(self):
        status, body = self.post("/api/limits", {"resets_at": "11:50pm"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "model")
        status, body = self.post("/api/limits", {"model": "fable"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "resets_at")
        status, body = self.post("/api/limits", {"model": "fable",
                                                 "resets_at": "half past nine-ish"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "resets_at")

    def test_clearing_an_unknown_window_is_a_404(self):
        status, body = self.post("/api/limits/999/clear")
        self.assertEqual(status, 404, body)


class TestLimitClearedFiresExactlyOnce(LimitBase):
    """The one property that actually matters.

    `limit_cleared` is not a log line the user reads — it is the signal the
    session re-dispatches on. Two of them is two agents on the same card, so
    "exactly once" is tested from every direction that could produce a storm:
    a sweep that runs on a tick, a manual clear racing that tick, and threads.
    """

    def passed_window(self):
        _, out = self.declare(resets=sprintd.now() - 1)
        return out["limit"]["id"]

    def test_a_sweep_on_a_tick_fires_once_no_matter_how_often_it_ticks(self):
        self.passed_window()
        fired = [self.app.sweep_limits() for _ in range(25)]
        self.assertEqual(sum(fired), 1)
        self.assertEqual(fired[0], 1)             # the FIRST tick is the one
        self.assertEqual(len(self.limit_events("limit_cleared")), 1)

    def test_concurrent_sweeps_and_clears_still_fire_once(self):
        limit_id = self.passed_window()
        errors = []

        def hammer(fn):
            def run():
                try:
                    for _ in range(10):
                        fn()
                except Exception as exc:       # a race must not throw either
                    errors.append(exc)
            return run

        threads = [threading.Thread(target=hammer(self.app.sweep_limits))
                   for _ in range(4)]
        threads += [threading.Thread(
            target=hammer(lambda: self.app.clear_limit(limit_id)))
            for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.limit_events("limit_cleared")), 1)

    def test_an_early_clear_and_the_clock_do_not_both_fire(self):
        """Cleared early at 11:30, reset time arrives at 11:50: one event."""
        _, out = self.declare(resets=sprintd.now() + 0.4)
        status, _ = self.post("/api/limits/%d/clear" % out["limit"]["id"])
        self.assertEqual(status, 200)
        time.sleep(0.6)                            # the window's time arrives
        self.assertEqual(self.app.sweep_limits(), 0)
        events = self.limit_events("limit_cleared")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["reason"], "cleared_early")

    def test_the_clock_path_says_the_window_passed(self):
        self.passed_window()
        self.app.sweep_limits()
        events = self.limit_events("limit_cleared")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["reason"], "window_passed")
        self.assertIn("can go back on", events[0]["payload"]["text"])

    def test_the_guard_is_what_prevents_the_storm(self):
        """Falsification. The test above would pass just as happily against an
        implementation that never fires at all, or one the sweep only ever
        reaches once by luck — so here the conditional UPDATE is REMOVED and
        the storm is observed. If this test stops seeing duplicates, the one
        above has stopped proving anything.
        """
        limit_id = self.passed_window()
        real = sprintd.App.end_limit

        def naive(app, lid, at=None):
            """What this looked like before the guard: write, then announce."""
            at = sprintd.now() if at is None else at
            with app.lock:
                row = app.conn.execute("SELECT * FROM limits WHERE id=?",
                                       (lid,)).fetchone()
                app.conn.execute("UPDATE limits SET cleared_at=? WHERE id=?",
                                 (at, lid))
                app._append_event(None, "server", "limit_cleared", {
                    "limit_id": lid, "model": row["model"],
                    "resets_at": row["resets_at"], "reason": "window_passed",
                    "text": "naive"})
            return True

        # The sweep only selects windows that are still open, so a naive writer
        # needs the window reopened between ticks to storm — which is exactly
        # what a crash between the UPDATE and the COMMIT would leave behind.
        sprintd.App.end_limit = naive
        try:
            for _ in range(3):
                self.app.conn.execute(
                    "UPDATE limits SET cleared_at=NULL WHERE id=?", (limit_id,))
                self.app.sweep_limits()
        finally:
            sprintd.App.end_limit = real
        self.assertGreater(len(self.limit_events("limit_cleared")), 1,
                           "the naive writer did not storm — this falsification "
                           "no longer proves the guard is load-bearing")

        # ...and the real one, given the identical provocation, does not.
        before = len(self.limit_events("limit_cleared"))
        for _ in range(3):
            self.app.conn.execute(
                "UPDATE limits SET cleared_at=NULL WHERE id=?", (limit_id,))
            self.app.sweep_limits()
        self.assertEqual(len(self.limit_events("limit_cleared")) - before, 3,
                         "each reopened window is its own episode")
        # one per reopening, never several per reopening
        self.assertEqual(self.app.sweep_limits(), 0)


class TestModelReason(Base):
    """WHY a card is not on the default model. Without it, "restore what was
    downgraded" is a thing the session has to remember rather than read."""

    def test_assign_records_it_and_every_payload_carries_it(self):
        num = self.new_card("something to downgrade")["num"]
        status, out = self.post("/api/cards/%d/assign" % num, {
            "agent_name": "sprint-card-%d" % num, "worktree": "/tmp/wt",
            "branch": "sprint/card-%d" % num, "model": "opus",
            "model_reason": "fable limited until 23:50"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["card"]["model_reason"], "fable limited until 23:50")

        status, board = self.get("/api/board")
        card = [c for c in board["cards"] if c["num"] == num][0]
        self.assertEqual(card["model"], "opus")
        self.assertEqual(card["model_reason"], "fable limited until 23:50")

        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["model_reason"], "fable limited until 23:50")
        note = [e for e in detail["timeline"]
                if e["kind"] == "note" and e["payload"].get("model_reason")][0]
        # the timeline reads as a sentence, not as a field dump
        self.assertIn("on opus", note["payload"]["text"])
        self.assertIn("fable limited until 23:50", note["payload"]["text"])

    def test_an_empty_string_clears_it_and_omitting_it_does_not(self):
        num = self.new_card("restore me")["num"]
        self.post("/api/cards/%d/assign" % num,
                  {"agent_name": "a", "model": "opus",
                   "model_reason": "fable limited until 23:50"})
        # a later assign that says nothing about the reason leaves it alone
        self.post("/api/cards/%d/assign" % num, {"agent_name": "a"})
        status, detail = self.get("/api/cards/%d" % num)
        self.assertEqual(detail["card"]["model_reason"], "fable limited until 23:50")
        # ...and "" is how the session says "this is not a downgrade any more"
        status, out = self.post("/api/cards/%d/assign" % num,
                                {"agent_name": "a", "model": "fable",
                                 "model_reason": ""})
        self.assertEqual(status, 200, out)
        self.assertIsNone(out["card"]["model_reason"])

    def test_it_is_one_capped_line(self):
        num = self.new_card("long reason")["num"]
        status, out = self.post("/api/cards/%d/assign" % num, {
            "agent_name": "a", "model_reason": "x" * 400 + "\nsecond line"})
        self.assertEqual(status, 200, out)
        self.assertLessEqual(len(out["card"]["model_reason"]), sprintd.MODEL_REASON_MAX)
        self.assertNotIn("\n", out["card"]["model_reason"])
        status, body = self.post("/api/cards/%d/assign" % num,
                                 {"agent_name": "a", "model_reason": 17})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "model_reason")


class TestLimitColumnsMigrate(unittest.TestCase):
    """A board that has been up since before this landed must open, gain the
    column and the table, and read a card written by the older server."""

    def test_an_old_database_gains_model_reason_and_the_limits_table(self):
        tmp = tempfile.mkdtemp(prefix="sprintd-limit-migrate-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "project")
        os.makedirs(root)
        app = sprintd.App(root, token="t")
        app.conn.execute("ALTER TABLE cards DROP COLUMN model_reason")
        app.conn.execute("DROP TABLE limits")
        app.conn.execute(
            "INSERT INTO cards(sprint_id, state, title, body, created_at, updated_at) "
            "VALUES(NULL,'in_progress','old','from before limits',1.0,1.0)")
        app.close()

        app2 = sprintd.App(root, token="t")
        self.addCleanup(app2.close)
        card = app2.card_json(app2.card_row(1))
        self.assertIsNone(card["model_reason"])          # not a 500
        out = app2.declare_limit("fable", "11:50pm")     # the table is back
        self.assertTrue(out["limit"]["active"])
        self.assertEqual([l["model"] for l in app2.active_limits()], ["fable"])
        # and the migrated column takes a write
        app2.assign(1, "sprint-card-1", None, None, model="opus",
                    model_reason="fable limited until 23:50")
        self.assertEqual(app2.card_json(app2.card_row(1))["model_reason"],
                         "fable limited until 23:50")


# --------------------------------------------------------------------------
# The ACCOUNT limit (card #47, bounce)
#
# The second incident, verbatim: "i'm about to hit my overall claude weekly
# limit... once i do, I need a big warning on top of every board, and then I'm
# going to go to the session, log out, log back in with a different claude
# session, then I should be able to hit a button in the big notice to have it
# auto-resume".
#
# So: a kind of window where nothing runs at all, visible on EVERY board on the
# machine including ones in other projects, with a button that ends it — and
# all of it has to work with NO session attached, because a dead session is the
# precondition, not an edge case.
# --------------------------------------------------------------------------


class AccountLimitBase(LimitBase):
    def declare_account(self, resets="11:50pm", **kw):
        body = {"kind": "account", "resets_at": resets}
        body.update(kw)
        status, out = self.post("/api/limits", body)
        self.assertIn(status, (200, 201), out)
        return status, out

    def sibling(self, name="other-project"):
        """A second board, another project, same machine — the case the user
        will actually be in: he declares on whichever board is in front of him
        and the OTHER ones have to say so too. Same $SPRINT_REGISTRY (Base
        points it at this test's temp dir), which is the whole channel."""
        root = os.path.join(self.tmp, name)
        os.makedirs(root, exist_ok=True)
        app = sprintd.App(root, log=self.logfh, token="test-token")
        self.addCleanup(app.close)
        return app


class TestAccountLimitWindows(AccountLimitBase):
    def test_declare_read_and_resume(self):
        status, out = self.declare_account(source="weekly limit",
                                           note="hit at 4pm")
        self.assertEqual(status, 201)
        lim = out["limit"]
        self.assertEqual(lim["kind"], "account")
        self.assertIsNone(lim["model"])       # an account is not a model
        self.assertTrue(lim["active"])

        status, board = self.get("/api/board")
        banner = board["account_limit"]
        self.assertEqual(banner["id"], lim["id"])
        # what happened, when it lifts, and what to DO — the three the user asked
        self.assertIn("account limit", banner["headline"].lower())
        self.assertIn(banner["resets_at_label"], banner["detail"])
        self.assertIn("another Claude session", banner["action"])
        self.assertIn("Resume", banner["action"])
        self.assertEqual(banner["resume_label"], "Resume")
        self.assertEqual(banner["resume_url"], "/api/limits/%d/clear" % lim["id"])

        # ...and the Resume button's one POST ends it
        status, out = self.post("/api/limits/%d/clear" % lim["id"])
        self.assertEqual(status, 200)
        self.assertTrue(out["cleared"])
        status, board = self.get("/api/board")
        self.assertIsNone(board["account_limit"])
        self.assertEqual(board["limits"], [])
        cleared = self.limit_events("limit_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["payload"]["kind"], "account")
        self.assertEqual(cleared[0]["payload"]["reason"], "cleared_early")
        self.assertIn("re-dispatch", cleared[0]["payload"]["text"])

    def test_one_account_window_at_a_time(self):
        """Every dead agent reports the same weekly limit. One window, one
        banner — the same rule model windows already have, keyed on the kind
        because there is only one account."""
        _, first = self.declare_account()
        _, second = self.declare_account(note="and a second session died")
        self.assertEqual(second["limit"]["id"], first["limit"]["id"])
        self.assertFalse(second["created"])
        status, body = self.get("/api/limits")
        self.assertEqual(len(body["active"]), 1)
        self.assertEqual(len(self.limit_events("limit_declared")), 1)

    def test_both_kinds_at_once(self):
        """A model window and an account window are different facts and both
        stay true: the quiet dashed line keeps its meaning under the banner."""
        _, model = self.declare(model="fable", resets="11:50pm")
        _, account = self.declare_account(resets="9pm")
        status, board = self.get("/api/board")
        kinds = sorted(l["kind"] for l in board["limits"])
        self.assertEqual(kinds, ["account", "model"])
        self.assertEqual(board["account_limit"]["id"], account["limit"]["id"])
        quiet = [l for l in board["limits"] if l["kind"] == "model"]
        self.assertEqual([l["model"] for l in quiet], ["fable"])
        # clearing the account one leaves the model one exactly where it was
        self.post("/api/limits/%d/clear" % account["limit"]["id"])
        status, board = self.get("/api/board")
        self.assertIsNone(board["account_limit"])
        self.assertEqual([l["id"] for l in board["limits"]],
                         [model["limit"]["id"]])

    def test_exactly_once_holds_for_the_account_kind_too(self):
        """The signal the session re-dispatches EVERYTHING on. Two of these is
        two agents per parked card, so the same guard is proved from the same
        three directions: repeated sweeps, a manual clear racing them, threads.
        """
        _, out = self.declare_account(resets=sprintd.now() - 1)
        limit_id = out["limit"]["id"]
        errors = []

        def hammer(fn):
            def run():
                try:
                    for _ in range(10):
                        fn()
                except Exception as exc:
                    errors.append(exc)
            return run

        threads = [threading.Thread(target=hammer(self.app.sweep_limits))
                   for _ in range(4)]
        threads += [threading.Thread(
            target=hammer(lambda: self.app.clear_limit(limit_id)))
            for _ in range(4)]
        # the reconciler is a third writer racing both, and it must not add one
        threads += [threading.Thread(
            target=hammer(self.app.reconcile_account_limit)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        cleared = self.limit_events("limit_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["payload"]["kind"], "account")

    def test_activeness_is_still_computed_not_stored(self):
        """A board asleep across the reset time comes back up knowing it is
        over — no sweep has to have run."""
        _, out = self.declare_account(resets=sprintd.now() + 0.4)
        status, board = self.get("/api/board")
        self.assertIsNotNone(board["account_limit"])
        time.sleep(0.6)
        status, board = self.get("/api/board")
        self.assertIsNone(board["account_limit"])
        self.assertEqual(board["limits"], [])

    def test_a_bad_kind_is_a_named_400(self):
        status, body = self.post("/api/limits",
                                 {"kind": "everything", "resets_at": "11:50pm"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], "bad_kind")
        self.assertEqual(body["field"], "kind")
        # and a model window still needs its model
        status, body = self.post("/api/limits", {"resets_at": "11:50pm"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["field"], "model")

    def test_an_account_window_needs_no_model_and_keeps_none(self):
        status, out = self.declare_account(model="fable")
        self.assertIsNone(out["limit"]["model"])
        status, board = self.get("/api/board")
        # nothing here can be mistaken for "fable is limited" — no model is
        self.assertIsNone(board["account_limit"]["model"])


class TestAccountLimitIsMachineWide(AccountLimitBase):
    """The banner has to reach boards that did not declare it — the user is
    looking at whichever project's tab is in front of him when the account
    dies, and that is rarely the one that noticed.

    The channel is one file beside the registry, not a push to siblings: no
    board holds another's bearer token, and a push cannot reach a board that
    STARTS after the declaration — which is the case that happens every time a
    wedged project gets restarted mid-limit.
    """

    def test_a_sibling_board_in_another_project_raises_the_same_banner(self):
        other = self.sibling()
        self.assertIsNone(other.board()["account_limit"])
        _, out = self.declare_account(resets="11:50pm")

        board = other.board()          # one read is all it takes
        banner = board["account_limit"]
        self.assertIsNotNone(banner)
        self.assertEqual(banner["kind"], "account")
        self.assertAlmostEqual(banner["resets_at"], out["limit"]["resets_at"],
                               places=0)
        self.assertIn("account limit", banner["headline"].lower())
        # It is the sibling's OWN row in the sibling's OWN database — not a
        # rendering of somebody else's — which is what lets it emit its own
        # single limit_cleared for its own parked cards later.
        self.assertEqual([l["kind"] for l in other.active_limits()], ["account"])
        self.assertEqual(other.limit_row(banner["id"])["kind"], "account")
        declared = [e for e in other.events_after(0)
                    if e["kind"] == "limit_declared"]
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0]["payload"]["kind"], "account")
        # and it says where it came from, so the banner is not from nowhere
        self.assertIn("declared on", (banner["source"] or ""))

    def test_a_board_that_starts_mid_limit_still_shows_it(self):
        """The case a push would miss entirely."""
        self.declare_account(resets="11:50pm")
        latecomer = self.sibling("started-late")
        self.assertIsNotNone(latecomer.board()["account_limit"])

    def test_resume_on_one_board_lifts_it_on_all_of_them(self):
        other = self.sibling()
        _, out = self.declare_account(resets="11:50pm")
        mirrored = other.board()["account_limit"]["id"]

        # the user presses Resume on the SIBLING, not on the declaring board
        other.clear_limit(mirrored)
        self.assertIsNone(other.board()["account_limit"])

        status, board = self.get("/api/board")
        self.assertIsNone(board["account_limit"])
        cleared = self.limit_events("limit_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["payload"]["kind"], "account")
        # each board emits exactly one, for its own cards
        theirs = [e for e in other.events_after(0) if e["kind"] == "limit_cleared"]
        self.assertEqual(len(theirs), 1)

    def test_reading_a_sibling_board_twice_does_not_re_declare(self):
        other = self.sibling()
        self.declare_account(resets="11:50pm")
        for _ in range(5):
            other.board()
        declared = [e for e in other.events_after(0)
                    if e["kind"] == "limit_declared"]
        self.assertEqual(len(declared), 1)

    def test_a_model_window_stays_local(self):
        """Only the account is machine-wide. One project running out of fable
        says nothing about another project's board."""
        other = self.sibling()
        self.declare(model="fable", resets="11:50pm")
        board = other.board()
        self.assertIsNone(board["account_limit"])
        self.assertEqual(board["limits"], [])
        self.assertFalse(os.path.exists(sprintd.account_limit_path()))

    def test_a_corrupt_shared_file_is_no_banner_not_a_500(self):
        path = sprintd.account_limit_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        status, board = self.get("/api/board")
        self.assertEqual(status, 200)
        self.assertIsNone(board["account_limit"])

    def test_the_real_home_state_is_never_touched(self):
        """The suite points $SPRINT_REGISTRY at a temp dir; this proves the
        account file follows it, because a test that declared into the
        developer's own ~/.sprint would put a banner on his live boards."""
        self.assertEqual(os.path.dirname(sprintd.account_limit_path()),
                         os.path.dirname(os.path.abspath(self.registry)))
        home = os.path.join(os.path.expanduser("~"), ".sprint",
                            "account-limit.json")
        before = os.path.exists(home)
        self.declare_account()
        self.assertTrue(os.path.exists(sprintd.account_limit_path()))
        self.assertEqual(os.path.exists(home), before)


class TestAccountLimitWithNoSession(AccountLimitBase):
    """The point of the whole feature: while the account is out the
    orchestrating session is DEAD. Server and browser are the only two things
    still moving, so everything here runs with nothing polling /api/wait, no
    cursor ever moving, and no background threads started (START_BACKGROUND is
    False on this class, so not even the sweep tick exists).
    """

    def assert_no_session(self):
        status, board = self.get("/api/board")
        sess = board["session"]
        self.assertFalse(sess["waiter_polling"])
        self.assertFalse(sess["waiter_alive"])
        self.assertIsNone(sess["waiter_seen_at"])
        self.assertEqual(board["cursor"], 0)     # nothing has drained anything
        self.assertIsNone(self.app._sweep_thread)
        return board

    def test_the_banner_renders_and_resume_clears_with_nothing_attached(self):
        _, out = self.declare_account(resets="11:50pm")
        board = self.assert_no_session()
        self.assertIsNotNone(board["account_limit"])
        self.assertEqual(board["account_limit"]["resume_url"],
                         "/api/limits/%d/clear" % out["limit"]["id"])

        # exactly what the button does: one POST, no session in the loop
        status, cleared = self.post(board["account_limit"]["resume_url"])
        self.assertEqual(status, 200)
        self.assertTrue(cleared["cleared"])
        board = self.assert_no_session()
        self.assertIsNone(board["account_limit"])
        self.assertEqual(len(self.limit_events("limit_cleared")), 1)

    def test_a_sibling_board_gets_it_from_the_browser_poll_alone(self):
        """FALSIFICATION of the "no session needed" claim. The sibling has no
        session, no waiter and no sweep thread — the ONLY thing that happens to
        it is the GET a browser tab makes. Take that GET away and it has no way
        to know; make it, and the banner is there. If this ever passes without
        the board read, the propagation has quietly grown a dependency on
        something that is not running when it matters.
        """
        other = self.sibling()
        self.assertIsNone(other._sweep_thread)
        self.declare_account(resets="11:50pm")

        # before any read: the sibling's own table knows nothing
        self.assertEqual(other.active_limits(), [])
        self.assertEqual([e for e in other.events_after(0)
                          if e["kind"].startswith("limit_")], [])

        board = other.board()          # the browser's poll, and nothing else
        self.assertIsNotNone(board["account_limit"])
        self.assertFalse(board["session"]["waiter_alive"])
        self.assertEqual(board["cursor"], 0)

        # and Resume from that same sessionless board really ends it
        other.clear_limit(board["account_limit"]["id"])
        self.assertIsNone(other.board()["account_limit"])
        self.assertIsNone(self.get("/api/board")[1]["account_limit"])


class TestSprintLimitCli(Base):
    """`bin/sprint-limit` — what the session actually types when it reads a
    kill message. Thin by design: the server owns the parsing."""

    SPRINT_LIMIT = os.path.join(os.path.dirname(HERE), "bin", "sprint-limit")

    def run_limit(self, *argv, token="test-token"):
        import subprocess
        env = dict(os.environ,
                   SPRINT_SERVER="http://%s:%d" % (self.host, self.port),
                   SPRINT_TOKEN=token)
        return subprocess.run([sys.executable, self.SPRINT_LIMIT]
                              + [str(a) for a in argv],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=90)

    def test_declare_list_clear(self):
        r = self.run_limit("declare", "--model", "fable", "--resets", "11:50pm",
                           "--source", "kill message")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("fable is limited until", out)
        # it prints the exact assign call the session owes next
        self.assertIn("model_reason", out)
        status, body = self.get("/api/limits")
        self.assertEqual(len(body["active"]), 1)
        limit_id = body["active"][0]["id"]

        r = self.run_limit("list")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("fable", r.stdout.decode())

        r = self.run_limit("clear", str(limit_id))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("available again", r.stdout.decode())
        status, body = self.get("/api/limits")
        self.assertEqual(body["active"], [])

    def test_declare_the_whole_account(self):
        r = self.run_limit("declare", "--account", "--resets", "11:50pm")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("ACCOUNT is limited until", out)
        # it says the thing the user has to do, not just the fact
        self.assertIn("another session", out)
        self.assertIn("Resume", out)
        status, board = self.get("/api/board")
        self.assertIsNotNone(board["account_limit"])
        limit_id = board["account_limit"]["id"]

        r = self.run_limit("list")
        self.assertIn("ACCOUNT", r.stdout.decode())

        r = self.run_limit("clear", str(limit_id))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("account is available again", r.stdout.decode())
        self.assertIsNone(self.get("/api/board")[1]["account_limit"])

    def test_it_will_not_guess_between_a_model_and_the_account(self):
        r = self.run_limit("declare", "--resets", "11:50pm")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--account", r.stderr.decode())
        r = self.run_limit("declare", "--account", "--model", "fable",
                           "--resets", "11:50pm")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.get("/api/limits")[1]["active"], [])

    def test_a_bad_time_fails_loudly_and_records_nothing(self):
        r = self.run_limit("declare", "--model", "fable", "--resets", "soonish")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("11:50pm", r.stderr.decode())     # it says what IS accepted
        status, body = self.get("/api/limits")
        self.assertEqual(body["active"], [])

    def test_it_refuses_to_run_without_a_server(self):
        import subprocess
        r = subprocess.run([sys.executable, self.SPRINT_LIMIT, "list"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env={k: v for k, v in os.environ.items()
                                if k not in ("SPRINT_SERVER", "SPRINT_TOKEN")},
                           timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SPRINT_SERVER", r.stderr.decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
