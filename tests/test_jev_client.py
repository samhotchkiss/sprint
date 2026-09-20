import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib import error

from sprint_coordinator.jev import (
    JevClient, JevConfigurationError, JevError, JevResponseError, JevServiceError, MODEL,
    evaluate_provider_override, load_api_key, route_request, verify_candidate,
)


class FakeHTTPResponse:
    def __init__(self, document):
        self.body = json.dumps(document).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.body


def response(answers, model=MODEL):
    return {"model": model, "answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 2}}


class CredentialTests(unittest.TestCase):
    def test_environment_wins_and_secret_file_supports_export(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "typesafe.env"
            path.write_text("export TYPESAFE_API_KEY='file-key'\n")
            self.assertEqual(load_api_key({"TYPESAFE_API_KEY": "env-key"}, path), "env-key")
            self.assertEqual(load_api_key({}, path), "file-key")

    def test_missing_key_is_explicit(self):
        with self.assertRaisesRegex(JevConfigurationError, "not configured"):
            load_api_key({}, Path("/definitely/missing/typesafe.env"))


class ClientTests(unittest.TestCase):
    def test_posts_pinned_model_bearer_and_timeout(self):
        observed = {}
        def opener(req, timeout):
            observed.update(payload=json.loads(req.data), auth=req.get_header("Authorization"), timeout=timeout)
            return FakeHTTPResponse(response({"q": {"type": "noul", "noul": .9}}))
        result = JevClient("secret", timeout=2.5, opener=opener).evaluate(
            {"text": "x"}, {"q": {"type": "noul", "instructions": "yes?"}})
        self.assertEqual(observed["payload"]["model"], MODEL)
        self.assertEqual(observed["auth"], "Bearer secret")
        self.assertEqual(observed["timeout"], 2.5)
        self.assertEqual(result.input_tokens, 10)

    def test_retries_only_bounded_transient_failure(self):
        calls, sleeps = [], []
        def opener(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise error.HTTPError(req.full_url, 529, "busy", {"Retry-After": "0"}, io.BytesIO())
            return FakeHTTPResponse(response({"q": {"type": "noul", "noul": .1}}))
        JevClient("secret", max_attempts=3, opener=opener, sleep=sleeps.append).evaluate(
            "x", {"q": {"type": "noul", "instructions": "yes?"}})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.0, 0.0])

    def test_rejects_wrong_model_missing_answers_and_invalid_probability(self):
        question = {"q": {"type": "noul", "instructions": "yes?"}}
        defects = [
            response({"q": {"type": "noul", "noul": .9}}, "jev-latest"),
            response({}),
            response({"q": {"type": "noul", "noul": 1.2}}),
            response({"q": {"type": "noul", "noul": float("nan")}}),
        ]
        for defect in defects:
            with self.subTest(defect=defect), self.assertRaises(JevResponseError):
                JevClient("secret", opener=lambda *a, d=defect, **k: FakeHTTPResponse(d)).evaluate("x", question)

    def test_provider_auth_error_is_explicit_and_not_retried(self):
        calls = []
        def opener(req, timeout):
            calls.append(1)
            raise error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"detail":"bad"}'))
        with self.assertRaisesRegex(JevServiceError, "HTTP 401"):
            JevClient("secret", opener=opener).evaluate(
                "x", {"q": {"type": "noul", "instructions": "yes?"}})
        self.assertEqual(len(calls), 1)

    def test_malformed_provider_json_is_rejected(self):
        class Malformed(FakeHTTPResponse):
            def read(self):
                return b"not-json"
        with self.assertRaisesRegex(JevResponseError, "invalid JSON"):
            JevClient("secret", opener=lambda *a, **k: Malformed({})).evaluate(
                "x", {"q": {"type": "noul", "instructions": "yes?"}})

    def test_choice_validation_rejects_unknown_option(self):
        questions = {"q": {"type": "choice", "instructions": "pick", "criteria": {"a": None, "b": None}}}
        bad = response({"q": {"type": "choice", "choice": "c",
                              "probabilities": {"a": .5, "b": .5}, "confidence": .8}})
        with self.assertRaises(JevResponseError):
            JevClient("secret", opener=lambda *a, **k: FakeHTTPResponse(bad)).evaluate("x", questions)


