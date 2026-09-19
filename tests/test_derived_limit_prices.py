"""Tests for derived limit prices: rounding, board rules, ST clamping, validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantdesk.data_truth.derived_limit_prices import (
    STInterval,
    board_ratio,
    clamp_open_only_stale,
    derive_limits,
    is_st_on,
    load_current_st_symbols,
    round_half_up,
)


def test_round_half_up():
    assert round_half_up(7.821) == 7.82
    assert round_half_up(1.2825) == 1.28
    assert round_half_up(14.175) == 14.18  # NOT banker's rounding (14.17)


def test_board_ratios():
    assert board_ratio("000001", is_st=False) == 0.10
    assert board_ratio("600519", is_st=False) == 0.10
    assert board_ratio("000001", is_st=True) == 0.05
    assert board_ratio("300750", is_st=False) == 0.20
    assert board_ratio("300750", is_st=True) == 0.20  # ChiNext ST stays 20%
    assert board_ratio("688981", is_st=False) == 0.20
    assert board_ratio("920000", is_st=False) == 0.30


def test_derive_limits_sealed_case():
    # 000017 2026-08-24: prev close 7.11 -> up 7.82 (real sealed limit-up)
    up, down = derive_limits(7.11, "000017", is_st=False)
    assert up == 7.82
    assert down == 6.40


def test_st_interval_logic():
    its = [STInterval(start="2020-04-29", end="2021-06-21")]
    assert is_st_on(its, "2020-06-01") is True
    assert is_st_on(its, "2026-08-24") is False
    open_it = [STInterval(start="2026-05-06", end="")]
    assert is_st_on(open_it, "2026-08-31") is True


def test_clamp_open_only_stale():
    st_map = {
        "002214": [STInterval(start="2025-04-29", end="2026-09-01", was_open=True)],
        "000078": [STInterval(start="2026-05-06", end="2026-09-01", was_open=True)],
    }
    fixed = clamp_open_only_stale(
        st_map, current_st_symbols={"000078"}, first_snapshot_date="2026-08-31"
    )
    assert fixed == 1
    assert st_map["002214"][0].end == "2026-08-31"  # clamped
    assert st_map["000078"][0].end == "2026-09-01"  # still ST, untouched


def test_load_current_st_symbols(tmp_path: Path):
    snap = tmp_path / "20260901.csv"
    snap.write_text(
        "symbol,name,close\n000078,ST海王,1.45\n002214,大立科技,14.85\n000017,深中华A,7.82\n",
        encoding="utf-8",
    )
    result = load_current_st_symbols(tmp_path)
    assert result == {"000078"}


def test_sealed_down_limit_counts_as_match():
    # 000545: prev 2.69, sealed at 2.42 = -10% down limit
    up, down = derive_limits(2.69, "000545", is_st=False)
    assert down == 2.42  # sealed bar matched the DOWN limit
    assert up == 2.96
