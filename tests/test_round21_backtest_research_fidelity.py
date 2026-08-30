"""Round 21 Part C: next_open refusal, official limit metadata, survivorship
front-page disclosure, DualWriter fail-closed, feature as-of truncation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd


def _history_with_missing_open(missing_open_on_execution_day: bool = True) -> pd.DataFrame:
    rows = []
    price = 10.0
    for day in range(1, 41):
        price += 0.05
        rows.append(
            {
                "date": f"2026-03-{day:02d}" if day <= 31 else f"2026-04-{day - 31:02d}",
                "open": price - 0.02,
                "high": price + 0.1,
                "low": price - 0.1,
                "close": price,
                "volume": 1_000_000,
                "amount": price * 1_000_000,
            }
        )
    # Force an entry signal near the end: big close above MA20 with high activity.
    rows[-2]["close"] = price * 1.2
    rows[-2]["amount"] = price * 6_000_000
    rows[-2]["volume"] = 6_000_000
    if missing_open_on_execution_day:
        rows[-1]["open"] = float("nan")
    return pd.DataFrame(rows)


class NextOpenRefusalTests(unittest.TestCase):
    def test_missing_next_open_refuses_fill_by_default(self) -> None:
        from quantdesk.backtest import BacktestConfig, requested_execution_price

        row = pd.Series({"open": float("nan"), "close": 10.0})
        price, fallback = requested_execution_price(row, BacktestConfig())
        self.assertFalse(fallback)
        self.assertNotEqual(price, price)  # NaN

    def test_close_fallback_requires_research_switch(self) -> None:
        from quantdesk.backtest import BacktestConfig, requested_execution_price

        row = pd.Series({"open": float("nan"), "close": 10.0})
        config = BacktestConfig(allow_close_fallback_research=True)
        price, fallback = requested_execution_price(row, config)
        self.assertEqual(price, 10.0)
        self.assertTrue(fallback)

    def test_blocked_reason_counts_track_no_fill_price_unavailable(self) -> None:
        from quantdesk.backtest import blocked_reason_counts

        counts = blocked_reason_counts(
            [{"blocked": True, "blocked_reason": "no_fill_price_unavailable"}]
        )
        self.assertEqual(counts["no_fill_price_unavailable"], 1)

    def test_assumptions_disclose_fallback_policy(self) -> None:
        from quantdesk.backtest import BacktestConfig
        from quantdesk.backtest_assumptions import backtest_assumptions_from_config

        default = backtest_assumptions_from_config(BacktestConfig())
        self.assertFalse(default["allow_close_fallback_research"])
        self.assertIn("no_fill_price_unavailable", str(default["execution_price_fallback"]))
        research = backtest_assumptions_from_config(
            BacktestConfig(allow_close_fallback_research=True), price_fallback_used=True
        )
        self.assertTrue(research["allow_close_fallback_research"])
        self.assertIn("research-only", str(research["execution_price_fallback"]))


class OfficialLimitMetadataTests(unittest.TestCase):
    def _metadata_dir(self, tmp: Path) -> Path:
        (tmp / "limit_price_history.csv").write_text(
            "trade_date,symbol,pre_close,up_limit,down_limit,source,provider\n"
            "2026-03-30,600000,10.0,11.0,9.0,unit,unit\n",
            encoding="utf-8",
        )
        (tmp / "suspension_history.csv").write_text(
            "trade_date,symbol,suspend_timing,suspend_type,source,provider\n"
            "2026-03-31,600000,,S,unit,unit\n",
            encoding="utf-8",
        )
        return tmp

    def test_loader_filters_symbols_and_flags_missing_files(self) -> None:
        from quantdesk.research.official_limit_metadata import load_official_limit_metadata

        with tempfile.TemporaryDirectory() as tmp:
            meta = load_official_limit_metadata(["600000"], metadata_dir=self._metadata_dir(Path(tmp)))
        self.assertEqual(meta.limit_prices[("600000", "2026-03-30")], (11.0, 9.0))
        self.assertIn(("600000", "2026-03-31"), meta.suspensions)
        with tempfile.TemporaryDirectory() as tmp:
            empty = load_official_limit_metadata(["600000"], metadata_dir=Path(tmp))
        self.assertTrue(any("official_limit_price_file_missing" in w for w in empty.warnings))

    def test_attach_marks_official_and_heuristic_rows(self) -> None:
        from quantdesk.research.official_limit_metadata import (
            LIMIT_SOURCE_HEURISTIC,
            LIMIT_SOURCE_OFFICIAL,
            attach_official_limit_metadata,
            load_official_limit_metadata,
        )

        df = pd.DataFrame(
            {"date": ["2026-03-30", "2026-03-31"], "open": [11.0, 10.0], "close": [11.0, 10.0]}
        )
        with tempfile.TemporaryDirectory() as tmp:
            meta = load_official_limit_metadata(["600000"], metadata_dir=self._metadata_dir(Path(tmp)))
        out = attach_official_limit_metadata(df, "600000", meta)
        self.assertEqual(list(out["limit_price_source"]), [LIMIT_SOURCE_OFFICIAL, LIMIT_SOURCE_HEURISTIC])
        self.assertEqual(out.loc[0, "official_up_limit"], 11.0)
        self.assertTrue(bool(out.loc[1, "official_suspended"]))

    def test_official_limit_price_blocks_buy_at_limit(self) -> None:
        from quantdesk.backtest import BacktestConfig, limit_up_blocked_for_execution

        config = BacktestConfig()
        row = pd.Series({"open": 11.0, "close": 11.0, "official_up_limit": 11.0, "official_down_limit": 9.0})
        # Heuristic alone would NOT block (+2% move), the official table does.
        self.assertTrue(limit_up_blocked_for_execution(row, 2.0, 10.78, "600000", config))
        row_free = pd.Series({"open": 10.5, "close": 10.5, "official_up_limit": 11.0, "official_down_limit": 9.0})
        self.assertFalse(limit_up_blocked_for_execution(row_free, 2.0, 10.29, "600000", config))

    def test_official_suspension_flag_blocks_trade(self) -> None:
        from quantdesk.backtest import is_trade_suspended

        row = pd.Series({"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100, "official_suspended": True})
        self.assertTrue(is_trade_suspended(row))
        row_ok = pd.Series({"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100, "official_suspended": False})
        self.assertFalse(is_trade_suspended(row_ok))


class SurvivorshipDisclosureTests(unittest.TestCase):
    def test_backtest_audit_front_shows_survivorship_high_without_pit(self) -> None:
        from quantdesk.backtest import BacktestConfig
        from quantdesk.backtest_assumptions import backtest_assumptions_from_config
        from quantdesk.reporting import BacktestMetrics, build_backtest_audit_markdown

        assumptions = backtest_assumptions_from_config(BacktestConfig())
        metrics = BacktestMetrics(
            annual_return=0.0,
            total_return=0.0,
            max_drawdown=0.0,
            sharpe=0.0,
            win_rate=0.0,
            days=0,
        )
        text = build_backtest_audit_markdown(
            metrics, assumptions, None, backtest_warning="", backtest_mode="single"
        )
        header = text[:600]
        self.assertIn("survivorship_bias=high", header)
        self.assertIn("pit_universe=false", header)

    def test_pit_degraded_listings_surface_not_official_listing_date(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "quantdesk" / "research" / "point_in_time_universe.py").read_text(encoding="utf-8")
        self.assertIn("list_date_is_first_available_local_price_date_not_official_listing_date", text)


class FeatureAsOfTests(unittest.TestCase):
    def test_as_of_truncates_future_rows(self) -> None:
        from quantdesk.daily_feature_engine import build_daily_features

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "daily"
            daily.mkdir()
            rows = ["date,open,high,low,close,volume,amount,adjust"]
            for day in range(1, 11):
                price = 10 + day * 0.1
                rows.append(f"2026-03-{day:02d},{price - 0.02},{price + 0.1},{price - 0.1},{price},1000,10000,raw")
            (daily / "600000.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            payload = build_daily_features(
                only_low_price=False,
                daily_dir=daily,
                symbols=["600000"],
                output_path=root / "features.csv",
                summary_path=root / "summary.json",
                as_of="2026-03-05",
            )
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["as_of"], "2026-03-05")
            frame = pd.read_csv(root / "features.csv", dtype=str)
        self.assertEqual(frame.loc[0, "latest_date"], "2026-03-05")

    def test_missing_ohlc_stays_na_with_degraded_label(self) -> None:
        from quantdesk.daily_feature_engine import build_daily_features

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "daily"
            daily.mkdir()
            rows = ["date,close,volume,amount,adjust"]
            for day in range(1, 6):
                rows.append(f"2026-03-{day:02d},{10 + day * 0.1},1000,10000,raw")
            (daily / "600000.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            payload = build_daily_features(
                only_low_price=False,
                daily_dir=daily,
                symbols=["600000"],
                output_path=root / "features.csv",
                summary_path=root / "summary.json",
            )
            self.assertTrue(payload["ok"], payload)
            frame = pd.read_csv(root / "features.csv", dtype=str).fillna("")
        row = frame.iloc[0]
        self.assertEqual(row["open"], "")
        self.assertEqual(row["high"], "")
        self.assertEqual(row["low"], "")
        self.assertIn("open", row["feature_degraded_ohlc"])
        # close must NOT be copied into the missing K-line fields.
        self.assertNotEqual(row["open"], row["close"])


if __name__ == "__main__":
    unittest.main()
