"""Round 21 Part B: mark-to-market basis gate, cost profiles, assumption alignment."""

from __future__ import annotations

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_ROWS = (
    "date,symbol,open,high,low,close,volume,amount,adjust\n"
    "2026-08-10,600000,10.0,10.5,9.9,10.2,1000000,10200000,raw\n"
    "2026-08-11,600000,10.2,10.6,10.0,10.4,1000000,10400000,raw\n"
)
QFQ_ROWS = (
    "date,symbol,open,high,low,close,volume,amount,adjust\n"
    "2026-08-10,600000,10.0,10.5,9.9,10.2,1000000,10200000,qfq\n"
    "2026-08-11,600000,10.2,10.6,10.0,10.4,1000000,10400000,qfq\n"
)
MIXED_ROWS = (
    "date,symbol,open,high,low,close,volume,amount,adjust\n"
    "2026-08-10,600000,10.0,10.5,9.9,10.2,1000000,10200000,raw\n"
    "2026-08-11,600000,10.2,10.6,10.0,10.4,1000000,10400000,qfq\n"
)


class ValuationBasisGateTests(unittest.TestCase):
    def _write(self, tmp: Path, content: str) -> Path:
        daily = tmp / "daily"
        daily.mkdir(parents=True, exist_ok=True)
        (daily / "600000.csv").write_text(content, encoding="utf-8")
        return daily

    def test_raw_daily_close_passes_gate(self) -> None:
        import tempfile

        from quantdesk.paper.position_management_engine import _read_daily_close

        with tempfile.TemporaryDirectory() as tmp:
            daily = self._write(Path(tmp), RAW_ROWS)
            price, date, warnings = _read_daily_close("600000", "2026-08-11", daily_dir=daily)
        self.assertEqual(price, 10.4)
        self.assertEqual(date, "2026-08-11")
        self.assertFalse([w for w in warnings if "valuation_basis_gate_blocked" in w])

    def test_qfq_contamination_degrades_instead_of_silently_mixing(self) -> None:
        import tempfile

        from quantdesk.paper.position_management_engine import _read_daily_close

        with tempfile.TemporaryDirectory() as tmp:
            daily = self._write(Path(tmp), QFQ_ROWS)
            price, date, warnings = _read_daily_close("600000", "2026-08-11", daily_dir=daily)
        self.assertIsNone(price)
        self.assertEqual(date, "")
        self.assertTrue(any("valuation_basis_gate_blocked" in w for w in warnings))

    def test_mixed_basis_is_fail_closed(self) -> None:
        import tempfile

        from quantdesk.paper.position_management_engine import _read_daily_close

        with tempfile.TemporaryDirectory() as tmp:
            daily = self._write(Path(tmp), MIXED_ROWS)
            price, _, warnings = _read_daily_close("600000", "2026-08-11", daily_dir=daily)
        self.assertIsNone(price)
        self.assertTrue(any("valuation_basis_gate_blocked" in w for w in warnings))

    def test_gate_marks_position_degraded_with_version(self) -> None:
        from quantdesk.paper.position_management_engine import (
            VALUATION_GATE_VERSION,
            _apply_valuation_gate_marks,
        )

        position = {"symbol": "600000", "latest_price": 9.9}
        _apply_valuation_gate_marks(
            position,
            ["valuation_basis_gate_blocked:600000:basis=forward_adjusted:reason=non_raw"],
        )
        self.assertTrue(position.get("valuation_degraded"))
        self.assertEqual(position.get("valuation_degraded_reason"), "daily_close_price_basis_not_raw")
        self.assertEqual(position.get("valuation_gate_version"), VALUATION_GATE_VERSION)


class CostProfileTests(unittest.TestCase):
    def test_default_profile_is_legacy_and_numbers_unchanged(self) -> None:
        from quantdesk.paper.trade_costs import COST_PROFILE_LEGACY, TradeCostConfig, compute_trade_costs

        costs = compute_trade_costs(side="buy", gross=100_000.0)
        self.assertEqual(costs["cost_profile"], COST_PROFILE_LEGACY)
        self.assertEqual(costs["transfer_fee"], 0.0)
        self.assertEqual(TradeCostConfig().cost_profile, COST_PROFILE_LEGACY)

    def test_cn_current_profile_charges_transfer_fee_both_sides(self) -> None:
        from quantdesk.paper.trade_costs import (
            CN_CURRENT_TRANSFER_FEE_RATE,
            COST_PROFILE_CN_CURRENT,
            TradeCostConfig,
            compute_trade_costs,
        )

        self.assertEqual(CN_CURRENT_TRANSFER_FEE_RATE, 0.00001)
        cfg = TradeCostConfig.cn_current()
        self.assertEqual(cfg.cost_profile, COST_PROFILE_CN_CURRENT)
        self.assertEqual(cfg.transfer_fee_rate, CN_CURRENT_TRANSFER_FEE_RATE)
        buy = compute_trade_costs(side="buy", gross=100_000.0, config=cfg)
        sell = compute_trade_costs(side="sell", gross=100_000.0, config=cfg)
        self.assertAlmostEqual(float(buy["transfer_fee"]), 1.0, places=6)
        self.assertAlmostEqual(float(sell["transfer_fee"]), 1.0, places=6)
        self.assertEqual(buy["cost_profile"], COST_PROFILE_CN_CURRENT)
        self.assertEqual(sell["cost_profile"], COST_PROFILE_CN_CURRENT)

    def test_cash_helpers_expose_cost_profile(self) -> None:
        from quantdesk.paper.trade_costs import COST_PROFILE_LEGACY, buy_cash_outflow, sell_cash_inflow

        buy = buy_cash_outflow(price=10.0, quantity=100)
        sell = sell_cash_inflow(price=11.0, quantity=100, avg_cost=10.0)
        self.assertEqual(buy["cost_profile"], COST_PROFILE_LEGACY)
        self.assertEqual(sell["cost_profile"], COST_PROFILE_LEGACY)


