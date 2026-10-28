"""Ledger integrity: a missing price must never look like a loss.

These pin four silent failures found on 2026-08-24 by an adversarial sweep. All
four were invisible in normal output, and the first corrupted the single number
the owner reads each night to judge the trader.
"""

from __future__ import annotations

from pathlib import Path

from ai_trader.ledger import mark_to_market
from ai_trader.philosophy import load_philosophy, record_stance, save_philosophy


def _book() -> dict:
    return {
        "cash": 20000.0,
        "positions": [
            {"symbol": "600000", "quantity": 1000, "avg_cost": 10.0,
             "market_price": 11.0, "acquired_on": "2026-08-01"},
            {"symbol": "600519", "quantity": 100, "avg_cost": 400.0,
             "market_price": 420.0, "acquired_on": "2026-08-05"},
        ],
    }


def test_unpriced_position_carries_last_mark_instead_of_zero() -> None:
    """The reproduction of the bug: equity read 31,000 instead of 73,000."""

    # Only one symbol priced, as settle_plan used to do with intent symbols only.
    out = mark_to_market(_book(), {"600000": 11.0}, as_of="2026-08-24")
    # 20,000 cash + 1,000x11 + 100x420 (carried) = 73,000, not 31,000.
    assert out["equity"] == 73000.0
    carried = [p for p in out["positions"] if p["symbol"] == "600519"][0]
    assert carried["market_value"] == 42000.0
    assert carried["stale_mark"] is True
    # Carried, not observed — and said so.
    assert any("stale_mark_price:600519" in w for w in out.get("warnings") or [])


def test_fresh_prices_take_precedence_over_the_carried_mark() -> None:
    out = mark_to_market(_book(), {"600000": 12.0, "600519": 430.0}, as_of="2026-08-24")
    assert out["equity"] == 20000.0 + 1000 * 12.0 + 100 * 430.0
    for position in out["positions"]:
        assert not position.get("stale_mark")


def test_position_with_no_price_and_no_history_still_reports_zero() -> None:
    """Nothing to carry => the old behaviour, so a real gap is not invented away."""

    book = {"cash": 1000.0, "positions": [
        {"symbol": "600000", "quantity": 100, "avg_cost": 0.0, "acquired_on": "2026-08-01"},
    ]}
    out = mark_to_market(book, {}, as_of="2026-08-24")
    assert out["equity"] == 1000.0
    assert any("missing_mark_price:600000" in w for w in out.get("warnings") or [])


def test_cash_floor_is_derived_from_real_equity() -> None:
    """The 15% floor was computed off the collapsed equity, loosening it ~2-3x."""

    from ai_trader.mandate import DEFAULT_MANDATE

    out = mark_to_market(_book(), {"600000": 11.0}, as_of="2026-08-24")
    floor = DEFAULT_MANDATE.min_cash_amount(float(out["equity"]))
    assert floor == round(73000.0 * 0.15, 2)


def test_settle_plan_prices_the_whole_book_not_just_intents() -> None:
    import inspect

    from ai_trader import overnight

    source = inspect.getsource(overnight.settle_plan)
    assert "for symbol in position_map(ledger)" in source, (
        "settle_plan no longer seeds marks for held symbols outside tonight's intents"
    )


def test_deferred_plan_is_not_overwritten_by_tonights_plan() -> None:
    import inspect

    from ai_trader import overnight

    # run_evening now waits for the requested day's data; the existing plan
    # protection lives in its ready-data delegate. Keep checking both wiring
    # and the original preservation statements after that split.
    assert "_run_evening_ready(" in inspect.getsource(overnight.run_evening)
    source = inspect.getsource(overnight._run_evening_ready)
    assert "deferred_plan = existing" in source
    assert "save_plan(deferred_plan, runtime.plan_latest)" in source, (
        "a committed but unsettled plan can again be destroyed by tonight's plan"
    )


def test_llm_sees_real_cost_basis_not_a_question_mark() -> None:
    """It decides sells; it must know whether a position is up or down."""

    import inspect

    from ai_trader import overnight

    source = inspect.getsource(overnight._build_llm_context)
    assert "pos.get('avg_cost')" in source
    # The keys that were never written must no longer be read (the comment
    # explaining the bug legitimately still names them, so match the read itself).
    assert "pos.get('cost_price')" not in source
    assert "pos.get('avg_price')" not in source
    for shown in ("现价", "浮动", "已持"):
        assert shown in source


def test_corrupt_philosophy_is_moved_aside_not_overwritten(tmp_path: Path) -> None:
    """A blue-screen must not cost the trader its entire stance history."""

    path = tmp_path / "philosophy.json"
    path.write_text("{truncated", encoding="utf-8")
    loaded = load_philosophy(path)
    assert loaded.get("error")
    backup = loaded.get("backup_path")
    assert backup and Path(backup).is_file(), "the damaged original was not preserved"
    assert Path(backup).read_text(encoding="utf-8") == "{truncated"
    # Saving afterwards must not persist the transient failure markers.
    saved = record_stance(
        loaded, as_of="2026-08-24", summary="重新开始。",
        preferred_styles=["长期"], reasoning="上次文件坏了。",
    )
    save_philosophy(saved, path)
    again = load_philosophy(path)
    assert again["stance"]["summary"] == "重新开始。"
    assert "error" not in again or not again.get("error")
    assert "backup_path" not in again or not again.get("backup_path")
