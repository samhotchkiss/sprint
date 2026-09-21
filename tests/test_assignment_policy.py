"""Injected verifier only. No TypeSafe network calls."""
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sprint_coordinator.assignment_policy import (
    fingerprint_for, gate_assignment, requested_identity,
)
from sprint_coordinator.jev import JevServiceError, VerificationOutcome


SETTINGS = {
    "worker": {
        "default_executor": "grok",
        "executors": {
            "grok": {"kind": "session", "model": "grok-4"},
            "claude": {"kind": "session", "model": "opus"},
            "codex": {"kind": "session", "model": "gpt-5.4"},
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


def enable(root: Path, daily_max=20):
    (root / "assignment-policy.json").write_text(json.dumps({
        "enabled": True,
        "key_file": "~/.config/sprint/typesafe.env",
        "daily_max_calls": daily_max,
    }))


class AssignmentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def gate(self, payload, **kwargs):
        return gate_assignment(
            self.root, settings=SETTINGS, card_num=kwargs.pop("card_num", 12),
            payload=payload, **kwargs)

    def usage(self):
        db = sqlite3.connect(self.root / "assignment-policy.sqlite")
        try:
            row = db.execute("SELECT calls FROM usage").fetchone()
            return 0 if row is None else row[0]
        finally:
            db.close()

    def test_disabled_allows_without_verifier(self):
        fake = FakeEvaluate(VerificationOutcome("deny", "nope"))
        out = self.gate({"executor": "claude", "model_reason": "x"}, evaluate=fake)
        self.assertTrue(out.allowed)
        self.assertEqual(out.reason, "assignment_policy_disabled")
        self.assertEqual(fake.calls, [])

    def test_default_executor_skips_paid_call(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("deny", "nope"))
        out = self.gate({"executor": "grok", "model": "grok-4"}, evaluate=fake,
                        assignment_id="a1")
        self.assertTrue(out.allowed)
        self.assertEqual(out.reason, "default_executor")
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

    def test_override_accept_uses_evaluate_provider_override_kwargs(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok", {"reason_specific": 0.9}, 12))
        reason = "Default Grok timed out twice on card 12 tool loop with the same stack."
        out = self.gate(
            {"executor": "codex", "model": "gpt-5.4", "override_reason": reason,
             "evidence": {"attempts": ["timeout"]}},
            evaluate=fake, assignment_id="asg-3", task="fix card 12 loop")
        self.assertTrue(out.allowed)
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["default_provider"], "grok")
        self.assertEqual(call["requested_provider"], "codex")
        self.assertEqual(call["requested_model"], "gpt-5.4")
        self.assertEqual(call["reason"], reason)
        self.assertEqual(self.usage(), 1)
        self.assertNotIn("TYPESAFE", json.dumps(out.as_dict()))
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

    def test_outage_and_missing_injected_client_fail_closed(self):
        enable(self.root)
        out = self.gate(
            {"executor": "claude", "override_reason": "default crashed twice"},
            evaluate=FakeEvaluate(JevServiceError("HTTP 503")),
            assignment_id="asg-down")
        self.assertEqual(out.action, "unavailable")
        self.assertEqual(out.reason, "verifier_unavailable")
        self.assertEqual(self.usage(), 1)

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

    def test_distinct_assignments_do_not_share_approval(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        payload = {"executor": "claude", "model_reason": "default timed out twice on this card"}
        a = self.gate(payload, evaluate=fake, assignment_id="one")
        b = self.gate(payload, evaluate=fake, assignment_id="two")
        self.assertTrue(a.allowed and b.allowed)
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(len(fake.calls), 2)

    def test_revalidate_same_assignment_skips_second_call(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("accept", "ok"))
        payload = {"executor": "claude", "model_reason": "default timed out twice on this card"}
        first = self.gate(payload, evaluate=fake, assignment_id="keep")
        again = self.gate(payload, evaluate=fake, assignment_id="keep")
        self.assertTrue(again.allowed)
        self.assertEqual(again.reason, "same_assignment_fingerprint")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(first.fingerprint, again.fingerprint)

    def test_existing_live_assignment_not_gated(self):
        enable(self.root)
        fake = FakeEvaluate(VerificationOutcome("deny", "nope"))
        out = self.gate({"executor": "claude"}, evaluate=fake, existing=True,
                        assignment_id="live")
        self.assertTrue(out.allowed)
        self.assertEqual(out.reason, "existing_assignment_preserved")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.usage(), 0)

    def test_identity_uses_model_reason_or_override_reason(self):
        ident = requested_identity(SETTINGS, {"executor": "claude", "model_reason": "  a  "})
        self.assertEqual(ident["reason"], "a")
        ident = requested_identity(SETTINGS, {"executor": "claude", "override_reason": "b"})
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


if __name__ == "__main__":
    unittest.main()
