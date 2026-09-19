"""Tests for v2.7.3 low-price workbench schema and defaults."""

from __future__ import annotations

import re
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

    def test_active_template_start_date_not_hardcoded_2025(self) -> None:
        """A backtest start date must not ship frozen to a single year.

        Re-anchored from the retired app/index.html onto the active template, which
        does carry date inputs (verified: two type="date" inputs present), so the
        assertion still has a real subject.
        """
        text = (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('type="date"', text, "active template has no date input to guard")
        self.assertNotIn('value="2025-01-01"', text)
        self.assertNotIn('value="2026-01-01"', text)


class UIWorkbenchTests(unittest.TestCase):
    """Re-anchored onto the ACTIVE 8000 desk.

    The retired app/index.html and app/static/app.js are gone. Each method below
    keeps the property it protected and points it at the subject the product
    actually serves. Two former methods in this class had no active subject and
    were retired instead of being propped up with a weaker assertion:
    the 低价学习池 / 低价工作台 heading (zero 低价 hits anywhere on the active desk).
    """

    @staticmethod
    def _bundle() -> str:
        return (PROJECT_ROOT / "app" / "static" / "js" / "daily_research.js").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _template() -> str:
        return (PROJECT_ROOT / "app" / "templates" / "daily_research.html").read_text(
            encoding="utf-8"
        )

    def test_active_desk_names_the_artifact_files_it_reads(self) -> None:
        """A reader must be able to see which file a number came from."""
        combined = self._template() + self._bundle()
        for path in (
            "results/data_health/extended_data_gates_latest.json",
            "state/ai_trader/ledger.json",
            "state/paper_watchlist.json",
        ):
            self.assertIn(path, combined, f"active desk no longer names {path}")

    def test_active_bundle_has_no_auto_run_before_user_interaction(self) -> None:
        """Nothing that touches the paper account may fire on page load.

        The retired bundle was guarded against runPaperTradingSimulation() firing
        before the first addEventListener. The active bundle must satisfy the same
        rule for every paper-related entry point, not just one name.
        """
        text = self._bundle()
        self.assertIn("addEventListener", text, "no event wiring found to split on")
        preamble = text.split("addEventListener")[0]
        offenders = re.findall(r"\b(\w*[Pp]aper\w*)\s*\(\s*\)", preamble)
        self.assertEqual(
            [], sorted(set(offenders)),
            f"paper-account code runs before any user interaction: {sorted(set(offenders))}",
        )

    def test_active_bundle_defines_number_format_helpers(self) -> None:
        """Numbers must go through shared formatters, not ad-hoc string building.

        Asserts the formatters the active bundle actually defines. The retired
        bundle's formatMoney / formatPercent do not exist here, so asserting those
        names would be asserting a function the product never had.
        """
        text = self._bundle()
        for name in ("formatNumber", "formatPercentValue", "formatYiAmount",
                     "formatSignedPercent"):
            self.assertIn(f"function {name}", text, f"missing shared formatter {name}")

    def test_active_bundle_renders_missing_values_explicitly(self) -> None:
        """A missing number must read as 数据缺失, never as NaN/undefined/blank.

        The retired bundle expressed this as formatMoney returning "-". The active
        bundle centralises it in safeText()/hasValue(); this asserts that mechanism
        exists and is actually used, which is the property that matters.
        """
        text = self._bundle()
        self.assertIn("function safeText", text)
        self.assertIn("数据缺失", text)
        self.assertIn("hasValue(", text)
        # the helper must be the routine path, not a single decorative call
        self.assertGreater(text.count("safeText("), 50)
        for forbidden in ("undefined", "NaN%"):
            self.assertNotIn(
                f">{forbidden}<", text,
                f"raw {forbidden} is rendered straight into the DOM",
            )

    def test_version(self) -> None:
        from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION
        self.assertTrue(APP_VERSION, "APP_VERSION must be non-empty")
        self.assertTrue(API_SCHEMA_VERSION, "API_SCHEMA_VERSION must be non-empty")


if __name__ == "__main__":
    unittest.main()


