from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quantdesk.paper import paper_vs_backtest_consistency as consistency

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PaperVsBacktestConsistencyTests(unittest.TestCase):
    def test_no_fill_does_not_change_cash_and_position(self) -> None:
        payload = {"events": [{"symbol": "600744", "action": "no_fill", "cash_before": 1000, "cash_after": 1000, "position_before": 0, "position_after": 0}]}
        result = consistency.run_paper_vs_backtest_consistency_check(
            target_date="2026-05-19",
            execution_payload=payload,
            strategy_validation_payload={},
            manifest_payload={},
            provider_payload={},
            strict=True,
            write=False,
        )
        self.assertFalse(any(item["type"] == "no_fill_state_change" for item in result["issues"]))

    def test_missing_intraday_does_not_change_cash_and_position(self) -> None:
        payload = {"events": [{"symbol": "600744", "action": "no_fill", "fill_model": "missing_intraday", "cash_delta": 0, "position_delta": 0}]}
        result = consistency.run_paper_vs_backtest_consistency_check(
            target_date="2026-05-19",
            execution_payload=payload,
            strategy_validation_payload={},
            manifest_payload={},
            provider_payload={},
            strict=True,
            write=False,
        )
        self.assertFalse(any(item["type"] == "missing_intraday_state_change" for item in result["issues"]))

    def test_state_change_on_no_fill_is_flagged(self) -> None:
        issues = consistency.run_paper_vs_backtest_consistency_check(
            target_date="2026-05-19",
            execution_payload={"events": [{"symbol": "600744", "action": "no_fill", "cash_delta": -100, "position_delta": 100}]},
            strategy_validation_payload={},
            manifest_payload={},
            provider_payload={},
            write=False,
        )["issues"]
        self.assertTrue(any(item["type"] == "no_fill_state_change" for item in issues))

    def test_reliable_event_requires_intraday_evidence(self) -> None:
        payload = {"events": [{"symbol": "600744", "action": "buy", "is_reliable": True, "fill_model": "intraday_1m_close"}]}
        issues = consistency.detect_reliable_event_without_intraday_evidence(payload, {})
        self.assertTrue(any(item["type"] == "reliable_event_missing_intraday_evidence" for item in issues))
        ok = consistency.detect_reliable_event_without_intraday_evidence(payload, {"available_symbols": ["600744"]})
        self.assertEqual(ok, [])

    def test_board_candidate_limit_up_risk_is_warning(self) -> None:
        result = consistency.run_paper_vs_backtest_consistency_check(
            target_date="2026-05-19",
            execution_payload={"events": [{"symbol": "600744", "action": "buy", "board_candidate": True, "limit_up_price": 10, "fill_price": 10}]},
            strategy_validation_payload={},
            manifest_payload={},
            provider_payload={},
            strict=True,
            write=False,
        )
        self.assertTrue(any(item["type"] == "board_candidate_fill_risk" for item in result["warnings"]))

    def test_consistency_check_cli_and_qd_help(self) -> None:
        qd = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8")
        self.assertIn("consistency-check", qd)
        self.assertIn("check_paper_vs_backtest_consistency.py", qd)

    def test_network_false_does_not_call_network(self) -> None:
        with mock.patch.object(consistency, "read_json", return_value={}) as mocked_read:
            result = consistency.run_paper_vs_backtest_consistency_check(symbol="600744", target_date="2026-05-19", network=False, write=False)
        self.assertFalse(result["network"])
        self.assertTrue(mocked_read.called)

    def test_cli_runs_network_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "check_paper_vs_backtest_consistency.py"),
                    "--symbol",
                    "600744",
                    "--target-date",
                    "2026-05-19",
                    "--network",
                    "false",
                    "--input-dir",
                    str(Path(tmp)),
                    "--output-dir",
                    str(Path(tmp)),
                    "--report-dir",
                    str(Path(tmp)),
                    "--strict",
                    "false",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("不主动联网", completed.stdout)

    def test_observation_context_contains_consistency_warning(self) -> None:
        from quantdesk.strategy_observation_contract import build_strategy_observation_context

        context = build_strategy_observation_context(
            symbol="600744",
            target_date="2026-05-19",
            data_health_payload={"status": "ok"},
            provider_payload={},
            strategy_payload={},
            workflow_payload={},
            conditional_plan_payload={},
            execution_payload={},
            intraday_payload={},
            portfolio_payload={},
            validation_payload={},
            consistency_payload={"status": "warning", "issue_count": 0, "warning_count": 1, "summary": {"consistency_status": "warning"}},
        )
        self.assertEqual(context["consistency_summary"]["status"], "warning")
        self.assertIn("当前纸面模拟与回测存在一致性风险，本报告仅作学习观察。", context["warnings"])

    def test_clean_zip_excludes_consistency_outputs(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8")
        self.assertIn('"results"', export_text)
        self.assertIn('"reports"', export_text)


if __name__ == "__main__":
    unittest.main()



