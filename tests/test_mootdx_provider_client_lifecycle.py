from __future__ import annotations

from unittest import mock

import pandas as pd

from quantdesk.providers import mootdx_provider


class _SuccessfulClient:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def quotes(self, symbols):
        return pd.DataFrame(
            [
                {
                    "code": "600004",
                    "price": 7.70,
                    "open": 7.54,
                    "high": 7.72,
                    "low": 7.50,
                }
            ]
        )

    def bars(self, *args, **kwargs):
        frequency = int(kwargs.get("frequency", args[1] if len(args) > 1 else 9))
        stamp = "2026-07-30 09:31:00" if frequency == 8 else "2026-07-30"
        return pd.DataFrame(
            [
                {
                    "datetime": stamp,
                    "open": 7.54,
                    "high": 7.72,
                    "low": 7.50,
                    "close": 7.70,
                    "volume": 100.0,
                    "amount": 770.0,
                }
            ]
        )

    def finance(self, *args, **kwargs):
        return pd.DataFrame([{"eps": 0.5}])


class _FailingDisconnectClient:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def quotes(self, *args, **kwargs):
        raise RuntimeError("forced provider failure")

    def bars(self, *args, **kwargs):
        raise RuntimeError("forced provider failure")

    def minutes(self, *args, **kwargs):
        raise RuntimeError("forced provider failure")

    def finance(self, *args, **kwargs):
        raise RuntimeError("forced provider failure")


def _fetch_cases():
    return (
        (
            "quote",
            lambda: mootdx_provider.fetch_realtime_quote("600004", network=True),
        ),
        (
            "daily",
            lambda: mootdx_provider.fetch_daily_bars(
                "600004", "2026-07-30", "2026-07-30", network=True
            ),
        ),
        (
            "intraday",
            lambda: mootdx_provider.fetch_intraday_bars(
                "600004", "2026-07-30", network=True
            ),
        ),
        (
            "fundamentals",
            lambda: mootdx_provider.fetch_fundamentals("600004", network=True),
        ),
    )


def test_every_successful_mootdx_request_closes_its_client() -> None:
    for case_name, fetch in _fetch_cases():
        client = _SuccessfulClient()
        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(
                mootdx_provider, "_import_quotes_client", return_value=client
            ):
                payload = fetch()

        assert payload["ok"], (case_name, payload)
        assert client.close_calls == 1, case_name


def test_every_failed_mootdx_request_disconnects_its_client() -> None:
    for case_name, fetch in _fetch_cases():
        client = _FailingDisconnectClient()
        with mock.patch.object(mootdx_provider, "dependency_available", return_value=True):
            with mock.patch.object(
                mootdx_provider, "_import_quotes_client", return_value=client
            ):
                payload = fetch()

        assert payload["status"] == "failed", (case_name, payload)
        assert "forced provider failure" in payload["failed_reason"]
        assert client.disconnect_calls == 1, case_name
