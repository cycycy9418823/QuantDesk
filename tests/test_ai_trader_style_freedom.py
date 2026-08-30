"""The trader chooses its own style; nothing here may force one on it.

The user's actual goal is to watch an AI decide for itself whether it is a
限价打板 chaser, a swing trader, or someone who buys and sits for a year. An
earlier version of this package hardcoded "新人短线波段股民…不是打板，不是基金经理"
plus a 5-day horizon, which silently forbade both ends of that range. These tests
exist so that coercion cannot come back:

- a long-horizon position must survive past the fallback horizon,
- the MA20 "story broken" rule must not liquidate a 中线/长期 thesis,
- an opening gap-up must not abandon a 打板 or 长期 buy,
- the declared style must reach the ledger, or tomorrow it is forgotten.

Risk rails (lot size, cash floor, position cap, next-open fills, T+1) are NOT
style and are covered by the other ai_trader tests; they must stay untouched.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ai_trader.ledger import empty_ledger
from ai_trader.mandate import DEFAULT_MANDATE, STYLES, max_open_gap_for_style
from ai_trader.philosophy import (
    add_note,
    load_philosophy,
    record_closed_trade,
    record_stance,
    save_philosophy,
    style_scoreboard,
)


def _daily(tmp_path: Path, symbol: str, rows: str) -> Path:
    daily = tmp_path / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / f"{symbol}.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n" + rows,
        encoding="utf-8",
    )
    return daily


# --- the mandate must not encode a style ---------------------------------------


def test_mandate_does_not_forbid_any_style() -> None:
    persona = DEFAULT_MANDATE.persona_zh
    for banned in ("不是打板", "不是基金经理", "新人短线波段"):
        assert banned not in persona, f"persona still forbids a style: {banned}"
    # All five styles remain available to declare.
    assert set(STYLES) == {"打板", "短线", "波段", "中线", "长期"}


def test_min_holdings_does_not_force_a_position_count() -> None:
    # Sitting in cash is a legitimate decision, so no floor may be imposed.
    assert DEFAULT_MANDATE.min_holdings == 0


def test_gap_tolerance_follows_declared_style() -> None:
    default = DEFAULT_MANDATE.max_chase_open_gap_pct
    # Chasing the gap is the entire point of 打板, and a year-long hold does not
    # care about the open, so neither is capped.
    assert max_open_gap_for_style("打板", default) is None
    assert max_open_gap_for_style("长期", default) is None
    assert max_open_gap_for_style("短线", default) == 5.0
    assert max_open_gap_for_style("中线", default) == 9.0
    # An undeclared style falls back to the account default rather than to None.
    assert max_open_gap_for_style("", default) == default
    assert max_open_gap_for_style(None, default) == default


# --- long horizons must actually be respected ----------------------------------


def test_long_horizon_position_is_not_sold_at_fallback_horizon(tmp_path: Path) -> None:
    """A 250-day thesis must still be open on day ~30."""

    from ai_trader.overnight import build_plan

    daily = _daily(tmp_path, "600000", "2024-02-20,600000,10.0,10.2,9.8,10.0,1000,10000,0,raw\n")
    ledger = empty_ledger()
    ledger["positions"] = [
        {
            "symbol": "600000",
            "quantity": 100,
            "avg_cost": 10.0,
            "acquired_on": "2024-01-02",
            "planned_hold_days": 250,
            "style": "长期",
            "thesis": "看公司，不看盘。",
            "market_price": 10.0,
        }
    ]
    plan = build_plan(
        decision_date="2024-02-20",
        trade_date="2024-02-21",
        ledger=ledger,
        panel=pd.DataFrame(),
        ranked=[],
        live=None,
        daily_dir=daily,
        minute_dir=tmp_path / "minutes",
        network_news=False,
        conviction="live",
    )
    sells = [row for row in plan["intents"] if row.get("side") == "sell"]
    assert sells == [], "a 250-day thesis was liquidated early"


def test_ma20_story_broken_does_not_apply_to_long_term(tmp_path: Path) -> None:
    """The MA20 break is a short-term rule; a 长期 hold must ignore it."""

    from ai_trader.overnight import build_plan

    daily = _daily(tmp_path, "600000", "2024-03-01,600000,8.0,8.2,7.8,8.0,1000,10000,0,raw\n")
    # A falling panel puts price well below its own 20-day mean.
    index = pd.date_range("2024-01-31", periods=21, freq="B").strftime("%Y-%m-%d")
    panel = pd.DataFrame({"600000": [12.0] * 20 + [8.0]}, index=index)
    as_of = index[-1]

    def _plan(style: str, hold: int) -> list[dict]:
        ledger = empty_ledger()
        ledger["positions"] = [{
            "symbol": "600000", "quantity": 100, "avg_cost": 12.0,
            "acquired_on": index[0], "planned_hold_days": hold,
            "style": style, "hypothesis_id": "ma20_trend", "market_price": 8.0,
        }]
        daily2 = _daily(tmp_path / style, "600000", f"{as_of},600000,8.0,8.2,7.8,8.0,1000,10000,0,raw\n")
        plan = build_plan(
            decision_date=as_of, trade_date="2024-03-04", ledger=ledger, panel=panel,
            ranked=[], live={"hypothesis_id": "ma20_trend", "title": "ma20"},
            daily_dir=daily2, minute_dir=tmp_path / "minutes",
            network_news=False, conviction="live",
        )
        return [row for row in plan["intents"] if row.get("side") == "sell"]

    assert _plan("长期", 250) == [], "MA20 break liquidated a 长期 position"
    assert _plan("中线", 60) == [], "MA20 break liquidated a 中线 position"
    # For a short-term position the same rule SHOULD still fire.
    assert _plan("短线", 5), "MA20 break no longer fires for a short-term position"
    assert daily.exists()


def test_llm_sell_only_honoured_for_held_symbols(tmp_path: Path) -> None:
    from ai_trader.overnight import build_plan

    daily = _daily(tmp_path, "600000", "2024-01-10,600000,10.0,10.2,9.8,10.0,1000,10000,0,raw\n")
    ledger = empty_ledger()
    ledger["positions"] = [{
        "symbol": "600000", "quantity": 100, "avg_cost": 10.0,
        "acquired_on": "2024-01-02", "planned_hold_days": 250,
        "style": "长期", "market_price": 10.0,
    }]
    plan = build_plan(
        decision_date="2024-01-10",
        trade_date="2024-01-11",
        ledger=ledger,
        panel=pd.DataFrame(),
        ranked=[],
        live=None,
        daily_dir=daily,
        minute_dir=tmp_path / "minutes",
        network_news=False,
        conviction="live",
        llm_sells=[
            {"symbol": "600000", "reason": "逻辑变了，认错。"},
            {"symbol": "000999", "reason": "根本没持有这只"},
        ],
        llm_directed=True,
    )
    sells = [row for row in plan["intents"] if row.get("side") == "sell"]
    assert [row["symbol"] for row in sells] == ["600000"]
    assert "逻辑变了" in sells[0]["thesis"]


# --- the declared style must survive to tomorrow -------------------------------


def test_style_is_persisted_onto_the_position() -> None:
    """Without this, tomorrow's exit rules cannot tell what it opened this as."""

    import inspect

    from ai_trader import trader

    source = inspect.getsource(trader)
    assert '"style": intent.get("style")' in source
    assert "fallback_hold_days" in source


