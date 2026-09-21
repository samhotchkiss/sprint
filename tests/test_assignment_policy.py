"""Injected verifier only. No TypeSafe network calls."""
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sprint_coordinator.assignment_policy import (
    fingerprint_for, gate_assignment, is_default_request, requested_identity,
)
from sprint_coordinator.jev import JevServiceError, VerificationOutcome


# Live worker.executors.grok is kind/command/session with no model.
LIVE_SETTINGS = {
    "worker": {
        "default_executor": "grok",
        "executors": {
            "grok": {"kind": "tmux", "command": "grok", "session": "sprint-grok"},
            "claude": {"kind": "tmux", "command": "claude", "session": "sprint-claude"},
            "codex": {"kind": "tmux", "command": "codex", "session": "sprint-codex"},
        },
    }
}

CONFIGURED_MODEL_SETTINGS = {
    "worker": {
        "default_executor": "grok",
        "executors": {
            "grok": {"kind": "tmux", "command": "grok", "model": "grok-4"},
            "claude": {"kind": "tmux", "command": "claude", "model": "opus"},
        },
    }
}


class FakeEvaluate:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def __call__(self, client, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def enable(root: Path, daily_max=20, extra=None):
    body = {
        "enabled": True,
        "key_file": "~/.config/sprint/typesafe.env",
        "daily_max_calls": daily_max,
    }
    if extra:
        body.update(extra)
    (root / "assignment-policy.json").write_text(json.dumps(body))


class AssignmentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def gate(self, payload, **kwargs):
        settings = kwargs.pop("settings", LIVE_SETTINGS)
        return gate_assignment(
            self.root, settings=settings, card_num=kwargs.pop("card_num", 12),
            payload=payload, **kwargs)

    def usage(self):
        path = self.root / "assignment-policy.sqlite"
        if not path.exists():
            return 0
        db = sqlite3.connect(path)
        try:
            row = db.execute("SELECT calls FROM usage").fetchone()
            return 0 if row is None else row[0]
        finally:
            db.close()

    def audit(self):
        db = sqlite3.connect(self.root / "assignment-policy.sqlite")
        try:
            return db.execute(
                "SELECT id, nonce, event FROM audit ORDER BY id"
            ).fetchall()
        finally:
            db.close()

    def test_missing_file_allows_without_verifier(self):
        fake = FakeEvaluate(VerificationOutcome("deny", "nope"))
        out = self.gate({"executor": "claude", "model_reason": "x"}, evaluate=fake)
        self.assertTrue(out.allowed)
        self.assertEqual(out.reason, "assignment_policy_disabled")
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.root / "assignment-policy.sqlite").exists())

    def test_live_default_grok_plus_grok46_is_passthrough(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("deny", "nope"))
        ident = requested_identity(LIVE_SETTINGS, {"executor": "grok", "model": "grok-4.6"})
        self.assertEqual(ident["default_model"], "")
        self.assertEqual(ident["requested_model"], "grok-4.6")
        self.assertTrue(is_default_request(ident))
        out = self.gate({"executor": "grok", "model": "grok-4.6"}, evaluate=fake,
                        assignment_id="a1")
        self.assertTrue(out.allowed)
        self.assertEqual(out.reason, "default_executor")
        self.assertEqual(out.identity["default_model"], "")
        self.assertEqual(out.identity["requested_model"], "grok-4.6")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.usage(), 0)
        self.assertTrue(out.fingerprint)
        self.assertEqual(out.identity["assignment_id"], "a1")

    def test_configured_default_model_is_compared(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        ident = requested_identity(
            CONFIGURED_MODEL_SETTINGS, {"executor": "grok", "model": "grok-4.6"})
        self.assertEqual(ident["default_model"], "grok-4")
        self.assertFalse(is_default_request(ident))
        out = self.gate(
            {"executor": "grok", "model": "grok-4.6",
             "model_reason": "Default grok-4 failed the same tool loop twice"},
            evaluate=fake, assignment_id="model-change",
            settings=CONFIGURED_MODEL_SETTINGS)
        self.assertTrue(out.allowed)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["default_model"], "grok-4")
        self.assertEqual(fake.calls[0]["requested_model"], "grok-4.6")

    def test_omitted_executor_is_not_filled_from_default(self):
        ident = requested_identity(LIVE_SETTINGS, {"model": "grok-4.6"})
        self.assertEqual(ident["requested_provider"], "")
        self.assertFalse(is_default_request(ident))
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        out = self.gate({"model": "grok-4.6"}, evaluate=fake, assignment_id="omit")
        self.assertEqual(out.action, "deny")
        self.assertEqual(out.reason, "override_reason_required")
        self.assertEqual(fake.calls, [])

    def test_provider_override_required_every_time(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        payload = {"executor": "claude", "model": "opus",
                   "model_reason": "Default Grok timed out twice on this card"}
        first = self.gate(payload, evaluate=fake, assignment_id="one")
        second = self.gate(payload, evaluate=fake, assignment_id="two")
        self.assertTrue(first.allowed and second.allowed)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(fake.calls), 2)

    def test_same_assignment_id_does_not_cache_approval(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        payload = {"executor": "claude", "model_reason": "Default Grok timed out twice on this card"}
        first = self.gate(payload, evaluate=fake, assignment_id="keep")
        again = self.gate(payload, evaluate=fake, assignment_id="keep")
        self.assertTrue(first.allowed and again.allowed)
        self.assertEqual(first.fingerprint, again.fingerprint)
        self.assertEqual(len(fake.calls), 2)
        self.assertNotEqual(first.nonce, again.nonce)

    def test_existing_flag_is_ignored(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("deny", "nope"))
        out = self.gate({"executor": "claude"}, evaluate=fake, existing=True,
                        assignment_id="live")
        self.assertEqual(out.action, "deny")
        self.assertEqual(out.reason, "override_reason_required")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.usage(), 0)

    def test_blank_override_reason_denied_without_call(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "should not run"))
        out = self.gate({"executor": "claude"}, evaluate=fake, assignment_id="a2")
        self.assertEqual(out.action, "deny")
        self.assertEqual(out.reason, "override_reason_required")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.usage(), 0)

    def test_override_accept_inserts_reserved_then_allowed_same_nonce(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok", {"reason_specific": 0.9}, 12))
        reason = "Default Grok timed out twice on card 12 tool loop with the same stack."
        out = self.gate(
            {"executor": "codex", "model": "gpt-5.4", "override_reason": reason,
             "evidence": {"attempts": ["timeout"]}},
            evaluate=fake, assignment_id="asg-3", task="fix card 12 loop")
        self.assertTrue(out.allowed)
        self.assertEqual(out.reason, "override_accepted")
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["default_provider"], "grok")
        self.assertEqual(call["default_model"], "")
        self.assertEqual(call["requested_provider"], "codex")
        self.assertEqual(call["requested_model"], "gpt-5.4")
        self.assertEqual(call["reason"], reason)
        self.assertEqual(self.usage(), 1)
        rows = self.audit()
        self.assertEqual([row[2] for row in rows], ["reserved", "allowed"])
        self.assertEqual(rows[0][1], rows[1][1])
        self.assertEqual(rows[0][1], out.nonce)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual(out.decision_id, rows[1][0])
        dumped = json.dumps(out.as_dict())
        self.assertNotIn("TYPESAFE", dumped)
        self.assertNotIn("typesafe.env", dumped)
        self.assertNotIn("typesafe.env", json.dumps(out.snapshot))

    def test_jev_deny_and_escalate_fail_closed(self):
        enable(self.root)
        denied = self.gate(
            {"executor": "claude", "model_reason": "I prefer Claude"},
            evaluate=FakeEvaluate(VerificationOutcome("deny", "preference")),
            assignment_id="asg-d")
        self.assertEqual(denied.action, "deny")
        self.assertFalse(denied.allowed)
        uncertain = self.gate(
            {"executor": "claude", "model_reason": "maybe harder"},
            evaluate=FakeEvaluate(VerificationOutcome("escalate", "not decisive")),
            assignment_id="asg-e")
        self.assertEqual(uncertain.action, "unavailable")
        self.assertFalse(uncertain.allowed)
        events = [row[2] for row in self.audit()]
        self.assertEqual(events, ["reserved", "denied", "reserved", "unavailable"])
        self.assertEqual(self.audit()[0][1], self.audit()[1][1])
        self.assertEqual(self.audit()[2][1], self.audit()[3][1])

    def test_outage_fail_closed_with_two_audit_rows(self):
        enable(self.root)
        out = self.gate(
            {"executor": "claude", "override_reason": "default crashed twice"},
            evaluate=FakeEvaluate(JevServiceError("HTTP 503")),
            assignment_id="asg-down")
        self.assertEqual(out.action, "unavailable")
        self.assertEqual(out.reason, "verifier_unavailable")
        self.assertEqual(self.usage(), 1)
        rows = self.audit()
        self.assertEqual([row[2] for row in rows], ["reserved", "unavailable"])
        self.assertEqual(rows[0][1], rows[1][1])

    def test_daily_cap_before_call(self):
        enable(self.root, daily_max=1)
        ok = FakeEvaluate(VerificationOutcome("accept", "ok"))
        first = self.gate({"executor": "claude", "model_reason": "failed twice with timeouts"},
                          evaluate=ok, assignment_id="c1")
        self.assertTrue(first.allowed)
        second = self.gate({"executor": "claude", "model_reason": "failed twice with timeouts"},
                           evaluate=ok, assignment_id="c2")
        self.assertEqual(second.action, "unavailable")
        self.assertEqual(second.reason, "assignment_policy_daily_cap")
        self.assertEqual(len(ok.calls), 1)
        self.assertEqual(self.usage(), 1)

    def test_cap_zero_means_zero(self):
        enable(self.root, daily_max=0)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        passthrough = self.gate({"executor": "grok", "model": "grok-4.6"},
                                evaluate=fake, assignment_id="free")
        self.assertTrue(passthrough.allowed)
        self.assertEqual(passthrough.reason, "default_executor")
        blocked = self.gate(
            {"executor": "claude", "model_reason": "default timed out twice"},
            evaluate=fake, assignment_id="paid")
        self.assertEqual(blocked.action, "unavailable")
        self.assertEqual(blocked.reason, "assignment_policy_daily_cap")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.usage(), 0)

    def test_malformed_policy_config_fail_closed(self):
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        (self.root / "assignment-policy.json").write_text("{not-json")
        bad_json = self.gate({"executor": "grok", "model": "grok-4.6"}, evaluate=fake)
        self.assertEqual(bad_json.action, "unavailable")
        self.assertEqual(bad_json.reason, "assignment_policy_config_invalid")

        (self.root / "assignment-policy.json").write_text(json.dumps(["enabled"]))
        bad_list = self.gate({"executor": "grok"}, evaluate=fake)
        self.assertEqual(bad_list.action, "unavailable")

        enable(self.root, extra={"daily_max_calls": "200"})
        bad_cap = self.gate({"executor": "claude", "model_reason": "x"}, evaluate=fake)
        self.assertEqual(bad_cap.action, "unavailable")
        self.assertEqual(bad_cap.reason, "assignment_policy_config_invalid")
        self.assertEqual(fake.calls, [])

    def test_old_approval_does_not_cover_changed_request(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        first = self.gate({"executor": "claude", "model": "opus",
                           "model_reason": "default timed out twice on this card"},
                          evaluate=fake, assignment_id="same")
        self.assertTrue(first.allowed)
        changed = self.gate({"executor": "codex", "model": "gpt-5.4",
                             "model_reason": "default timed out twice on this card"},
                            evaluate=fake, assignment_id="same")
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        self.assertEqual(len(fake.calls), 2)

    def test_identity_uses_model_reason_or_override_reason(self):
        ident = requested_identity(LIVE_SETTINGS, {"executor": "claude", "model_reason": "  a  "})
        self.assertEqual(ident["reason"], "a")
        ident = requested_identity(LIVE_SETTINGS, {"executor": "claude", "override_reason": "b"})
        self.assertEqual(ident["reason"], "b")
        self.assertFalse(
            fingerprint_for(card_num=1, assignment_id="x", identity=ident)
            == fingerprint_for(card_num=1, assignment_id="y", identity=ident))

    def test_malformed_outcome_unavailable(self):
        enable(self.root)
        out = self.gate({"executor": "claude", "model_reason": "default failed twice"},
                        evaluate=FakeEvaluate(object()), assignment_id="bad")
        self.assertEqual(out.action, "unavailable")
        self.assertEqual(out.reason, "malformed_verifier_response")
        rows = self.audit()
        self.assertEqual([row[2] for row in rows], ["reserved", "unavailable"])

    def test_legacy_unique_nonce_schema_migrates(self):
        db = sqlite3.connect(self.root / "assignment-policy.sqlite")
        db.executescript("""
            CREATE TABLE audit (
              id INTEGER PRIMARY KEY,
              nonce TEXT NOT NULL UNIQUE,
              fingerprint TEXT NOT NULL,
              assignment_id TEXT NOT NULL,
              card_num INTEGER NOT NULL,
              event TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              detail_json TEXT NOT NULL,
              created_at REAL NOT NULL
            );
        """)
        db.execute(
            "INSERT INTO audit(nonce, fingerprint, assignment_id, card_num, event, "
            "snapshot_json, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("old", "fp", "id", 1, "passthrough", "{}", "{}", 0),
        )
        db.commit()
        db.close()
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        out = self.gate(
            {"executor": "claude", "model_reason": "default timed out twice"},
            evaluate=fake, assignment_id="mig")
        self.assertTrue(out.allowed)
        events = [row[2] for row in self.audit()]
        self.assertEqual(events[-2:], ["reserved", "allowed"])


if __name__ == "__main__":
    unittest.main()
