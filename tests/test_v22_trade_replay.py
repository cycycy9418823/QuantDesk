from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from quantdesk.paper.strategy_trade_replay import (
    TRADE_REPLAY_DISCLAIMER,
    TRADE_REPLAY_MODE,
    TRADE_REPLAY_MODE_LABEL,
    TRADE_REPLAY_STATUS,
    strategy_trade_replay,
)
from quantdesk.paper.trade_costs import TradeCostConfig, compute_trade_costs
from quantdesk.strategy_historical_replay import strategy_historical_replay

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _history(
    rows: int = 160,
    *,
    drop_tail: bool = True,
    volume_spike: bool = True,
    down_only: bool = False,
) -> list[dict]:
    dates = pd.date_range("2024-01-01", periods=rows, freq="B")
    closes: list[float] = []
    volumes: list[int] = []
    for idx in range(rows):
        if down_only:
            close = 10.0 - idx * 0.01
        elif drop_tail and idx >= rows - 25:
            close = 10.0 + (rows - 25) * 0.06 - (idx - (rows - 25)) * 0.35
        else:
            close = 10.0 + idx * 0.06
        closes.append(close)
        if volume_spike and rows - 55 <= idx < rows - 25:
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


def _signal_row(history: list[dict], signal_date: str) -> dict:
    for row in history:
        if row.get("date") == signal_date:
            return row
    raise AssertionError(f"Missing row for date {signal_date}")