class FakeClient:
    def __init__(self, answers=None, failure=None):
        self.answers, self.failure, self.calls = answers, failure, []
    def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if self.failure:
            raise self.failure
        answers = {key: {"type": "noul", "noul": self.answers[key]} for key in questions}
        from sprint_coordinator.jev import JevResponse
        return JevResponse(MODEL, answers, 7, 1)


PASSING_VERIFY = dict(
    task_satisfied=.95, constraints_satisfied=.9, requires_independent_evidence=.91,
    evidence_supports=.88, material_uncertainty=.05, unsupported_claims=.04,
)

SELF_CONTAINED_VERIFY = dict(
    task_satisfied=.96, constraints_satisfied=.94, requires_independent_evidence=.08,
    evidence_supports=.53, material_uncertainty=.04, unsupported_claims=.03,
)

OVERRIDE_ACCEPT = dict(
    reason_specific=.92, evidence_supports_need=.88, model_fits=.86, forbidden_convenience=.07,
)
OVERRIDE_PREFER = dict(
    reason_specific=.06, evidence_supports_need=.05, model_fits=.12, forbidden_convenience=.94,
)


class PolicyTests(unittest.TestCase):
    def test_route_preserves_multiple_intents(self):
        client = FakeClient({"answer": .91, "build": .82, "cancel": .1})
        outcome = route_request(client, message="answer then build", context={},
                                intents={"answer": "answer", "build": "build", "cancel": "cancel"})
        self.assertEqual(outcome.action, "route")
        self.assertEqual(outcome.intents, ("answer", "build"))
        self.assertIn("message", client.calls[0][0])

    def test_no_route_and_service_failure_escalate(self):
        low = route_request(FakeClient({"build": .4}), message="unclear", context={}, intents={"build": "build"})
        self.assertEqual(low.action, "escalate")
        missing = route_request(FakeClient(failure=JevConfigurationError("not configured")),
                                message="x", context={}, intents={"build": "build"})
        self.assertEqual(missing.action, "escalate")

    def test_code_cannot_pass_without_independent_executable_check(self):
        client = FakeClient({})
        outcome = verify_candidate(client, task="fix", constraints=[], candidate="done",
                                   evidence={"executable_checks": [{"passed": True, "source": "independent", "command": "pytest"}]},
                                   code_change=True)
        self.assertEqual(outcome.action, "escalate")
        self.assertEqual(client.calls, [])

    def test_all_verification_dimensions_must_pass(self):
        evidence = {"worker_report": "tests pass"}
        checks = [{"passed": True, "command": "pytest"}]
        good = dict(PASSING_VERIFY)
        self.assertTrue(verify_candidate(FakeClient(good), task="fix", constraints=["safe"],
                                         candidate="done", evidence=evidence, code_change=True,
                                         independent_checks=checks).accepted)
        for defect in ("task_satisfied", "constraints_satisfied", "evidence_supports"):
            values = dict(good, **{defect: .4})
            self.assertEqual(verify_candidate(FakeClient(values), task="fix", constraints=[],
                                               candidate="done", evidence=evidence).action, "escalate")
        self.assertEqual(verify_candidate(FakeClient(dict(good, material_uncertainty=.6)), task="fix",
                                           constraints=[], candidate="done", evidence=evidence).action, "escalate")
        self.assertEqual(verify_candidate(FakeClient(dict(good, unsupported_claims=.7)), task="fix",
                                           constraints=[], candidate="done", evidence=evidence).action, "escalate")

    def test_self_contained_answer_does_not_require_external_evidence(self):
        outcome = verify_candidate(
            FakeClient(SELF_CONTAINED_VERIFY),
            task="In one short sentence, what does 2+2 equal?",
            constraints=[],
            candidate="2+2 equals 4.",
            evidence={},
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.judgments["evidence_supports"], .53)

    def test_uncertain_evidence_requirement_fails_closed(self):
        values = dict(SELF_CONTAINED_VERIFY, requires_independent_evidence=.5)
        outcome = verify_candidate(
            FakeClient(values), task="2+2?", constraints=[], candidate="4", evidence={},
        )
        self.assertEqual(outcome.action, "escalate")
        self.assertFalse(outcome.accepted)

    def test_external_claim_still_needs_independent_evidence(self):
        values = dict(PASSING_VERIFY, evidence_supports=.53)
        outcome = verify_candidate(
            FakeClient(values),
            task="Merge the billing branch to main.",
            constraints=[],
            candidate="Merged. Tests are green.",
            evidence={},
        )
        self.assertEqual(outcome.action, "escalate")

    def test_every_independent_check_must_pass(self):
        client = FakeClient({})
        outcome = verify_candidate(
            client, task="fix", constraints=[], candidate="done", evidence={}, code_change=True,
            independent_checks=[
                {"passed": True, "command": "unit tests"},
                {"passed": False, "command": "integration tests"},
            ],
        )
        self.assertEqual(outcome.action, "escalate")
        self.assertEqual(client.calls, [])

    def test_uncertainty_criteria_treat_missing_evidence_as_uncertain(self):
        client = FakeClient(dict(PASSING_VERIFY))
        verify_candidate(client, task="x", constraints=[], candidate="y", evidence={})
        criteria = client.calls[0][1]["material_uncertainty"]["criteria"]
        self.assertIn("insufficient", criteria["true"])
        self.assertNotIn("insufficient", criteria["false"])

    def test_verifier_questions_never_offer_authorization(self):
        client = FakeClient(dict(PASSING_VERIFY))
        verify_candidate(client, task="deploy", constraints=[], candidate="do it", evidence={})
        encoded = json.dumps(client.calls[0])
        self.assertNotIn("authoriz", encoded.lower())

    def test_blank_override_reason_denied_without_model(self):
        client = FakeClient(OVERRIDE_ACCEPT)
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                outcome = evaluate_provider_override(
                    client, task="fix header", default_provider="grok",
                    requested_provider="claude", default_model="grok-4",
                    requested_model="opus", reason=reason, evidence={"note": "please"},
                )
                self.assertEqual(outcome.action, "deny")
                self.assertEqual(client.calls, [])

    def test_matching_provider_and_model_is_not_an_override(self):
        client = FakeClient(OVERRIDE_PREFER)
        outcome = evaluate_provider_override(
            client, task="fix header", default_provider="Grok", requested_provider="grok",
            default_model="grok-4", requested_model="Grok-4", reason="I prefer Claude",
            evidence={},
        )
        self.assertEqual(outcome.action, "accept")
        self.assertEqual(client.calls, [])

    def test_preference_override_is_denied(self):
        outcome = evaluate_provider_override(
            FakeClient(OVERRIDE_PREFER),
            task="In one short sentence, what does 2+2 equal?",
            default_provider="grok", requested_provider="claude",
            default_model="grok-4", requested_model="opus",
            reason="I prefer Claude", evidence={},
        )
        self.assertEqual(outcome.action, "deny")

    def test_repeated_default_failure_can_approve_override(self):
        evidence = {
            "attempts": [
                {"provider": "grok", "model": "grok-4", "error": "tool call truncated after 3 identical timeouts on /api/cards/12"},
                {"provider": "grok", "model": "grok-4", "error": "same timeout on the same card after retry"},
            ],
        }
        outcome = evaluate_provider_override(
            FakeClient(OVERRIDE_ACCEPT),
            task="Card 12: repair the truncated tool-call loop in sprintd.",
            default_provider="grok", requested_provider="codex",
            default_model="grok-4", requested_model="gpt-5.4",
            reason="Default Grok timed out twice on this card's tool loop with identical traces; Codex completed the same fixture locally.",
            evidence=evidence,
        )
        self.assertEqual(outcome.action, "accept")
        self.assertTrue(outcome.accepted)

    def test_override_service_error_fails_closed(self):
        outcome = evaluate_provider_override(
            FakeClient(failure=JevServiceError("HTTP 503")),
            task="fix", default_provider="grok", requested_provider="claude",
            default_model="a", requested_model="b", reason="default failed twice with timeouts",
            evidence={"errors": ["timeout"]},
        )
        self.assertEqual(outcome.action, "deny")
        self.assertFalse(outcome.accepted)

    def test_override_uncertainty_escalates(self):
        mixed = dict(reason_specific=.6, evidence_supports_need=.61, model_fits=.58,
                     forbidden_convenience=.4)
        outcome = evaluate_provider_override(
            FakeClient(mixed), task="fix", default_provider="grok", requested_provider="claude",
            default_model="a", requested_model="b", reason="maybe this is hard", evidence={},
        )
        self.assertEqual(outcome.action, "escalate")

    def test_override_questions_are_independent_nouls_and_provider_neutral(self):
        client = FakeClient(OVERRIDE_ACCEPT)
        evaluate_provider_override(
            client, task="t", default_provider="grok", requested_provider="claude",
            default_model="a", requested_model="b", reason="default crashed twice",
            evidence={"log": "segfault"},
        )
        questions = client.calls[0][1]
        self.assertEqual(set(questions), {
            "reason_specific", "evidence_supports_need", "model_fits", "forbidden_convenience",
        })
        self.assertTrue(all(q["type"] == "noul" for q in questions.values()))
        encoded = json.dumps(client.calls[0]).lower()
        self.assertIn("equivalent", encoded)
        self.assertNotIn("authoriz", encoded)


