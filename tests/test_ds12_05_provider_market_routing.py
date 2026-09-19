"""DS12-05 (Section-C1 / Section-C5): offline provider market-routing counterexamples.

Offline only. These pin the fail-closed behaviour around exchange routing so a
code is never silently mapped to a guessed provider prefix:

- Section-C1: Beijing Stock Exchange codes (4xx/8xx/9xx) cannot be consumed as a
  plain identity; they normalize to an explicit ``unsupported`` support status.
- Section-C5: the Sina minute interface must reject codes it cannot reliably
  map. The plain-``9`` bucket (see ``akshare_sina_symbol``) mixes Shanghai
  B-shares (``900xxx``) with Beijing codes, so the only safe assertion is that
  no provider symbol is emitted -- the bucket label itself is not trusted.
"""
from __future__ import annotations

import pytest

from quantdesk.intraday.intraday_providers import akshare_sina_symbol
from quantdesk.provider_failure_classifier import classify_failure
from quantdesk.symbol_market_normalizer import normalize_symbol_market


@pytest.mark.parametrize("symbol", ["bj920010", "920010", "830001", "430047"])
def test_beijing_codes_are_explicitly_unsupported_not_plain_identity(symbol: str) -> None:
    result = normalize_symbol_market(symbol)

    assert result["support_status"] == "unsupported_beijing_exchange"
    assert result["market"] == "bj"
    # The provider prefix is surfaced for inspection only; it is never consumed.
    assert result["provider_symbol"] == f"bj{result['normalized_symbol']}"


@pytest.mark.parametrize("symbol", ["900901", "sh900901", "200001"])
def test_b_share_codes_are_unsupported(symbol: str) -> None:
    result = normalize_symbol_market(symbol)

    assert result["support_status"] == "unsupported_b_share"


def test_beijing_code_failure_is_not_retryable() -> None:
    result = classify_failure("bj920010", "any provider text")

    assert result["final_failure_reason"] == "unsupported_market"
    assert result["retryable"] is False


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
