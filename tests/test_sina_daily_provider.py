from __future__ import annotations

import pandas as pd

from quantdesk import daily_price_providers as providers
from quantdesk.daily_price_update_engine import DEFAULT_PROVIDERS


def test_sina_daily_is_implemented_last_resort_provider() -> None:
    availability = providers.provider_availability("sina_daily")
    assert availability["status"] == "available"
    assert availability["implemented"] is True
    assert DEFAULT_PROVIDERS[-1] == "sina_daily"


def test_sina_daily_normalizes_indexed_raw_frame(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch(symbol: str, start_date: str, end_date: str, adjust: str):
        captured.update(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        return pd.DataFrame(
            {
                "open": [5.0],
                "high": [5.2],
                "low": [4.9],
                "close": [5.1],
                "volume": [1000],
                "amount": [5100],
            },
            index=pd.Index([pd.Timestamp("2026-07-16")], name="date"),
        )

    monkeypatch.setattr("quantdesk.data_providers.fetch_daily_sina", fake_fetch)
    frame, warnings = providers.fetch_daily(
        "sina_daily",
        "600405",
        "2026-07-16",
        "2026-07-16",
        network=True,
    )

    assert captured == {
        "symbol": "sh600405",
        "start_date": "20260716",
        "end_date": "20260716",
        "adjust": "",
    }
    assert len(frame) == 1
    assert frame.iloc[0]["symbol"] == "600405"
    assert frame.iloc[0]["adjust"] == "raw"
    assert frame.iloc[0]["source"] == "sina_daily"
    assert str(frame.iloc[0]["date"])[:10] == "2026-07-16"
    assert not any("missing adjust" in warning for warning in warnings)


def test_sina_daily_network_false_never_calls_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "quantdesk.data_providers.fetch_daily_sina",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    frame, warnings = providers.fetch_daily(
        "sina_daily",
        "600405",
        "2026-07-16",
        "2026-07-16",
        network=False,
    )
    assert frame.empty
    assert warnings and "network=false" in warnings[0]