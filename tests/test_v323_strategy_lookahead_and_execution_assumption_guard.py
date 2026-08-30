from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


class StrategyLookaheadGuardTests(unittest.TestCase):
    def test_future_dates_and_target_leakage_are_detected(self) -> None:
        from quantdesk.strategy_lookahead_guard import (
            build_strategy_timeline_context,
            detect_future_dates_in_inputs,
            detect_target_date_leakage,
        )

        rows = [{"date": "2026-05-19", "close": 10}, {"date": "2026-05-20", "close": 11}]
        issues = detect_future_dates_in_inputs(rows, "2026-05-19")
        self.assertTrue(any(item["type"] == "future_date_in_input" for item in issues))
        context = build_strategy_timeline_context(target_date="2026-05-19", inputs=rows)
        leakage = detect_target_date_leakage(context)
        self.assertTrue(any(item["type"] == "post_target_row" for item in leakage))

    def test_artifact_generated_at_does_not_trigger_future_date(self) -> None:
        # ``generated_at``/``retrieved_at`` are produced-artifact timestamps
        # (when the report was written), NOT the date of the underlying data
        # rows. A payload whose only future-looking value is ``generated_at``
        # must not be flagged as look-ahead leakage.
        from quantdesk.strategy_lookahead_guard import detect_future_dates_in_inputs

        payload = {"generated_at": "2026-07-02", "row_count": 5}
        issues = detect_future_dates_in_inputs(payload, "2026-07-01")
        self.assertEqual(issues, [])

    def test_content_date_still_flagged_alongside_artifact_timestamp(self) -> None:
        # A genuine future *content* date must still be detected even when a
        # future artifact timestamp is present in the same row.
        from quantdesk.strategy_lookahead_guard import detect_future_dates_in_inputs

        rows = [{"trade_date": "2026-07-05", "generated_at": "2026-07-02", "close": 10}]
        issues = detect_future_dates_in_inputs(rows, "2026-07-01")
        self.assertTrue(
            any(item["type"] == "future_date_in_input" and "trade_date" in item.get("path", "") for item in issues)
        )

    def test_forward_conditional_plan_is_not_lookahead_error(self) -> None:
        # A conditional-execution plan carries a ``plan_id`` and a ``trade_date``
        # for the NEXT trading day (it is produced today to be executed later).
        # Its future trade_date is a forward-plan schedule, NOT historical
        # look-ahead leakage, so it must be an info-level ``forward_plan_date``
        # rather than an error-level ``future_date_in_input``.
        from quantdesk.strategy_lookahead_guard import detect_future_dates_in_inputs

        plan = {"plan_id": "plan_20260702_for_20260703", "trade_date": "2026-07-03"}
        issues = detect_future_dates_in_inputs(plan, "2026-07-01")
        types = {item["type"] for item in issues}
        self.assertNotIn("future_date_in_input", types)
        self.assertTrue(any(item["type"] == "forward_plan_date" for item in issues))
        forward = next(item for item in issues if item["type"] == "forward_plan_date")
        self.assertEqual(forward.get("severity"), "info")

    def test_forward_plan_does_not_fail_lookahead_validation(self) -> None:
        # A forward conditional plan must not push the guard into failed/serious.
        from quantdesk.strategy_lookahead_guard import validate_strategy_no_lookahead

        result = validate_strategy_no_lookahead(
            target_date="2026-07-01",
            executions={"plan_id": "plan_20260702_for_20260703", "trade_date": "2026-07-03"},
            strict=True,
        )
        types = {item["type"] for item in result["issues"]}
        self.assertNotIn("future_date_in_input", types)
        self.assertNotEqual(result["status"], "failed")

    def test_daily_bar_intraday_proxy_is_detected(self) -> None:
        from quantdesk.strategy_lookahead_guard import detect_daily_bar_intraday_proxy

        issues = detect_daily_bar_intraday_proxy({"fill_model": "daily_high_low", "trigger_source": "daily_close"})
        self.assertTrue(any(item["type"] == "daily_bar_intraday_proxy" for item in issues))

    def test_missing_intraday_cannot_be_reliable_execution(self) -> None:
        from quantdesk.strategy_lookahead_guard import validate_strategy_no_lookahead

        result = validate_strategy_no_lookahead(
            target_date="2026-05-19",
            executions=[{"action": "buy", "actual_fill_model": "missing_intraday", "is_reliable": True}],
            intraday_status={"status": "missing_intraday"},
        )
        issue_types = {item["type"] for item in result["issues"]}
        self.assertIn("reliable_execution_without_intraday", issue_types)
        self.assertIn("missing_intraday_marked_as_fill", issue_types)
        self.assertEqual(result["status"], "failed")


