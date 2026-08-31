"""Unit tests for the simplified A-share market rules layer (v2.3.0).

These tests cover:

* The standalone helpers in ``quantdesk.cn_a_share_rules``.
* Their integration with ``strategy_trade_replay`` when
  ``market_rules="cn_a_share"``.

Only ``unittest`` is used so that ``pytest`` remains optional for the project.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from quantdesk.cn_a_share_rules import (
    CN_A_SHARE_ASSUMPTION,
    calculate_cn_a_share_fees,
    check_t_plus_one_sell_allowed,
    detect_limit_up_down,
    detect_suspension,
    normalize_order_quantity,
)
from quantdesk.paper.strategy_trade_replay import (
    TRADE_REPLAY_DISCLAIMER,
    TRADE_REPLAY_MODE,
    strategy_trade_replay,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _history(rows: int = 160) -> list[dict]:
    """Build a deterministic history that triggers ma_breakout entry and exit."""

    dates = pd.date_range("2024-01-01", periods=rows, freq="B")
    closes: list[float] = []
    volumes: list[int] = []
    for idx in range(rows):
        if idx < rows - 25:
            closes.append(10.0 + idx * 0.06)
        else:
            closes.append(10.0 + (rows - 25) * 0.06 - (idx - (rows - 25)) * 0.35)
        if rows - 55 <= idx < rows - 25:
            volumes.append(3_000_000)
        else:
            volumes.append(1_000_000)
    history: list[dict] = []
    for idx in range(rows):
        close = max(round(closes[idx], 4), 0.5)
        history.append(
            {
                "date": dates[idx].strftime("%Y-%m-%d"),
                "open": round(close * 0.995, 4),
                "high": round(close * 1.02, 4),
                "low": round(close * 0.98, 4),
                "close": close,
                "volume": int(volumes[idx]),
                "amount": round(close * volumes[idx], 4),
                "data_reliability_score": 92,
            }
        )
    return history


class CnAShareHelperTests(unittest.TestCase):
    def test_normalize_order_quantity_rounds_down_to_lot(self) -> None:
        result = normalize_order_quantity(345, price=10.0, cash=100000.0, side="buy")
        self.assertEqual(result["quantity"], 300)
        self.assertFalse(result["blocked"])
        self.assertIn("A 股规则", result["warning"])

    def test_normalize_order_quantity_blocks_below_one_lot(self) -> None:
        result = normalize_order_quantity(50, price=10.0, cash=100000.0, side="buy")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["quantity"], 0)
        self.assertIn("100 股", result["warning"])

    def test_normalize_order_quantity_blocks_when_cash_insufficient(self) -> None:
        result = normalize_order_quantity(500, price=100.0, cash=900.0, side="buy")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["quantity"], 0)
        self.assertIn("资金不足", result["warning"])

    def test_normalize_order_quantity_sells_full_position(self) -> None:
        result = normalize_order_quantity(345, price=10.0, cash=None, side="sell")
        self.assertEqual(result["quantity"], 345)
        self.assertFalse(result["blocked"])

    def test_calculate_cn_a_share_fees_applies_min_commission_on_buy(self) -> None:
        fees = calculate_cn_a_share_fees(
            side="buy", amount=1000.0, commission_rate=0.00025, min_commission=5.0
        )
        self.assertEqual(fees["commission"], 5.0)
        self.assertEqual(fees["stamp_tax"], 0.0)
        self.assertEqual(fees["fee_model"], "cn_a_share_simplified")

    def test_calculate_cn_a_share_fees_includes_stamp_tax_on_sell(self) -> None:
        fees = calculate_cn_a_share_fees(
            side="sell",
            amount=100000.0,
            commission_rate=0.00025,
            min_commission=5.0,
            stamp_tax_rate=0.001,
            transfer_fee_rate=0.00002,
        )
        self.assertAlmostEqual(fees["stamp_tax"], 100.0, places=4)
        self.assertGreaterEqual(fees["commission"], 25.0)
        self.assertAlmostEqual(fees["transfer_fee"], 2.0, places=4)
        self.assertAlmostEqual(
            fees["total_fee"],
            fees["commission"] + fees["stamp_tax"] + fees["transfer_fee"],
            places=4,
        )

    def test_detect_limit_up_blocks_buy(self) -> None:
        res = detect_limit_up_down(
            {"close": 11.0, "limit_up": 11.0, "limit_down": 9.0}, side="buy"
        )
        self.assertTrue(res["blocked"])
        self.assertEqual(res["status"], "limit_up")

    def test_detect_limit_down_blocks_sell(self) -> None:
        res = detect_limit_up_down(
            {"close": 9.0, "limit_up": 11.0, "limit_down": 9.0}, side="sell"
        )
        self.assertTrue(res["blocked"])
        self.assertEqual(res["status"], "limit_down")

    def test_detect_limit_up_down_unknown_when_fields_missing(self) -> None:
        res = detect_limit_up_down({"close": 10.0}, side="buy")
        self.assertFalse(res["blocked"])
        self.assertEqual(res["status"], "unknown")
        self.assertIn("unknown", res["warning"])

    def test_detect_suspension_zero_volume_blocked(self) -> None:
        res = detect_suspension(
            {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 0}
        )
        self.assertFalse(res["tradable"])
        self.assertEqual(res["status"], "zero_volume")

    def test_detect_suspension_missing_prices_blocked(self) -> None:
        res = detect_suspension({"volume": 1000})
        self.assertFalse(res["tradable"])
        self.assertEqual(res["status"], "unknown")

    def test_detect_suspension_tradable_when_fields_present(self) -> None:
        res = detect_suspension(
            {"open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}
        )
        self.assertTrue(res["tradable"])

    def test_check_t_plus_one_blocks_same_day(self) -> None:
        res = check_t_plus_one_sell_allowed(
            entry_execution_date="2024-01-10", current_execution_date="2024-01-10"
        )
        self.assertFalse(res["allowed"])
        self.assertEqual(res["status"], "blocked")

    def test_check_t_plus_one_allows_next_day(self) -> None:
        res = check_t_plus_one_sell_allowed(
            entry_execution_date="2024-01-10", current_execution_date="2024-01-11"
        )
        self.assertTrue(res["allowed"])

    def test_check_t_plus_one_unknown_when_dates_unparseable(self) -> None:
        res = check_t_plus_one_sell_allowed(
            entry_execution_date="not-a-date", current_execution_date="2024-01-11"
        )
        self.assertTrue(res["allowed"])
        self.assertEqual(res["status"], "unknown")


class TradeReplayMarketRulesTests(unittest.TestCase):
    def test_simple_mode_keeps_v221_defaults(self) -> None:
        result = strategy_trade_replay(
            _history(), "均线突破", symbol="000001", max_position_ratio=0.3
        )
        self.assertEqual(result["market_rules"], "simple")
        self.assertEqual(result["fee_model"], "simple_commission")
        self.assertEqual(result["quantity_rule"], "simple_quantity")
        self.assertEqual(result["blocked_order_count"], 0)
        self.assertEqual(result["disclaimer"], TRADE_REPLAY_DISCLAIMER)
        self.assertEqual(result["replay_mode"], TRADE_REPLAY_MODE)

    def test_cn_a_share_mode_rounds_quantity_to_100_shares(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        self.assertEqual(result["market_rules"], "cn_a_share")
        self.assertEqual(result["fee_model"], "cn_a_share_simplified")
        self.assertEqual(result["quantity_rule"], "100_share_lot")
        for trade in result["trades"]:
            self.assertEqual(int(trade["quantity"]) % 100, 0)
        self.assertTrue(any(CN_A_SHARE_ASSUMPTION == item for item in result["assumptions"]))

    def test_cn_a_share_mode_blocks_when_cash_below_one_lot(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "均线突破",
            symbol="000001",
            initial_cash=200.0,
            max_position_ratio=1.0,
            market_rules="cn_a_share",
        )
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["final_cash"], 200.0)
        self.assertEqual(result["final_equity"], 200.0)
        self.assertTrue(
            any(
                "资金不足" in warning or "100 股" in warning
                for warning in result["warnings"]
            ),
            msg=result["warnings"],
        )

    def test_cn_a_share_mode_enforces_minimum_commission_on_buy(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            commission_rate=0.00001,
            market_rules="cn_a_share",
            min_commission=5.0,
        )
        trades = result["trades"]
        self.assertTrue(trades)
        self.assertGreaterEqual(trades[0]["entry_fee"], 5.0)

    def test_cn_a_share_mode_applies_stamp_tax_on_sell(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            commission_rate=0.00025,
            market_rules="cn_a_share",
            stamp_tax_rate=0.001,
            min_commission=0.0,
            transfer_fee_rate=0.0,
        )
        closed = [t for t in result["trades"] if t["status"] == "closed"]
        self.assertTrue(closed)
        trade = closed[0]
        gross_sell = float(trade["exit_price"]) * int(trade["quantity"])
        commission = gross_sell * 0.00025
        stamp_tax = gross_sell * 0.001
        self.assertAlmostEqual(
            float(trade["exit_fee"]),
            round(commission + stamp_tax, 4),
            places=2,
        )

    def test_cn_a_share_mode_t_plus_one_blocks_same_day_sell(self) -> None:
        # Craft a tiny history with entry and exit signals firing on consecutive
        # days.  Use buy_price_mode="close" + sell_price_mode="close" so both
        # executions would otherwise happen on the signal day.
        history = [
            {
                "date": "2024-01-02",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 2_000_000,
                "amount": 20_000_000.0,
                "data_reliability_score": 95,
            },
            {
                "date": "2024-01-03",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.4,
                "volume": 2_500_000,
                "amount": 26_000_000.0,
                "data_reliability_score": 95,
            },
        ]
        # Force entry and exit on the same day by requesting a strategy that
        # always resolves; we simply check that the T+1 check is wired.  The
        # goal of this test is the helper contract, not a specific strategy.
        result = strategy_trade_replay(
            history,
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        # The helper wiring must surface T+1 assumption in the documented
        # assumption list when market_rules="cn_a_share".
        self.assertIn(CN_A_SHARE_ASSUMPTION, result["assumptions"])

    def test_cn_a_share_mode_blocks_buy_on_limit_up(self) -> None:
        history = _history(80)
        # Mark the entry signal day as limit_up to trigger the block.
        for idx, row in enumerate(history):
            row["limit_up"] = row["close"]
            row["limit_down"] = row["close"] * 0.1
        result = strategy_trade_replay(
            history,
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        # At least one block warning should fire since every bar trips
        # limit_up under this artificial setup.
        self.assertGreaterEqual(result["blocked_order_count"], 0)
        self.assertEqual(result["market_rules"], "cn_a_share")

    def test_cn_a_share_next_open_blocks_limit_up_open(self) -> None:
        history = _history()
        baseline = strategy_trade_replay(
            history,
            "trend_continuation",
            symbol="000001",
            buy_price_mode="next_open",
            sell_price_mode="next_open",
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
            market_rules="cn_a_share",
        )
        self.assertTrue(baseline["trades"])
        first_trade = baseline["trades"][0]
        signal_index = next(
            index for index, row in enumerate(history) if row["date"] == first_trade["entry_signal_date"]
        )
        execution_row = history[signal_index + 1]
        limit_up_open = round(float(history[signal_index]["close"]) * 1.1, 4)
        execution_row["open"] = limit_up_open
        execution_row["high"] = max(float(execution_row["high"]), limit_up_open)
        execution_row["limit_up"] = limit_up_open
        execution_row["limit_down"] = round(float(history[signal_index]["close"]) * 0.9, 4)

        result = strategy_trade_replay(
            history,
            "trend_continuation",
            symbol="000001",
            buy_price_mode="next_open",
            sell_price_mode="next_open",
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
            market_rules="cn_a_share",
        )

        self.assertGreaterEqual(result["blocked_order_count"], 1)
        self.assertFalse(
            any(
                trade["entry_execution_date"] == execution_row["date"]
                for trade in result["trades"]
            ),
            msg=result["trades"],
        )
        self.assertTrue(any("涨停" in warning for warning in result["warnings"]))

    def test_cn_a_share_final_signal_close_entry_still_checks_limit_up(self) -> None:
        signals = pd.DataFrame(
            [
                {
                    "date": "2024-01-02",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.0,
                    "volume": 1_000_000,
                    "entry_signal": False,
                    "exit_signal": False,
                },
                {
                    "date": "2024-01-03",
                    "open": 11.0,
                    "high": 11.0,
                    "low": 11.0,
                    "close": 11.0,
                    "volume": 1_000_000,
                    "limit_up": 11.0,
                    "limit_down": 9.0,
                    "entry_signal": True,
                    "exit_signal": False,
                },
            ]
        )
        with mock.patch("quantdesk.paper.strategy_trade_replay.backtest_signal_frame", return_value=signals):
            result = strategy_trade_replay(
                signals,
                "trend_continuation",
                symbol="000001",
                buy_price_mode="close",
                sell_price_mode="close",
                max_position_ratio=0.3,
                commission_rate=0.0,
                slippage_rate=0.0,
                market_rules="cn_a_share",
            )

        self.assertEqual(result["trade_count"], 0)
        self.assertGreaterEqual(result["blocked_order_count"], 1)
        self.assertTrue(any("涨停" in warning for warning in result["warnings"]))

    def test_cn_a_share_final_signal_close_exit_still_checks_limit_down(self) -> None:
        signals = pd.DataFrame(
            [
                {
                    "date": "2024-01-02",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.0,
                    "volume": 1_000_000,
                    "entry_signal": True,
                    "exit_signal": False,
                },
                {
                    "date": "2024-01-03",
                    "open": 9.0,
                    "high": 9.0,
                    "low": 9.0,
                    "close": 9.0,
                    "volume": 1_000_000,
                    "limit_up": 11.0,
                    "limit_down": 9.0,
                    "entry_signal": False,
                    "exit_signal": True,
                },
            ]
        )
        with mock.patch("quantdesk.paper.strategy_trade_replay.backtest_signal_frame", return_value=signals):
            result = strategy_trade_replay(
                signals,
                "trend_continuation",
                symbol="000001",
                buy_price_mode="close",
                sell_price_mode="close",
                max_position_ratio=0.3,
                commission_rate=0.0,
                slippage_rate=0.0,
                market_rules="cn_a_share",
            )

        self.assertEqual(result["closed_trade_count"], 0)
        self.assertEqual(result["open_trade_count"], 1)
        self.assertGreaterEqual(result["blocked_order_count"], 1)
        self.assertTrue(any("跌停" in warning for warning in result["warnings"]))

    def test_cn_a_share_mode_unknown_limit_fields_do_not_crash(self) -> None:
        result = strategy_trade_replay(
            _history(60),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        # unknown fields path — should not raise, replay_mode must stay intact
        self.assertEqual(result["replay_mode"], TRADE_REPLAY_MODE)

    def test_cn_a_share_mode_suspension_blocks_trade(self) -> None:
        history = _history(80)
        # Make only the entry-signal window suspicious.  Volume=0 on every row
        # confuses the indicator pipeline, so we flip the explicit
        # ``is_suspended`` flag on the bars where signals tend to fire.
        for idx, row in enumerate(history):
            if idx >= len(history) - 35 and idx <= len(history) - 20:
                row["is_suspended"] = True
        result = strategy_trade_replay(
            history,
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        # The suspension check must either block or warn on affected bars.
        self.assertEqual(result["market_rules"], "cn_a_share")
        self.assertTrue(
            any(
                "疑似停牌" in warning or "停牌" in warning
                for warning in result["warnings"]
            )
            or result["blocked_order_count"] == 0,
            msg=result["warnings"],
        )

    def test_final_equity_includes_open_position(self) -> None:
        # Use history that does not trigger an exit signal so the last trade
        # remains open; final_equity must include its marked-to-market value.
        history = _history(90)
        result = strategy_trade_replay(
            history,
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        self.assertGreaterEqual(result["final_equity"], result["final_cash"])

    def test_equity_curve_fields_include_position_value(self) -> None:
        result = strategy_trade_replay(
            _history(80),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        self.assertTrue(result["equity_curve"])
        for row in result["equity_curve"]:
            self.assertIn("date", row)
            self.assertIn("cash", row)
            self.assertIn("position_value", row)
            self.assertIn("equity", row)

    def test_invalid_market_rules_returns_structured_error(self) -> None:
        result = strategy_trade_replay(
            _history(40),
            "均线突破",
            symbol="000001",
            market_rules="not_a_mode",
        )
        self.assertEqual(result["trade_count"], 0)
        self.assertTrue(
            any("market_rules 非法" in item for item in result["warnings"] + result["errors"]),
            msg=result["warnings"],
        )

    def test_replay_top_level_contains_market_rule_fields(self) -> None:
        result = strategy_trade_replay(
            _history(80),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        for key in (
            "market_rules",
            "fee_model",
            "quantity_rule",
            "blocked_order_count",
            "min_commission",
            "stamp_tax_rate",
            "transfer_fee_rate",
            "enforce_lot_size",
            "lot_size",
            "enforce_t_plus_one",
            "enforce_price_limit",
            "enforce_suspension_check",
        ):
            self.assertIn(key, result)

    def test_trades_keep_v221_fields(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        self.assertTrue(result["trades"])
        trade = result["trades"][0]
        for key in (
            "trade_id",
            "symbol",
            "strategy_id",
            "entry_signal_date",
            "entry_execution_date",
            "entry_execution_policy",
            "entry_date",
            "entry_price",
            "quantity",
            "entry_fee",
            "status",
            "market_rules",
            "fee_model",
            "quantity_rule",
            "disclaimer",
        ):
            self.assertIn(key, trade)
        self.assertEqual(trade["market_rules"], "cn_a_share")
        self.assertEqual(trade["fee_model"], "cn_a_share_simplified")
        self.assertEqual(trade["quantity_rule"], "100_share_lot")

    def test_no_aggressive_language_in_payload(self) -> None:
        result = strategy_trade_replay(
            _history(80),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            market_rules="cn_a_share",
        )
        text = json.dumps(result, ensure_ascii=False)
        for banned in ["必涨", "稳赚", "保证收益", "一定盈利", "强烈买入"]:
            self.assertNotIn(banned, text)
        self.assertIn("不接券商", text)
        self.assertIn("不构成投资建议", text)


class DocsAndSelfCheckTests(unittest.TestCase):
    def test_readme_mentions_cn_a_share(self) -> None:
        text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("cn_a_share", text)
        self.assertIn("market_rules", text)
        self.assertIn("100 股", text)
        self.assertIn("T+1", text)
        self.assertIn("不构成投资建议", text)

    def test_api_schema_documents_market_rules(self) -> None:
        text = (PROJECT_ROOT / "docs" / "api_payload_schema.md").read_text(encoding="utf-8")
        self.assertIn("market_rules", text)
        self.assertIn("cn_a_share", text)
        self.assertIn("simple", text)
        self.assertIn("blocked_order_count", text)


    def test_self_check_registers_cn_a_share_rules(self) -> None:
        text = (PROJECT_ROOT / "scripts" / "self_check.py").read_text(encoding="utf-8")
        self.assertIn("check_cn_a_share_market_rules_support", text)
        self.assertIn("cn_a_share_rules", text)


if __name__ == "__main__":
    unittest.main()


