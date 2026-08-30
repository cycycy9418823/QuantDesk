"""Tests for the unified candidate eligibility gate (Phase 2 选股正确性).

Verifies the single eligibility definition used by selection / ledger /
paper-trading: ST/delist names, one-word limit-up (unbuyable), new listings,
and suspended / zero-volume bars are all excluded.
"""
from __future__ import annotations

from quantdesk.screening import candidate_filters as cf


def test_st_name_is_excluded() -> None:
    res = cf.evaluate_candidate_eligibility({"symbol": "600000", "name": "*ST平安"})
    assert res.eligible is False
    assert any("st" in r.lower() or "ST" in r for r in res.reasons)


def test_delist_name_marker_excluded() -> None:
    assert cf.is_excluded_name("平安退") is True
    assert cf.is_excluded_name("平安银行") is False


def test_one_word_limit_up_is_unbuyable() -> None:
    # limit_up_proxy True marks a one-word board → cannot buy next day.
    row = {"symbol": "300001", "name": "创业板股", "limit_up_proxy": True, "history_days": 200}
    res = cf.evaluate_candidate_eligibility(row)
    assert res.eligible is False
    assert any("limit" in r.lower() for r in res.reasons)


def test_one_word_limit_candidate_helper() -> None:
    assert cf.is_one_word_limit_candidate({"limit_up_proxy": True}) is True
    assert cf.is_one_word_limit_candidate({"limit_up_proxy": False}) is False
    assert cf.is_one_word_limit_candidate(
        {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pre_close": 10.0}
    ) is True
    assert cf.is_one_word_limit_candidate(
        {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "pct_chg": 0.0}
    ) is False
    assert cf.is_one_word_limit_candidate(
        {"open": 10.01, "high": 10.01, "low": 10.01, "close": 10.01, "pct_chg": 0.1}
    ) is False


def test_new_listing_excluded_when_history_known() -> None:
    row = {"symbol": "600000", "name": "平安银行", "history_days": 20}
    res = cf.evaluate_candidate_eligibility(row, min_history_days=60)
    assert res.eligible is False
    assert any("new" in r.lower() or "history" in r.lower() for r in res.reasons)


def test_missing_history_days_does_not_block() -> None:
    # symbol_metrics rows do not carry history_days; absence must NOT be
    # treated as a new listing (would wrongly drop the whole universe).
    row = {"symbol": "600000", "name": "平安银行", "close": 10.0, "avg_volume_20d": 1e6}
    res = cf.evaluate_candidate_eligibility(row)
    assert res.eligible is True, res.reasons


def test_suspended_zero_volume_excluded() -> None:
    row = {"symbol": "600000", "name": "平安银行", "avg_volume_20d": 0.0, "amount_proxy": 0.0, "history_days": 200}
    res = cf.evaluate_candidate_eligibility(row)
    assert res.eligible is False


def test_normal_row_passes() -> None:
    row = {
        "symbol": "600000",
        "name": "平安银行",
        "close": 10.0,
        "avg_volume_20d": 1_000_000.0,
        "amount_proxy": 10_000_000.0,
        "limit_up_proxy": False,
        "history_days": 200,
    }
    res = cf.evaluate_candidate_eligibility(row)
    assert res.eligible is True, res.reasons


def test_filter_candidate_rows_splits_kept_and_excluded() -> None:
    rows = [
        {"symbol": "600000", "name": "平安银行", "close": 10.0, "avg_volume_20d": 1e6,
         "amount_proxy": 1e7, "limit_up_proxy": False, "history_days": 200},
        {"symbol": "600001", "name": "*ST退市", "close": 5.0, "avg_volume_20d": 1e6,
         "amount_proxy": 1e7, "history_days": 200},
        {"symbol": "300001", "name": "一字板", "limit_up_proxy": True, "history_days": 200},
    ]
    kept, excluded = cf.filter_candidate_rows(rows)
    kept_syms = {r["symbol"] for r in kept}
    assert "600000" in kept_syms
    assert "600001" not in kept_syms
    assert "300001" not in kept_syms
    assert len(excluded) == 2
