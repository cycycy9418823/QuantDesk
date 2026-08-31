from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = PROJECT_ROOT / ".quantdesk_test_tmp"


def _symbols(count: int = 80) -> list[str]:
    return [f"{600000 + index:06d}" for index in range(count)]


class V337DataUpdateOutputSlimMobileVisualCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def _temp_dir(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT)

    def test_version_is_v337(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

        self.assertIn(PACKAGE_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertIn(API_SCHEMA_VERSION, {"3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertTrue(APP_VERSION.startswith(API_SCHEMA_VERSION))

    def test_symbol_display_utils_summarize_and_truncate_long_lists(self) -> None:
        from quantdesk.symbol_display_utils import format_symbol_preview, summarize_symbols, truncate_command_symbols

        symbols = _symbols(12)
        summary = summarize_symbols(symbols, max_visible=5)
        self.assertEqual(summary["count"], 12)
        self.assertEqual(summary["visible"], symbols[:5])
        self.assertEqual(summary["omitted_count"], 7)
        self.assertTrue(summary["is_truncated"])

        preview = format_symbol_preview(symbols, max_visible=5)
        self.assertIn(symbols[0], preview)
        self.assertIn("另有 7 只", preview)
        self.assertNotIn(symbols[-1], preview)

        command = "python scripts/qd.py data-update --symbols " + ",".join(symbols) + " --network false"
        short = truncate_command_symbols(command, max_symbols=10)
        self.assertIn("--symbols", short)
        self.assertIn("...", short)
        self.assertNotIn(symbols[-1], short)

    def test_data_update_default_stdout_is_short_and_actionable(self) -> None:
        from scripts.run_data_update import print_summary

        symbols = _symbols(80)
        payload = {
            "target_complete_trade_date": "2026-05-21",
            "target_date": "2026-05-21",
            "data_scope": "current_analysis_pool",
            "symbol_count": 80,
            "symbols_checked": symbols,
            "stale_symbols": symbols,
            "coverage_date": "2026-05-15",
            "latest_date_max": "2026-05-20",
            "status": "warning",
            "success_count": 2,
            "skipped_count": 78,
            "failed_count": 0,
            "network": False,
            "output_path": "results/data_update/data_update_latest.json",
            "report_path": "reports/data_update_report_latest.md",
            "remediation_actions": [
                {
                    "title": "补齐日线",
                    "command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool",
                    "follow_up_command": "python scripts/qd.py data-health --network false --scope current_analysis_pool",
                    "network_required": True,
                }
            ],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary(payload)
        text = buf.getvalue()
        self.assertIn("QuantDesk 数据更新完成", text)
        self.assertIn("results/data_update/data_update_latest.json", text)
        self.assertIn("我现在该怎么修", text)
        self.assertIn("需要显式联网授权", text)
        self.assertIn("已省略长 symbols 列表", text)
        self.assertIn(symbols[0], text)
        self.assertNotIn(symbols[-1], text)

    def test_data_health_default_stdout_is_short_and_summarized(self) -> None:
        from scripts.run_data_health_dashboard import print_summary

        symbols = _symbols(80)
        payload = {
            "target_date": "2026-05-21",
            "data_scope": "current_analysis_pool",
            "status": "warning",
            "symbols_checked": symbols,
            "freshness": {
                "data_scope": "current_analysis_pool",
                "symbol_count": 80,
                "symbols_checked": symbols,
                "stale_symbols": symbols,
                "missing_symbols": [],
                "coverage_date": "2026-05-15",
                "latest_date_max": "2026-05-20",
                "fresh_count": 2,
                "stale_count": 78,
                "missing_count": 0,
            },
            "intraday": {"coverage_rate": 0},
            "output_paths": {"dashboard_json": "results/data_health/data_health_dashboard_latest.json"},
            "remediation_actions": [
                {
                    "title": "补齐日线",
                    "command": "python scripts/qd.py data-update --enable-network --scope current_analysis_pool",
                    "network_required": True,
                }
            ],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary(payload)
        text = buf.getvalue()
        self.assertIn("QuantDesk 数据健康诊断完成", text)
        self.assertIn("新鲜：2", text)
        self.assertIn("过期：78", text)
        self.assertIn("缺失：0", text)
        self.assertIn("results/data_health/data_health_dashboard_latest.json", text)
        self.assertIn("已省略长 symbols 列表", text)
        self.assertNotIn(symbols[-1], text)

    def test_data_update_json_true_keeps_full_json_details(self) -> None:
        from scripts.run_data_update import main

        symbols = _symbols(12)
        with self._temp_dir() as tmp:
            root = Path(tmp)
            output_dir = root.relative_to(PROJECT_ROOT) / "data_update"
            report_path = root.relative_to(PROJECT_ROOT) / "data_update_report.md"
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(
                    [
                        "--symbols",
                        ",".join(symbols),
                        "--target-date",
                        "2026-05-21",
                        "--network",
                        "false",
                        "--max-symbols",
                        "80",
                        "--output-dir",
                        str(output_dir).replace("\\", "/"),
                        "--report-path",
                        str(report_path).replace("\\", "/"),
                        "--json",
                        "true",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn(symbols[0], text)
        self.assertIn(symbols[-1], text)
        self.assertIn("symbols_checked", payload)

    def test_active_template_mobile_layout_guards(self) -> None:
        """A narrow screen must stay readable, and text must never rotate.

        Re-anchored from the retired app/static/styles.css: the active desk carries
        its CSS inline in the template. The old exact 768px breakpoint is not one the
        active desk uses - it defines 760/860/900/980px - so this asserts that a
        narrow-screen breakpoint exists rather than pinning a pixel value the product
        never chose.
        """
        html = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8"
        )
        self.assertRegex(html, r"@media\s*\(max-width:\s*[0-9]{3}px\)")
        self.assertIn("overflow-x", html)
        self.assertIn("overflow-wrap: anywhere", html)
        self.assertIn("min-width: 0", html)
        self.assertNotIn("writing-mode", html)


if __name__ == "__main__":
    unittest.main()


