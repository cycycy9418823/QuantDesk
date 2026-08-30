"""Tests for v2.7.3 low-price workbench schema and defaults."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.daily_schema import (
    has_amount_field,
    normalize_daily_columns,
)
from quantdesk.data_confidence import calculate_data_confidence


class DailySchemaTests(unittest.TestCase):
    def test_recognizes_amount(self) -> None:
        df = pd.DataFrame({"amount": [100.0]})
        result = normalize_daily_columns(df)
        self.assertIn("amount", result.columns)

    def test_recognizes_turnover(self) -> None:
        df = pd.DataFrame({"turnover": [200.0]})
        result = normalize_daily_columns(df)
        self.assertIn("amount", result.columns)

    def test_recognizes_chinese_amount(self) -> None:
        df = pd.DataFrame({"成交额": [300.0]})
        result = normalize_daily_columns(df)
        self.assertIn("amount", result.columns)

    def test_recognizes_volume(self) -> None:
        df = pd.DataFrame({"volume": [1000]})
        result = normalize_daily_columns(df)
        self.assertIn("volume", result.columns)

    def test_recognizes_chinese_volume(self) -> None:
        df = pd.DataFrame({"成交量": [2000]})
        result = normalize_daily_columns(df)
        self.assertIn("volume", result.columns)

    def test_amount_ma20_calculation(self) -> None:
        df = pd.DataFrame({"amount": [1_000_000.0] * 25})
        normalized = normalize_daily_columns(df)
        normalized["amount_ma20"] = normalized["amount"].rolling(20, min_periods=20).mean()
        self.assertAlmostEqual(normalized["amount_ma20"].iloc[-1], 1_000_000.0)

    def test_volume_ma20_calculation(self) -> None:
        df = pd.DataFrame({"volume": [50000] * 25})
        normalized = normalize_daily_columns(df)
        normalized["volume_ma20"] = normalized["volume"].rolling(20, min_periods=20).mean()
        self.assertAlmostEqual(normalized["volume_ma20"].iloc[-1], 50000.0)

    def test_has_amount_true(self) -> None:
        df = pd.DataFrame({"amount": [100.0, 200.0]})
        self.assertTrue(has_amount_field(normalize_daily_columns(df)))

    def test_has_amount_false_when_missing(self) -> None:
        df = pd.DataFrame({"close": [10.0]})
        self.assertFalse(has_amount_field(normalize_daily_columns(df)))

    def test_liquidity_not_unknown_when_amount_present(self) -> None:
        from quantdesk.strategy_signals import liquidity_status_from_row
        row = {"amount_ma20": 50_000_000.0}
        self.assertEqual(liquidity_status_from_row(row), "ok")

    def test_liquidity_unknown_when_no_amount_volume(self) -> None:
        from quantdesk.strategy_signals import liquidity_status_from_row
        row = {}
        self.assertEqual(liquidity_status_from_row(row), "unknown")


class DataConfidenceTests(unittest.TestCase):
    def test_generates_status(self) -> None:
        result = calculate_data_confidence({"latest_close": 10.0, "row_count": 100})
        self.assertIn("data_confidence_status", result)
        self.assertIn(result["data_confidence_status"], ("ok", "warning", "blocked"))

    def test_blocked_when_no_close(self) -> None:
        result = calculate_data_confidence({})
        self.assertEqual(result["data_confidence_status"], "blocked")
        self.assertIn("close", result["missing_required_fields"])

    def test_low_row_count_deducts(self) -> None:
        full = calculate_data_confidence({"latest_close": 10.0, "row_count": 100})
        short = calculate_data_confidence({"latest_close": 10.0, "row_count": 30})
        self.assertGreater(full["data_confidence_score"], short["data_confidence_score"])

    def test_stale_deducts(self) -> None:
        fresh = calculate_data_confidence({"latest_close": 10.0, "row_count": 100, "is_stale": False})
        stale = calculate_data_confidence({"latest_close": 10.0, "row_count": 100, "is_stale": True})
        self.assertGreater(fresh["data_confidence_score"], stale["data_confidence_score"])

    def test_missing_optional_not_blocked(self) -> None:
        result = calculate_data_confidence({
            "latest_close": 10.0, "row_count": 100,
            "has_amount_field": False, "has_volume_field": False,
        })
        # Should be warning, not blocked (close is present)
        self.assertIn(result["data_confidence_status"], ("ok", "warning"))


class PaperTradingDefaultsTests(unittest.TestCase):
    def test_live_mode_does_not_default_2025(self) -> None:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "run_paper_trading_simulator.py"), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertIn("--lookback-days", result.stdout)
        # The default for start-date should be empty, not 2025-01-01
        self.assertNotIn("default='2025-01-01'", result.stdout)

    def test_lookback_days_in_help(self) -> None:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "run_paper_trading_simulator.py"), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertIn("--lookback-days", result.stdout)

    def test_ui_start_date_not_hardcoded_2025(self) -> None:
        text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        # The start date input should not have value="2025-01-01"
        self.assertNotIn('value="2025-01-01"', text)


class UIWorkbenchTests(unittest.TestCase):
    def test_index_has_low_price_workbench(self) -> None:
        text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        self.assertTrue("低价学习池" in text or "低价工作台" in text)

    def test_index_shows_file_paths(self) -> None:
        text = (PROJECT_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        self.assertTrue(
            "low_price_learning_symbols" in text
            or "low_price_learning_report" in text
            or "paper_trading_latest" in text,
        )

    def test_app_js_no_auto_run_on_load(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("runPaperTradingSimulation()", text.split("addEventListener")[0])

    def test_app_js_defines_number_format_helpers(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function formatNumber", text)
        self.assertIn("function formatMoney", text)
        self.assertIn("function formatPercent", text)

    def test_app_js_format_number_before_low_price_table(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertLess(text.index("function formatNumber"), text.index("function renderLowPriceReportTable"))

    def test_app_js_format_number_calls_have_definition(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function formatNumber", text)
        self.assertGreater(text.count("formatNumber("), 1)

    def test_low_price_report_missing_amount_uses_dash_safe_helper(self) -> None:
        text = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        helper_start = text.index("function formatMoney")
        helper_end = text.index("let polling", helper_start)
        helper_body = text[helper_start:helper_end]
        self.assertIn('value === ""', helper_body)
        self.assertIn('return "-"', helper_body)
        self.assertIn("Number.isNaN(Number(value))", helper_body)
        self.assertIn("formatMoney(row.amount_ma20)", text)

    def test_version(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION
        self.assertTrue(APP_VERSION, "APP_VERSION must be non-empty")
        self.assertTrue(API_SCHEMA_VERSION, "API_SCHEMA_VERSION must be non-empty")


if __name__ == "__main__":
    unittest.main()


