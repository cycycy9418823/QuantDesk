from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class StrategyObservationContractAndAgentBridgeTests(unittest.TestCase):
    def _context(self):
        from quantdesk.strategy_observation_contract import build_strategy_observation_context

        return build_strategy_observation_context(
            symbol="600744",
            target_date="2026-05-19",
            data_health_payload={
                "health_level": "yellow",
                "summary_text": "daily available, intraday missing",
                "cards": {"intraday_data": {"status": "yellow"}},
                "data_source_trace": {"daily_data": "local_existing -> tencent_daily"},
            },
            provider_payload={
                "steps": [{"provider": "tencent_daily", "category": "daily_kline", "status": "ok", "ok": True}],
            },
            strategy_payload={"ok": True, "candidates": []},
            workflow_payload={"summary": {"plan_count": 1}},
            conditional_plan_payload={"trade_date": "2026-05-19", "plans": []},
            execution_payload={"summary": {"missing_intraday_count": 1}},
            intraday_payload={"target_trade_date": "2026-05-19", "missing_count": 1, "missing_symbols": ["600744"]},
            portfolio_payload={"cash": 100000, "positions": []},
            fractal_payload={"symbol": "600744", "fractals": [{"fractal_type": "top"}], "diagnostics": {"top_count": 1}},
            bi_payload={"symbol": "600744", "bi_list": [], "diagnostics": {}},
            segment_payload={"symbol": "600744", "segments": [], "diagnostics": {}},
        )

    def test_strategy_observation_context_required_fields(self) -> None:
        context = self._context()
        for key in (
            "schema_version",
            "symbol",
            "date",
            "data_health",
            "strategy_outputs",
            "provider_chain",
            "chanlun",
            "warnings",
            "disclaimer",
        ):
            self.assertIn(key, context)
        self.assertIn(context["schema_version"], {"3.1.3", "3.2.0", "3.2.1", "3.2.2", "3.2.3", "3.2.4", "3.2.5", "3.2.6", "3.2.6.1", "3.2.7", "3.2.8", "3.2.9", "3.3.0", "3.3.1", "3.3.2", "3.3.2.1", "3.3.2.2", "3.3.2.3", "3.3.2.4", "3.3.2.5", "3.3.3", "3.3.4", "3.3.5", "3.3.6", "3.3.7", "3.4.0", "3.4.1", "3.4.2", "3.4.2.1", "3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertEqual(context["symbol"], "600744")
        self.assertTrue(context["not_investment_advice"])

    def test_missing_intraday_marks_cannot_verify_trigger(self) -> None:
        context = self._context()
        self.assertTrue(context["cannot_verify_intraday_trigger"])
        self.assertEqual(context["intraday_status"]["status"], "cannot_verify_intraday_trigger")
        self.assertIn("600744", context["intraday_status"]["missing_symbols"])

    def test_observation_context_surfaces_verified_minute_pool_counts(self) -> None:
        from quantdesk.strategy_observation_contract import build_strategy_observation_context

        context = build_strategy_observation_context(
            symbol="600744",
            target_date="2026-07-03",
            data_health_payload={"health_level": "green", "summary_text": "ok"},
            provider_payload={"steps": []},
            strategy_payload={"ok": True, "candidates": []},
            workflow_payload={"summary": {"plan_count": 2}},
            conditional_plan_payload={"trade_date": "2026-07-03", "plans": []},
            execution_payload={
                "summary": {
                    "missing_intraday_count": 0,
                    "verified_completed_intraday_count": 1,
                    "local_unverified_intraday_count": 1,
                    "intraday_quality_gate": {
                        "pool_status": "passed",
                        "pool_passed": True,
                        "pool_job_path": "data/vendor_staging/top200",
                        "pool_source_snapshot": "last_passed",
                        "pool_latest_quality_status": "partial",
                        "pool_latest_snapshot_path": "results/data_health/tushare_minute_quality_latest.json",
                        "warnings": ["verified_pool_from_last_passed_snapshot"],
                        "quality_target_date": "2026-07-03",
                    },
                }
            },
            intraday_payload={
                "target_trade_date": "2026-07-03",
                "available_count": 2,
                "missing_count": 0,
                "verified_minute_pool_available_count": 1,
                "verified_minute_pool_missing_count": 1,
                "verified_minute_pool": {
                    "status": "passed",
                    "target_date": "2026-07-03",
                    "job_path": "data/vendor_staging/top200",
                    "source_snapshot": "last_passed",
                    "latest_quality_status": "partial",
                    "latest_snapshot_path": "results/data_health/tushare_minute_quality_latest.json",
                    "warnings": ["verified_pool_from_last_passed_snapshot"],
                },
            },
            portfolio_payload={"cash": 100000, "positions": []},
        )

        intraday = context["intraday_status"]
        self.assertFalse(context["cannot_verify_intraday_trigger"])
        self.assertEqual(intraday["status"], "available")
        self.assertEqual(intraday["verified_completed_intraday_count"], 1)
        self.assertEqual(intraday["local_unverified_intraday_count"], 1)
        self.assertTrue(intraday["verified_minute_pool"]["passed"])
        self.assertEqual(intraday["verified_minute_pool"]["available_count"], 1)
        self.assertEqual(intraday["verified_minute_pool"]["source_snapshot"], "last_passed")
        self.assertEqual(intraday["verified_minute_pool"]["latest_quality_status"], "partial")
        self.assertIn("verified_pool_from_last_passed_snapshot", intraday["verified_minute_pool"]["warnings"])

    def test_unreliable_data_health_marks_observation_only(self) -> None:
        context = self._context()
        self.assertTrue(context["observation_only"])
        self.assertTrue(context["data_health"]["observation_only"])

    def test_agent_bridge_is_offline_and_template_only(self) -> None:
        from quantdesk.agent.agent_safe_bridge import build_agent_readonly_context, generate_stub_observation_report

        readonly = build_agent_readonly_context(self._context())
        self.assertFalse(readonly["network"])
        self.assertFalse(readonly["llm_called"])
        payload = generate_stub_observation_report(self._context())
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["llm_called"])
        self.assertFalse(payload["network"])

    def test_stub_report_is_chinese_learning_review_and_policy_safe(self) -> None:
        from quantdesk.agent.agent_safe_bridge import generate_stub_observation_report

        report = generate_stub_observation_report(self._context())["report"]
        self.assertIn("学习复盘", report)
        self.assertIn("不构成投资建议", report)
        for forbidden in ("寤鸿涔板叆", "鎺ㄨ崘涔板叆", "鐩爣浠", "姝㈡崯浠", "浠撲綅"):
            self.assertNotIn(forbidden, report)

    def test_policy_guard_detects_chinese_and_english_terms(self) -> None:
        from quantdesk.agent.agent_policy_guard import detect_forbidden_trading_advice

        self.assertIn("target price", detect_forbidden_trading_advice("Do not write target price."))
        self.assertIn("target price", detect_forbidden_trading_advice("Do not write target price."))

    def test_policy_guard_sanitizes_forbidden_terms(self) -> None:
        from quantdesk.agent.agent_policy_guard import sanitize_forbidden_trading_terms, validate_agent_allowed_output

        text = sanitize_forbidden_trading_terms("target price 10 and stop loss 8")
        self.assertNotIn("target price", text.lower())
        self.assertNotIn("stop loss", text.lower())
        self.assertTrue(validate_agent_allowed_output(text)["ok"])

    def test_qd_help_contains_observation_report(self) -> None:
        qd = PROJECT_ROOT / "scripts" / "qd.py"
        result = subprocess.run([sys.executable, str(qd), "help"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("observation-report", result.stdout)

    def test_observation_report_cli_network_false_is_offline(self) -> None:
        script = PROJECT_ROOT / "scripts" / "run_strategy_observation_report.py"
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
            result = subprocess.run(
                [sys.executable, str(script), "--symbol", "600744", "--target-date", "2026-05-19", "--network", "false"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("network=false", result.stdout)
        self.assertTrue((PROJECT_ROOT / "results" / "strategy_observation_context_latest.json").exists())
        self.assertTrue((PROJECT_ROOT / "results" / "strategy_observation_report_latest.md").exists())

    def test_clean_zip_excludes_strategy_observation_results(self) -> None:
        export_text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("results", export_text)
        self.assertIn("reports", export_text)

    def test_docs_and_ui_text_are_wired(self) -> None:
        qd = (PROJECT_ROOT / "scripts" / "qd.py").read_text(encoding="utf-8")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        app = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("run_strategy_observation_report.py", qd)
        self.assertIn("Strategy Observation Contract And Agent Safe Bridge", readme)
        self.assertIn("生成学习复盘报告", html + app)
        self.assertIn("当前报告模式", html + app)


if __name__ == "__main__":
    unittest.main()





