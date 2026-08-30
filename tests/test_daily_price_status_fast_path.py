from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from quantdesk import daily_price_providers as providers
from quantdesk import daily_price_update_engine as engine


class DailyPriceStatusFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.daily_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _calendar() -> dict[str, object]:
        return {
            "is_open_by_date": {
                "2026-07-01": True,
                "2026-07-02": True,
                "2026-07-03": True,
                "2026-07-04": False,
                "2026-07-05": False,
                "2026-07-06": True,
                "2026-07-07": True,
                "2026-07-08": True,
            }
        }

    def test_status_reader_uses_bounded_columns_and_keeps_valid_dates(self) -> None:
        path = self.daily_dir / "000001.csv"
        path.write_text(
            "\n".join(
                [
                    "date,symbol,open,high,low,close,volume,amount,change,pct_chg,adjust,source,updated_at",
                    "2026-07-01,000001,9.8,10.1,9.7,10.0,1000,10000,0.1,1.0,raw,test,2026-07-01T15:01:00",
                    "2026-07-02,000001,10.0,10.3,9.9,10.2,1100,11220,0.2,2.0,raw,test,2026-07-02T15:01:00",
                    "2026-07-08,000001,10.2,10.4,10.1,10.3,900,9270,0.1,0.98,raw,test,2026-07-08T15:01:00",
                ]
            )
            + "\n",
            encoding="utf-8-sig",
        )
        calls: list[dict[str, object]] = []
        real_read = providers.read_csv_frame

        def recording_read(csv_path: Path, **kwargs: object):
            calls.append(dict(kwargs))
            return real_read(csv_path, **kwargs)

        with patch.object(providers, "read_csv_frame", side_effect=recording_read):
            frame, warnings = providers.read_local_daily_status_dates_with_warnings(
                "000001",
                daily_dir=self.daily_dir,
            )

        self.assertEqual(frame["date"].tolist(), ["2026-07-01", "2026-07-02", "2026-07-08"])
        self.assertEqual(frame["volume"].tolist(), [1000, 1100, 900])
        self.assertEqual(frame["adjust"].tolist(), ["raw", "raw", "raw"])
        self.assertEqual(warnings, [])
        self.assertTrue(calls)
        self.assertTrue(all(callable(call.get("usecols")) for call in calls))

    def test_local_status_fast_path_preserves_gap_and_corrupt_cache_diagnostics(self) -> None:
        (self.daily_dir / "000001.csv").write_text(
            "date,close,open,high,low,volume,amount\n"
            "2026-07-01,10.0,9.8,10.1,9.7,1000,10000\n"
            "2026-07-02,10.2,10.0,10.3,9.9,1100,11220\n"
            "2026-07-08,10.3,10.2,10.4,10.1,900,9270\n",
            encoding="utf-8-sig",
        )
        (self.daily_dir / "000002.csv").write_text("", encoding="utf-8-sig")

        with ExitStack() as stack:
            stack.enter_context(patch.object(engine, "DAILY_DIR", self.daily_dir))
            stack.enter_context(patch.object(engine, "load_cn_trading_calendar", return_value=self._calendar()))
            payload = engine.local_daily_status_for_symbols(
                [
                    {"symbol": "000001", "name": "valid"},
                    {"symbol": "000002", "name": "corrupt"},
                ],
                target_date="2026-07-08",
            )

        self.assertEqual(payload["latest_by_symbol"]["000001"], "2026-07-08")
        self.assertEqual(payload["raw_positive_volume_target_symbols"], [])
        self.assertEqual(payload["raw_zero_volume_target_symbols"], [])
        self.assertEqual(
            payload["missing_recent_trade_dates_by_symbol"]["000001"],
            ["2026-07-03", "2026-07-06", "2026-07-07"],
        )
        self.assertIn("000002", payload["missing_symbols"])
        self.assertTrue(any("local_daily_cache_read_failed:000002" in item for item in payload["warnings"]))

    def test_local_status_does_not_load_full_ohlcv_frames(self) -> None:
        (self.daily_dir / "000001.csv").write_text(
            "date,close,open,high,low,volume,amount\n"
            "2026-07-08,10.3,10.2,10.4,10.1,900,9270\n",
            encoding="utf-8-sig",
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(engine, "DAILY_DIR", self.daily_dir))
            stack.enter_context(patch.object(engine, "load_cn_trading_calendar", return_value=self._calendar()))
            stack.enter_context(
                patch.object(
                    engine,
                    "read_local_daily_with_warnings",
                    side_effect=AssertionError("daily status must not normalize full OHLCV frames"),
                    create=True,
                )
            )
            payload = engine.local_daily_status_for_symbols(
                [{"symbol": "000001", "name": "valid"}],
                target_date="2026-07-08",
            )

        self.assertEqual(payload["latest_by_symbol"]["000001"], "2026-07-08")

    def test_local_status_reports_raw_zero_volume_target_symbol(self) -> None:
        (self.daily_dir / "000061.csv").write_text(
            "date,close,volume,adjust\n"
            "2026-07-07,10.0,100,raw\n"
            "2026-07-08,10.0,0,raw\n",
            encoding="utf-8-sig",
        )
        (self.daily_dir / "000062.csv").write_text(
            "date,close,volume,adjust\n"
            "2026-07-08,10.0,0,qfq\n",
            encoding="utf-8-sig",
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(engine, "DAILY_DIR", self.daily_dir))
            stack.enter_context(patch.object(engine, "load_cn_trading_calendar", return_value=self._calendar()))
            payload = engine.local_daily_status_for_symbols(
                [
                    {"symbol": "000061", "name": "raw zero"},
                    {"symbol": "000062", "name": "qfq zero"},
                ],
                target_date="2026-07-08",
            )

        self.assertEqual(payload["raw_zero_volume_target_symbols"], ["000061"])


if __name__ == "__main__":
    unittest.main()
