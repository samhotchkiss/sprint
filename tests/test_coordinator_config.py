import json
import math
from pathlib import Path
import tempfile
import unittest

from sprint_coordinator.config import CoordinatorConfig, example_config, init_config


def configured(project):
    raw = example_config(str(project))
    raw["workers"] = {
        "low": {"command": ["/usr/bin/low-worker"], "role": "response"},
        "high": {"command": ["/usr/bin/high-worker"], "role": "response"},
    }
    return raw


class CoordinatorConfigTests(unittest.TestCase):
    def test_initialized_config_is_not_activation_ready_or_fake(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.json"
            init_config(path, Path(directory))
            raw = json.loads(path.read_text())
            self.assertIs(raw["activation_ready"], False)
            self.assertEqual(raw["allowed_checks"], [])
            self.assertNotIn("fake_worker", json.dumps(raw))
            self.assertNotIn("print('ok')", json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "activation_ready"):
                CoordinatorConfig(raw).validate_active()

    def test_zero_budgets_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = configured(Path(directory))
            raw.update(daily_max_calls=0, daily_max_spend_usd=0,
                       max_escalation_retries=0)
            config = CoordinatorConfig(raw)
            self.assertEqual((config.daily_max_calls, config.daily_max_spend_usd,
                              config.max_escalation_retries), (0, 0, 0))

    def test_negative_and_nonfinite_budgets_are_rejected(self):
        cases = (("daily_max_calls", -1), ("daily_max_spend_usd", -0.01),
                 ("daily_max_calls", 1.5),
                 ("daily_max_spend_usd", math.nan),
                 ("daily_max_spend_usd", math.inf))
        with tempfile.TemporaryDirectory() as directory:
            for key, value in cases:
                with self.subTest(key=key, value=value):
                    raw = configured(Path(directory))
                    raw[key] = value
                    with self.assertRaises(ValueError):
                        CoordinatorConfig(raw)

    def test_zero_and_nonfinite_timeouts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for key, value in (("worker_timeout_seconds", 0),
                               ("reply_deadline_seconds", math.nan)):
                raw = configured(Path(directory))
                raw[key] = value
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                    CoordinatorConfig(raw)

    def test_boolean_model_and_version_validation_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            for mutate, message in (
                (lambda raw: raw.update(activation_ready="false"), "boolean"),
                (lambda raw: raw["jev"].update(enabled="false"), "boolean"),
                (lambda raw: raw["jev"].update(model="jev-latest"), "evaluated model"),
                (lambda raw: raw.update(version=2), "unsupported"),
            ):
                raw = configured(Path(directory))
                mutate(raw)
                with self.assertRaisesRegex(ValueError, message):
                    CoordinatorConfig(raw)

    def test_checks_are_nonempty_unique_and_project_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = configured(root)
            raw["allowed_checks"] = [
                {"id": "unit", "command": ["pytest"], "timeout_seconds": 30}
            ]
            check = CoordinatorConfig(raw).allowed_checks[0]
            self.assertEqual(check["cwd"], str(root.resolve()))
            for checks in (
                [{"id": "unit", "command": []}],
                [{"id": "unit", "command": ["pytest"]},
                 {"id": "unit", "command": ["pytest"]}],
                [{"id": "escape", "command": ["pytest"], "cwd": "../"}],
            ):
                bad = configured(root)
                bad["allowed_checks"] = checks
                with self.subTest(checks=checks), self.assertRaises(ValueError):
                    CoordinatorConfig(bad)

    def test_active_mode_rejects_fake_workers_and_disabled_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = configured(Path(directory))
            raw["activation_ready"] = True
            CoordinatorConfig(raw).validate_active()
            raw["workers"]["low"]["command"] = ["sprint_coordinator.fake_worker"]
            with self.assertRaisesRegex(ValueError, "fake workers"):
                CoordinatorConfig(raw).validate_active()
            raw = configured(Path(directory))
            raw.update(activation_ready=True, require_jev_for_approval=False)
            with self.assertRaisesRegex(ValueError, "verification"):
                CoordinatorConfig(raw).validate_active()


if __name__ == "__main__":
    unittest.main()
