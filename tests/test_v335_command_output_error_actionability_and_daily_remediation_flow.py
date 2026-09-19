from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = Path(tempfile.gettempdir()) / ".quantdesk_test_tmp"


class V335CommandOutputRemediationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def _temp_dir(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_version_is_v335_and_remediation_module_exists(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

        self.assertIn(PACKAGE_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertTrue(APP_VERSION.startswith(API_SCHEMA_VERSION))
        self.assertTrue((PROJECT_ROOT / "quantdesk" / "remediation_advisor.py").exists())

    def test_remediation_advisor_classifies_core_warning_kinds(self) -> None:
        from quantdesk.remediation_advisor import (
            build_missing_intraday_remediation,
            build_remediation_plan,
            build_schema_warning_remediation,
        )

        stale = build_remediation_plan(
            data_update={
                "status": "warning",
                "target_date": "2026-05-21",
                "coverage_date": "2026-05-15",
                "latest_date_max": "2026-05-20",
                "stale_count": 78,
            }
        )
        self.assertEqual(stale["status"], "warning")
        self.assertIn("data-update", stale["actions"][0]["command"])
        self.assertIn("--enable-network", stale["actions"][0]["command"])
        self.assertTrue(stale["actions"][0]["network_required"])

        intraday = build_missing_intraday_remediation({"missing_count": 2, "target_date": "2026-05-21"})
        self.assertEqual(intraday[0]["warning_kind"], "intraday_missing")
        self.assertNotIn("high", intraday[0]["command"].lower())
        self.assertNotIn("low", intraday[0]["command"].lower())
        self.assertIn("data/intraday/1m/2026-05-21/{symbol}.csv", intraday[0]["follow_up_command"])
        self.assertNotIn("{target_date}", intraday[0]["follow_up_command"])

        low = build_schema_warning_remediation({"status": "warning", "confirmed_secret_leak_count": 0, "secret_like_reference_count": 3})
        self.assertNotEqual(low[0]["severity"], "critical")
        high = build_schema_warning_remediation({"status": "failed", "confirmed_secret_leak_count": 1})
        self.assertEqual(high[0]["severity"], "critical")

    def test_remediation_plan_filters_stale_market_intel_target_actions(self) -> None:
        from quantdesk.remediation_advisor import build_remediation_plan

        plan = build_remediation_plan(
            data_health={
                "status": "warning",
                "target_date": "2026-06-10",
                "freshness": {
                    "target_date": "2026-06-10",
                    "coverage_date": "2026-06-05",
                    "latest_date_max": "2026-06-05",
                    "stale_count": 1,
                    "missing_count": 0,
                },
            },
            market_intel_payload={
                "status": "warning",
                "target_date": "2026-05-21",
                "remediation_actions": [
                    {
                        "warning_kind": "intraday_missing",
                        "priority": "medium",
                        "severity": "warning",
                        "command": "python scripts/qd.py data-health --target-date 2026-05-21 --network false --scope current_analysis_pool",
                        "follow_up_command": "导入 data/intraday/1m/2026-05-21/{symbol}.csv 后重新运行 market-intel",
                    }
                ],
            },
        )
        commands = "\n".join(f"{item.get('command', '')} {item.get('follow_up_command', '')}" for item in plan["actions"])
        self.assertEqual(plan["target_date"], "2026-06-10")
        self.assertIn("market-intel --target-date 2026-06-10", commands)
        self.assertNotIn("2026-05-21", commands)

    def test_remediation_plan_filters_market_intel_placeholder_target_actions(self) -> None:
        from quantdesk.remediation_advisor import build_remediation_plan

        plan = build_remediation_plan(
            data_health={
                "status": "warning",
                "target_date": "2026-06-10",
                "freshness": {
                    "target_date": "2026-06-10",
                    "coverage_date": "2026-06-05",
                    "latest_date_max": "2026-06-05",
                    "stale_count": 1,
                    "missing_count": 0,
                },
            },
            market_intel_payload={
                "status": "warning",
                "target_date": "",
                "remediation_actions": [
                    {
                        "warning_kind": "intraday_missing",
                        "priority": "medium",
                        "severity": "warning",
                        "command": "python scripts/qd.py data-health --target-date YYYY-MM-DD --network false --scope current_analysis_pool",
                        "follow_up_command": "导入 data/intraday/1m/YYYY-MM-DD/{symbol}.csv 后重新运行 market-intel",
                    }
                ],
            },
        )
        commands = "\n".join(f"{item.get('command', '')} {item.get('follow_up_command', '')}" for item in plan["actions"])
        self.assertEqual(plan["target_date"], "2026-06-10")
        self.assertNotIn("YYYY-MM-DD", commands)

    def test_remediation_plan_dedupes_duplicate_intraday_actions(self) -> None:
        from quantdesk.remediation_advisor import build_remediation_plan

        plan = build_remediation_plan(
            data_health={
                "status": "warning",
                "target_date": "2026-06-10",
                "freshness": {
                    "status": "passed",
                    "target_date": "2026-06-10",
                    "coverage_date": "2026-06-10",
                    "latest_date_max": "2026-06-10",
                    "stale_count": 0,
                    "missing_count": 0,
                    "symbol_count": 1,
                },
                "intraday": {"target_date": "2026-06-10", "missing_count": 1},
            },
            market_intel_payload={
                "status": "warning",
                "target_date": "2026-06-10",
                "remediation_actions": [
                    {
                        "warning_kind": "intraday_missing",
                        "priority": "medium",
                        "severity": "warning",
                        "command": "python scripts/qd.py data-health --target-date 2026-06-10 --network false --scope current_analysis_pool",
                        "follow_up_command": "瀵煎叆 data/intraday/1m/2026-06-10/{symbol}.csv 鍚庨噸鏂拌繍琛?market-intel",
                    },
                    {
                        "warning_kind": "sector_mapping_missing",
                        "priority": "medium",
                        "severity": "warning",
                        "command": "python scripts/qd.py market-intel --network false --scope current_analysis_pool",
                        "follow_up_command": "瀵煎叆 data/universe/stock_theme_map.csv 鍚庨噸鏂拌繍琛?market-intel",
                    },
                ],
            },
        )
        kinds = [item.get("warning_kind") for item in plan["actions"]]
        self.assertEqual(kinds.count("intraday_missing"), 1)
        self.assertIn("sector_mapping_missing", kinds)

    def test_fix_plan_prioritizes_data_gates_monitoring_when_download_queue_is_active(self) -> None:
        from quantdesk.remediation_advisor import build_remediation_plan

        plan = build_remediation_plan(
            data_update={
                "status": "warning",
                "target_complete_trade_date": "2026-07-03",
                "effective_target_date": "2026-07-03",
                "data_scope": "current_analysis_pool",
                "symbol_count": 50,
                "coverage_date": "2026-06-29",
                "latest_date_max": "2026-07-02",
                "trading_day_lag": 0,
                "stale_count": 914,
                "missing_count": 0,
            },
            data_health={
                "status": "warning",
                "target_date": "2026-07-03",
                "data_health_summary": {
                    "extended_data_current_gate": "download_queue",
                    "extended_data_gate_suggested_next_action": "continue_monitoring",
                    "extended_data_operator_wait_reason": "download_queue_active",
                    "extended_data_safe_next_commands": [
                        "python scripts/qd.py tushare-queue-status --summary true",
                        "python scripts/qd.py tushare-minute-status --summary true",
                    ],
                    "extended_data_blocked_without_confirmation": [
                        "python scripts/qd.py import-extended-data --apply true",
                    ],
                    "extended_data_queue_liveness": "active",
                    "extended_data_queue_progress_state": "advancing",
                },
                "freshness": {
                    "target_date": "2026-07-03",
                    "stale_count": 914,
                    "missing_count": 0,
                },
            },
        )

        self.assertEqual(plan["status"], "warning")
        self.assertEqual(plan["actions"][0]["warning_kind"], "download_queue_active")
        self.assertEqual(plan["actions"][0]["command"], "python scripts/qd.py tushare-queue-status --summary true")
        self.assertFalse(plan["actions"][0]["network_required"])
        self.assertIn("continue_monitoring", plan["actions"][0]["reason"])
        self.assertIn(
            "python scripts/qd.py import-extended-data --apply true",
            plan["actions"][0]["blocked_without_confirmation"],
        )
        runnable_commands = "\n".join(
            str(action.get(key) or "")
            for action in plan["actions"]
            for key in ("command", "follow_up_command")
        )
        self.assertNotIn("data-update --target-date 2026-07-03", runnable_commands)
        self.assertNotIn("import-extended-data --apply true", runnable_commands)

    def test_fix_plan_reads_direct_data_gates_snapshot_when_data_health_is_stale(self) -> None:
        from quantdesk.remediation_advisor import build_remediation_plan

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_update" / "data_update_latest.json",
                {
                    "status": "warning",
                    "target_complete_trade_date": "2026-07-03",
                    "effective_target_date": "2026-07-03",
                    "data_scope": "current_analysis_pool",
                    "symbol_count": 50,
                    "trading_day_lag": 0,
                    "stale_count": 914,
                    "missing_count": 0,
                },
            )
            self._write_json(
                root / "results" / "data_health" / "data_health_dashboard_latest.json",
                {
                    "status": "warning",
                    "target_date": "2026-07-03",
                    "freshness": {
                        "target_date": "2026-07-03",
                        "stale_count": 914,
                        "missing_count": 0,
                    },
                },
            )
            self._write_json(
                root / "results" / "data_health" / "extended_data_gates_latest.json",
                {
                    "status": "warning",
                    "current_gate": "download_queue",
                    "suggested_next_action": "continue_monitoring",
                    "operator_wait_reason": "download_queue_active",
                    "operator_action_policy": {
                        "safe_next_commands": [
                            "python scripts/qd.py tushare-queue-status --summary true",
                        ],
                        "blocked_without_confirmation": [
                            "python scripts/qd.py import-extended-data --apply true",
                        ],
                    },
                    "gate_chain": {
                        "queue": {
                            "status": "running",
                            "liveness": "active",
                            "progress_state": "advancing",
                        }
                    },
                },
            )
            plan = build_remediation_plan(project_root=root)

        self.assertEqual(plan["suggested_next_action"], "python scripts/qd.py tushare-queue-status --summary true")
        self.assertEqual(plan["actions"][0]["warning_kind"], "download_queue_active")
        self.assertEqual(plan["actions"][0]["command"], "python scripts/qd.py tushare-queue-status --summary true")

    def test_fix_plan_reports_stale_download_queue_without_calling_it_active(self) -> None:
        from quantdesk.remediation_advisor import build_remediation_plan

        plan = build_remediation_plan(
            data_health={"status": "warning", "target_date": "2026-08-11"},
            extended_data_gates={
                "status": "warning",
                "current_gate": "download_queue",
                "suggested_next_action": "restart_queue_after_user_approval",
                "operator_wait_reason": "download_queue_stale",
                "operator_action_policy": {
                    "safe_next_commands": [
                        "python scripts/qd.py tushare-queue-status --summary true",
                    ],
                    "blocked_without_confirmation": [
                        "python scripts/qd.py import-extended-data --apply true",
                    ],
                },
                "gate_chain": {
                    "queue": {
                        "status": "running",
                        "queue_liveness": "stale",
                        "progress_state": "no_change",
                    }
                },
            },
        )

        action = plan["actions"][0]
        self.assertEqual(action["warning_kind"], "download_queue_stale")
        self.assertIn("stale", action["reason"].lower())
        self.assertNotIn(" is active", action["reason"].lower())

    def test_daily_console_summary_contains_remediation_plan(self) -> None:
        from quantdesk.daily_output_summary import build_daily_console_summary, format_daily_console_summary

        payload = {
            "status": "warning",
            "scope": "low_price_included_only",
            "summary": {"target_complete_trade_date": "2026-05-21", "daily_price_stale_count": 78},
            "daily_price_status": {
                "target_date": "2026-05-21",
                "coverage_date": "2026-05-15",
                "latest_date_max": "2026-05-20",
                "stale_count": 78,
                "total_symbols": 80,
            },
            "guardrail_summary": {"enabled": True, "warning_count": 3, "failed_count": 0},
            "artifacts": {"daily_report": "reports/daily_quant_report_latest.md", "daily_result": "results/daily_workflow_latest.json"},
        }
        summary = build_daily_console_summary(payload)
        self.assertIn("remediation_actions", summary)
        self.assertIn("data-update", summary["remediation_actions"][0]["command"])
        text = format_daily_console_summary({"daily_console_summary": summary})
        self.assertIn("我现在该怎么修", text)
        self.assertIn("影响：", text)

    def test_status_json_contains_remediation_actions_and_console_is_concise(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status, format_status_console_output

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_update" / "data_update_latest.json",
                {
                    "status": "warning",
                    "target_date": "2026-05-21",
                    "coverage_date": "2026-05-15",
                    "latest_date_max": "2026-05-20",
                    "trading_day_lag": 4,
                    "stale_count": 78,
                    "symbol_count": 80,
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
            text = format_status_console_output(payload)
        self.assertIn("remediation_actions", payload)
        self.assertLessEqual(len([line for line in text.splitlines() if line.strip()]), 15)
        self.assertIn("优先修复", text)
        self.assertIn("data-update", text)

    def test_status_remediation_plan_includes_market_intelligence_actions(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_health" / "data_health_dashboard_latest.json",
                {
                    "status": "passed",
                    "target_date": "2026-06-10",
                    "freshness": {
                        "status": "passed",
                        "target_date": "2026-06-10",
                        "coverage_date": "2026-06-10",
                        "latest_date_max": "2026-06-10",
                        "stale_count": 0,
                        "missing_count": 0,
                        "symbol_count": 1,
                    },
                },
            )
            self._write_json(
                root / "results" / "market_intelligence" / "market_intelligence_latest.json",
                {
                    "status": "warning",
                    "target_date": "2026-06-10",
                    "remediation_actions": [
                        {
                            "priority": "medium",
                            "category": "data",
                            "severity": "warning",
                            "warning_kind": "sector_mapping_missing",
                            "title": "板块映射不足",
                            "command": "python scripts/qd.py market-intel --network false --scope current_analysis_pool",
                            "follow_up_command": "导入 data/universe/stock_theme_map.csv 后重新运行 market-intel",
                            "network_required": False,
                        }
                    ],
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
        kinds = [item.get("warning_kind") for item in payload.get("remediation_actions", [])]
        self.assertIn("sector_mapping_missing", kinds)

    def test_fix_plan_command_is_read_only_and_outputs_copyable_commands(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "fix-plan"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("QuantDesk 修复计划", completed.stdout)
        self.assertIn("python scripts/qd.py", completed.stdout)
        path = PROJECT_ROOT / "results" / "fix_plan_latest.json"
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(payload["network"])
        self.assertFalse(payload["llm_enabled"])

    def test_command_center_reads_remediation_from_status_and_fix_plan(self) -> None:
        from quantdesk import command_center_status as ccs
        from quantdesk.operation_log import append_operation_log, build_operation_record

        with self._temp_dir() as tmp:
            root = Path(tmp)
            action = {
                "title": "本地日线数据过期",
                "command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool",
                "impact": "daily 可信度下降。",
            }
            self._write_json(root / "results" / "status_latest.json", {"remediation_actions": [action]})
            self._write_json(root / "results" / "fix_plan_latest.json", {"actions": [action]})
            log_path = root / "results" / "operation_log" / "operation_timeline.jsonl"
            append_operation_log(build_operation_record(command="status", status="warning", output_paths=["results/status_latest.json"]), log_path)
            append_operation_log(build_operation_record(command="fix-plan", status="warning", output_paths=["results/fix_plan_latest.json"]), log_path)
            old_root = ccs.PROJECT_ROOT
            try:
                ccs.PROJECT_ROOT = root
                payload = ccs.build_command_center_status(log_path=log_path)
            finally:
                ccs.PROJECT_ROOT = old_root
        rows = {row["command"]: row for row in payload["commands"]}
        self.assertIn("fix-plan", rows)
        self.assertEqual(rows["status"]["top_remediation"]["title"], "本地日线数据过期")
        self.assertIn("data-update", rows["fix-plan"]["suggested_next_action"])

    def test_artifact_center_contains_remediation_command_field(self) -> None:
        from quantdesk.workbench_overview import build_workbench_artifacts

        with self._temp_dir() as tmp:
            payload = build_workbench_artifacts(project_root=Path(tmp))
        flat = [item for rows in payload["groups"].values() for item in rows]
        self.assertTrue(all("remediation_command" in item for item in flat))
        self.assertTrue(any(item["module"] == "daily" and "daily --network false" in item["remediation_command"] for item in flat))

    def test_data_update_and_data_health_summaries_print_next_steps(self) -> None:
        from scripts.run_data_health_dashboard import print_summary as print_health
        from scripts.run_data_update import print_summary as print_update

        action = {
            "command": "python scripts/qd.py data-health --network false --scope current_analysis_pool",
            "follow_up_command": "python scripts/qd.py daily --network false --with-guardrails true",
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_update({"target_complete_trade_date": "2026-05-21", "network": False, "remediation_actions": [action]})
        self.assertIn("下一步", buf.getvalue())
        self.assertIn("data-health", buf.getvalue())

        buf = io.StringIO()
        with redirect_stdout(buf):
            print_health(
                {
                    "target_date": "2026-05-21",
                    "network": False,
                    "freshness": {"stale_count": 2, "missing_count": 0},
                    "remediation_actions": [{"command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool"}],
                    "output_paths": {},
                }
            )
        self.assertIn("建议", buf.getvalue())
        self.assertIn("data-update", buf.getvalue())

    def test_qd_help_offers_the_fix_plan_command(self) -> None:
        """A user who hits an error must be told how to find the fix plan.

        Split from the retired app/static/app.js half: the old bundle's
        "remediation" / 复制命令 copy has no counterpart on the active desk, so that
        assertion retired instead of being pointed at a weaker substitute. The CLI
        half is fully active and keeps the guarantee.
        """
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "help"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("fix-plan", completed.stdout)


if __name__ == "__main__":
    unittest.main()



