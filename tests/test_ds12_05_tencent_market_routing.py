"""DS12-05 Section-C5: Tencent market routing and the 900/920 boundary.

Offline only (no network; only the symbol-mapping layer is exercised).

Pins the explicit-market path and the 900/920 boundary:

- explicit ``sh``/``sz``/``bj`` prefixes and ``.SH``/``.SZ``/``.BJ`` suffixes win;
- bare ``900xxx`` (Shanghai B-share) and ``200xxx`` (Shenzhen B-share) are NOT
  labelled BJ, so they are excluded from the Tencent A-share default route;
- bare ``920xxx`` / 4xx / 8xx remain BJ.

Remote support for BJ is not guaranteed; this only fixes local routing.
"""

from __future__ import annotations

import pytest

from quantdesk.providers.tencent_provider import to_tencent_symbol


@pytest.mark.parametrize(
    ("symbol", "market", "tencent_symbol"),
    [
        ("600000", "SH", "sh600000"),
        ("sh600000", "SH", "sh600000"),
        ("600000.SH", "SH", "sh600000"),
        ("000001", "SZ", "sz000001"),
        ("sz000001", "SZ", "sz000001"),
        ("000001.SZ", "SZ", "sz000001"),
        ("920010", "BJ", "bj920010"),
        ("bj920010", "BJ", "bj920010"),
        ("920010.BJ", "BJ", "bj920010"),
        ("430047", "BJ", "bj430047"),
        ("830001", "BJ", "bj830001"),
    ],
)
def test_explicit_and_inferred_markets(symbol: str, market: str, tencent_symbol: str) -> None:
    result = to_tencent_symbol(symbol)

    assert result["market"] == market
    assert result["tencent_symbol"] == tencent_symbol


@pytest.mark.parametrize("symbol", ["900901", "200001"])
def test_bare_b_share_codes_are_not_routed_as_bj(symbol: str) -> None:
    result = to_tencent_symbol(symbol)

    assert result["market"] == "UNSUPPORTED"
    assert result["tencent_symbol"] == ""
    assert result["warnings"]


def test_explicit_suffix_wins_for_b_share_code() -> None:
    # The explicit ``.SH`` is authoritative: 900901.SH is a Shanghai B-share and
    # must not be silently dropped into the BJ bucket.
    result = to_tencent_symbol("900901.SH")

    assert result["market"] == "SH"
    assert result["tencent_symbol"] == "sh900901"


def test_unknown_code_is_unsupported_not_guessed() -> None:
    result = to_tencent_symbol("777777")

    assert result["market"] == "UNSUPPORTED"
    assert result["tencent_symbol"] == ""
    assert result["warnings"]
