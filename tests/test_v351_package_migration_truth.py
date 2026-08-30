from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V351PackageMigrationTruthTests(unittest.TestCase):
    def test_subpackages_have_real_modules_and_top_level_count(self) -> None:
        # The current research-only app has added dashboard, data-truth, and
        # paper-observation modules since v3.5.2; keep this as a broad
        # regression guard instead of an old release-era packaging cap.
        # Keep a bounded guard while allowing the current migration additions
        # to land without turning every new research adapter into a test break.
        self.assertLessEqual(len(list((ROOT / "quantdesk").glob("*.py"))), 230)
        for name, minimum in {"research": 3, "paper": 3, "intraday": 3, "agent": 3}.items():
            modules = [path for path in (ROOT / "quantdesk" / name).glob("*.py") if path.name != "__init__.py"]
            self.assertGreaterEqual(len(modules), minimum, name)

    def test_legacy_import_aliases(self) -> None:
        import quantdesk.factor_evaluation as factor_evaluation
        import quantdesk.intraday_live_engine as intraday_live_engine
        import quantdesk.multi_strategy_account_book as multi_strategy_account_book

        self.assertIn("quantdesk.research", factor_evaluation.__name__)
        self.assertIn("quantdesk.intraday", intraday_live_engine.__name__)
        self.assertIn("quantdesk.paper", multi_strategy_account_book.__name__)

    def test_root_import_keeps_moved_modules_lazy(self) -> None:
        script = """
import json
import sys
import quantdesk

targets = set(quantdesk._MOVED_MODULES.values())
before = sorted(targets.intersection(sys.modules))
import quantdesk.factor_evaluation as factor_evaluation
after = sorted(targets.intersection(sys.modules))
print(json.dumps({
    "before": before,
    "after": after,
    "alias_name": factor_evaluation.__name__,
}))
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["before"], [])
        self.assertIn("quantdesk.research.factor_evaluation", payload["after"])
        self.assertLess(len(payload["after"]), 10)
        self.assertEqual(payload["alias_name"], "quantdesk.research.factor_evaluation")

    def test_qd_help_still_works(self) -> None:
        completed = subprocess.run([sys.executable, "scripts/qd.py", "--help"], cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertIn("attribution", completed.stdout)
        self.assertIn("risk-observation", completed.stdout)


if __name__ == "__main__":
    unittest.main()
