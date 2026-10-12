"""DS12-03: two-ledger fill evidence, valuation, price basis, suspension proof.

Offline and synthetic only: no network, no real ledger, no real market data.
These tests pin the *public* entry points of the strategy book and the AI book.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from quantdesk.paper import auto_paper_portfolio as app


def _write_daily(directory: Path, symbol: str, rows: list[str]) -> None:
    header = "date,symbol,open,high,low,close,volume,amount,adjust,source,fetched_at\n"
    (directory / f"{symbol}.csv").write_text(header + "".join(rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# TRD-03: missing / stale / suspended quotes must be distinguishable, and a
# suspended position must be frozen at its last tradable price, not zeroed.
# ---------------------------------------------------------------------------


def test_latest_daily_quote_flags_suspended_zero_volume_bar(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app, "DAILY_DIR", tmp_path)
    _write_daily(
        tmp_path,
        "600500",
        [
            "2026-09-09,600500,5.00,5.10,4.90,5.00,1000000,5000000,raw,diag,2026-09-09T16:00:00+08:00\n",
            "2026-09-10,600500,5.00,5.00,5.00,5.00,0,0,raw,diag,2026-09-10T16:00:00+08:00\n",
        ],
    )
    quote = app._latest_daily_quote("600500", decision_date="2026-09-11")

    assert quote["suspended"] is True
    assert quote["is_suspended"] is True
    # Frozen at the last genuinely tradable bar, never at the zero-volume row.
    assert quote["price"] == 5.00
    assert any("suspended_daily_bar" in warning for warning in quote["warnings"])
    assert any("stale_daily_quote" in warning for warning in quote["warnings"])


def test_mark_position_freezes_suspended_and_keeps_equity_nonzero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app, "DAILY_DIR", tmp_path)
    _write_daily(
        tmp_path,
        "600500",
        [
            "2026-09-09,600500,5.00,5.10,4.90,5.00,1000000,5000000,raw,diag,2026-09-09T16:00:00+08:00\n",
            "2026-09-10,600500,5.00,5.00,5.00,5.00,0,0,raw,diag,2026-09-10T16:00:00+08:00\n",
        ],
    )
    position = {"symbol": "600500", "quantity": 1000, "entry_price": 4.0, "entry_date": "2026-01-01"}
    marked = app._mark_position(
        position,
        5.00,
        "2026-09-11",
        "hold",
        quote=app._latest_daily_quote("600500", decision_date="2026-09-11"),
    )

    assert marked["valuation_status"] == "suspended_frozen"
    assert marked["tradeability_status"] == "suspended"
    assert marked["can_sell"] is False
    assert marked["market_value"] > 0, "a suspended position must not be valued at zero"
    assert marked["valuation_warning"]


def test_latest_daily_quote_missing_is_visible(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app, "DAILY_DIR", tmp_path)
    quote = app._latest_daily_quote("600519", decision_date="2026-09-11")

    assert quote["price"] is None
    assert quote["missing"] is True
    assert any("missing_daily_quote" in warning for warning in quote["warnings"])


# ---------------------------------------------------------------------------
# TRD-04: the original frame is classified before use; mixed basis is not raw.
# ---------------------------------------------------------------------------


def test_raw_basis_frame_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app, "DAILY_DIR", tmp_path)
    _write_daily(
        tmp_path,
        "600500",
        ["2026-09-10,600500,5.00,5.10,4.90,5.00,1000000,5000000,raw,diag,2026-09-10T16:00:00+08:00\n"],
    )
    quote = app._latest_daily_quote("600500")

    assert quote["price_basis"] == "raw"
    assert quote["price_basis_reason"] == ""
    assert not quote.get("price_basis_untrusted")


def test_mixed_basis_frame_is_flagged_untrusted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app, "DAILY_DIR", tmp_path)
    _write_daily(
        tmp_path,
        "600500",
        [
            "2026-09-09,600500,5.00,5.10,4.90,5.00,1000000,5000000,raw,diag,2026-09-09T16:00:00+08:00\n",
            "2026-09-10,600500,5.00,5.10,4.90,5.10,1000000,5000000,qfq,diag,2026-09-10T16:00:00+08:00\n",
        ],
    )
    quote = app._latest_daily_quote("600500", decision_date="2026-09-11")

    assert quote["price_basis_reason"] == "mixed_price_basis"
    assert quote.get("price_basis_untrusted") is True
    assert any("price_basis_untrusted" in warning for warning in quote["warnings"])


# ---------------------------------------------------------------------------
# B-03a: non-empty suspension text cannot upgrade a decision to verified, and
# the statement gate never aborts the whole plan (no new hard gate).
# ---------------------------------------------------------------------------


def test_suspension_text_cannot_upgrade_decision_to_verified(tmp_path: Path) -> None:
    from ai_trader import decision_log

    record = {
        "decision_id": "dec_x",
        "symbol": "600500",
        "decision_date": "2026-09-10",
        "trade_date": "2026-09-10",
        "result": decision_log.RESULT_PENDING,
        "suspension": "停牌",
        "judgment": "该股停牌，无法验证",
        "verification": {
            "kind": "stand_above",
            "level": 5.0,
            "n_sessions": 3,
            "due_date": "2026-09-30",
        },
    }
    updated = decision_log.score_decision(record, daily_dir=tmp_path, as_of="2026-09-11")

    assert updated["result"] not in {decision_log.RESULT_RIGHT, decision_log.RESULT_WRONG}
    assert updated["result"] == decision_log.RESULT_PENDING


# ---------------------------------------------------------------------------
# Strategy book real entry: a fill is only booked off a known price basis.
# ---------------------------------------------------------------------------


def _strategy_env(tmp_path: Path, monkeypatch) -> Path:
    state_dir = tmp_path / "state"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    daily_dir = tmp_path / "data" / "daily"
    for directory in (state_dir, results_dir, reports_dir, daily_dir):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app, "STATE_DIR", state_dir)
    monkeypatch.setattr(app, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(app, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(app, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(app, "AUTO_PAPER_PORTFOLIO_STATE_JSON", state_dir / "auto_paper_portfolio.json")
    monkeypatch.setattr(app, "AUTO_PAPER_PORTFOLIO_LATEST_JSON", results_dir / "auto_paper_portfolio_latest.json")
    monkeypatch.setattr(app, "AUTO_PAPER_PORTFOLIO_HISTORY_JSON", results_dir / "auto_paper_portfolio_history.json")
    monkeypatch.setattr(
        app,
        "evaluate_data_freshness",
        lambda: {
            "ok": True,
            "target_complete_trade_date": "2026-06-18",
            "can_review_yesterday_positions": True,
            "can_update_portfolio_valuation": True,
        },
    )
    return daily_dir


_STRATEGY_CANDIDATE = {
    "symbol": "000001",
    "name": "Alpha",
    "strategy_type": "long",
    "close": 10.5,
    "quantity": 100,
}


def _run_strategy(monkeypatch) -> dict:
    # Exercise the real candidate buy loop through the formal entry, stubbing
    # only the upstream formal-intake mapping.
    monkeypatch.setattr(
        app,
        "load_formal_candidate_intake",
        lambda *args, **kwargs: {
            "input_source": "unit_test",
            "status": "passed",
            "path": "",
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        app,
        "formal_candidate_strategy_payload",
        lambda *args, **kwargs: {"generated_at": "2026-06-18T15:30:00"},
    )
    monkeypatch.setattr(
        app,
        "candidate_rows_from_strategy_payload",
        lambda *args, **kwargs: [dict(_STRATEGY_CANDIDATE)],
    )
    return app._run_auto_paper_portfolio_unlocked(
        mode="init",
        allow_daily_approx=True,
        risk_gate_shadow_enabled=False,
    )


def _strategy_buys(payload: dict) -> list[dict]:
    return [a for a in payload.get("actions") or [] if a.get("action") == "buy" and a.get("symbol") == "000001"]


def test_strategy_raw_basis_candidate_still_fills(tmp_path: Path, monkeypatch) -> None:
    daily_dir = _strategy_env(tmp_path, monkeypatch)
    _write_daily(
        daily_dir,
        "000001",
        [
            # 1A: the previous session's close is the limit-price evidence for 06-18.
            "2026-06-17,000001,10.1,10.3,10.0,10.2,1000000,10000000,raw,diag,2026-06-17T15:00:00+08:00\n",
            "2026-06-18,000001,10.0,10.6,9.9,10.5,1000000,10000000,raw,diag,2026-06-18T15:00:00+08:00\n",
        ],
    )
    payload = _run_strategy(monkeypatch)

    assert _strategy_buys(payload), payload.get("warnings")


def test_strategy_mixed_basis_candidate_is_rejected(tmp_path: Path, monkeypatch) -> None:
    daily_dir = _strategy_env(tmp_path, monkeypatch)
    _write_daily(
        daily_dir,
        "000001",
        [
            "2026-06-17,000001,10.0,10.6,9.9,10.2,1000000,10000000,raw,diag,2026-06-17T15:00:00+08:00\n",
            "2026-06-18,000001,10.0,10.6,9.9,10.5,1000000,10000000,qfq,diag,2026-06-18T15:00:00+08:00\n",
        ],
    )
    payload = _run_strategy(monkeypatch)

    assert not _strategy_buys(payload)
    assert any(
        "价格口径不可用于成交" in str(action.get("reason"))
        for action in payload.get("actions") or []
    )


def test_strategy_unknown_basis_candidate_is_rejected(tmp_path: Path, monkeypatch) -> None:
    daily_dir = _strategy_env(tmp_path, monkeypatch)
    (daily_dir / "000001.csv").write_text("date,close\n2026-06-18,10.5\n", encoding="utf-8")
    payload = _run_strategy(monkeypatch)

    assert not _strategy_buys(payload)


# ---------------------------------------------------------------------------
# Missing-valuation completeness in the funded-ledger snapshot contract.
# ---------------------------------------------------------------------------


def _funded_snapshot(positions: list[dict], *, basis: str = "raw"):
    from quantdesk.paper.portfolio_contracts import (
        build_portfolio_snapshot,
        resolve_paper_capital_profile,
    )

    ledger = {
        "ledger_id": "unit_funded_ledger",
        "initial_cash": 10000.0,
        "cash": 5000.0,
        "account": {"cash": 5000.0, "available_cash": 5000.0},
        "positions": positions,
    }
    profile = resolve_paper_capital_profile(existing_state=ledger, existing_state_verified=True)
    return build_portfolio_snapshot(ledger, profile, as_of="2026-09-11", valuation_price_basis=basis)


def test_snapshot_carries_a_trustworthy_prior_valuation_instead_of_dropping_it() -> None:
    snap = _funded_snapshot(
        [{"symbol": "600500", "quantity": 1000, "last_tradable_price": 5.0, "price_basis": "raw"}]
    )

    assert snap.market_value == 5000.0
    assert "position_market_value_carried_forward" in snap.reason_codes
    assert snap.coverage["carried_forward_position_count"] == 1
    assert snap.coverage["valuation_complete"] is True


def test_snapshot_with_no_trustworthy_valuation_is_marked_incomplete() -> None:
    snap = _funded_snapshot([{"symbol": "600500", "quantity": 1000, "price_basis": "raw"}])

    assert "position_market_value_missing" in snap.reason_codes
    assert "equity_incomplete_unknown_positions" in snap.reason_codes
    assert snap.coverage["unknown_position_count"] == 1
    assert snap.coverage["valuation_complete"] is False
    assert snap.allocation_eligible is False


def test_statement_gate_does_not_abort_the_whole_plan(tmp_path: Path) -> None:
    from ai_trader import decision_log

    plan = {
        "decision_date": "2026-09-10",
        "trade_date": "2026-09-11",
        "intents": [
            {
                "symbol": "600500",
                "side": "buy",
                "quantity": 100,
                "ref_close": 5.0,
                "predicted_direction": "up",
                "note": "停牌",
            }
        ],
    }
    result = decision_log.apply_gate_to_plan(plan, daily_dir=tmp_path)

    assert result is not None
    assert isinstance(plan.get("intents"), list)
    assert "statement_review" in plan
    assert not plan.get("aborted")
    assert not plan.get("halted")