class ExecutionAssumptionGuardTests(unittest.TestCase):
    def test_no_fill_and_missing_intraday_do_not_change_state(self) -> None:
        from quantdesk.execution_assumption_guard import validate_execution_assumptions

        payload = [
            {"action": "no_fill", "cash_delta": 0, "position_delta": 0},
            {"action": "no_fill", "fill_model": "missing_intraday", "cash_before": 1000, "cash_after": 1000, "position_before": 0, "position_after": 0},
        ]
        result = validate_execution_assumptions(payload)
        self.assertFalse(any(item["type"] == "no_fill_state_change" for item in result["issues"]))

    def test_state_change_on_no_fill_is_detected(self) -> None:
        from quantdesk.execution_assumption_guard import validate_execution_assumptions

        result = validate_execution_assumptions([{"action": "no_fill", "cash_delta": -100, "position_delta": 100}])
        self.assertTrue(any(item["type"] == "no_fill_state_change" for item in result["issues"]))

    def test_board_candidate_and_limit_up_fill_risk_are_detected(self) -> None:
        from quantdesk.execution_assumption_guard import (
            detect_board_candidate_fill_risk,
            detect_limit_up_fill_assumption,
        )

        payload = [{"action": "buy", "is_board_candidate": True, "fill_price": 11.0, "limit_up_price": 11.0}]
        self.assertTrue(detect_board_candidate_fill_risk(payload))
        self.assertTrue(detect_limit_up_fill_assumption(payload))

    def test_assumptions_include_board_candidate_execution(self) -> None:
        from quantdesk.backtest import default_backtest_assumptions

        assumptions = default_backtest_assumptions()
        self.assertIn("board_candidate_execution", assumptions)
        self.assertIn("不代表真实可成交", assumptions["board_candidate_execution"])


class StrategyValidationSuiteTests(unittest.TestCase):
    def test_suite_writes_json_and_markdown_report(self) -> None:
        from quantdesk.shared import write_json
        from quantdesk.strategy_validation_suite import run_strategy_validation_suite

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "inputs"
            output_dir = root / "results"
            report_dir = root / "reports"
            input_dir.mkdir()
            write_json(
                input_dir / "conditional_execution_latest.json",
                {
                    "events": [
                        {"action": "buy", "fill_model": "daily_high_low", "date": "2026-05-20"},
                        {"action": "no_fill", "cash_delta": 0, "position_delta": 0},
                    ]
                },
            )
            result = run_strategy_validation_suite(
                symbol="600744",
                target_date="2026-05-19",
                input_dir=input_dir,
                output_dir=output_dir,
                report_dir=report_dir,
                network=False,
                strict=True,
            )
            self.assertEqual(result["status"], "failed")
            self.assertTrue((output_dir / "strategy_validation_latest.json").exists())
            self.assertTrue((report_dir / "strategy_validation_report_latest.md").exists())

    def test_strategy_validate_cli_and_qd_help(self) -> None:
        qd = subprocess.run(
            [str(PYTHON), "scripts/qd.py", "help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(qd.returncode, 0, qd.stderr)
        self.assertIn("strategy-validate", qd.stdout)

    def test_strategy_validate_network_false_does_not_call_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    str(PYTHON),
                    "scripts/run_strategy_validation.py",
                    "--symbol",
                    "600744",
                    "--target-date",
                    "2026-05-19",
                    "--network",
                    "false",
                    "--input-dir",
                    tmp,
                    "--output-dir",
                    tmp,
                    "--strict",
                    "false",
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("network: False", completed.stdout)
            self.assertTrue((Path(tmp) / "strategy_validation_latest.json").exists())


class ObservationAndAuditIntegrationTests(unittest.TestCase):
    def test_validation_failed_is_in_observation_context(self) -> None:
        from quantdesk.strategy_observation_contract import build_strategy_observation_context

        context = build_strategy_observation_context(
            symbol="600744",
            target_date="2026-05-19",
            validation_payload={
                "status": "failed",
                "ok": False,
                "issue_count": 2,
                "summary": {"lookahead_status": "failed", "execution_assumption_status": "warning"},
            },
            data_health_payload={"status": "ok"},
            provider_payload={},
            strategy_payload={},
            workflow_payload={},
            conditional_plan_payload={},
            execution_payload={},
            intraday_payload={},
            portfolio_payload={},
        )
        self.assertEqual(context["validation_summary"]["status"], "failed")
        self.assertTrue(any("当前策略验证存在风险" in item for item in context["warnings"]))

    def test_agent_report_audit_keeps_validation_summary(self) -> None:
        from quantdesk.agent.agent_evidence_pack import build_agent_evidence_pack
        from quantdesk.agent.agent_report_audit import build_report_audit_record

        context = {
            "symbol": "600744",
            "date": "2026-05-19",
            "warnings": [],
            "data_gaps": [],
            "validation_summary": {"status": "failed", "issue_count": 1},
        }
        evidence = build_agent_evidence_pack(context)
        record = build_report_audit_record(
            context,
            evidence,
            {"provider": "offline_stub", "report": "仅用于学习复盘，不构成投资建议。", "llm_used": False, "fallback_used": True},
        )
        self.assertEqual(record["validation_summary"]["status"], "failed")
        self.assertEqual(evidence["validation_summary"]["status"], "failed")


class CalendarAndCleanZipTests(unittest.TestCase):
    def test_trading_calendar_single_definitions(self) -> None:
        source = (ROOT / "quantdesk" / "trading_calendar.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        load_count = sum(1 for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_load_calendar")
        note_count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id == "WEEKEND_ONLY_NOTE"
        )
        self.assertEqual(load_count, 1)
        self.assertEqual(note_count, 1)

    def test_clean_zip_rules_exclude_validation_outputs(self) -> None:
        text = (ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn('"results"', text)
        self.assertIn('"reports"', text)
        self.assertIn('".quantdesk_test_tmp"', text)


if __name__ == "__main__":
    unittest.main()


