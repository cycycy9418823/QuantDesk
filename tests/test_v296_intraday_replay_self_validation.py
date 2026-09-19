from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.intraday.intraday_replay_self_validation import (  # noqa: E402
    build_self_validation_plan,
    run_intraday_replay_self_validation,
    write_synthetic_intraday,
)


class IntradayReplaySelfValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmp_root = Path(cls.tmp.name)
        cls.real_intraday_dir = PROJECT_ROOT / "data" / "intraday" / "1m"
        cls.real_intraday_before = sorted(p.name for p in cls.real_intraday_dir.glob("*.csv")) if cls.real_intraday_dir.exists() else []
        cls.payload = run_intraday_replay_self_validation(
            runtime_dir=cls.tmp_root / "runtime",
            output_path=cls.tmp_root / "intraday_replay_self_validation_latest.json",
            execution_output_path=cls.tmp_root / "conditional_execution_self_validation.json",
            keep_artifacts=True,
            write_latest=False,
            update_portfolio=False,
            verbose=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def scenario(self, name: str) -> dict:
        for row in self.payload.get("scenarios") or []:
            if row.get("name") == name:
                return row
        self.fail(f"missing scenario {name}")

    def test_synthetic_intraday_data_is_generated(self) -> None:
        generated = self.tmp_root / "runtime" / "1m" / "600744.csv"
        self.assertTrue(generated.exists())
        text = generated.read_text(encoding="utf-8-sig")
        self.assertIn("2026-05-15 10:23:00", text)
        self.assertIn("self_validation_synthetic", text)

    def test_synthetic_data_does_not_write_real_intraday_dir(self) -> None:
        after = sorted(p.name for p in self.real_intraday_dir.glob("*.csv")) if self.real_intraday_dir.exists() else []
        self.assertEqual(after, self.real_intraday_before)

    def test_pullback_buy_scenario_passes(self) -> None:
        row = self.scenario("pullback_buy_trigger")
        self.assertTrue(row["passed"])
        self.assertEqual(row["actual_action"], "buy")

    def test_breakout_buy_scenario_passes(self) -> None:
        row = self.scenario("breakout_buy_trigger")
        self.assertTrue(row["passed"])
        self.assertEqual(row["actual_action"], "buy")

    def test_take_profit_reduce_half_scenario_passes(self) -> None:
        row = self.scenario("take_profit_reduce_half_trigger")
        self.assertTrue(row["passed"])
        self.assertIn(row["actual_action"], {"reduce_half", "take_profit"})

    def test_stop_loss_sell_all_scenario_passes(self) -> None:
        row = self.scenario("stop_loss_sell_all_trigger")
        self.assertTrue(row["passed"])
        self.assertIn(row["actual_action"], {"stop_loss", "sell_all"})

    def test_no_fill_scenario_passes(self) -> None:
        row = self.scenario("no_fill_not_triggered")
        self.assertTrue(row["passed"])
        self.assertEqual(row["actual_action"], "no_fill")
        self.assertTrue(row["is_reliable"])

    def test_missing_intraday_scenario_passes(self) -> None:
        row = self.scenario("missing_intraday")
        self.assertTrue(row["passed"])
        self.assertEqual(row["actual_fill_model"], "missing_intraday")
        self.assertFalse(row["is_reliable"])

    def test_stop_loss_priority_is_higher_than_add(self) -> None:
        row = self.scenario("stop_loss_priority_over_add")
        self.assertTrue(row["passed"])
        self.assertIn(row["actual_action"], {"stop_loss", "sell_all"})
        self.assertEqual(row["actual_trigger_time"], "2026-05-15 09:45:00")

    def test_trigger_time_and_price_are_recorded(self) -> None:
        row = self.scenario("pullback_buy_trigger")
        self.assertEqual(row["actual_trigger_time"], "2026-05-15 10:23:00")
        self.assertEqual(float(row["actual_trigger_price"]), 8.45)

    def test_reliable_event_updates_portfolio(self) -> None:
        row = self.scenario("a_share_t1_frozen_quantity")
        self.assertTrue(row["passed"])
        self.assertEqual(row["actual_frozen_quantity"], 100)
        self.assertEqual(row["actual_sellable_quantity"], 0)

    def test_missing_intraday_and_no_fill_do_not_change_cash(self) -> None:
        row = self.scenario("no_fill_and_missing_do_not_change_cash")
        self.assertTrue(row["passed"])

    def test_cli_success_returns_zero(self) -> None:
        from scripts.run_intraday_replay_self_validation import main
        from quantdesk.intraday import intraday_replay_self_validation as replay

        with mock.patch.multiple(
            replay,
            TEST_RUNTIME_DIR=self.tmp_root / "cli_runtime",
            SELF_VALIDATION_LATEST_JSON=self.tmp_root / "cli_latest.json",
            SELF_VALIDATION_EXECUTION_JSON=self.tmp_root / "cli_execution.json",
        ):
            self.assertEqual(main(["--keep-artifacts", "false"]), 0)

    def test_cli_failure_returns_non_zero(self) -> None:
        from scripts import run_intraday_replay_self_validation as cli

        with mock.patch.object(cli, "run_intraday_replay_self_validation", return_value={"ok": False, "summary": {"failed": 1}}):
            self.assertEqual(cli.main(["--keep-artifacts", "false"]), 1)

    def test_self_check_contains_validation_hook(self) -> None:
        text = (PROJECT_ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_intraday_replay_self_validation_support", text)
        self.assertIn("intraday_replay_self_validation", text)

    def test_report_contains_required_risk_text(self) -> None:
        report_path = PROJECT_ROOT / str(self.payload["artifacts"]["report_path"])
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("合成分钟线", text)
        self.assertIn("不接券商", text)
        self.assertIn("不真实下单", text)
        self.assertIn("不构成投资建议", text)

    def test_report_humanizes_minute_gap_and_scenario_labels(self) -> None:
        report_path = PROJECT_ROOT / str(self.payload["artifacts"]["report_path"])
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("缺分钟线次数", text)
        self.assertIn("未成交/未触发数量", text)
        self.assertIn("执行动作", text)
        self.assertIn("触发时间", text)
        self.assertIn("现金和持仓保持不变", text)
        for raw in (
            "- missing_intraday：",
            "- no_fill：",
            " / no_fill / ",
            " / missing_intraday / ",
            "missing_intraday / no_fill",
        ):
            self.assertNotIn(raw, text)

    def test_payload_summary_is_complete(self) -> None:
        self.assertTrue(self.payload["ok"])
        summary = self.payload["summary"]
        self.assertGreaterEqual(summary["scenario_count"], 8)
        self.assertEqual(summary["failed"], 0)
        self.assertGreaterEqual(summary["reliable_event_count"], 4)
        self.assertGreaterEqual(summary["missing_intraday_count"], 1)


    def test_keep_artifacts_false_removes_runtime_dir(self) -> None:
        runtime_dir = self.tmp_root / "delete_runtime"
        payload = run_intraday_replay_self_validation(
            runtime_dir=runtime_dir,
            output_path=self.tmp_root / "delete_latest.json",
            execution_output_path=self.tmp_root / "delete_execution.json",
            keep_artifacts=False,
            write_latest=False,
            update_portfolio=False,
        )
        self.assertTrue(payload["ok"])
        self.assertFalse(runtime_dir.exists())

    def test_build_plan_uses_synthetic_schema_version(self) -> None:
        plan = build_self_validation_plan()
        self.assertIn(plan["schema_version"], {"2.9.6", "2.9.7", "2.9.8", "2.9.9", "3.0.0", "3.0.1", "3.0.2", "3.0.3", "3.0.4", "3.1.0", "3.1.1", "3.1.2", "3.1.3", "3.2.0", "3.2.1", "3.2.2", "3.2.3", "3.2.4", "3.2.5", "3.2.6", "3.2.6.1", "3.2.7", "3.2.8", "3.2.9", "3.3.0", "3.3.1", "3.3.2", "3.3.2.1", "3.3.2.2", "3.3.2.3", "3.3.2.4", "3.3.2.5", "3.3.3", "3.3.4", "3.3.5", "3.3.6", "3.3.7", "3.4.0", "3.4.1", "3.4.2", "3.4.2.1", "3.4.2.2", "3.4.2.3", "3.4.2.4", "3.4.2.5", "3.7.0", "3.7.0.1", "3.7.0.2", "3.7.0.3", "3.7.0.4", "3.7.0.5", "3.7.0.6"})
        self.assertEqual(plan["trade_date"], "2026-05-15")

    def test_write_synthetic_intraday_can_target_temp_dir(self) -> None:
        target = self.tmp_root / "standalone_runtime"
        if target.exists():
            shutil.rmtree(target)
        info = write_synthetic_intraday(target)
        self.assertTrue((target / "1m" / "002115.csv").exists())
        self.assertIn("002115", info["files"])


if __name__ == "__main__":
    unittest.main()


