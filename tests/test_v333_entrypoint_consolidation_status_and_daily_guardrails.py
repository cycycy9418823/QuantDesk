from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = PROJECT_ROOT / ".quantdesk_test_tmp"


class V333EntrypointStatusDailyGuardrailsTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def _temp_dir(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_version_is_v333(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

        self.assertIn(PACKAGE_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertTrue(APP_VERSION.startswith(API_SCHEMA_VERSION))

    def test_qd_help_recommends_status_and_groups_commands(self) -> None:
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
        if "\\u4e0d\\u77e5\\u9053" in completed.stdout:
            help_text = completed.stdout.encode("utf-8").decode("unicode_escape")
            self.assertIn("status", help_text)
            self.assertIn("\u4e0d\u77e5\u9053\u8be5\u8dd1\u4ec0\u4e48\uff1f\u5148\u8fd0\u884c python scripts/qd.py status", help_text)
            self.assertIn("\u63a8\u8350\u65b0\u624b\u547d\u4ee4", help_text)
            self.assertIn("\u5e38\u7528\u9a8c\u8bc1", help_text)
            self.assertNotIn("daily_quant_report.py", help_text)
            self.assertNotIn("generate_sample_report.py", help_text)
            return
        self.assertIn("status", completed.stdout)
        self.assertIn("不知道该跑什么？先运行 python scripts/qd.py status", completed.stdout)
        self.assertIn("推荐新手命令", completed.stdout)
        self.assertIn("常用验证", completed.stdout)
        self.assertNotIn("daily_quant_report.py", completed.stdout)
        self.assertNotIn("generate_sample_report.py", completed.stdout)

    def test_status_build_is_read_only_and_recommends_data_update_when_stale(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_update" / "data_update_latest.json",
                {
                    "status": "warning",
                    "target_complete_trade_date": "2026-05-21",
                    "effective_target_date": "2026-05-21",
                    "data_scope": "low_price_included",
                    "symbol_count": 80,
                    "coverage_date": "2026-05-15",
                    "latest_date_max": "2026-05-20",
                    "trading_day_lag": 4,
                    "stale_count": 78,
                    "missing_count": 0,
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
        self.assertFalse(payload["network"])
        self.assertFalse(payload["llm_enabled"])
        self.assertFalse(payload["full_scan"])
        self.assertEqual(payload["next_actions"][0]["category"], "data")
        self.assertIn("data-update", payload["next_actions"][0]["command"])

    def test_status_clean_zip_recommends_offline_bootstrap_first(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            payload = build_quantdesk_status(project_root=root, write=False)
        self.assertIn("data-bootstrap --preset low_price_demo --network false", payload["suggested_next_action"])
        self.assertNotIn("--enable-network", payload["suggested_next_action"])
        self.assertEqual(payload["remediation_actions"][0]["command"], "python scripts/qd.py data-bootstrap --preset low_price_demo --network false")
        self.assertFalse(payload["remediation_actions"][0]["network_required"])

    def test_remediation_omits_target_placeholder_when_calendar_unavailable(self) -> None:
        from quantdesk import remediation_advisor as advisor

        stale_payload = {
            "status": "warning",
            "stale_count": 1,
            "missing_count": 0,
            "coverage_date": "2026-06-01",
            "latest_date_max": "2026-06-01",
        }
        with mock.patch.object(advisor, "resolve_target_complete_trade_date", return_value={}):
            actions = advisor.build_data_stale_remediation(stale_payload)
            guardrail_actions = advisor.build_guardrail_remediation({"warnings": ["sample too small"]})
        text = json.dumps(actions + guardrail_actions, ensure_ascii=False)
        self.assertNotIn("YYYY-MM-DD", text)
        self.assertNotIn("{target_date}", text)
        self.assertNotIn("--target-date  --", text)

    def test_status_next_actions_use_latest_health_target_when_update_is_stale(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_update" / "data_update_latest.json",
                {
                    "status": "warning",
                    "target_complete_trade_date": "2026-05-21",
                    "effective_target_date": "2026-05-21",
                    "data_scope": "low_price_included",
                    "symbol_count": 80,
                    "trading_day_lag": 4,
                    "stale_count": 78,
                    "missing_count": 0,
                },
            )
            self._write_json(
                root / "results" / "data_health" / "data_health_dashboard_latest.json",
                {
                    "status": "warning",
                    "target_date": "2026-06-10",
                    "data_scope": "current_analysis_pool",
                    "freshness_summary": {
                        "target_date": "2026-06-10",
                        "trading_day_lag": 4,
                        "stale_count": 78,
                        "missing_count": 0,
                    },
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
        commands = "\n".join(item["command"] for item in payload["next_actions"])
        self.assertIn("--target-date 2026-06-10", commands)
        self.assertNotIn("--target-date 2026-05-21", commands)

    def test_status_prioritizes_data_gates_monitoring_while_download_queue_is_active(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

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
                    "suggested_next_action": "wait_for_queue_then_audit_and_import",
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
                    "freshness_summary": {
                        "target_date": "2026-07-03",
                        "trading_day_lag": 0,
                        "stale_count": 914,
                        "missing_count": 0,
                    },
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)

        self.assertEqual(payload["suggested_next_action"], "python scripts/qd.py tushare-queue-status --summary true")
        self.assertEqual(payload["next_actions"][0]["category"], "data-gates")
        self.assertEqual(payload["next_actions"][0]["command"], "python scripts/qd.py tushare-queue-status --summary true")
        self.assertIn("continue_monitoring", payload["next_actions"][0]["reason"])
        self.assertEqual(payload["remediation_actions"][0]["command"], "python scripts/qd.py tushare-queue-status --summary true")
        runnable_commands = "\n".join(
            str(action.get(key) or "")
            for action in payload["next_actions"] + payload["remediation_actions"]
            for key in ("command", "follow_up_command")
        )
        self.assertNotIn("data-update --target-date 2026-07-03", runnable_commands)
        self.assertNotIn("import-extended-data --apply true", runnable_commands)
        self.assertIn(
            "python scripts/qd.py import-extended-data --apply true",
            payload["next_actions"][0]["blocked_without_confirmation"],
        )

    def test_status_reads_direct_data_gates_snapshot_when_data_health_is_stale(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

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
                    "freshness_summary": {
                        "target_date": "2026-07-03",
                        "trading_day_lag": 0,
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
                            "python scripts/qd.py tushare-minute-status --summary true",
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
            payload = build_quantdesk_status(project_root=root, write=False)

        self.assertEqual(payload["suggested_next_action"], "python scripts/qd.py tushare-queue-status --summary true")
        self.assertEqual(payload["next_actions"][0]["category"], "data-gates")
        self.assertEqual(payload["next_actions"][0]["command"], "python scripts/qd.py tushare-queue-status --summary true")
        self.assertIn("continue_monitoring", payload["next_actions"][0]["reason"])

    def test_status_reports_stale_download_queue_without_calling_it_active(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_health" / "data_health_dashboard_latest.json",
                {"status": "warning", "target_date": "2026-08-11"},
            )
            self._write_json(
                root / "results" / "data_health" / "extended_data_gates_latest.json",
                {
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
            payload = build_quantdesk_status(project_root=root, write=False)

        action = payload["next_actions"][0]
        self.assertEqual(action["warning_kind"], "download_queue_stale")
        self.assertIn("stale", action["reason"].lower())
        self.assertNotIn(" is active", action["reason"].lower())

    def test_status_summary_prefers_data_health_scope_and_coverage(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_update" / "data_update_latest.json",
                {
                    "status": "dry_run",
                    "data_scope": "daily",
                    "target_complete_trade_date": "2026-06-09",
                    "effective_target_date": "2026-06-09",
                    "coverage_date": "2026-06-05",
                    "latest_date_max": "2026-06-05",
                    "symbol_count": 1,
                    "stale_count": 0,
                    "missing_count": 0,
                },
            )
            self._write_json(
                root / "results" / "data_health" / "data_health_dashboard_latest.json",
                {
                    "status": "warning",
                    "data_scope": "current_analysis_pool",
                    "target_date": "2026-06-10",
                    "freshness": {
                        "data_scope": "current_analysis_pool",
                        "coverage_date": "2026-06-05",
                        "latest_date_max": "2026-06-05",
                        "symbol_count": 1,
                        "stale_count": 1,
                        "missing_count": 0,
                    },
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
        self.assertEqual(payload["target_date"], "2026-06-10")
        self.assertEqual(payload["data_summary"]["data_scope"], "current_analysis_pool")
        self.assertEqual(payload["data_summary"]["coverage_date"], "2026-06-05")
        self.assertEqual(payload["data_summary"]["latest_date_max"], "2026-06-05")

    def test_status_target_uses_latest_daily_target_when_health_is_stale(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_update" / "data_update_latest.json",
                {
                    "status": "warning",
                    "target_complete_trade_date": "2026-05-21",
                    "effective_target_date": "2026-05-21",
                    "data_scope": "current_analysis_pool",
                    "symbol_count": 1,
                    "trading_day_lag": 4,
                    "stale_count": 1,
                    "missing_count": 0,
                },
            )
            self._write_json(
                root / "results" / "data_health" / "data_health_dashboard_latest.json",
                {
                    "status": "warning",
                    "target_date": "2026-05-21",
                    "freshness": {"target_date": "2026-05-21", "stale_count": 1, "missing_count": 0},
                },
            )
            self._write_json(
                root / "results" / "daily_workflow_latest.json",
                {
                    "workflow_status": "warning",
                    "summary": {"target_complete_trade_date": "2026-06-10"},
                    "daily_price_status": {"target_date": "2026-06-10"},
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
        self.assertEqual(payload["target_date"], "2026-06-10")
        commands = "\n".join(item["command"] for item in payload["next_actions"])
        self.assertIn("--target-date 2026-06-10", commands)
        self.assertNotIn("--target-date 2026-05-21", commands)

    def test_status_recommends_daily_when_data_fresh_but_validation_missing(self) -> None:
        from quantdesk.status_advisor import build_quantdesk_status

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "data_update" / "data_update_latest.json",
                {
                    "status": "passed",
                    "target_complete_trade_date": "2026-05-21",
                    "effective_target_date": "2026-05-21",
                    "data_scope": "specified_symbols",
                    "symbol_count": 2,
                    "coverage_date": "2026-05-21",
                    "latest_date_max": "2026-05-21",
                    "trading_day_lag": 0,
                    "stale_count": 0,
                    "missing_count": 0,
                },
            )
            payload = build_quantdesk_status(project_root=root, write=False)
        commands = "\n".join(item["command"] for item in payload["next_actions"])
        self.assertTrue("daily" in commands or "strategy-validate" in commands)

    def test_qd_status_outputs_concise_summary_and_writes_latest(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/qd.py", "status"],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertIn(completed.returncode, {0, 1}, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 15)
        self.assertIn("QuantDesk 当前状态", completed.stdout)
        self.assertTrue((PROJECT_ROOT / "results" / "status_latest.json").exists())

    def test_command_center_tracks_status_command(self) -> None:
        from quantdesk.command_center_status import build_command_center_status
        from quantdesk.operation_log import append_operation_log, build_operation_record

        with self._temp_dir() as tmp:
            log_path = Path(tmp) / "operation_timeline.jsonl"
            append_operation_log(
                build_operation_record(command="status", status="passed", output_paths=["results/status_latest.json"]),
                log_path,
            )
            payload = build_command_center_status(log_path=log_path)
        rows = {row["command"]: row for row in payload["commands"]}
        self.assertIn("status", rows)
        self.assertEqual(rows["status"]["last_status"], "passed")

    def test_command_center_top_remediation_prefers_data_gate_safe_monitoring_when_queue_active(self) -> None:
        from scripts.show_command_center_status import _top_remediation

        top = _top_remediation(
            {
                "commands": [
                    {
                        "command": "fix-plan",
                        "top_remediation": {
                            "title": "联网补齐日线",
                            "command": "python scripts/qd.py data-update --target-date 2026-07-03 --enable-network --scope current_analysis_pool",
                        },
                    },
                    {
                        "command": "data-health",
                        "data_gate_summary": {
                            "extended_data_current_gate": "download_queue",
                            "extended_data_operator_wait_reason": "download_queue_active",
                            "extended_data_safe_next_commands": [
                                "python scripts/qd.py tushare-queue-status --summary true",
                            ],
                            "extended_data_blocked_without_confirmation": [
                                "python scripts/qd.py import-extended-data --apply true",
                            ],
                        },
                    },
                ]
            }
        )

        self.assertEqual(top["command"], "python scripts/qd.py tushare-queue-status --summary true")
        self.assertEqual(top["warning_kind"], "download_queue_active")
        self.assertIn("import-extended-data --apply true", top["blocked_without_confirmation"][0])

    def test_command_center_summary_keeps_data_gate_safe_command_as_top_next_action(self) -> None:
        from quantdesk.command_center_status import summarize_command_status

        summary = summarize_command_status(
            {
                "suggested_next_action": "python scripts/qd.py tushare-queue-status --summary true",
                "commands": [
                    {
                        "command": "fix-plan",
                        "last_status": "warning",
                        "top_remediation": {
                            "title": "联网补齐日线",
                            "command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool",
                        },
                    },
                    {
                        "command": "data-health",
                        "last_status": "warning",
                        "data_gate_summary": {
                            "extended_data_current_gate": "download_queue",
                            "extended_data_operator_wait_reason": "download_queue_active",
                            "extended_data_safe_next_commands": [
                                "python scripts/qd.py tushare-queue-status --summary true",
                            ],
                        },
                    },
                ],
            }
        )

        self.assertEqual(
            summary["suggested_next_action"],
            "python scripts/qd.py tushare-queue-status --summary true",
        )
        self.assertNotIn("data-update", summary["suggested_next_action"])

    def test_data_health_suggested_action_prioritizes_active_data_gate(self) -> None:
        from scripts.run_data_health_dashboard import _choose_suggested_next_action

        suggested = _choose_suggested_next_action(
            freshness={"status": "warning", "suggested_next_action": "refresh_daily_data"},
            intraday={"status": "warning", "suggested_next_action": "refresh_intraday"},
            provider_stats={"status": "warning", "suggested_next_action": "check_provider"},
            extended_data_gates={
                "current_gate": "download_queue",
                "operator_wait_reason": "download_queue_active",
                "suggested_next_action": "continue_monitoring",
            },
            extended_data_readiness={"suggested_next_action": "wait_for_queue_then_rerun_audit"},
            tushare_retry_plan={"retry_gate": "wait_for_queue_completion", "suggested_next_action": "continue_monitoring"},
            extended_import_verification={
                "post_import_gate": "wait_for_download_completion",
                "suggested_next_action": "wait_for_queue_then_audit_and_import",
            },
        )

        self.assertEqual(suggested, "continue_monitoring")

    def test_script_inventory_exists_and_marks_legacy_scripts(self) -> None:
        path = PROJECT_ROOT / "docs" / "script_entrypoint_inventory.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("python scripts/qd.py status", text)
        for name in ["live_monitor.py", "check_data_providers.py", "daily_quant_report.py", "rebuild_cached_report.py", "generate_sample_report.py"]:
            self.assertIn(name, text)

    def test_daily_workflow_guardrail_summary_can_be_enabled_and_disabled(self) -> None:
        from quantdesk import daily_workflow_orchestrator as dwo

        with self._temp_dir() as tmp:
            root = Path(tmp)
            fake_daily = {
                "ok": True,
                "target_date": "2026-05-21",
                "latest_local_date": "2026-05-21",
                "is_stale": False,
                "stale_count": 0,
                "missing_count": 0,
            }
            fake_freshness = {
                "stale_mode": False,
                "can_review_yesterday_positions": True,
                "portfolio_review_valid": True,
                "intraday_review_valid": False,
                "target_complete_trade_date": "2026-05-21",
                "local_daily_latest_date": "2026-05-21",
                "review_mode": "current_review",
            }
            patches = [
                mock.patch.object(dwo, "RESULTS_DIR", root / "results"),
                mock.patch.object(dwo, "REPORTS_DIR", root / "reports"),
                mock.patch.object(dwo, "DAILY_WORKFLOW_LATEST_JSON", root / "results" / "daily_workflow_latest.json"),
                mock.patch.object(dwo, "AUTO_PAPER_PORTFOLIO_STATE_JSON", root / "state" / "auto_paper_portfolio.json"),
                mock.patch.object(dwo, "_low_price_status", return_value={"ok": True, "included_count": 2, "path": "data/universe/low.csv", "message": "ok"}),
                mock.patch.object(dwo, "daily_price_status", return_value=fake_daily),
                mock.patch.object(dwo, "_latest_daily_date", return_value="2026-05-21"),
                mock.patch.object(dwo, "_run_low_price_report", return_value={"ok": True, "summary": {"analyzed_count": 0}, "files": {}}),
                mock.patch.object(dwo, "build_daily_features", return_value={"ok": True, "message": "ok"}),
                mock.patch.object(dwo, "daily_feature_status", return_value={"ok": True, "feature_count": 0, "latest_date": "2026-05-21", "path": "data/features/daily_features_latest.csv"}),
                mock.patch.object(dwo, "evaluate_data_freshness", return_value=fake_freshness),
                mock.patch.object(dwo, "theme_map_status", return_value={"mapped_count": 1, "unknown_count": 0}),
                mock.patch.object(dwo, "run_auto_strategy_cycle", return_value={"ok": True, "summary": {}, "long_candidates": [], "board_candidates": []}),
                mock.patch.object(dwo, "load_theme_map", return_value={}),
                mock.patch.object(dwo, "validate_theme_map_for_current_candidates", return_value={"unknown_current_candidates": 0, "message": "ok"}),
                mock.patch.object(dwo, "intraday_gap_status", return_value={"ok": True, "target_trade_date": "2026-05-21", "total_symbols": 0, "available_count": 0, "missing_count": 0, "invalid_count": 0}),
                mock.patch.object(dwo, "build_daily_watchpool_step", return_value={"ok": True, "candidate_count": 0, "selected_count": 0, "added_count": 0, "watchlist_count": 0, "added_symbols": []}),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12], patches[13], patches[14], patches[15], patches[16]:
                enabled = dwo.run_daily_workflow(with_guardrails=True)
                disabled = dwo.run_daily_workflow(with_guardrails=False)
        self.assertIn("guardrail_summary", enabled)
        self.assertTrue(enabled["guardrail_summary"]["enabled"])
        self.assertNotEqual(enabled["workflow_status"], "failed")
        self.assertFalse(disabled["guardrail_summary"]["enabled"])
        self.assertTrue(any(step["name"] == "quick_guardrails" and step["status"] == "skipped" for step in disabled["steps"]))

    def test_daily_workflow_distinguishes_partial_pool_from_fresh_review_scope(self) -> None:
        from quantdesk import daily_workflow_orchestrator as dwo

        with self._temp_dir() as tmp:
            root = Path(tmp)
            fake_daily = {
                "ok": True,
                "target_date": "2026-07-10",
                "total_symbols": 914,
                "latest_local_date": "2026-06-29",
                "max_local_date": "2026-07-10",
                "fresh_count": 700,
                "stale_count": 214,
                "missing_count": 0,
                "is_stale": True,
            }
            fake_freshness = {
                "status": "passed",
                "data_freshness_status": "fresh",
                "stale_mode": False,
                "can_review_yesterday_positions": True,
                "portfolio_review_valid": True,
                "intraday_review_valid": False,
                "target_complete_trade_date": "2026-07-10",
                "local_daily_latest_date": "2026-07-10",
                "review_scope_stale_symbol_count": 0,
                "review_scope_missing_symbol_count": 0,
                "stale_symbol_count": 0,
                "missing_symbol_count": 0,
                "daily_pool_partial": True,
                "daily_pool_latest_date": "2026-06-29",
                "daily_pool_stale_symbol_count": 214,
                "daily_pool_missing_symbol_count": 0,
                "review_mode": "daily_review_only",
                "message": "完整日线可用于收益复盘，但缺少分钟线，无法验证盘中条件触发。",
            }
            patches = [
                mock.patch.object(dwo, "RESULTS_DIR", root / "results"),
                mock.patch.object(dwo, "REPORTS_DIR", root / "reports"),
                mock.patch.object(dwo, "DAILY_WORKFLOW_LATEST_JSON", root / "results" / "daily_workflow_latest.json"),
                mock.patch.object(dwo, "AUTO_PAPER_PORTFOLIO_STATE_JSON", root / "state" / "auto_paper_portfolio.json"),
                mock.patch.object(dwo, "_low_price_status", return_value={"ok": True, "included_count": 914, "path": "data/universe/low.csv", "message": "ok"}),
                mock.patch.object(dwo, "daily_price_status", return_value=fake_daily),
                mock.patch.object(dwo, "_latest_daily_date", return_value="2026-07-10"),
                mock.patch.object(dwo, "_run_low_price_report", return_value={"ok": True, "summary": {"analyzed_count": 700, "latest_complete_daily_date": "2026-06-29"}, "files": {}}),
                mock.patch.object(dwo, "build_daily_features", return_value={"ok": True, "message": "ok"}),
                mock.patch.object(dwo, "daily_feature_status", return_value={"ok": True, "feature_count": 914, "latest_date": "2026-07-10", "path": "data/features/daily_features_latest.csv"}),
                mock.patch.object(dwo, "evaluate_data_freshness", return_value=fake_freshness),
                mock.patch.object(dwo, "theme_map_status", return_value={"mapped_count": 1, "unknown_count": 0}),
                mock.patch.object(dwo, "run_auto_strategy_cycle", return_value={"ok": True, "summary": {}, "long_candidates": [], "board_candidates": []}),
                mock.patch.object(dwo, "load_theme_map", return_value={}),
                mock.patch.object(dwo, "validate_theme_map_for_current_candidates", return_value={"unknown_current_candidates": 0, "message": "ok"}),
                mock.patch.object(dwo, "intraday_gap_status", return_value={"ok": True, "target_trade_date": "2026-07-10", "total_symbols": 0, "available_count": 0, "missing_count": 0, "invalid_count": 0}),
                mock.patch.object(dwo, "build_daily_watchpool_step", return_value={"ok": True, "candidate_count": 0, "selected_count": 0, "added_count": 0, "watchlist_count": 0, "added_symbols": []}),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12], patches[13], patches[14], patches[15], patches[16]:
                payload = dwo.run_daily_workflow(with_guardrails=False)
                report = Path(payload["report_path"]).read_text(encoding="utf-8")

        cache_steps = [step for step in payload["steps"] if step["name"] == "daily_cache_check"]
        messages = "\n".join(step["message"] for step in cache_steps)

        self.assertIn("低价研究池仍有 214 只日期滞后", messages)
        self.assertIn("不会进入目标日候选", messages)
        self.assertNotIn("本次复盘仍基于现有完整日线", messages)
        self.assertIn("共同覆盖日期下限：2026-06-29", messages)
        self.assertIn("池内最晚覆盖日期：2026-07-10", messages)
        self.assertNotIn("本地完整日线最新日期：2026-06-29", messages)
        self.assertEqual(payload["summary"]["data_freshness_status"], "fresh")
        self.assertTrue(payload["summary"]["portfolio_review_valid"])
        self.assertEqual(payload["summary"]["local_daily_latest_date"], "2026-07-10")
        self.assertEqual(payload["summary"]["stale_symbol_count"], 0)
        self.assertEqual(payload["summary"]["daily_pool_stale_symbol_count"], 214)
        self.assertIn("当前复核范围滞后股票数：0", report)
        self.assertIn("低价研究池日期滞后股票数：214", report)
        self.assertNotIn("本次日线补全未完成，因此策略候选和特征可能仍基于", report)

    def test_daily_report_guardrail_section_uses_user_facing_labels(self) -> None:
        from quantdesk.daily_workflow_orchestrator import build_daily_workflow_report

        report = build_daily_workflow_report(
            {
                "workflow_status": "completed",
                "summary": {"report_generated_at": "2026-07-07T09:30:00"},
                "guardrail_summary": {
                    "enabled": True,
                    "mode": "quick",
                    "lookahead_status": "passed",
                    "execution_assumption_status": "warning",
                    "feature_quality_status": "passed",
                    "statistical_validation_status": "warning",
                    "parameter_sensitivity_status": "missing",
                    "warning_count": 2,
                },
            }
        )

        self.assertIn("## 每日流程防护诊断", report)
        self.assertIn("前视检查：通过", report)
        self.assertIn("执行假设：注意", report)
        self.assertIn("特征审计：通过", report)
        self.assertIn("统计校验：注意", report)
        self.assertIn("参数敏感性：缺失", report)
        self.assertIn("警告数量：2", report)
        self.assertIn("防护诊断只追加治理提示", report)
        self.assertNotIn("## Quick Guardrails", report)
        self.assertNotIn("enabled:", report)
        self.assertNotIn("mode:", report)
        self.assertNotIn("lookahead_status", report)
        self.assertNotIn("execution_assumption_status", report)
        self.assertNotIn("feature_quality_status", report)
        self.assertNotIn("statistical_validation_status", report)
        self.assertNotIn("parameter_sensitivity_status", report)
        self.assertNotIn("warning_count:", report)
        self.assertNotIn("quick guardrails", report)

    def test_daily_report_paper_consistency_section_uses_user_facing_labels(self) -> None:
        from quantdesk.daily_workflow_orchestrator import build_daily_workflow_report

        report = build_daily_workflow_report(
            {
                "workflow_status": "completed",
                "summary": {"report_generated_at": "2026-07-07T09:30:00"},
                "paper_consistency_check": {
                    "status": "warning",
                    "issue_count": 1,
                    "warning_count": 2,
                    "source": "results/paper_vs_backtest.json",
                },
            }
        )

        self.assertIn("## 纸面账户与回测一致性检查", report)
        self.assertIn("检查状态：注意", report)
        self.assertIn("问题数量：1", report)
        self.assertIn("警告数量：2", report)
        self.assertIn("来源：results/paper_vs_backtest.json", report)
        self.assertIn("本检查只比较本地纸面账户与回测结果", report)
        self.assertNotIn("## Paper vs Backtest Consistency", report)
        self.assertNotIn("status:", report)
        self.assertNotIn("issue_count:", report)
        self.assertNotIn("warning_count:", report)
        self.assertNotIn("source:", report)
        self.assertNotIn("does not connect brokers", report)

    def test_daily_workflow_top_level_warnings_are_deduped(self) -> None:
        from quantdesk.daily_workflow_orchestrator import _dedupe_warnings

        warnings = _dedupe_warnings([
            "network_disabled: realtime quote refresh skipped; use local/synthetic quotes or --network true",
            "minute_data_unavailable: condition plan trigger preview skipped",
            "network_disabled: realtime quote refresh skipped; use local/synthetic quotes or --network true",
            "",
            None,
            "historical_st_periods_unavailable",
        ])

        self.assertEqual(
            warnings,
            [
                "network_disabled: realtime quote refresh skipped; use local/synthetic quotes or --network true",
                "minute_data_unavailable: condition plan trigger preview skipped",
                "historical_st_periods_unavailable",
            ],
        )

    def test_daily_report_contains_single_data_freshness_gate_section(self) -> None:
        from quantdesk.daily_workflow_orchestrator import build_daily_workflow_report

        report = build_daily_workflow_report(
            {
                "workflow_status": "completed",
                "summary": {
                    "report_generated_at": "2026-06-10T09:30:00",
                    "data_freshness_status": "stale",
                    "target_complete_trade_date": "2026-06-10",
                    "local_daily_latest_date": "2026-06-05",
                    "daily_feature_date": "2026-06-05",
                    "daily_price_target_date": "2026-06-10",
                    "stale_symbol_count": 1,
                    "review_mode": "stale_observation",
                    "portfolio_review_valid": False,
                    "intraday_review_valid": False,
                    "message": "本次只能基于滞后数据观察，不能证明目标交易日策略表现。",
                },
            }
        )
        self.assertEqual(report.count("## 数据新鲜度门禁"), 1)
        self.assertIn("目标完整交易日：2026-06-10", report)
        self.assertNotIn("目标完整交易日：-", report)

    def test_daily_report_surfaces_verified_minute_pool_provenance(self) -> None:
        from quantdesk.daily_workflow_orchestrator import build_daily_workflow_report

        report = build_daily_workflow_report(
            {
                "workflow_status": "completed",
                "summary": {
                    "report_generated_at": "2026-07-07T09:30:00",
                    "intraday_required_symbols": 200,
                    "intraday_available_symbols": 200,
                    "intraday_missing_symbols": 0,
                },
                "intraday_status": {
                    "target_trade_date": "2026-07-03",
                    "total_symbols": 200,
                    "available_count": 200,
                    "missing_count": 0,
                    "verified_minute_pool": {
                        "status": "passed",
                        "usable_count": 200,
                        "source_snapshot": "last_passed",
                        "latest_quality_status": "partial",
                        "latest_snapshot_path": "results/data_health/tushare_minute_quality_latest.json",
                        "warnings": ["verified_pool_from_last_passed_snapshot"],
                    },
                },
            }
        )

        self.assertIn("已验证分钟池", report)
        self.assertIn("来源：最近一次通过质检的快照", report)
        self.assertIn("最新质检状态：部分完成", report)
        self.assertIn("告警：使用最近一次通过质检的分钟池快照", report)
        self.assertNotIn("source_snapshot", report)
        self.assertNotIn("latest_quality_status", report)
        self.assertNotIn("latest_snapshot_path", report)
        self.assertNotIn("last_passed", report)
        self.assertNotIn("verified_pool_from_last_passed_snapshot", report)
        self.assertNotIn("missing_intraday 数量", report)

    def test_stale_portfolio_payload_recovers_positions_from_state_backup(self) -> None:
        from quantdesk import daily_workflow_orchestrator as dwo
        from quantdesk.paper.state_store import PaperStateStore

        with self._temp_dir() as tmp:
            state_path = Path(tmp) / "state" / "auto_paper_portfolio.json"
            PaperStateStore(state_path, schema_version="test-schema", max_backups=3).write(
                {
                    "positions": [{"symbol": "600001", "quantity": 100, "latest_date": "2026-05-20"}],
                    "cash": 99000,
                }
            )
            state_path.write_text("{bad json", encoding="utf-8")
            with mock.patch.object(dwo, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_path):
                payload = dwo._stale_portfolio_payload(
                    {
                        "review_mode": "stale_observation",
                        "can_review_yesterday_positions": False,
                    }
                )

        self.assertEqual(payload["summary"]["position_count"], 1)
        self.assertEqual(payload["positions"][0]["symbol"], "600001")
        self.assertTrue(payload["state_recovered_from_backup"])

    def test_time_utils_exists_and_no_new_now_text_defs(self) -> None:
        self.assertTrue((PROJECT_ROOT / "quantdesk" / "time_utils.py").exists())
        offenders = []
        for path in (PROJECT_ROOT / "quantdesk").glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "def _now_text" in text or "def _now(" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_daily_workflow_guardrails_doc_exists(self) -> None:
        text = (PROJECT_ROOT / "docs" / "daily_workflow_guardrails.md").read_text(encoding="utf-8")
        self.assertIn("quick guardrails", text)
        self.assertIn("不构成投资建议", text)


if __name__ == "__main__":
    unittest.main()



