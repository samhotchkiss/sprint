"""Message quality: mocked Jev only. No paid calls or board publication."""
from __future__ import annotations

import unittest

from sprint_coordinator.message_quality import (
    DIMENSIONS, FORMAT_TOOLS, GUIDANCE_VISIBLE_CHARS, HARD_VISIBLE_CHARS,
    QualityDecision, assess_message, generation_instructions, validate_envelope,
)


class FakeResponse:
    def __init__(self, scores, input_tokens=4, output_tokens=1):
        self.answers = {
            name: {"type": "noul", "noul": float(scores[name])} for name in DIMENSIONS
        }
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeClient:
    def __init__(self, scores=None, error=None):
        self.scores = scores or {name: 0.95 for name in DIMENSIONS}
        self.error = error
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.scores)


def passing(**overrides):
    scores = {name: 0.95 for name in DIMENSIONS}
    scores.update(overrides)
    return scores


class EnvelopeTests(unittest.TestCase):
    def test_valid_markdown_and_optional_detail(self):
        visible = "Tests failed on card 12.\n\n- blocker: missing fixture\n- next: add the fixture"
        ok = validate_envelope("card", visible, detail="stack trace here")
        self.assertIsInstance(ok, dict)
        self.assertEqual(ok["surface"], "card")
        self.assertEqual(ok["visible"], visible)
        self.assertEqual(ok["detail"], "stack trace here")
        self.assertEqual(ok["notes"], ())

    def test_rejects_bad_surface_empty_and_nul(self):
        bad_surface = validate_envelope("thread", "ok")
        self.assertEqual(bad_surface.action, "reject")
        self.assertEqual(bad_surface.reason, "invalid_surface")
        self.assertEqual(validate_envelope("sidebar", "   ").reason, "visible_empty")
        self.assertEqual(validate_envelope("sidebar", None).reason, "visible_not_string")
        self.assertEqual(validate_envelope("sidebar", "ok\x00").reason, "visible_nul")
        self.assertEqual(validate_envelope("sidebar", "ok", detail="").reason, "detail_empty")
        self.assertEqual(validate_envelope("sidebar", "ok", detail=1).reason, "detail_not_string")

    def test_guidance_is_not_a_hard_cut_and_does_not_truncate(self):
        text = "A" * (GUIDANCE_VISIBLE_CHARS + 20)
        ok = validate_envelope("sidebar", text)
        self.assertIsInstance(ok, dict)
        self.assertEqual(ok["visible"], text)
        self.assertEqual(ok["visible_chars"], len(text))
        self.assertIn("over_guidance", ok["notes"])

    def test_hard_cap_rejects_without_slicing(self):
        text = "B" * (HARD_VISIBLE_CHARS + 1)
        decision = validate_envelope("sidebar", text)
        self.assertIsInstance(decision, QualityDecision)
        self.assertEqual(decision.action, "reject")
        self.assertEqual(decision.reason, "visible_too_long")
        self.assertEqual(decision.visible_chars, len(text))
        self.assertFalse(decision.verified)


class AssessTests(unittest.TestCase):
    def test_accept_requires_all_dimensions(self):
        client = FakeClient(passing())
        decision = assess_message(
            client=client, surface="sidebar",
            visible="Ship is unblocked. Merge when you are ready.",
            outcome="say whether merge is allowed",
            context="unit checks passed",
        )
        self.assertEqual(decision.action, "accept")
        self.assertIs(decision.verified, True)
        self.assertEqual(set(decision.dimensions), set(DIMENSIONS))
        self.assertEqual(decision.failing, ())
        self.assertFalse(decision.as_dict()["rewritten"])
        state, questions = client.calls[0]
        self.assertEqual(state["visible"], "Ship is unblocked. Merge when you are ready.")
        self.assertEqual(set(questions), set(DIMENSIONS))

    def test_revise_returns_exact_failing_dimensions(self):
        client = FakeClient(passing(
            answer_first=0.1,
            no_superfluous_narration=0.2,
        ))
        draft = "I looked through several files and considered approaches.\n\nAnyway the header still overlaps."
        decision = assess_message(
            client=client, surface="card", visible=draft,
            outcome="fix the overlapping header",
        )
        self.assertEqual(decision.action, "revise")
        self.assertIs(decision.verified, False)
        self.assertEqual(decision.failing, ("no_superfluous_narration", "answer_first"))
        self.assertEqual(decision.reason, "dimensions_unmet")
        self.assertEqual(draft, client.calls[0][0]["visible"])

    def test_threshold_is_inclusive_and_exact(self):
        low = FakeClient(passing(readable_structure=0.79))
        high = FakeClient(passing(readable_structure=0.80))
        args = dict(surface="sidebar", visible="Done. Restart the preview.",
                    outcome="tell the user the next command")
        self.assertEqual(assess_message(client=low, **args).action, "revise")
        self.assertEqual(assess_message(client=high, **args).action, "accept")

    def test_hidden_blocker_is_revise_not_accept(self):
        client = FakeClient(passing(preserves_decision_material=0.05))
        decision = assess_message(
            client=client, surface="sidebar",
            visible="All good.",
            outcome="report whether deploy is safe",
            context="migration is blocked on missing backup",
        )
        self.assertEqual(decision.action, "revise")
        self.assertIn("preserves_decision_material", decision.failing)
        self.assertIs(decision.verified, False)

    def test_unavailable_verifier_is_fail_closed(self):
        missing = assess_message(
            client=None, surface="sidebar", visible="Answer first.",
            outcome="reply")
        self.assertEqual(missing.action, "unavailable")
        self.assertIs(missing.verified, False)
        self.assertEqual(missing.reason, "verifier_unavailable")

        broken = FakeClient(error=RuntimeError("timeout"))
        down = assess_message(
            client=broken, surface="sidebar", visible="Answer first.",
            outcome="reply")
        self.assertEqual(down.action, "unavailable")
        self.assertIs(down.verified, False)
        self.assertNotEqual(down.action, "accept")

    def test_malformed_scores_fail_closed(self):
        class Bad:
            answers = {"advances_next_decision": {"noul": 0.9}}
            input_tokens = 1
            output_tokens = 0

        class Client:
            def evaluate(self, state, questions):
                return Bad()

        decision = assess_message(
            client=Client(), surface="sidebar", visible="Hello.",
            outcome="greet")
        self.assertEqual(decision.action, "unavailable")
        self.assertEqual(decision.reason, "incomplete_verifier_response")
        self.assertIs(decision.verified, False)

    def test_envelope_failure_does_not_call_jev(self):
        client = FakeClient()
        decision = assess_message(
            client=client, surface="sidebar", visible="  ",
            outcome="reply")
        self.assertEqual(decision.action, "reject")
        self.assertEqual(client.calls, [])

    def test_generation_instructions_are_formatting_tools(self):
        text = generation_instructions(
            surface="sidebar", outcome="say the next command",
            context="preview is running")
        self.assertIn("say the next command", text)
        self.assertIn(str(GUIDANCE_VISIBLE_CHARS), text)
        for item in FORMAT_TOOLS:
            self.assertIn(item, text)
        self.assertIn("Lead with the answer", text)

    def test_over_guidance_can_still_accept(self):
        client = FakeClient(passing())
        visible = "Next: run the fixture test.\n\n" + ("ok " * 200)
        decision = assess_message(
            client=client, surface="card", visible=visible,
            outcome="tell the next test command")
        self.assertEqual(decision.action, "accept")
        self.assertIn("over_guidance", decision.envelope_notes)
        self.assertEqual(decision.visible_chars, len(visible))


if __name__ == "__main__":
    unittest.main()
