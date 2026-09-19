"""DS12-05 Section-C1: intraday monitor must consume Beijing quotes correctly.

Offline only (the batch provider is monkeypatched; no network). Covers:

- a mixed ``sh``/``sz``/``bj`` batch,
- a ``bj920010`` quote matching a ``920010`` holding (the previous prefix
  stripping only handled ``sh``/``sz``),
- per-symbol provider-error / missing-quote passthrough instead of silent drops.
"""

from __future__ import annotations

import pytest

import quantdesk.intraday.intraday_monitor as monitor


class _FakeBatch:
    def __init__(self, rows, *, provider_errors=(), warnings=()):
        self.rows = list(rows)
        self.provider_errors = list(provider_errors)
        self.warnings = list(warnings)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sh600000", "600000"),
        ("sz000001", "000001"),
        ("bj920010", "920010"),
        ("920010", "920010"),
        ("SH600000", "600000"),
        ("600000", "600000"),
        ("", ""),
        ("bad", ""),
        # Malformed identities must be rejected, never digit-extracted:
        ("hk920010", ""),
        ("bad920010", ""),
        ("bj1920010", ""),
        ("us600000", ""),
        ("sh60000", ""),
        ("6000000", ""),
    ],
)
def test_quote_symbol_key_strips_any_market_prefix(raw: str, expected: str) -> None:
    assert monitor._quote_symbol_key(raw) == expected


def test_mixed_batch_matches_bj_holding(monkeypatch) -> None:
    portfolio = {"positions": [{"symbol": "920010", "shares": 100}]}
    all_symbols, held, _candidates = monitor._collect_monitor_symbols(portfolio, [])

    assert "920010" in all_symbols
    assert "920010" in held

    rows = [
        {"symbol": "sh600000", "price": 10.0},
        {"symbol": "sz000001", "price": 12.0},
        {"symbol": "bj920010", "price": 5.0},
    ]
    monkeypatch.setattr(
        monitor, "fetch_spot_sina_batch", lambda symbols, batch_size=50: _FakeBatch(rows)
    )

    quotes = monitor._fetch_prices(all_symbols)

    assert set(quotes) == {"600000", "000001", "920010"}
    assert quotes["920010"]["price"] == 5.0


def test_malformed_rows_cannot_overwrite_valid_quote(monkeypatch) -> None:
    rows = [
        {"symbol": "bj920010", "price": 5.0},
        {"symbol": "hk920010", "price": 99.0},
        {"symbol": "bad920010", "price": 99.0},
        {"symbol": "bj1920010", "price": 99.0},
    ]
    monkeypatch.setattr(
        monitor, "fetch_spot_sina_batch", lambda symbols, batch_size=50: _FakeBatch(rows)
    )

    quotes, errors, warnings = monitor._fetch_prices_detailed(["920010"])

    assert quotes["920010"]["price"] == 5.0
    assert any("无法识别报价标识" in item for item in warnings)
    assert not errors


def test_normal_hit_merges_into_live_price(monkeypatch) -> None:
    monkeypatch.setattr(
        monitor,
        "fetch_spot_sina_batch",
        lambda symbols, batch_size=50: _FakeBatch(
            [{"symbol": "sh600000", "price": 10.5, "prev_close": 10.0, "open": 10.1, "high": 10.6, "low": 9.9}]
        ),
    )

    quotes = monitor._fetch_prices(["600000"])
    merged = monitor._merge_live_price({"close": 9.0}, quotes["600000"])

    assert merged["close"] == 10.5
    assert merged["live_price"] == 10.5
    assert merged["previous_close"] == 10.0
    assert merged["change_pct"] == 5.0


def test_provider_errors_and_missing_symbols_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(
        monitor,
        "fetch_spot_sina_batch",
        lambda symbols, batch_size=50: _FakeBatch(
            [{"symbol": "sh600000", "price": 10.0}],
            provider_errors=["sina_batch[0:2]: timeout"],
            warnings=["无法识别 Sina 格式：'bad'"],
        ),
    )

    quotes, errors, warnings = monitor._fetch_prices_detailed(["600000", "920010"])

    assert set(quotes) == {"600000"}
    assert any("sina_provider_error" in item for item in errors)
    assert any("sina_warning" in item for item in warnings)
    assert any("920010" in item for item in warnings)


def test_empty_symbols_returns_empty_without_provider_call(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("provider must not be called for an empty symbol set")

    monkeypatch.setattr(monitor, "fetch_spot_sina_batch", _boom)

    assert monitor._fetch_prices([]) == {}
    assert monitor._fetch_prices_detailed([]) == ({}, [], [])
