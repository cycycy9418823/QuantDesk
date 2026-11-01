"""Retained active contracts; original mixed tests remain in the retirement evidence archive."""


from __future__ import annotations


import pytest


from quantdesk.intraday.intraday_providers import akshare_sina_symbol


@pytest.mark.parametrize("symbol", ["920010", "830001", "430047"])
def test_sina_minute_rejects_beijing_codes(symbol: str) -> None:
    result = akshare_sina_symbol(symbol)

    assert result["supported"] is False
    assert result["symbol"] == ""
    assert result["reason"]


def test_sina_minute_never_guesses_for_b_share_code() -> None:
    # 900901 shares the plain-9 bucket with Beijing codes; the interface must
    # stay fail-closed and must not emit a provider symbol.
    result = akshare_sina_symbol("900901")

    assert result["supported"] is False
    assert result["symbol"] == ""


def test_sina_minute_still_maps_supported_main_board_codes() -> None:
    assert akshare_sina_symbol("600000")["symbol"] == "sh600000"
    assert akshare_sina_symbol("000001")["symbol"] == "sz000001"
