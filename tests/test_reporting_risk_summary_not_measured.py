from __future__ import annotations

import math
import unittest

from quantdesk import reporting


class ReportingRiskSummaryNotMeasuredTests(unittest.TestCase):
    def test_empty_summary_renders_not_measured_not_zero(self) -> None:
        markdown = reporting.build_backtest_risk_summary_markdown({})
        self.assertIn("not_measured", markdown)
        # A missing risk pass must not be rendered as a clean "0 blocked" result.
        self.assertNotIn("Blocked trades total: 0", markdown)
        self.assertNotIn("Turnover limit triggered: False", markdown)

    def test_none_summary_renders_not_measured(self) -> None:
        markdown = reporting.build_backtest_risk_summary_markdown(None)  # type: ignore[arg-type]
        self.assertIn("not_measured", markdown)

    def test_note_lines_empty_summary_is_not_measured(self) -> None:
        lines = reporting.risk_summary_note_lines({})
        text = " ".join(lines)
        self.assertIn("not_measured", text)
        self.assertNotIn("Blocked trades total: 0", text)

    def test_missing_key_is_not_measured_but_present_zero_is_zero(self) -> None:
        # Genuine measured zero must still render as 0, not not_measured.
        self.assertEqual(reporting.risk_value_or_unknown({"blocked_trades_total": 0}, "blocked_trades_total"), 0)
        # Absent key on a non-empty summary is not_measured.
        self.assertEqual(reporting.risk_value_or_unknown({"other": 1}, "blocked_trades_total"), "not_measured")

    def test_none_and_nan_values_are_not_measured(self) -> None:
        self.assertEqual(reporting.risk_value_or_unknown({"max_industry_weight": None}, "max_industry_weight"), "not_measured")
        self.assertEqual(reporting.risk_value_or_unknown({"max_industry_weight": math.nan}, "max_industry_weight"), "not_measured")

    def test_populated_summary_renders_measured_values(self) -> None:
        summary = {
            "blocked_trades_total": 3,
            "blocked_limit_up": 1,
            "blocked_limit_down": 0,
            "blocked_suspended": 2,
            "blocked_cash_insufficient": 0,
            "blocked_turnover_limit": 0,
            "excluded_by_data_error": 0,
            "included_with_data_warning": 0,
            "included_with_fallback": 0,
            "industry_concentration_warning": "",
            "max_industry_weight": 0.25,
            "max_single_rebalance_turnover": 0.1,
            "turnover_limit_triggered": False,
        }
        markdown = reporting.build_backtest_risk_summary_markdown(summary)
        self.assertIn("Blocked trades total: 3", markdown)
        self.assertIn("Turnover limit triggered: False", markdown)
        self.assertNotIn("not_measured", markdown)


if __name__ == "__main__":
    unittest.main()