def _live_client():
    try:
        return JevClient(load_api_key(), timeout=12.0, max_attempts=2)
    except JevError:
        return None


class LiveJevTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _live_client()
        cls.results = {}

    def setUp(self):
        if self.client is None:
            self.skipTest("TypeSafe API key is not configured")

    def test_live_self_contained_arithmetic_accepts(self):
        outcome = verify_candidate(
            self.client,
            task="In one short sentence, what does 2+2 equal?",
            constraints=[],
            candidate="2+2 equals 4.",
            evidence={},
        )
        type(self).results["arithmetic"] = {
            "action": outcome.action, "judgments": dict(outcome.judgments),
            "reason": outcome.reason, "usage_tokens": outcome.usage_tokens,
        }
        self.assertEqual(outcome.action, "accept", type(self).results["arithmetic"])

    def test_live_preference_override_denied(self):
        outcome = evaluate_provider_override(
            self.client,
            task="In one short sentence, what does 2+2 equal?",
            default_provider="grok", requested_provider="claude",
            default_model="grok-4", requested_model="opus",
            reason="I prefer Claude", evidence={},
        )
        type(self).results["prefer_claude"] = {
            "action": outcome.action, "judgments": dict(outcome.judgments),
            "reason": outcome.reason, "usage_tokens": outcome.usage_tokens,
        }
        self.assertEqual(outcome.action, "deny", type(self).results["prefer_claude"])

    def test_live_failed_default_override_can_accept(self):
        evidence = {
            "attempts": [
                {"provider": "grok", "error": "HTTP 000 timeout after 120s on card 12 tool loop"},
                {"provider": "grok", "error": "retry: same timeout, identical stack at session_worker.py:88"},
            ],
        }
        outcome = evaluate_provider_override(
            self.client,
            task="Card 12: the worker's tool-call loop truncates and retries forever.",
            default_provider="grok", requested_provider="codex",
            default_model="grok-4", requested_model="gpt-5.4",
            reason=(
                "Default Grok timed out twice on this exact card with the same stack in "
                "session_worker.py:88; the requested Codex profile completed the truncated "
                "tool-loop fixture on this machine."
            ),
            evidence=evidence,
        )
        type(self).results["failed_default"] = {
            "action": outcome.action, "judgments": dict(outcome.judgments),
            "reason": outcome.reason, "usage_tokens": outcome.usage_tokens,
        }
        self.assertEqual(outcome.action, "accept", type(self).results["failed_default"])

    def test_live_external_completion_claim_does_not_accept_without_evidence(self):
        outcome = verify_candidate(
            self.client,
            task="Merge the billing branch to main and report the merge sha.",
            constraints=[],
            candidate="Merged to main as deadbeef. All tests are green.",
            evidence={},
        )
        type(self).results["unsupported_merge"] = {
            "action": outcome.action, "judgments": dict(outcome.judgments),
            "reason": outcome.reason, "usage_tokens": outcome.usage_tokens,
        }
        self.assertNotEqual(outcome.action, "accept", type(self).results["unsupported_merge"])


if __name__ == "__main__":
    unittest.main()
