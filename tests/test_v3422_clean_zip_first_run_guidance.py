from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantdesk.data_freshness_gate import evaluate_data_freshness
from quantdesk.data_freshness_summary import build_data_freshness_summary
from quantdesk.remediation_advisor import build_remediation_plan
from quantdesk.status_advisor import build_quantdesk_status


class CleanZipFirstRunGuidanceTests(unittest.TestCase):
    def test_data_health_empty_pool_prefers_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            freshness = build_data_freshness_summary(
                symbols="",
                target_date="2026-05-22",
                daily_dir=root / "data" / "daily",
                low_price_universe_path=root / "data" / "universe" / "missing.csv",
                write=False,
            )
        self.assertEqual(freshness["freshness_status"], "no_universe")
        self.assertEqual(freshness["warning_kind"], "no_universe")
        self.assertEqual(
            freshness["suggested_next_action"],
            "python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
        )
        plan = build_remediation_plan(data_health={"freshness": freshness})
        command = plan["actions"][0]["command"]
        self.assertIn("data-bootstrap", command)
        self.assertNotIn("data-update --scope current_analysis_pool", command)

    def test_daily_no_universe_is_not_fresh(self) -> None:
        gate = evaluate_data_freshness(
            daily_price_payload={
                "target_date": "2026-05-22",
                "total_symbols": 0,
                "latest_local_date": "",
                "max_local_date": "",
                "stale_count": 0,
                "missing_count": 0,
            },
            daily_feature_payload={},
        )
        self.assertEqual(gate["data_freshness_status"], "no_universe")
        self.assertEqual(gate["status"], "warning")
        self.assertEqual(
            gate["suggested_next_action"],
            "python scripts/qd.py data-bootstrap --preset low_price_demo --network false",
        )
        self.assertNotEqual(gate["data_freshness_status"], "fresh")

    def test_daily_no_data_does_not_claim_review_threshold(self) -> None:
        gate = evaluate_data_freshness(
            daily_price_payload={
                "target_date": "2026-05-22",
                "total_symbols": 2,
                "latest_local_date": "",
                "max_local_date": "",
                "stale_count": 0,
                "missing_count": 0,
            },
            daily_feature_payload={},
        )
        self.assertEqual(gate["data_freshness_status"], "no_data")
        self.assertNotIn("满足复盘门槛", gate["message"])

    def test_daily_next_actions_no_universe_prefers_offline_bootstrap(self) -> None:
        from quantdesk.daily_output_summary import build_daily_next_actions

        actions = build_daily_next_actions(
            {
                "summary": {"data_freshness_status": "no_universe", "target_complete_trade_date": "2026-05-22"},
                "daily_price_status": {"target_date": "2026-05-22", "total_symbols": 0},
            }
        )
        commands = [item["command"] for item in actions]
        self.assertIn("python scripts/qd.py data-bootstrap --preset low_price_demo --network false", commands)
        self.assertNotIn("python scripts/qd.py data-bootstrap --preset low_price_demo --enable-network", commands)

    def test_qd_help_exposes_data_bootstrap(self) -> None:
        from scripts import qd

        self.assertIn("data-bootstrap", qd.COMMANDS)
        self.assertIn("screen", qd.COMMANDS)
        self.assertIn("walk-forward", qd.COMMANDS)
        self.assertIn("paper", qd.COMMANDS)

    def test_status_and_fix_plan_first_run_prioritize_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = build_remediation_plan(project_root=root)
            status = build_quantdesk_status(project_root=root, write=False)
        self.assertEqual(plan["actions"][0]["command"], "python scripts/qd.py data-bootstrap --preset low_price_demo --network false")
        self.assertEqual(status["suggested_next_action"], "python scripts/qd.py data-bootstrap --preset low_price_demo --network false")


if __name__ == "__main__":
    unittest.main()
