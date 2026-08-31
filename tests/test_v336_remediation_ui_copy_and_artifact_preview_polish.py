from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = PROJECT_ROOT / ".quantdesk_test_tmp"


class V336RemediationUiCopyArtifactPreviewPolishTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def _temp_dir(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT)

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_version_is_v336(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

        self.assertIn(PACKAGE_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.5.1", "3.5.1.1", "3.5.2", "3.6.0", "3.6.1", "3.6.2", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertTrue(APP_VERSION.startswith(API_SCHEMA_VERSION))


    def test_artifact_center_preview_payload_supports_json_and_markdown(self) -> None:
        from quantdesk.workbench_overview import build_workbench_artifacts

        with self._temp_dir() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "results" / "daily_workflow_latest.json",
                {
                    "schema_version": "3.3.6",
                    "app_version": "3.3.6-remediation-ui-copy-and-artifact-preview-polish",
                    "status": "warning",
                    "generated_at": "2026-05-21 18:00:00",
                    "target_date": "2026-05-21",
                    "warning_count": 2,
                    "failed_count": 0,
                    "remediation_actions": [{"command": "python scripts/qd.py daily --network false --with-guardrails true"}],
                },
            )
            self._write_text(root / "reports" / "daily_quant_report_latest.md", "\n".join(f"line {i}" for i in range(100)))
            payload = build_workbench_artifacts(project_root=root)

        flat = [item for rows in payload["groups"].values() for item in rows]
        daily_json = next(item for item in flat if item["path"] == "results/daily_workflow_latest.json")
        daily_md = next(item for item in flat if item["path"] == "reports/daily_quant_report_latest.md")

        self.assertEqual(daily_json["preview_type"], "json")
        self.assertEqual(daily_json["preview_summary"]["schema_version"], "3.3.6")
        self.assertEqual(daily_json["preview_summary"]["remediation_action_count"], 1)
        self.assertEqual(daily_md["preview_type"], "markdown")
        self.assertTrue(daily_md["preview_truncated"])
        self.assertIn("line 0", daily_md["preview_summary_text"])


    def test_fix_plan_formatter_groups_offline_and_network_commands(self) -> None:
        from quantdesk.remediation_advisor import format_remediation_console

        text = format_remediation_console(
            {
                "actions": [
                    {
                        "title": "刷新数据健康",
                        "command": "python scripts/qd.py data-health --network false --scope current_analysis_pool",
                        "network_required": False,
                    },
                    {
                        "title": "更新当前分析池日线",
                        "command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool",
                        "network_required": True,
                    },
                ]
            }
        )
        self.assertIn("离线可运行", text)
        self.assertIn("需要联网授权", text)
        self.assertIn("python scripts/qd.py data-update", text)
        self.assertIn("联网命令只在你显式运行时执行", text)

    def test_command_outputs_use_unified_network_copy_language(self) -> None:
        from quantdesk.daily_output_summary import format_daily_console_summary
        from scripts.run_data_health_dashboard import print_summary as print_health
        from scripts.run_data_update import print_summary as print_update

        action = {
            "title": "刷新数据健康",
            "command": "python scripts/qd.py data-health --network false --scope current_analysis_pool",
            "follow_up_command": "python scripts/qd.py daily --network false --with-guardrails true",
            "network_required": False,
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_update({"target_complete_trade_date": "2026-05-21", "network": False, "remediation_actions": [action]})
        self.assertIn("下一步", buf.getvalue())
        self.assertIn("离线可运行，不会联网", buf.getvalue())

        buf = io.StringIO()
        with redirect_stdout(buf):
            print_health(
                {
                    "target_date": "2026-05-21",
                    "network": False,
                    "freshness": {"stale_count": 2, "missing_count": 0},
                    "remediation_actions": [
                        {
                            "command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool",
                            "network_required": True,
                        }
                    ],
                    "output_paths": {},
                }
            )
        self.assertIn("建议修复", buf.getvalue())
        self.assertIn("需要显式联网授权", buf.getvalue())

        daily_text = format_daily_console_summary(
            {
                "daily_console_summary": {
                    "headline": "QuantDesk 每日流程完成",
                    "status": "warning",
                    "remediation_actions": [
                        {
                            "title": "本地日线数据过期",
                            "impact": "后续 daily 可信度下降。",
                            "command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool",
                            "network_required": True,
                        }
                    ],
                }
            }
        )
        self.assertIn("我现在该怎么修", daily_text)
        self.assertIn("需要显式联网授权", daily_text)


if __name__ == "__main__":
    unittest.main()


