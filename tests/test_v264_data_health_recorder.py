"""Tests for v2.6.4 data-health recorder split.

QuantDesk v2.6.4-data-health-recorder-split moves the data-health
recording and aggregation helpers from ``scripts/daily_quant_report.py``
into ``quantdesk/data_health_recorder.py``.  These tests lock down that:

- the new module is importable without going through the big report
  script;
- every moved public name is still reachable via the old
  ``scripts.daily_quant_report`` module for backwards compatibility;
- ``reset_data_health`` / ``record_data_health`` /
  ``data_health_summary`` / ``data_health_frame`` behave identically
  whether called via the new module or the old shim (they share the
  same module-level list);
- ``data_health_frame`` keeps the column order defined by
  ``DataFetchStatus``;
- ``classify_data_warning`` and ``data_freshness_status_from_quote``
  continue to return the key branches that already shipped.

No new strategy, factor, indicator, broker connection, or investment
advice is exercised here.  Only stateless helpers and the
module-level ``DATA_HEALTH_ROWS`` list are checked.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, get_type_hints

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.daily_quant_report as dqr  # noqa: E402
from quantdesk import data_health_recorder as dhr  # noqa: E402

EMPTY_MERGED = pd.DataFrame(columns=["日期", "开盘", "收盘"])


def _sample_history(latest_date: str = "2026-05-08", rows: int = 5) -> pd.DataFrame:
    dates = pd.date_range(end=latest_date, periods=rows, freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘": [10.0] * rows,
            "收盘": [10.1] * rows,
            "最高": [10.2] * rows,
            "最低": [9.9] * rows,
            "成交量": [100000] * rows,
            "成交额": [1_000_000] * rows,
        }
    )


class NewModuleImportTests(unittest.TestCase):
    def test_new_module_exposes_all_migrated_names(self) -> None:
        for name in (
            "DATA_HEALTH_ROWS",
            "DATA_HEALTH_COLUMNS",
            "DATA_STALE_WARNING_DAYS",
            "DATA_STALE_ERROR_DAYS",
            "DATA_MIN_HISTORY_ROWS_WARNING",
            "DataFetchStatus",
            "reset_data_health",
            "record_data_health",
            "data_health_frame",
            "data_health_summary",
            "data_health_reason_summary",
            "attach_data_health",
            "data_quality_risk_note",
            "data_risk_flag",
            "expected_latest_trading_date",
            "_health_reference_datetime",
            "data_freshness_status_from_quote",
            "classify_data_warning",
        ):
            self.assertTrue(hasattr(dhr, name), msg=f"missing from new module: {name}")

    def test_constants_match_previous_values(self) -> None:
        self.assertEqual(dhr.DATA_STALE_WARNING_DAYS, 3)
        self.assertEqual(dhr.DATA_STALE_ERROR_DAYS, 10)
        self.assertEqual(dhr.DATA_MIN_HISTORY_ROWS_WARNING, 120)

    def test_private_bool_helper_annotations_resolve(self) -> None:
        hints = get_type_hints(dhr._coerce_bool_value)
        self.assertIs(hints["value"], Any)


class OldEntryPointShimTests(unittest.TestCase):
    def test_old_module_still_exposes_migrated_names(self) -> None:
        for name in (
            "DATA_HEALTH_ROWS",
            "DATA_HEALTH_COLUMNS",
            "DATA_STALE_WARNING_DAYS",
            "DATA_STALE_ERROR_DAYS",
            "DATA_MIN_HISTORY_ROWS_WARNING",
            "DataFetchStatus",
            "reset_data_health",
            "record_data_health",
            "data_health_frame",
            "data_health_summary",
            "data_health_reason_summary",
            "attach_data_health",
            "data_quality_risk_note",
            "data_risk_flag",
            "expected_latest_trading_date",
            "_health_reference_datetime",
            "data_freshness_status_from_quote",
            "classify_data_warning",
        ):
            self.assertTrue(hasattr(dqr, name), msg=f"missing from daily_quant_report shim: {name}")

    def test_shim_shares_module_state_with_new_module(self) -> None:
        # reset via one entry point, mutate via the other; both views must
        # observe the same list.
        dhr.reset_data_health()
        self.assertIs(dqr.DATA_HEALTH_ROWS, dhr.DATA_HEALTH_ROWS)
        self.assertIs(dqr.DataFetchStatus, dhr.DataFetchStatus)
        self.assertIs(dqr.record_data_health, dhr.record_data_health)

    def test_shim_reset_clears_recorder_state(self) -> None:
        dhr.reset_data_health()
        dhr.record_data_health(
            symbol="600000",
            name="shim-reset",
            adjust="qfq",
            effective_source="eastmoney",
            merged=_sample_history(),
            end_date="2026-05-08",
            fresh_fetch_success=True,
            cache_present=False,
            cache_merged=False,
            served_from_cache=False,
            fallback_used=False,
            error_message="",
        )
        self.assertEqual(len(dqr.DATA_HEALTH_ROWS), 1)
        dqr.reset_data_health()
        self.assertEqual(len(dqr.DATA_HEALTH_ROWS), 0)
        self.assertEqual(len(dhr.DATA_HEALTH_ROWS), 0)


class DataHealthFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        dhr.reset_data_health()

    def tearDown(self) -> None:
        dhr.reset_data_health()

    def test_empty_frame_keeps_column_order_from_dataclass(self) -> None:
        frame = dhr.data_health_frame()
        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), dhr.DATA_HEALTH_COLUMNS)
        # A few columns that MUST stay for the JSON payload contract.
        for column in (
            "symbol",
            "name",
            "effective_source",
            "fresh_fetch_success",
            "served_from_cache",
            "fallback_used",
            "latest_date",
            "expected_latest_date",
            "stale_days",
            "row_count",
            "warning_level",
            "warning_message",
            "data_grade",
            "data_grade_label",
            "research_allowed",
            "backtest_allowed",
            "fetched_at",
        ):
            self.assertIn(column, dhr.DATA_HEALTH_COLUMNS)

    def test_recorded_frame_matches_columns(self) -> None:
        dhr.record_data_health(
            symbol="600000",
            name="demo",
            adjust="qfq",
            effective_source="eastmoney",
            merged=_sample_history(),
            end_date="2026-05-08",
            fresh_fetch_success=True,
            cache_present=False,
            cache_merged=False,
            served_from_cache=False,
            fallback_used=False,
            error_message="",
        )
        frame = dhr.data_health_frame()
        self.assertEqual(len(frame), 1)
        self.assertEqual(list(frame.columns), dhr.DATA_HEALTH_COLUMNS)

    def test_dqr_and_dhr_see_the_same_recorded_row(self) -> None:
        dhr.record_data_health(
            symbol="000001",
            name="bridge-test",
            adjust="qfq",
            effective_source="sina",
            merged=_sample_history(latest_date="2026-05-07", rows=3),
            end_date="2026-05-08",
            fresh_fetch_success=True,
            cache_present=True,
            cache_merged=True,
            served_from_cache=False,
            fallback_used=True,
            fallback_from="eastmoney",
            fallback_to="sina",
            error_message="",
        )
        frame_from_dhr = dhr.data_health_frame()
        frame_from_dqr = dqr.data_health_frame()
        self.assertEqual(len(frame_from_dhr), 1)
        self.assertEqual(len(frame_from_dqr), 1)
        self.assertEqual(
            frame_from_dhr.iloc[0].to_dict(),
            frame_from_dqr.iloc[0].to_dict(),
        )


class DataHealthSummaryTests(unittest.TestCase):
    def test_empty_summary_has_grade_keys(self) -> None:
        summary = dhr.data_health_summary(pd.DataFrame())
        for key in (
            "total",
            "fresh_fetch_success",
            "ok",
            "info",
            "warning",
            "error",
            "UNAVAILABLE",
            "OBSERVE_ONLY",
            "SCORABLE",
            "RESEARCH_READY",
            "BACKTEST_READY",
        ):
            self.assertIn(key, summary)
            self.assertEqual(summary[key], 0)

    def test_summary_counts_level_and_grade_buckets(self) -> None:
        health = pd.DataFrame(
            [
                {"warning_level": "OK", "data_grade": "BACKTEST_READY", "stale_days": 0, "row_count": 600, "data_freshness_status": "latest_complete_daily_bar"},
                {"warning_level": "WARNING", "data_grade": "OBSERVE_ONLY", "stale_days": 15, "row_count": 30, "data_freshness_status": "stale"},
                {"warning_level": "ERROR", "data_grade": "UNAVAILABLE", "stale_days": 9999, "row_count": 0, "data_freshness_status": "no_data"},
            ]
        )
        summary = dhr.data_health_summary(health)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["warning"], 1)
        self.assertEqual(summary["error"], 1)
        self.assertEqual(summary["BACKTEST_READY"], 1)
        self.assertEqual(summary["OBSERVE_ONLY"], 1)
        self.assertEqual(summary["UNAVAILABLE"], 1)
        self.assertEqual(summary["no_data"], 1)
        # ``stale`` counts every row whose ``stale_days`` exceeds
        # ``DATA_STALE_WARNING_DAYS`` (3); the fixture has one WARNING row
        # at 15 days and one ERROR row at 9999 days, so the count is 2.
        self.assertEqual(summary["stale"], 2)


class DataFreshnessAndWarningTests(unittest.TestCase):
    """Key branches of the freshness and warning classifiers."""

    def test_waiting_for_close_branch(self) -> None:
        status = dhr.data_freshness_status_from_quote(
            "2026-04-30", datetime(2026, 5, 6, 14, 0)
        )
        self.assertEqual(status["status"], "waiting_for_close")

    def test_provider_not_updated_after_close_branch(self) -> None:
        status = dhr.data_freshness_status_from_quote(
            "2026-04-30", datetime(2026, 5, 6, 21, 17)
        )
        self.assertEqual(status["status"], "provider_not_updated_after_close")

    def test_no_data_branch(self) -> None:
        status = dhr.data_freshness_status_from_quote("", "2026-05-08")
        self.assertEqual(status["status"], "no_data")

    def test_classify_error_when_no_rows(self) -> None:
        level, message = dhr.classify_data_warning(
            row_count=0,
            fresh_fetch_success=False,
            served_from_cache=False,
            fallback_used=False,
            stale_days=9999,
            error_message="",
        )
        self.assertEqual(level, "ERROR")
        self.assertIn("没有任何可用日线", message)

    def test_classify_ok_when_latest_complete_daily_bar(self) -> None:
        level, message = dhr.classify_data_warning(
            row_count=10,
            fresh_fetch_success=True,
            served_from_cache=False,
            fallback_used=False,
            stale_days=0,
            error_message="",
            data_freshness_status="latest_complete_daily_bar",
        )
        self.assertEqual(level, "OK")
        self.assertIn("A 股交易日历", message)

    def test_classify_warning_when_cache_only(self) -> None:
        level, _ = dhr.classify_data_warning(
            row_count=10,
            fresh_fetch_success=False,
            served_from_cache=True,
            fallback_used=False,
            stale_days=0,
            error_message="eastmoney down",
            data_freshness_status="latest_complete_daily_bar",
        )
        self.assertEqual(level, "WARNING")

    def test_classify_info_when_fallback_used(self) -> None:
        level, _ = dhr.classify_data_warning(
            row_count=10,
            fresh_fetch_success=True,
            served_from_cache=False,
            fallback_used=True,
            stale_days=0,
            error_message="",
            data_freshness_status="latest_complete_daily_bar",
        )
        self.assertEqual(level, "INFO")

    def test_classify_error_when_very_stale(self) -> None:
        level, _ = dhr.classify_data_warning(
            row_count=10,
            fresh_fetch_success=False,
            served_from_cache=False,
            fallback_used=False,
            stale_days=30,
            error_message="",
            data_freshness_status="stale",
        )
        self.assertEqual(level, "ERROR")


class DataQualityRiskNoteTests(unittest.TestCase):
    def test_empty_summary_returns_empty_note(self) -> None:
        self.assertEqual(dhr.data_quality_risk_note({"warning": 0, "error": 0}), "")

    def test_warning_summary_returns_disclaimer_note(self) -> None:
        note = dhr.data_quality_risk_note({"warning": 1, "error": 0})
        self.assertIn("数据质量警告", note)

    def test_error_summary_returns_disclaimer_note(self) -> None:
        note = dhr.data_quality_risk_note({"warning": 0, "error": 2})
        self.assertIn("数据质量警告", note)


class AttachDataHealthTests(unittest.TestCase):
    def test_empty_health_fills_safe_defaults(self) -> None:
        features = pd.DataFrame({"code": ["600000", "000001"]})
        merged = dhr.attach_data_health(features, pd.DataFrame())
        self.assertEqual(list(merged["data_warning_level"]), ["OK", "OK"])
        self.assertEqual(list(merged["data_grade"]), ["SCORABLE", "SCORABLE"])
        self.assertEqual(list(merged["data_risk_flag"]), ["", ""])
        self.assertNotIn("code_key", merged.columns)

    def test_attach_merges_health_by_code(self) -> None:
        features = pd.DataFrame({"code": ["600000"]})
        health = pd.DataFrame(
            [
                {
                    "symbol": "600000",
                    "warning_level": "WARNING",
                    "data_grade": "OBSERVE_ONLY",
                    "data_grade_label": "仅供观察",
                    "data_grade_reason": "history_short",
                    "research_allowed": False,
                    "backtest_allowed": False,
                    "effective_source": "eastmoney",
                    "latest_date": "2026-05-08",
                    "expected_latest_date": "2026-05-08",
                    "data_freshness_status": "latest_complete_daily_bar",
                    "expected_reason": "最新日线符合预期",
                    "calendar_note": "calendar ok",
                }
            ]
        )
        merged = dhr.attach_data_health(features, health)
        self.assertEqual(merged.iloc[0]["data_warning_level"], "WARNING")
        self.assertEqual(merged.iloc[0]["data_effective_source"], "eastmoney")
        self.assertEqual(merged.iloc[0]["data_latest_date"], "2026-05-08")
        self.assertFalse(bool(merged.iloc[0]["data_invalid"]))


class ReasonSummaryTests(unittest.TestCase):
    def test_reason_summary_counts_expected_buckets(self) -> None:
        health = pd.DataFrame(
            [
                {"data_freshness_status": "latest_complete_daily_bar", "is_market_holiday": False, "served_from_cache": False, "fallback_used": False, "fresh_fetch_success": True, "warning_level": "OK", "error_message": ""},
                {"data_freshness_status": "using_last_trading_day", "is_market_holiday": True, "served_from_cache": False, "fallback_used": False, "fresh_fetch_success": True, "warning_level": "OK", "error_message": ""},
                {"data_freshness_status": "waiting_for_quote_update", "is_market_holiday": False, "served_from_cache": False, "fallback_used": False, "fresh_fetch_success": True, "warning_level": "INFO", "error_message": ""},
                {"data_freshness_status": "stale", "is_market_holiday": False, "served_from_cache": True, "fallback_used": False, "fresh_fetch_success": False, "warning_level": "WARNING", "error_message": "provider down"},
            ]
        )
        summary = dhr.data_health_reason_summary(health)
        self.assertEqual(summary["latest_complete_daily_bar"], 1)
        self.assertEqual(summary["market_holiday_using_last_trading_day"], 1)
        self.assertEqual(summary["waiting_for_quote_update"], 1)
        self.assertEqual(summary["stale"], 1)
        self.assertEqual(summary["cache_only"], 1)
        self.assertEqual(summary["fetch_failed"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


