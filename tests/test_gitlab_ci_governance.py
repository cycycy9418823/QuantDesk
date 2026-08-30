from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GitlabCiGovernanceTests(unittest.TestCase):
    def test_001_gitlab_ci_exists_and_runs_offline_contract_gates(self) -> None:
        text = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

        self.assertIn("python:3.11-slim", text)
        self.assertIn("python -m compileall -q quantdesk scripts tests", text)
        self.assertIn("maintenance frontend-api-contract --json true --summary true", text)
        self.assertIn("maintenance dependency-contract --json true --summary true", text)
        self.assertIn("maintenance data-backtest-contract --json true --summary true", text)
        self.assertIn("maintenance coverage-contract --require-pytest-cov true --json true --summary true", text)
        self.assertIn("tests.test_coverage_contract_governance", text)
        self.assertIn("tests.test_gitlab_ci_governance", text)
        self.assertIn("python scripts/self_check.py", text)
        self.assertIn("node --check app/static/js/daily_research.js", text)
        self.assertIn("reports/coverage/", text)

    def test_002_gitlab_ci_keeps_research_only_boundaries(self) -> None:
        text = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8").lower()

        forbidden = [
            "git push",
            "export_project.ps1",
            "enable-network",
            "--network true",
            "broker",
            "real order",
            "qmt",
            "ptrade",
            "sourceportable",
        ]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, text)
        self.assertIn('quantdesk_network: "false"', text)
        self.assertIn('quantdesk_llm: "false"', text)


if __name__ == "__main__":
    unittest.main()