class AssumptionAlignmentTests(unittest.TestCase):
    def test_default_assumptions_come_from_real_backtest_config(self) -> None:
        from quantdesk.backtest import BacktestConfig
        from quantdesk.backtest_assumptions import default_backtest_assumptions

        assumptions = default_backtest_assumptions()
        config = BacktestConfig()
        self.assertTrue(assumptions["costs_implemented"])
        self.assertEqual(assumptions["commission_rate"], config.commission_rate)
        self.assertEqual(assumptions["stamp_duty_rate"], config.stamp_duty_rate)
        self.assertEqual(assumptions["slippage_bps"], config.slippage_bps)
        self.assertEqual(assumptions["cost_profile"], config.cost_profile)
        self.assertIn("board_candidate_execution", assumptions)
        self.assertIn("不代表真实可成交", str(assumptions["board_candidate_execution"]))
        self.assertEqual(assumptions["price_basis"], "raw")

    def test_legacy_stamp_branch_semantics_disclosed(self) -> None:
        from quantdesk.backtest import BacktestConfig
        from quantdesk.backtest_assumptions import backtest_assumptions_from_config

        legacy = BacktestConfig(cost_profile="legacy")
        self.assertEqual(legacy.stamp_duty_rate, 0.001)
        assumptions = backtest_assumptions_from_config(legacy)
        self.assertIn("legacy", str(assumptions["stamp_duty_rate_warning"]))


class InitialCashConfigTests(unittest.TestCase):
    def test_builtin_config_has_paper_initial_cash(self) -> None:
        from quantdesk.config import BUILTIN_DEFAULT_CONFIG

        self.assertEqual(BUILTIN_DEFAULT_CONFIG["paper"]["initial_cash"], 100000)

    def test_orchestrator_reads_config_not_hardcoded_10k(self) -> None:
        text = (PROJECT_ROOT / "quantdesk" / "daily_workflow_orchestrator.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("initial_cash=10000", text)
        self.assertIn("_paper_initial_cash", text)

    def test_account_scale_helper_labels_legacy_10k(self) -> None:
        from quantdesk.paper.auto_paper_portfolio import paper_account_scale

        scale, note = paper_account_scale(10000)
        self.assertEqual(scale, "legacy_10k")
        self.assertIn("教学规模", note)
        self.assertIn("不与 10 万回测比收益", note)
        scale_default, note_default = paper_account_scale(100000)
        self.assertEqual(scale_default, "configured")
        self.assertEqual(note_default, "")


class AdjustmentFactorAdjudicationTests(unittest.TestCase):
    def test_loader_never_reads_singular_alias(self) -> None:
        import tempfile

        from quantdesk.paper.adjustment_policy import load_adjustment_factor_table

        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp)
            (meta / "adjustment_factor.csv").write_text(
                "symbol,trade_date,adj_factor,source\n600000,2026-01-02,1.0,unit\n",
                encoding="utf-8",
            )
            table = load_adjustment_factor_table(meta)
        self.assertTrue(
            any("missing_adjustment_factor_file" in w for w in table.warnings),
            table.warnings,
        )
        self.assertTrue(
            any("alias_not_used" in w for w in table.warnings),
            table.warnings,
        )

    def test_loader_prefers_canonical_plural(self) -> None:
        import tempfile

        from quantdesk.paper.adjustment_policy import load_adjustment_factor_table

        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp)
            (meta / "adjustment_factors.csv").write_text(
                "symbol,trade_date,adj_factor,source\n600000,2026-01-02,1.0,unit\n",
                encoding="utf-8",
            )
            table = load_adjustment_factor_table(meta)
        self.assertTrue(str(table.path).endswith("adjustment_factors.csv"))


if __name__ == "__main__":
    unittest.main()
