from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from quantdesk.research.execution_realism import build_execution_realism_report, compute_partial_fill
from quantdesk.research.fees_china_a import estimate_china_a_fees
from quantdesk.research.limit_up_fill_model import limit_up_fill_probability
from quantdesk.research.slippage_curve import estimate_slippage_bps
from quantdesk.paper.trade_costs import TradeCostConfig, compute_trade_costs

try:
    from tests._operation_log_isolation import isolate_operation_log_dir
except ModuleNotFoundError:  # pragma: no cover - unittest discovery adds tests/ directly
    from _operation_log_isolation import isolate_operation_log_dir

isolate_operation_log_dir()

ROOT = Path(__file__).resolve().parents[1]


class V360ExecutionRealismTests(unittest.TestCase):
    def test_partial_fill_model(self):
        payload = compute_partial_fill(5000, 100000, 0.01)
        self.assertEqual(payload["filled_qty"], 1000)
        self.assertEqual(payload["unfilled_qty"], 4000)

    def test_limit_up_fill_probability(self):
        payload = limit_up_fill_probability("sealed_limit_up")
        self.assertGreater(payload["fill_probability"], 0)
        self.assertIn("assumptions", payload)

    def test_dynamic_slippage(self):
        self.assertGreaterEqual(
            estimate_slippage_bps(order_notional=100000, daily_amount=10000000, volatility=0.03, liquidity_score=50)["estimated_slippage_bps"],
            0,
        )

    def test_china_a_fees(self):
        fees = estimate_china_a_fees(100000, side="sell")
        self.assertGreater(fees["total_fee"], 0)

    def test_china_a_fees_match_unified_cost_service_without_slippage(self):
        fees = estimate_china_a_fees(100000, side="sell")
        expected = compute_trade_costs(
            side="sell",
            gross=100000,
            config=TradeCostConfig(slippage_bps=0.0),
        )

        self.assertEqual(fees["commission"], expected["commission"])
        self.assertEqual(fees["stamp_tax"], expected["stamp_tax"])
        self.assertEqual(fees["transfer_fee"], expected["transfer_fee"])
        self.assertEqual(fees["total_fee"], expected["total_fee"])
        self.assertEqual(estimate_china_a_fees(0, side="buy")["total_fee"], 0.0)

    def test_execution_realism_report_is_optional(self):
        payload = build_execution_realism_report({"execution_realism": True})
        self.assertTrue(payload["default_backtest_unchanged"])
        self.assertIn("execution_realism_assumptions", payload)

    def test_cli_backtest_accepts_execution_realism_flags(self):
        proc = subprocess.run(
            [sys.executable, "scripts/qd.py", "backtest", "--network", "false", "--execution-realism", "true", "--synthetic-test-data", "true", "--json", "true", "--summary", "true"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(proc.stdout)["task_type"], "backtest")


if __name__ == "__main__":
    unittest.main()