# --- the philosophy diary ------------------------------------------------------


def test_closed_sell_carries_style_for_attribution() -> None:
    """A filled sell must record style/held_days/net_return.

    ``record_closed_trade`` existed but was called from nowhere in production, so
    ``style_ledger`` stayed permanently empty and the trader could never be shown
    its own per-style record. These assertions pin the wiring in place.
    """

    import inspect

    from ai_trader import overnight, trader

    trader_source = inspect.getsource(trader)
    for field in ('"style": pos.get("style")', '"held_days"', '"net_return"'):
        assert field in trader_source, f"sell trade no longer carries {field}"

    overnight_source = inspect.getsource(overnight.run_evening)
    assert "record_closed_trade" in overnight_source, "attribution is not wired into the cycle"
    # Rule-engine exits close positions too, so saving must not require an LLM.
    assert "philosophy_dirty" in overnight_source


def test_net_return_on_cost_is_relative_to_capital_deployed() -> None:
    from ai_trader.trader import _net_return_on_cost

    fill = {"realized_pnl": 500.0}
    pos = {"avg_cost": 10.0}
    # 500 gained on 100 shares at cost 10 = 1000 deployed => +50%.
    assert _net_return_on_cost(fill, pos, 100) == 0.5
    # Degrades to None rather than dividing by zero.
    assert _net_return_on_cost(fill, {"avg_cost": 0.0}, 100) is None
    assert _net_return_on_cost({}, pos, 100) is None


