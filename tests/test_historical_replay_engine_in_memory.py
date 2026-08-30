from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quantdesk import historical_replay_engine as replay


class HistoricalReplayEngineInMemoryTests(unittest.TestCase):
    def test_get_daily_slice_truncates_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = Path(tmp)
            pd.DataFrame(
                {
                    "date": ["2026-01-01", "2026-01-02", "2026-01-05"],
                    "close": [10.0, 11.0, 12.0],
                    "adjust": ["raw", "raw", "raw"],
                }
            ).to_csv(daily_dir / "600000.csv", index=False, encoding="utf-8-sig")
            frame, warnings = replay.get_daily_slice("600000", "2026-01-02", source_dir=daily_dir)
        self.assertEqual(frame["date"].max().strftime("%Y-%m-%d"), "2026-01-02")
        self.assertTrue(any("截断" in item for item in warnings))

    def test_get_daily_slice_skips_bad_daily_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = Path(tmp)
            (daily_dir / "600000.csv").write_text(
                "\n".join(
                    [
                        "date,open,high,low,close,adjust",
                        "2026-01-02,10,11,9,10.5,raw",
                        "2026-01-03,bad,row,with,too,many,fields,raw",
                    ]
                )
                + "\n",
                encoding="utf-8-sig",
            )

            frame, warnings = replay.get_daily_slice("600000", "2026-01-02", source_dir=daily_dir)

        self.assertEqual(frame["date"].max().strftime("%Y-%m-%d"), "2026-01-02")
        self.assertTrue(any("csv_bad_lines_skipped" in item for item in warnings))

    def test_low_price_universe_skips_bad_rows_with_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            universe = Path(tmp) / "low_price_learning_symbols.csv"
            universe.write_text(
                "\n".join(
                    [
                        "symbol,name,included",
                        "600000,浦发银行,true",
                        "600001,坏行,true,extra",
                    ]
                )
                + "\n",
                encoding="utf-8-sig",
            )
            warnings: list[str] = []

            symbols, names = replay._read_low_price_symbols(universe, csv_read_warnings=warnings)

        self.assertEqual(symbols, ["600000"])
        self.assertEqual(names["600000"], "浦发银行")
        self.assertTrue(any("csv_bad_lines_skipped" in item for item in warnings))

    def test_replay_default_uses_in_memory_cache_not_temp_dir_preparation(self) -> None:
        source = inspect.getsource(replay.run_historical_intraday_replay)
        self.assertIn("_prepare_signal_daily_cache", source)
        self.assertNotIn("_prepare_signal_daily_dir(", source)


if __name__ == "__main__":
    unittest.main()
