#!/usr/bin/env python3
"""Packaging checks for the Codex Sprint adapter."""

import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bin" / "sprint-codex-install"
ADAPTER = ROOT / "codex" / "skills" / "sprint"


class TestCodexInstall(unittest.TestCase):
    def run_installer(self, codex_home: pathlib.Path):
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        return subprocess.run(
            [str(INSTALLER)],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_installs_symlink_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="sprint-codex-test-") as tmp:
            codex_home = pathlib.Path(tmp) / "codex"
            first = self.run_installer(codex_home)
            self.assertEqual(first.returncode, 0, first.stderr)

            target = codex_home / "skills" / "sprint"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), ADAPTER.resolve())

            second = self.run_installer(codex_home)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("already installed", second.stdout)

    def test_refuses_to_replace_an_existing_skill(self):
        with tempfile.TemporaryDirectory(prefix="sprint-codex-test-") as tmp:
            codex_home = pathlib.Path(tmp) / "codex"
            target = codex_home / "skills" / "sprint"
            target.mkdir(parents=True)
            marker = target / "keep-me"
            marker.write_text("user-owned\n", encoding="utf-8")

            result = self.run_installer(codex_home)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("leaving it untouched", result.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "user-owned\n")

    def test_adapter_is_portable_and_declares_codex_mappings(self):
        text = (ADAPTER / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("/Users/", text)
        self.assertIn("collaboration.spawn_agent", text)
        self.assertIn("yield_control", text)
        self.assertIn("gpt-5.6-luna", text)
        self.assertIn("namespace worker branches with the board slug", text)
        self.assertTrue((ADAPTER / "agents" / "openai.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
