from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeMootdxClient:
    def quotes(self, symbols):
        return pd.DataFrame([
            {
                "code": "600744",
                "name": "华银电力",
                "price": 8.45,
                "last_close": 8.30,
                "open": 8.32,
                "high": 8.60,
                "low": 8.20,
                "volume": 12345,
                "amount": 9876.5,
                "datetime": "2026-05-19 10:23:00",
            }
        ])

    def bars(self, *args, **kwargs):
        return pd.DataFrame([
            {"datetime": "2026-05-15", "open": 8.20, "high": 8.50, "low": 8.10, "close": 8.40, "vol": 1000, "amount": 8400},
            {"datetime": "2026-05-18", "open": 8.40, "high": 8.60, "low": 8.30, "close": 8.45, "vol": 1200, "amount": 10000},
        ])

    def minutes(self, *args, **kwargs):
        return pd.DataFrame([
            {"datetime": "2026-05-19 09:31:00", "open": 8.40, "high": 8.45, "low": 8.39, "close": 8.44, "volume": 100, "amount": 844},
            {"datetime": "2026-05-19 09:32:00", "open": 8.44, "high": 8.48, "low": 8.43, "close": 8.46, "volume": 120, "amount": 1015},
        ])


class RealMootdxProviderAndFallbackTests(unittest.TestCase):
    def test_mootdx_daily_requests_nonempty_page_and_converts_lots_to_shares(self) -> None:
        from quantdesk.providers import mootdx_provider

        calls: list[dict[str, object]] = []

        class RecordingClient:
            def bars(self, *args, **kwargs):
                calls.append(dict(kwargs))
                frame = pd.DataFrame([
                    {
                        "datetime": "2026-07-15 15:00:00",
                        "open": 7.54,
                        "high": 7.72,
                        "low": 7.50,
                        "close": 7.70,
                        "vol": 136651.0,
                        "volume": 136651.0,
                        "amount": 104599864.0,
                    }
                ])
                frame.index = pd.Index([pd.Timestamp("2026-07-15 15:00:00")], name="datetime")
                return frame

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=RecordingClient()):
                payload = mootdx_provider.fetch_daily_bars(
                    "600004", "2026-07-15", "2026-07-15", network=True
                )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(calls, [{"symbol": "600004", "frequency": 9, "start": 0, "offset": 800}])
        self.assertEqual(payload["adjust"], "raw")
        self.assertEqual(payload["data"][0]["symbol"], "600004")
        self.assertEqual(payload["data"][0]["volume"], 13665100.0)

    def test_mootdx_historical_intraday_uses_full_ohlcv_bars_page(self) -> None:
        from quantdesk.providers import mootdx_provider

        calls: list[dict[str, object]] = []

        class RecordingClient:
            def bars(self, *args, **kwargs):
                calls.append(dict(kwargs))
                frame = pd.DataFrame([
                    {
                        "datetime": "2026-07-15 09:31:00",
                        "open": 7.54,
                        "high": 7.55,
                        "low": 7.53,
                        "close": 7.54,
                        "vol": 100.0,
                        "volume": 100.0,
                        "amount": 754.0,
                    },
                    {
                        "datetime": "2026-07-15 09:32:00",
                        "open": 7.54,
                        "high": 7.56,
                        "low": 7.54,
                        "close": 7.55,
                        "vol": 120.0,
                        "volume": 120.0,
                        "amount": 906.0,
                    },
                ])
                frame.index = pd.Index(pd.to_datetime(frame["datetime"]), name="datetime")
                return frame

            def minutes(self, *args, **kwargs):
                raise AssertionError("price-only minutes must not precede full OHLCV bars")

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=RecordingClient()):
                payload = mootdx_provider.fetch_intraday_bars(
                    "600004", "2026-07-15", network=True
                )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(calls, [{"symbol": "600004", "frequency": 8, "start": 0, "offset": 800}])
        self.assertEqual(payload["data"][0]["open"], 7.54)
        self.assertEqual(payload["data"][0]["volume"], 100.0)

    def test_mootdx_intraday_ignores_redundant_calendar_components(self) -> None:
        from quantdesk.providers import mootdx_provider

        class RealShapeClient:
            def bars(self, *args, **kwargs):
                frame = pd.DataFrame([
                    {
                        "open": 11.20,
                        "close": 11.21,
                        "high": 11.22,
                        "low": 11.19,
                        "vol": 100.0,
                        "amount": 1121.0,
                        "year": 2026,
                        "month": 7,
                        "day": 15,
                        "hour": 9,
                        "minute": 31,
                        "datetime": "2026-07-15 09:31",
                        "volume": 100.0,
                    },
                    {
                        "open": 11.21,
                        "close": 11.22,
                        "high": 11.23,
                        "low": 11.20,
                        "vol": 120.0,
                        "amount": 1346.4,
                        "year": 2026,
                        "month": 7,
                        "day": 15,
                        "hour": 9,
                        "minute": 32,
                        "datetime": "2026-07-15 09:32",
                        "volume": 120.0,
                    },
                ])
                frame.index = pd.DatetimeIndex(
                    pd.to_datetime(frame["datetime"]), name="datetime"
                )
                return frame

            def minutes(self, *args, **kwargs):
                raise AssertionError("full OHLCV bars must not fall back to price-only minutes")

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=RealShapeClient()):
                payload = mootdx_provider.fetch_intraday_bars(
                    "002115", "2026-07-15", network=True
                )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data_rows"], 2)
        self.assertEqual(payload["data"][0]["datetime"], "2026-07-15 09:31:00")

    def test_mootdx_bse_intraday_uses_explicit_market_two(self) -> None:
        from quantdesk.providers import mootdx_provider

        calls: list[tuple[int, int, str, int, int]] = []

        class RawApi:
            def get_security_bars(self, frequency, market, code, start, count):
                calls.append((frequency, market, code, start, count))
                return [
                    {
                        "open": 9.10,
                        "close": 9.12,
                        "high": 9.13,
                        "low": 9.09,
                        "vol": 100.0,
                        "amount": 912.0,
                        "year": 2026,
                        "month": 7,
                        "day": 15,
                        "hour": 9,
                        "minute": 31,
                        "datetime": "2026-07-15 09:31",
                    },
                    {
                        "open": 9.12,
                        "close": 9.11,
                        "high": 9.13,
                        "low": 9.10,
                        "vol": 120.0,
                        "amount": 1093.2,
                        "year": 2026,
                        "month": 7,
                        "day": 15,
                        "hour": 9,
                        "minute": 32,
                        "datetime": "2026-07-15 09:32",
                    },
                ]

        class BseClient:
            def __init__(self) -> None:
                self.client = RawApi()

            def bars(self, *args, **kwargs):
                raise AssertionError("BSE must not use mootdx automatic market inference")

            def minutes(self, *args, **kwargs):
                raise AssertionError("full OHLCV BSE bars must not fall back to minutes")

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=BseClient()):
                payload = mootdx_provider.fetch_intraday_bars(
                    "920266", "2026-07-15", network=True
                )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(calls, [(8, 2, "920266", 0, 800)])
        self.assertEqual(payload["data_rows"], 2)
        self.assertEqual(payload["data"][0]["symbol"], "920266")

    def test_mootdx_bse_daily_uses_explicit_market_two_and_frequency_nine(self) -> None:
        from quantdesk.providers import mootdx_provider

        calls: list[tuple[int, int, str, int, int]] = []

        class RawApi:
            def get_security_bars(self, frequency, market, code, start, count):
                calls.append((frequency, market, code, start, count))
                return [
                    {
                        "datetime": "2026-07-15",
                        "open": 9.10,
                        "close": 9.12,
                        "high": 9.13,
                        "low": 9.09,
                        "vol": 100.0,
                        "amount": 912.0,
                    }
                ]

        class BseClient:
            def __init__(self) -> None:
                self.client = RawApi()

            def bars(self, *args, **kwargs):
                raise AssertionError("BSE daily must use explicit market=2 transport")

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=BseClient()):
                payload = mootdx_provider.fetch_daily_bars(
                    "920145",
                    "2026-07-15",
                    "2026-07-15",
                    network=True,
                )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(calls, [(9, 2, "920145", 0, 800)])
        self.assertEqual(payload["data_rows"], 1)
        self.assertEqual(payload["data"][0]["adjust"], "raw")

    def test_mootdx_intraday_pages_back_to_historical_target(self) -> None:
        from quantdesk.providers import mootdx_provider

        starts: list[int] = []

        class PagingClient:
            def bars(self, *args, **kwargs):
                start = int(kwargs["start"])
                starts.append(start)
                day = "2026-07-13" if start == 0 else "2026-07-10"
                return pd.DataFrame([
                    {
                        "datetime": f"{day} 09:31:00",
                        "open": 7.5,
                        "high": 7.6,
                        "low": 7.4,
                        "close": 7.55,
                        "volume": 100.0,
                        "amount": 755.0,
                    },
                    {
                        "datetime": f"{day} 09:32:00",
                        "open": 7.55,
                        "high": 7.6,
                        "low": 7.5,
                        "close": 7.58,
                        "volume": 120.0,
                        "amount": 909.6,
                    },
                ])

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=PagingClient()):
                payload = mootdx_provider.fetch_intraday_bars(
                    "600004", "2026-07-10", network=True
                )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(starts, [0, 800])
        self.assertTrue(all(row["date"] == "2026-07-10" for row in payload["data"]))

    def test_canonical_tencent_daily_requires_explicit_raw_response(self) -> None:
        from quantdesk.daily_price_providers import fetch_tencent_daily

        with mock.patch(
            "quantdesk.providers.tencent_provider.fetch_daily_bars",
            return_value={
                "ok": True,
                "status": "ok",
                "effective_adjust": "qfq",
                "data": [
                    {
                        "symbol": "600004",
                        "date": "2026-07-15",
                        "open": 7.54,
                        "high": 7.72,
                        "low": 7.50,
                        "close": 7.70,
                        "volume": 13665100.0,
                        "adjust": "qfq",
                    }
                ],
                "warnings": [],
            },
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "canonical_raw_required"):
                fetch_tencent_daily("600004", "2026-07-15", "2026-07-15", network=True)

        self.assertEqual(fetch.call_args.kwargs["adjust"], "none")

    def test_missing_mootdx_capability_is_not_available(self) -> None:
        from quantdesk.providers import mootdx_provider

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=False):
            payload = mootdx_provider.capability(network=True)
        self.assertEqual(payload["status"], "not_available")
        self.assertIn("mootdx", payload["failed_reason"])

    def test_network_false_never_imports_or_requests(self) -> None:
        from quantdesk.providers import mootdx_provider

        with mock.patch.object(mootdx_provider, "_import_quotes_client", side_effect=AssertionError("network called")):
            quote = mootdx_provider.fetch_realtime_quote("600744", network=False)
            daily = mootdx_provider.fetch_daily_bars("600744", network=False)
            intraday = mootdx_provider.fetch_intraday_bars("600744", network=False)
        self.assertEqual(quote["status"], "skipped")
        self.assertEqual(daily["status"], "skipped")
        self.assertEqual(intraday["status"], "skipped")
        self.assertIn("network=false", quote["skipped_reason"])

    def test_mock_mootdx_quote_daily_intraday_success(self) -> None:
        from quantdesk.providers import mootdx_provider

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=FakeMootdxClient()):
                quote = mootdx_provider.fetch_realtime_quote("600744", network=True)
                daily = mootdx_provider.fetch_daily_bars("600744", "2026-05-15", "2026-05-18", network=True)
                intraday = mootdx_provider.fetch_intraday_bars("600744", "2026-05-19", network=True)
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["data"][0]["symbol"], "600744")
        self.assertEqual(quote["data"][0]["price"], 8.45)
        self.assertTrue(daily["ok"], daily)
        self.assertEqual([row["date"] for row in daily["data"]], ["2026-05-15", "2026-05-18"])
        self.assertEqual(daily["data"][0]["open"], 8.20)
        self.assertTrue(intraday["ok"], intraday)
        self.assertEqual(intraday["data"][0]["date"], "2026-05-19")
        self.assertEqual(intraday["data"][0]["close"], 8.44)

    def test_mootdx_intraday_normalizes_tdxpy_zero_volume_sentinel(self) -> None:
        from quantdesk.providers import mootdx_provider

        sentinel = 2.0 ** -127

        class SentinelClient:
            def minutes(self, *args, **kwargs):
                return pd.DataFrame([
                    {
                        "datetime": "2026-05-19 14:59:00",
                        "open": 8.44,
                        "high": 8.44,
                        "low": 8.44,
                        "close": 8.44,
                        "volume": sentinel,
                        "amount": sentinel,
                    },
                    {
                        "datetime": "2026-05-19 15:00:00",
                        "open": 8.45,
                        "high": 8.45,
                        "low": 8.45,
                        "close": 8.45,
                        "volume": 100.0,
                        "amount": 845.0,
                    },
                ])

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", return_value=SentinelClient()):
                payload = mootdx_provider.fetch_intraday_bars(
                    "600744", "2026-05-19", network=True
                )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"][0]["volume"], 0.0)
        self.assertEqual(payload["data"][0]["amount"], 0.0)
        self.assertTrue(
            any("mootdx_zero_decoder_sentinel_normalized" in item for item in payload["warnings"]),
            payload,
        )

    def test_mootdx_exception_returns_failed_reason(self) -> None:
        from quantdesk.providers import mootdx_provider

        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(mootdx_provider, "_import_quotes_client", side_effect=RuntimeError("server unavailable")):
                payload = mootdx_provider.fetch_realtime_quote("600744", network=True)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("server unavailable", payload["failed_reason"])

    def test_adapter_falls_back_to_tencent_after_mootdx_failed(self) -> None:
        from quantdesk import a_stock_data_adapter

        with mock.patch.object(a_stock_data_adapter, "read_local_daily_with_warnings", return_value=(pd.DataFrame(), [])):
            with mock.patch("quantdesk.providers.mootdx_provider.fetch_daily_bars", return_value={
                "ok": False,
                "status": "failed",
                "provider": "mootdx_daily",
                "failed_reason": "mootdx down",
                "data": [],
            }):
                with mock.patch("quantdesk.providers.tencent_provider.fetch_daily_bars", return_value={
                    "ok": True,
                    "status": "ok",
                    "provider": "tencent_daily",
                    "data": [{"symbol": "600744", "date": "2026-05-15", "open": 8.4, "high": 8.6, "low": 8.3, "close": 8.45}],
                    "warnings": [],
                    "errors": [],
                }):
                    payload = a_stock_data_adapter.get_daily_bars("600744", "2026-05-15", "2026-05-15", network=True)
        self.assertTrue(payload["ok"], payload)
        providers = [row["provider"] for row in payload["provider_chain"]]
        self.assertIn("mootdx_daily", providers)
        self.assertIn("tencent_daily", providers)

    def test_daily_and_intraday_provider_facades_are_no_longer_not_implemented(self) -> None:
        from quantdesk import daily_price_providers
        from quantdesk.intraday import intraday_providers

        self.assertNotIn("mootdx_daily adapter is not implemented yet", Path(daily_price_providers.__file__).read_text(encoding="utf-8"))
        self.assertNotIn("mootdx_intraday adapter is not implemented yet", Path(intraday_providers.__file__).read_text(encoding="utf-8"))

    def test_daily_price_provider_mootdx_uses_provider_payload(self) -> None:
        from quantdesk.daily_price_providers import fetch_mootdx_daily

        with mock.patch("quantdesk.providers.mootdx_provider.fetch_daily_bars", return_value={
            "ok": True,
            "status": "ok",
            "data": [{"symbol": "600744", "date": "2026-05-15", "open": 8.4, "high": 8.6, "low": 8.3, "close": 8.45}],
            "warnings": [],
        }):
            frame, warnings = fetch_mootdx_daily("600744", "2026-05-15", "2026-05-15", network=True)
        self.assertFalse(frame.empty)
        self.assertEqual(frame.iloc[0]["symbol"], "600744")
        self.assertIsInstance(warnings, list)

    def test_intraday_provider_mootdx_uses_provider_payload(self) -> None:
        from quantdesk.intraday.intraday_providers import fetch_intraday_1m

        with mock.patch("quantdesk.providers.mootdx_provider.fetch_intraday_bars", return_value={
            "ok": True,
            "status": "ok",
            "provider": "mootdx_intraday",
            "data": [
                {"symbol": "600744", "datetime": "2026-05-19 09:31:00", "open": 8.4, "high": 8.5, "low": 8.3, "close": 8.45}
            ],
            "warnings": [],
        }):
            payload = fetch_intraday_1m("mootdx_intraday", "600744", "2026-05-19", network=True)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["rows"], 1)

    def test_provider_smoke_supports_tencent_and_mootdx(self) -> None:
        from quantdesk.provider_network_smoke import run_provider_network_smoke

        payload = run_provider_network_smoke(
            symbols="600744",
            target_date="2026-05-19",
            network=False,
            providers="tencent,mootdx",
            write=False,
        )
        self.assertEqual(payload["providers"], ["tencent", "mootdx"])
        self.assertEqual(payload["summary"]["step_count"], 8)
        self.assertTrue(all(step["status"] == "skipped" for step in payload["steps"]))

    def test_clean_zip_excludes_provider_network_smoke_result(self) -> None:
        text = (PROJECT_ROOT / "export_project.ps1").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("results", text)
        self.assertIn("reports", text)

    def test_002115_symbol_leading_zero_kept(self) -> None:
        from quantdesk.providers.mootdx_provider import to_mootdx_symbol

        self.assertEqual(to_mootdx_symbol("002115")["code"], "002115")
        self.assertEqual(to_mootdx_symbol("002115.SZ")["market"], "SZ")


if __name__ == "__main__":
    unittest.main()
