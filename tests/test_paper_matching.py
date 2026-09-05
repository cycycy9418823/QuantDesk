"""Shared matching library: one policy table, ledger headers must not impersonate."""

from __future__ import annotations

import pytest

from ai_trader.ledger import empty_ledger
from quantdesk.paper.auto_paper_portfolio import _empty_state
from quantdesk.paper.matching import (
    POLICY_DAILY_APPROX,
    POLICY_NEXT_OPEN,
    POLICY_SNAPSHOT,
    declared_fill_policy,
    ensure_declared_fill_policy,
    get_policy,
    next_open_limit_blocked,
    stamp_fill_policy,
)
from ai_trader.trader import _limit_blocked


def test_ai_ledger_header_declares_next_open() -> None:
    ledger = empty_ledger()
    assert ledger["fill_policy"] == POLICY_NEXT_OPEN
    assert ledger["fill_policy_fill_model"] == "next_open_only"
    assert "一字涨停" in str(ledger["fill_policy_label_zh"])


def test_strategy_ledger_header_declares_daily_approx() -> None:
    state = _empty_state(10_000.0)
    assert state["fill_policy"] == POLICY_DAILY_APPROX
    assert state["fill_policy_fill_model"] == "daily_approx"
    assert declared_fill_policy(state) == POLICY_DAILY_APPROX


def test_ledgers_cannot_impersonate_each_other() -> None:
    ai = empty_ledger()
    with pytest.raises(ValueError, match="fill_policy_impersonation"):
        stamp_fill_policy(ai, POLICY_DAILY_APPROX)
    strategy = _empty_state(10_000.0)
    with pytest.raises(ValueError, match="fill_policy_impersonation"):
        stamp_fill_policy(strategy, POLICY_NEXT_OPEN)


def test_unlive_policy_cannot_be_stamped() -> None:
    with pytest.raises(ValueError, match="fill_policy_not_live"):
        stamp_fill_policy({}, POLICY_SNAPSHOT)
    with pytest.raises(ValueError, match="unknown_fill_policy"):
        get_policy("made_up")


def test_next_open_limit_up_blocks_buy_not_sell() -> None:
    assert next_open_limit_blocked("600000", 10.0, "buy")
    assert next_open_limit_blocked("600000", 10.0, "sell") is None
    assert next_open_limit_blocked("600000", -10.0, "sell")
    assert next_open_limit_blocked("600000", -10.0, "buy") is None
    assert next_open_limit_blocked("600000", None, "buy") is None
    # Same rule the AI trader already used; the wrapper must not drift.
    assert _limit_blocked("600000", 10.0, "buy") == next_open_limit_blocked("600000", 10.0, "buy")


def test_ensure_declared_is_idempotent() -> None:
    ledger = {"ledger_id": "ai_trader_100k", "cash": 1}
    ensure_declared_fill_policy(ledger)
    ensure_declared_fill_policy(ledger)
    assert ledger["fill_policy"] == POLICY_NEXT_OPEN
