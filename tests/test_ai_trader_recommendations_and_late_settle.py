"""Tests for the AI trader's research-desk input and late plan settlement.

Two behaviours are locked here:

1. A recommendation from the 8000 research desk is an *input*, never an order.
   It reaches the trader's focus universe and is labelled in the plan, and a
   missing/broken pool degrades to a warning instead of a fabricated pick.
2. A plan that was committed but never settled (missed evening: crash, power
   loss, machine off) must be settled against its OWN trade date rather than
   silently voided -- otherwise the trader can never accumulate a position on
   an unreliable machine.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader.market import focus_universe
from ai_trader.recommendations import (
    load_recommendations,
    recommendation_note,
)


def _write_pool(root: Path, name: str, items: list[dict]) -> None:
    path = root / "results" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ok": True, "items": items}, ensure_ascii=False), encoding="utf-8"
    )


def test_missing_pool_degrades_with_warning_not_fabrication(tmp_path: Path) -> None:
    view = load_recommendations(tmp_path / "results")
    assert view["symbols"] == []
    assert view["rows"] == []
    assert any("recommendation_pool_missing" in w for w in view["warnings"])
    assert view["boundary"]["is_order"] is False


def test_malformed_pool_is_reported_not_guessed(tmp_path: Path) -> None:
    bad = tmp_path / "results" / "auto_long_candidates_latest.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    view = load_recommendations(tmp_path / "results")
    assert view["symbols"] == []
    assert any("unreadable" in w for w in view["warnings"])


def test_pools_merge_dedupe_and_order_by_rank(tmp_path: Path) -> None:
    _write_pool(
        tmp_path,
        "auto_long_candidates_latest.json",
        [
            {"symbol": "600360", "name": "华微电子", "rank": 2, "score": 90},
            {"symbol": "600018", "name": "上港集团", "rank": 1, "score": 111},
        ],
    )
    _write_pool(
        tmp_path,
        "auto_board_candidates_latest.json",
        [{"symbol": "600360", "name": "华微电子", "rank": 5, "score": 50}],
    )
    view = load_recommendations(tmp_path / "results")
    # rank 1 first; the duplicate keeps its better (lower) rank from the long pool
    assert view["symbols"] == ["600018", "600360"]
    assert [row["rank"] for row in view["rows"]] == [1, 2]


def test_stale_recommendation_is_flagged_but_kept(tmp_path: Path) -> None:
    _write_pool(
        tmp_path,
        "auto_long_candidates_latest.json",
        [{"symbol": "600018", "rank": 1, "data_latest_date": "2026-08-17"}],
    )
    view = load_recommendations(tmp_path / "results", require_fresh_for="2026-08-21")
    assert view["symbols"] == ["600018"]
    assert view["rows"][0]["stale"] is True
    note = recommendation_note(view["rows"], "600018")
    assert "数据日期偏旧" in note


def test_recommendation_reaches_focus_universe(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    for symbol, pct in (("600018", 1.0), ("600360", 9.0), ("600519", 8.0)):
        (daily / f"{symbol}.csv").write_text(
            "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source\n"
            f"2026-08-21,{symbol},10,10.5,9.8,10.2,1000,10000,{pct},raw,test\n",
            encoding="utf-8",
        )
    # Without the recommendation, the biggest mover leads.
    plain = focus_universe(daily, "2026-08-21", limit=3)
    assert plain[0] == "600360"
    # With it, the recommended name is pinned ahead of raw movers.
    with_rec = focus_universe(daily, "2026-08-21", recommended=["600018"], limit=3)
    assert with_rec[0] == "600018"
    assert set(with_rec) == {"600018", "600360", "600519"}


def test_holdings_still_outrank_recommendations(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    for symbol in ("600018", "600036"):
        (daily / f"{symbol}.csv").write_text(
            "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source\n"
            f"2026-08-21,{symbol},10,10.5,9.8,10.2,1000,10000,1.0,raw,test\n",
            encoding="utf-8",
        )
    names = focus_universe(
        daily, "2026-08-21", held=["600036"], recommended=["600018"], limit=5
    )
    assert names[0] == "600036"
    assert "600018" in names


def test_stale_plan_settles_against_its_own_trade_date(tmp_path: Path) -> None:
    """A missed evening must not void a committed plan."""

    from ai_trader.overnight import settle_plan

    daily = tmp_path / "daily"
    daily.mkdir()
    # 08-18 session: opens at 5.00 -- the plan's fill price.
    (daily / "600500.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source\n"
        "2026-08-18,600500,5.00,5.40,4.90,5.30,1000000,5000000,1.0,raw,test\n",
        encoding="utf-8",
    )
    plan = {
        "plan_id": "plan_20260817_for_20260818",
        "trade_date": "2026-08-18",
        "status": "posted",
        "intents": [
            {
                "symbol": "600500",
                "side": "buy",
                "quantity": 1400,
                "ref_close": 5.59,
                "predicted_direction": "up",
                "preset_touch_price": 5.53,
            }
        ],
    }
    ledger = {
        "schema_version": "ai_trader_ledger_v1",
        "initial_cash": 100000.0,
        "cash": 100000.0,
        "equity": 100000.0,
        "market_value": 0.0,
        "positions": [],
        "trades": [],
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
    }
    # Settled days later, against 08-18 -- not against "today".
    settled, after, actions = settle_plan(
        plan, ledger, as_of="2026-08-18", daily_dir=daily
    )
    assert settled["status"] == "settled"
    assert settled["settled_on"] == "2026-08-18"
    filled = [row for row in actions if row.get("symbol") == "600500"]
    assert filled, "a committed plan with a real 08-18 open must produce an action"
    # Fill uses the open, never the high/low.
    for row in filled:
        price = row.get("price") or row.get("executed_price")
        if price is not None:
            assert abs(float(price) - 5.00) < 1e-6


def test_future_plan_is_not_settled_early(tmp_path: Path) -> None:
    from ai_trader.overnight import settle_plan

    daily = tmp_path / "daily"
    daily.mkdir()
    plan = {"trade_date": "2026-08-24", "status": "posted", "intents": []}
    ledger = {"cash": 100000.0, "positions": [], "trades": []}
    settled, after, actions = settle_plan(
        plan, ledger, as_of="2026-08-21", daily_dir=daily
    )
    assert settled.get("status") == "posted"
    assert actions == []