class V22TradeReplayTests(unittest.TestCase):
    def _assert_paper_boundary(self, payload: dict) -> None:
        self.assertFalse(payload["is_real_order"])
        self.assertTrue(payload["research_only"])
        self.assertTrue(payload["paper_only"])
        self.assertTrue(payload["no_broker"])
        self.assertTrue(payload["not_investment_advice"])
        self.assertTrue(payload["not_position_advice"])

    def test_empty_history_returns_local_disclaimer(self) -> None:
        result = strategy_trade_replay([], "均线突破", symbol="000001")
        self._assert_paper_boundary(result)
        self.assertEqual(result["replay_mode"], TRADE_REPLAY_MODE)
        self.assertEqual(result["replay_mode_label"], TRADE_REPLAY_MODE_LABEL)
        self.assertEqual(result["trade_replay_status"], TRADE_REPLAY_STATUS)
        self.assertEqual(result["disclaimer"], TRADE_REPLAY_DISCLAIMER)
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["closed_trade_count"], 0)
        self.assertEqual(result["final_equity"], 100000.0)
        self.assertTrue(any("缺少" in warning for warning in result["warnings"]))

    def test_ma_breakout_generates_trades_and_closes(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "均线突破",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        self._assert_paper_boundary(result)
        self.assertEqual(result["replay_mode"], TRADE_REPLAY_MODE)
        self.assertGreaterEqual(result["trade_count"], 1)
        self.assertGreaterEqual(result["closed_trade_count"], 1)
        self.assertEqual(result["disclaimer"], TRADE_REPLAY_DISCLAIMER)
        # Equity curve must be present and aligned with signals.
        self.assertGreater(len(result["equity_curve"]), 20)
        # Trades must carry structural fields expected by the UI.
        first_trade = result["trades"][0]
        self._assert_paper_boundary(first_trade)
        for key in (
            "trade_id",
            "entry_date",
            "entry_price",
            "quantity",
            "status",
            "matched_conditions",
            "risk_line_type",
            "disclaimer",
        ):
            self.assertIn(key, first_trade)
        # Lot rounding is enforced.
        self.assertEqual(first_trade["quantity"] % 100, 0)
        self.assertGreaterEqual(first_trade["quantity"], 100)

    def test_next_open_falls_back_when_open_missing(self) -> None:
        hist = _history(60)
        for row in hist:
            row.pop("open", None)
        result = strategy_trade_replay(
            hist,
            "均线突破",
            symbol="000001",
            buy_price_mode="next_open",
            sell_price_mode="next_open",
            max_position_ratio=0.3,
        )
        self.assertEqual(result["buy_price_mode"], "next_open")
        self.assertTrue(any("next_open 不可用" in warning for warning in result["warnings"]))

    def test_position_size_respects_lot_and_cash(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "均线突破",
            symbol="000001",
            initial_cash=100000.0,
            max_position_ratio=0.1,
        )
        for trade in result["trades"]:
            self.assertEqual(int(trade["quantity"]) % 100, 0)
            self.assertGreater(float(trade["entry_price"]), 0)

    def test_cn_a_share_star_replay_uses_board_lot_size(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="688001",
            name="sample",
            initial_cash=100000.0,
            position_size=100,
            market_rules="cn_a_share",
            commission_rate=0.0,
            slippage_rate=0.0,
        )

        self.assertEqual(result["trade_count"], 0)
        self.assertGreaterEqual(int(result["blocked_order_count"]), 1)
        self.assertEqual(int(result["lot_size"]), 200)

    def test_commission_and_slippage_applied(self) -> None:
        cheap = strategy_trade_replay(
            _history(80),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
        )
        costly = strategy_trade_replay(
            _history(80),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            commission_rate=0.005,
            slippage_rate=0.005,
        )
        self.assertGreaterEqual(cheap["final_equity"], costly["final_equity"])

    def test_result_carries_source_metadata(self) -> None:
        result = strategy_trade_replay(
            _history(80),
            "均线突破",
            symbol="000001",
            max_position_ratio=0.3,
            history_source_symbol="000001",
            history_source_note="本地历史日线",
        )
        self.assertEqual(result["history_source_symbol"], "000001")
        self.assertEqual(result["history_source_note"], "本地历史日线")
        self.assertGreater(result["history_rows"], 0)

    def test_no_aggressive_language_in_payload(self) -> None:
        result = strategy_trade_replay(_history(80), "均线突破", symbol="000001", max_position_ratio=0.3)
        text = json.dumps(result, ensure_ascii=False)
        for banned in ["必涨", "稳赚", "保证收益", "一定盈利", "强烈买入"]:
            self.assertNotIn(banned, text)
        # Disclaimer must make local-only and no-broker boundary explicit.
        self.assertIn("不接券商", text)
        self.assertIn("不构成投资建议", text)

    def test_trade_replay_output_fields_complete(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        for key in [
            "replay_mode",
            "initial_cash",
            "final_cash",
            "final_equity",
            "total_return",
            "max_drawdown",
            "trade_count",
            "win_rate",
            "average_trade_return",
            "holding_days_avg",
            "equity_curve",
            "trades",
            "warnings",
            "assumptions",
        ]:
            self.assertIn(key, result)
        self.assertEqual(result["replay_mode"], TRADE_REPLAY_MODE)
        self.assertEqual(result["trade_replay_status"], TRADE_REPLAY_STATUS)
        self.assertIsInstance(result["warnings"], list)
        self.assertIsInstance(result["assumptions"], list)
        self.assertEqual(len(result["warnings"]), len(set(result["warnings"])))
        self.assertEqual(len(result["assumptions"]), len(set(result["assumptions"])))

    def test_trade_row_fields_complete(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        trade = result["trades"][0]
        self._assert_paper_boundary(trade)
        for key in [
            "symbol",
            "strategy_id",
            "entry_signal_date",
            "entry_execution_date",
            "entry_execution_policy",
            "entry_price",
            "entry_signal",
            "quantity",
            "entry_fee",
            "exit_signal_date",
            "exit_execution_date",
            "exit_execution_policy",
            "exit_price",
            "exit_signal",
            "exit_fee",
            "pnl",
            "return_pct",
            "holding_days",
            "status",
        ]:
            self.assertIn(key, trade)

    def test_next_open_buy_uses_next_day_open(self) -> None:
        history = _history()
        result = strategy_trade_replay(
            history,
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
            buy_price_mode="next_open",
            sell_price_mode="next_open",
        )
        trade = result["trades"][0]
        entry_index = next(index for index, row in enumerate(history) if row["date"] == trade["entry_signal_date"])
        self.assertEqual(trade["entry_execution_policy"], "next_open")
        self.assertEqual(trade["entry_execution_date"], history[entry_index + 1]["date"])
        self.assertAlmostEqual(float(trade["entry_price"]), float(history[entry_index + 1]["open"]), places=6)

    def test_next_open_sell_uses_next_day_open(self) -> None:
        history = _history()
        result = strategy_trade_replay(
            history,
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
            buy_price_mode="next_open",
            sell_price_mode="next_open",
        )
        trade = result["trades"][0]
        exit_index = next(index for index, row in enumerate(history) if row["date"] == trade["exit_signal_date"])
        self.assertEqual(trade["exit_execution_policy"], "next_open")
        self.assertEqual(trade["exit_execution_date"], history[exit_index + 1]["date"])
        self.assertAlmostEqual(float(trade["exit_price"]), float(history[exit_index + 1]["open"]), places=6)

    def test_close_mode_uses_signal_day(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
            buy_price_mode="close",
            sell_price_mode="close",
        )
        trade = result["trades"][0]
        self.assertEqual(trade["entry_signal_date"], trade["entry_execution_date"])
        self.assertEqual(trade["exit_signal_date"], trade["exit_execution_date"])
        self.assertEqual(trade["entry_execution_policy"], "signal_close")
        self.assertEqual(trade["exit_execution_policy"], "signal_close")

    def test_commission_affects_equity_and_pnl(self) -> None:
        base = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
        )
        costly = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.005,
            slippage_rate=0.0,
        )
        self.assertLess(float(costly["final_equity"]), float(base["final_equity"]))
        self.assertLess(float(costly["trades"][0]["pnl"]), float(base["trades"][0]["pnl"]))

    def test_cn_a_share_replay_exposes_unified_fee_breakdown(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.00025,
            slippage_rate=0.0,
            market_rules="cn_a_share",
            transfer_fee_rate=0.001,
        )

        self.assertGreaterEqual(result["closed_trade_count"], 1)
        trade = result["trades"][0]
        config = TradeCostConfig(
            commission_rate=0.00025,
            min_commission=5.0,
            stamp_tax_rate=0.0005,
            transfer_fee_rate=0.001,
            slippage_bps=0.0,
            apply_min_commission=True,
        )
        entry_gross = float(trade["entry_price"]) * int(trade["quantity"])
        exit_gross = float(trade["exit_price"]) * int(trade["quantity"])
        expected_entry = compute_trade_costs(side="buy", gross=entry_gross, config=config)
        expected_exit = compute_trade_costs(side="sell", gross=exit_gross, config=config)

        self.assertAlmostEqual(float(trade["entry_commission"]), float(expected_entry["commission"]), places=4)
        self.assertAlmostEqual(float(trade["entry_stamp_duty"]), 0.0, places=4)
        self.assertAlmostEqual(float(trade["entry_transfer_fee"]), float(expected_entry["transfer_fee"]), places=4)
        self.assertAlmostEqual(float(trade["entry_fee"]), float(expected_entry["total_fee"]), places=4)
        self.assertAlmostEqual(float(trade["exit_commission"]), float(expected_exit["commission"]), places=4)
        self.assertAlmostEqual(float(trade["exit_stamp_duty"]), float(expected_exit["stamp_duty"]), places=4)
        self.assertAlmostEqual(float(trade["exit_transfer_fee"]), float(expected_exit["transfer_fee"]), places=4)
        self.assertAlmostEqual(float(trade["exit_fee"]), float(expected_exit["total_fee"]), places=4)
        self.assertEqual(trade["entry_fee_model"], expected_entry["fee_model"])
        self.assertEqual(trade["exit_fee_model"], expected_exit["fee_model"])

    def test_slippage_affects_entry_and_exit_prices(self) -> None:
        base = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.0,
            buy_price_mode="next_open",
            sell_price_mode="next_open",
        )
        slipped = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
            commission_rate=0.0,
            slippage_rate=0.01,
            buy_price_mode="next_open",
            sell_price_mode="next_open",
        )
        self.assertGreater(float(slipped["trades"][0]["entry_price"]), float(base["trades"][0]["entry_price"]))
        self.assertLess(float(slipped["trades"][0]["exit_price"]), float(base["trades"][0]["exit_price"]))

    def test_open_trade_marks_to_market(self) -> None:
        result = strategy_trade_replay(
            _history(drop_tail=False),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        self.assertEqual(result["open_trade_count"], 1)
        self.assertEqual(result["trades"][0]["status"], "open")
        self.assertGreater(float(result["final_equity"]), float(result["final_cash"]))
        self.assertTrue(any("最后一天仍有持仓" in warning for warning in result["warnings"]))

    def test_no_signals_return_zero_trades(self) -> None:
        result = strategy_trade_replay(
            _history(rows=40, drop_tail=False, volume_spike=False, down_only=True),
            "ma_breakout",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(float(result["final_cash"]), 100000.0)
        self.assertEqual(float(result["final_equity"]), 100000.0)
        self.assertTrue(result["equity_curve"])
        self.assertTrue(any("没有买入信号" in warning for warning in result["warnings"]))

    def test_repeated_buy_signals_do_not_reopen(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["closed_trade_count"], 1)
        self.assertTrue(any("单策略单股票仅保留单笔仓位" in warning for warning in result["warnings"]))

    def test_funds_insufficient_emit_warning(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=1.0,
            max_position_ratio=0.3,
        )
        self.assertEqual(result["trade_count"], 0)
        self.assertTrue(any("资金不足" in warning for warning in result["warnings"]))

    def test_invalid_params_return_structured_errors(self) -> None:
        cases = [
            ({"initial_cash": 0}, "initial_cash"),
            ({"commission_rate": -0.1}, "commission_rate"),
            ({"slippage_rate": -0.1}, "slippage_rate"),
            ({"buy_price_mode": "bad"}, "buy_price_mode"),
            ({"sell_price_mode": "bad"}, "sell_price_mode"),
            ({"start_date": "2024-02-01", "end_date": "2024-01-01"}, "start_date"),
        ]
        for kwargs, token in cases:
            with self.subTest(kwargs=kwargs):
                result = strategy_trade_replay(
                    _history(),
                    "trend_continuation",
                    symbol="000001",
                    name="样例股票",
                    max_position_ratio=0.3,
                    **kwargs,
                )
                self.assertTrue(result["errors"])
                self.assertTrue(any(token in error for error in result["errors"]))
                self.assertEqual(result["trade_count"], 0)

    def test_unknown_strategy_returns_warning_not_crash(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "unknown_strategy",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        self.assertTrue(result["strategy_warning"])
        self.assertIn("回退", result["strategy_warning"])

    def test_max_drawdown_and_equity_curve_have_expected_keys(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        self.assertLessEqual(float(result["max_drawdown"]), 0.0)
        self.assertGreaterEqual(float(result["max_drawdown"]), -1.0)
        point = result["equity_curve"][0]
        for key in ["date", "cash", "position_value", "equity"]:
            self.assertIn(key, point)

    def test_rule_replay_and_trade_replay_modes_are_distinct(self) -> None:
        rule_replay = strategy_historical_replay(
            [
                {
                    "date": "2024-01-02",
                    "symbol": "000001",
                    "name": "样例",
                    "close": 10,
                    "ma20": 9,
                    "activity_value": 200,
                    "activity_avg_20": 100,
                    "data_reliability_score": 90,
                }
            ],
            "均线突破",
        )
        trade_replay = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        self.assertEqual(rule_replay["replay_mode"], "rule_replay")
        self.assertEqual(trade_replay["replay_mode"], "trade_replay")

    def test_trade_replay_docs_and_ui_distinguish_modes(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        docs = (PROJECT_ROOT / "docs" / "api_payload_schema.md").read_text(encoding="utf-8")
        app = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        for text in (readme, docs, app):
            self.assertIn("策略历史回放（规则层）", text)
            self.assertIn("交易路径回放", text)
            self.assertIn("rule_replay", text)
            self.assertIn("trade_replay", text)
        self.assertIn("不构成投资建议", readme)

    def test_temp_helper_no_longer_defaults_to_desktop_paths(self) -> None:
        helper = (PROJECT_ROOT / "tests" / "test_v20_local_decision_assistant.py").read_text(encoding="utf-8")
        self.assertIn("TemporaryDirectory", helper)
        self.assertNotIn(".quantdesk_test_tmp", helper)
        self.assertNotIn("Desktop", helper)

    def test_trade_replay_json_has_no_aggressive_language(self) -> None:
        result = strategy_trade_replay(
            _history(),
            "trend_continuation",
            symbol="000001",
            name="样例股票",
            initial_cash=100000.0,
            max_position_ratio=0.3,
        )
        text = json.dumps(result, ensure_ascii=False)
        for banned in ["必涨", "稳赚", "保证收益", "自动下单", "强烈买入", "满仓"]:
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()


