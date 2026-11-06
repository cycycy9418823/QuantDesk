"""Contracts at the extracted valuation boundary, with injected local evidence."""
from __future__ import annotations

import copy
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper import valuation


def _calendar() -> dict:
    return {"is_open_by_date": {"2026-11-04": True, "2026-11-05": True}}


def _position() -> dict:
    return {"symbol": "600000", "quantity": 100, "entry_price": 10.0,
            "avg_cost": 10.5, "entry_date": "2026-11-04",
            "latest_price": 10.0, "latest_date": "2026-11-04"}


def _forbid(*args, **kwargs):
    raise AssertionError("Unexpected dependency call")


def test_standalone_import_does_not_load_execution_or_publication():
    program = '''
import builtins
import sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in ('quantdesk.paper.auto_paper_portfolio', 'quantdesk.auto_paper_portfolio',
                'quantdesk.paper.publication', 'quantdesk.paper.state_store'):
        raise AssertionError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from quantdesk.paper import valuation
assert valuation.paper_account_scale(10000)[0] == 'legacy_10k'
for forbidden in ('quantdesk.paper.auto_paper_portfolio', 'quantdesk.auto_paper_portfolio',
                  'quantdesk.paper.publication', 'quantdesk.paper.state_store'):
    assert forbidden not in sys.modules, forbidden
'''
    cp = subprocess.run([sys.executable, "-B", "-c", program], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr


def test_mark_keeps_in_place_changes_then_policy_order(monkeypatch):
    position = _position()
    events = []
    quote = {"price": 12.0, "date": "2026-11-05", "price_basis": "raw", "source": "fixture"}
    factors = object()

    def suspension(row, evidence):
        assert row is position and evidence is quote
        assert row["market_value"] == 1200 and row["unrealized_pnl"] == 150
        events.append("suspension")
        return {**row, "from_suspension": True}

    def adjustment(row, **kwargs):
        assert row is not position and row["from_suspension"]
        assert kwargs == {"latest_price": 12.0, "latest_date": "2026-11-05", "factors": factors}
        events.append("adjustment")
        return {**row, "from_adjustment": True}

    monkeypatch.setattr(paper, "_latest_daily_quote", _forbid)
    monkeypatch.setattr(paper, "_holding_days", lambda *_: 23)
    monkeypatch.setattr(paper, "apply_suspension_valuation_policy", suspension)
    monkeypatch.setattr(paper, "apply_paper_adjustment_policy", adjustment)
    result = paper._mark_position(position, 999, "2026-11-05", "fixture", quote=quote, adjustment_factors=factors)
    assert events == ["suspension", "adjustment"]
    assert position["latest_price"] == 12 and position["holding_days"] == 23
    assert "from_suspension" not in position and result["from_adjustment"]


def test_quote_provider_is_resolved_at_each_call(monkeypatch):
    seen = []
    monkeypatch.setattr(paper, "apply_suspension_valuation_policy", lambda row, _: row)
    monkeypatch.setattr(paper, "apply_paper_adjustment_policy", lambda row, **_: row)
    for price in (11, 13):
        def quote(symbol, *, decision_date, value=price):
            seen.append((symbol, decision_date, value))
            return {"price": value, "date": decision_date, "price_basis": "raw"}
        monkeypatch.setattr(paper, "_latest_daily_quote", quote)
        result = paper._mark_position(_position(), 999, "2026-11-05", "fixture")
        assert result["latest_price"] == price
    assert seen == [("600000", "2026-11-05", 11), ("600000", "2026-11-05", 13)]


@pytest.mark.parametrize("quote, expected_status, expected_date", [
    ({"price": 12, "date": "2026-11-04", "price_basis": "raw"}, "stale", "2026-11-04"),
    ({"price": 99, "date": "2026-11-05", "price_basis": "qfq"}, "missing_or_untrusted", "2026-11-04"),
    ({"price": 99, "date": "2026-11-05", "missing": True}, "missing_or_untrusted", "2026-11-04"),
])
def test_untrusted_marks_keep_evidence_labels(monkeypatch, quote, expected_status, expected_date):
    monkeypatch.setattr(paper, "apply_suspension_valuation_policy", lambda row, _: row)
    monkeypatch.setattr(paper, "apply_paper_adjustment_policy", lambda row, **_: row)
    row = paper._mark_position(_position(), 888, "2026-11-05", "fixture", quote=quote)
    assert row["valuation_status"] == expected_status and row["latest_date"] == expected_date
    if expected_status == "missing_or_untrusted":
        assert row["latest_price"] == 10


@pytest.mark.parametrize("kwargs, expected", [
    ({"valuation_date": "2026-11-05", "valuation_valid": False}, "current_valuation_unverified"),
    ({"valuation_date": "bad"}, "valuation_date_invalid"),
    ({"valuation_date": "2026-11-05", "calendar": {}}, "previous_session_unverified"),
])
def test_daily_pnl_early_exits_do_not_read(monkeypatch, kwargs, expected):
    monkeypatch.setattr(paper, "_archive_project_root", _forbid)
    monkeypatch.setattr(paper, "load_a_share_trading_calendar", _forbid)
    monkeypatch.setattr(paper, "read_json_safe", _forbid)
    summary = {"total_equity": 1030, "daily_pnl": 999}
    before = copy.deepcopy(summary)
    result = paper._with_daily_pnl(summary, **kwargs)
    assert result["daily_pnl_status"] == expected and result["daily_pnl"] is None
    assert summary == before


def test_persisted_reference_avoids_history_read(monkeypatch):
    monkeypatch.setattr(paper, "_archive_project_root", _forbid)
    monkeypatch.setattr(paper, "read_json_safe", _forbid)
    previous = {"daily_pnl_reference": {"valuation_date": "2026-11-05",
                "previous_equity_date": "2026-11-04", "previous_total_equity": 1000}}
    result = paper._with_daily_pnl({"total_equity": 1030}, valuation_date="2026-11-05",
                                   previous_state=previous, calendar=_calendar())
    assert result["daily_pnl"] == 30
    assert result["daily_pnl_source"] == "persisted_previous_session_reference"


def test_calendar_and_history_overrides_remain_late_bound(monkeypatch, tmp_path):
    events = []
    for name in ("first", "second"):
        root = tmp_path / name
        history_path = tmp_path / (name + "_history.json")
        monkeypatch.setattr(paper, "_archive_project_root", lambda root=root: root)
        monkeypatch.setattr(paper, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", history_path)

        def calendar(path):
            events.append(("calendar", path))
            return _calendar()

        def history(path, default):
            events.append(("history", path))
            return {"items": [{"valuation_date": "2026-11-04", "cash": 400, "market_value": 600, "total_equity": 1000}]}

        monkeypatch.setattr(paper, "load_a_share_trading_calendar", calendar)
        monkeypatch.setattr(paper, "read_json_safe", history)
        result = paper._with_daily_pnl({"total_equity": 1030}, valuation_date="2026-11-05")
        assert result["daily_pnl"] == 30
        assert events[-2:] == [("calendar", root / "data/calendar/a_share_trading_calendar.csv"), ("history", history_path)]


def test_damaged_latest_history_does_not_revive_older_history(monkeypatch):
    monkeypatch.setattr(paper, "read_json_safe", _forbid)
    rows = [dict(valuation_date="2026-11-04", generated_at="2026-11-04T20:00:00", cash=1000, market_value=0, total_equity=1000),
            dict(valuation_date="2026-11-04", generated_at="2026-11-04T21:00:00", cash=1000, market_value=0, total_equity=9999)]
    result = paper._with_daily_pnl({"total_equity": 1030}, valuation_date="2026-11-05", calendar=_calendar(), history={"items": rows})
    assert result["daily_pnl"] is None
    fallback = paper._with_daily_pnl({"total_equity": 1030}, valuation_date="2026-11-05", calendar=_calendar(), history={"items": rows},
        previous_state={"valuation_date": "2026-11-04", "cash": 700, "market_value": 300})
    assert fallback["daily_pnl"] == 30 and fallback["daily_pnl_source"] == "previous_session_state"


def test_calendar_gap_is_not_skipped():
    result = paper._with_daily_pnl({"total_equity": 1030}, valuation_date="2026-11-05",
        calendar={"is_open_by_date": {"2026-11-05": True, "2026-11-03": True}}, history={"items": []})
    assert result["daily_pnl_status"] == "previous_session_unverified"


def test_summary_uses_injected_accounting_without_repairing_state(monkeypatch):
    state = {"cash": 500, "initial_cash": 1000, "positions": [{"market_value": 600, "unrealized_pnl": 40}],
             "realized_pnl": 7, "unrealized_pnl": 8, "closed_positions": [{"realized_pnl": 10}]}
    before = copy.deepcopy(state)
    seen = []
    monkeypatch.setattr(paper, "paper_account_scale", lambda _: ("injected", "fixture"))
    monkeypatch.setattr(paper, "inspect_realized_adjustment", lambda *a, **k: {"effective_realized_adjustment": 2})
    monkeypatch.setattr(paper, "_paper_correction_fields", lambda _: {"fixture_correction": True})
    monkeypatch.setattr(paper, "strategy_pnl_identity", lambda row: seen.append(row) or {"status": "mismatch"})
    result = paper._summary(state, [{"action": "buy"}, {"action": "stop_loss"}])
    assert seen[0]["realized_pnl"] == 7 and seen[0]["unrealized_pnl"] == 8
    assert result["accounting_status"] == "unverified" and result["account_scale"] == "injected"
    assert result["realized_pnl"] == 12 and result["fixture_correction"]
    assert result["today_new_count"] == result["today_sell_count"] == 1 and state == before


@pytest.mark.parametrize("state, expected", [
    ({"cash": 10, "market_value": 50, "total_equity": 777, "positions": [{"market_value": 20}]}, 777),
    ({"cash": 10, "market_value": 50, "positions": [{"market_value": 20}]}, 30),
    ({"cash": 10, "market_value": 50, "positions": []}, 60),
])
def test_existing_total_equity_precedence_is_distinct_from_summary(state, expected):
    assert paper._total_equity_from_state(state) == expected


@pytest.mark.parametrize("position, decision, expected", [
    ({"entry_date": "2026-10-30"}, "2026-11-02", 3),
    ({"entry_date": "2026-11-06"}, "2026-11-05", 0),
    ({"entry_date": "bad", "holding_days": 12}, "2026-11-05", 12),
])
def test_holding_days_remain_calendar_days(position, decision, expected):
    assert paper._holding_days(position, decision) == expected


def test_holding_days_keeps_datetime_injection(monkeypatch):
    class DateClock:
        @staticmethod
        def fromisoformat(value):
            return datetime(2026, 11, 5) if value == "fixture" else datetime(2026, 11, 1)
    monkeypatch.setattr(paper, "datetime", DateClock)
    assert paper._holding_days({"entry_date": "2026-11-01"}, "fixture") == 4


def test_reference_only_exports_verified_daily_equity():
    assert paper._daily_pnl_reference({"daily_pnl_status": "missing_previous_equity"}, "2026-11-05") == {}
    summary = {"daily_pnl_status": "verified", "previous_equity_date": "2026-11-04", "previous_total_equity": 1000}
    assert paper._daily_pnl_reference(summary, "2026-11-05") == {"valuation_date": "2026-11-05", "previous_equity_date": "2026-11-04", "previous_total_equity": 1000}