def test_stance_change_is_recorded_as_a_revision() -> None:
    philosophy = load_philosophy(Path("does-not-exist.json"))
    philosophy = record_stance(
        philosophy, as_of="2026-08-24", summary="先做波段试试水。",
        preferred_styles=["波段"], reasoning="没什么把握，先短。",
    )
    assert philosophy["stance"]["summary"] == "先做波段试试水。"
    assert philosophy["revisions"] == []
    # Restating the same view is not a change of mind.
    same = record_stance(
        philosophy, as_of="2026-08-25", summary="先做波段试试水。",
        preferred_styles=["波段"], reasoning="还是这么想。",
    )
    assert same["revisions"] == []
    # An actual change is kept, so the turning point stays visible.
    changed = record_stance(
        same, as_of="2026-09-30", summary="改成长期持有，短线被成本吃掉了。",
        preferred_styles=["长期"], reasoning="波段做了十几笔，扣完费是负的。",
    )
    assert len(changed["revisions"]) == 1
    assert changed["revisions"][0]["summary"] == "先做波段试试水。"
    assert changed["stance"]["preferred_styles"] == ["长期"]


def test_style_ledger_tracks_outcomes_per_style() -> None:
    philosophy = load_philosophy(Path("does-not-exist.json"))
    for net in (0.05, -0.02, -0.03):
        philosophy = record_closed_trade(philosophy, style="波段", net_return=net, hold_days=7)
    philosophy = record_closed_trade(philosophy, style="长期", net_return=0.30, hold_days=250)
    swing = philosophy["style_ledger"]["波段"]
    assert swing["trades"] == 3
    assert swing["wins"] == 1
    assert abs(swing["mean_net"] - 0.0) < 0.01
    assert swing["win_rate"] == round(1 / 3, 4)
    assert swing["mean_hold_days"] == 7.0
    board = style_scoreboard(philosophy)
    assert any("波段" in line for line in board)
    assert any("长期" in line for line in board)


def test_philosophy_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "philosophy.json"
    philosophy = load_philosophy(path)
    philosophy = record_stance(
        philosophy, as_of="2026-08-24", summary="长期为主。",
        preferred_styles=["长期"], reasoning="成本最低。",
    )
    philosophy = add_note(philosophy, as_of="2026-08-24", note="追涨停那次亏得最惨。")
    save_philosophy(philosophy, path)
    again = load_philosophy(path)
    assert again["stance"]["summary"] == "长期为主。"
    assert again["notes"][-1]["note"] == "追涨停那次亏得最惨。"


def test_corrupt_philosophy_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "philosophy.json"
    path.write_text("{not json", encoding="utf-8")
    philosophy = load_philosophy(path)
    assert philosophy["stance"]["summary"] == ""
    assert philosophy["style_ledger"] == {}
