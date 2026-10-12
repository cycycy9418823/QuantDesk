"""Batch 1A-A: one shared, dated limit-price function for every paper fill gate.

Charter §2 invariant 2: 涨停不买、跌停不卖、缺数据不成交.  The old gates compared
the change percent with "board limit minus a tolerance" and missed low-priced
stocks (pre-close 1.04 -> limit 1.14 is only +9.615%).  These tests pin the new
rule: limit price = pre-close x (1 +/- ratio) rounded half-up to the cent, ratio
by board and date.
"""

from __future__ import annotations

import pytest

from quantdesk.board_rules import (
    LIMIT_DOWN_BLOCK,
    LIMIT_OUTSIDE_BAND,
    LIMIT_UNVERIFIABLE,
    LIMIT_UP_BLOCK,
    classify_board,
    compute_limit_prices,
    limit_fill_check,
    limit_ratio,
    listing_marker_from_name,
    round_price_half_up,
    st_status_from_name,
)


def test_low_priced_limit_up_is_caught_by_price_not_percent() -> None:
    limits = compute_limit_prices("600001", trade_date="2026-09-21", pre_close=1.04)
    assert (limits.up_limit, limits.down_limit) == (1.14, 0.94)
    # +9.615% would pass the old "pct >= 9.8" / "limit - 0.3pp" gates.
    check = limit_fill_check("600001", "buy", price=1.14, trade_date="2026-09-21", pre_close=1.04)
    assert check["ok"] is False and check["code"] == LIMIT_UP_BLOCK
    sell_low = limit_fill_check("600001", "sell", price=0.94, trade_date="2026-09-21", pre_close=1.04)
    assert sell_low["code"] == LIMIT_DOWN_BLOCK


def test_rounding_is_half_up_to_the_cent_without_binary_float_error() -> None:
    # 2.45 * 1.1 = 2.695 exactly; binary floats give 2.6949999..., a naive floor(x*100+0.5)
    # would publish 2.69.
    assert compute_limit_prices("600001", trade_date="2026-09-21", pre_close=2.45).up_limit == 2.70
    assert round_price_half_up(2.675) == 2.68
    st = compute_limit_prices("600001", trade_date="2026-07-03", pre_close=7.25, st_status="st")
    assert (st.up_limit, st.down_limit) == (7.61, 6.89)


@pytest.mark.parametrize(
    "symbol,day,st,expected",
    [
        ("300001", "2020-08-21", "not_st", 0.10),
        ("300001", "2020-08-24", "not_st", 0.20),
        ("300001", "2020-08-21", "st", 0.05),
        ("300001", "2020-08-24", "st", 0.20),
        ("302132", "2026-09-21", "not_st", 0.20),
        ("688001", "2026-09-21", "st", 0.20),
        ("689009", "2026-09-21", "not_st", 0.20),
        ("920001", "2026-09-21", "not_st", 0.30),
        ("430001", "2026-09-21", "st", 0.30),
        ("600001", "2026-07-03", "st", 0.05),
        ("600001", "2026-07-06", "st", 0.10),
        ("000001", "2026-07-06", "not_st", 0.10),
        ("600001", "2026-07-03", "unknown", 0.05),
        ("600001", "2026-07-06", "unknown", 0.10),
    ],
)
def test_ratio_follows_board_and_date(symbol: str, day: str, st: str, expected: float) -> None:
    ratio, _basis = limit_ratio(symbol, day, st_status=st)
    assert ratio == pytest.approx(expected)


def test_unknown_st_before_rule_change_uses_strict_band_but_no_out_of_band_claim() -> None:
    limits = compute_limit_prices("600001", trade_date="2026-07-03", pre_close=10.0)
    assert limits.st_status == "unknown" and limits.up_limit == 10.5
    assert "st_unknown_strict_5pct" in limits.ratio_basis
    # Buying at +5% is refused (it could be a sealed ST board) ...
    assert limit_fill_check("600001", "buy", price=10.5, trade_date="2026-07-03", pre_close=10.0)["code"] == LIMIT_UP_BLOCK
    # ... but a +6% sell is not called "bad data": the identity is unknown.
    assert limit_fill_check("600001", "sell", price=10.6, trade_date="2026-07-03", pre_close=10.0)["ok"] is True


def test_known_band_rejects_a_price_outside_it() -> None:
    check = limit_fill_check("600001", "sell", price=11.5, trade_date="2026-09-21", pre_close=10.0)
    assert check["ok"] is False and check["code"] == LIMIT_OUTSIDE_BAND


def test_missing_pre_close_reconstructs_from_pct_else_refuses() -> None:
    rebuilt = compute_limit_prices("600001", trade_date="2026-09-21", price=1.14, pct_chg=9.615)
    assert rebuilt.pre_close == 1.04 and rebuilt.up_limit == 1.14
    assert rebuilt.pre_close_basis == "reconstructed_from_price_and_pct_chg"
    missing = limit_fill_check("600001", "buy", price=1.10, trade_date="2026-09-21")
    assert missing["ok"] is False and missing["code"] == LIMIT_UNVERIFIABLE


def test_buy_below_limit_and_sell_above_limit_fill() -> None:
    assert limit_fill_check("600001", "buy", price=10.99, trade_date="2026-09-21", pre_close=10.0)["ok"] is True
    assert limit_fill_check("600001", "sell", price=11.0, trade_date="2026-09-21", pre_close=10.0)["ok"] is True
    assert limit_fill_check("600001", "buy", price=9.0, trade_date="2026-09-21", pre_close=10.0)["ok"] is True


def test_new_listing_period_has_no_price_band() -> None:
    assert listing_marker_from_name("N华虹") == "N"
    assert listing_marker_from_name("C华虹") == "C"
    assert listing_marker_from_name("NEXT") == ""
    check = limit_fill_check("688001", "buy", price=50.0, trade_date="2026-09-21", pre_close=10.0, name="N新股")
    assert check["ok"] is True and check["limits"]["status"] == "no_limit"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("*ST美丽", "st"),
        ("ST国华", "st"),
        ("ＳＴ国华", "st"),
        ("S*ST前锋", "st"),
        ("XD浦发银", "not_st"),
        ("STAR Market", "not_st"),
        ("平安银行", "not_st"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_st_identity_comes_only_from_the_name_prefix(name, expected: str) -> None:
    assert st_status_from_name(name) == expected


def test_name_drives_st_band_when_no_flag_is_given() -> None:
    limits = compute_limit_prices("600001", trade_date="2026-07-03", pre_close=10.0, name="*ST测试")
    assert limits.st_status == "st" and limits.up_limit == 10.5
    after = compute_limit_prices("600001", trade_date="2026-07-06", pre_close=10.0, name="*ST测试")
    assert after.up_limit == 11.0


def test_classify_board_keeps_legacy_default_and_follows_dates_when_given() -> None:
    assert classify_board("600001", is_st=True).limit_up_pct == 5.0
    assert classify_board("600001", is_st=True, trade_date="2026-07-06").limit_up_pct == 10.0
    assert classify_board("300001", trade_date="2020-08-21").limit_up_pct == 10.0
    assert classify_board("300001").limit_up_pct == 20.0
