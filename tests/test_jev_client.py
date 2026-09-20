import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib import error

from sprint_coordinator.jev import (
    JevClient, JevConfigurationError, JevResponseError, JevServiceError, MODEL,
    load_api_key, route_request, verify_candidate,
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
        good = dict(task_satisfied=.95, constraints_satisfied=.9, evidence_supports=.88, material_uncertainty=.05)
        self.assertTrue(verify_candidate(FakeClient(good), task="fix", constraints=["safe"],
                                         candidate="done", evidence=evidence, code_change=True,
                                         independent_checks=checks).accepted)
        for defect in ("task_satisfied", "constraints_satisfied", "evidence_supports"):
            values = dict(good, **{defect: .4})
            self.assertEqual(verify_candidate(FakeClient(values), task="fix", constraints=[],
                                               candidate="done", evidence=evidence).action, "escalate")
        self.assertEqual(verify_candidate(FakeClient(dict(good, material_uncertainty=.6)), task="fix",
                                           constraints=[], candidate="done", evidence=evidence).action, "escalate")

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
        values = dict(task_satisfied=.9, constraints_satisfied=.9,
                      evidence_supports=.9, material_uncertainty=.1)
        client = FakeClient(values)
        verify_candidate(client, task="x", constraints=[], candidate="y", evidence={})
        criteria = client.calls[0][1]["material_uncertainty"]["criteria"]
        self.assertIn("insufficient", criteria["true"])
        self.assertNotIn("insufficient", criteria["false"])

    def test_verifier_questions_never_offer_authorization(self):
        client = FakeClient(dict(task_satisfied=.9, constraints_satisfied=.9,
                                 evidence_supports=.9, material_uncertainty=.1))
        verify_candidate(client, task="deploy", constraints=[], candidate="do it", evidence={})
        encoded = json.dumps(client.calls[0])
        self.assertNotIn("authoriz", encoded.lower())


if __name__ == "__main__":
    unittest.main()
