from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from ai_trader.cycle import CyclePaths, run_cycle
from ai_trader.hypotheses import empty_book
from ai_trader.ledger import empty_ledger, save_ledger
from ai_trader.mandate import DEFAULT_MANDATE
from ai_trader.paths import (
    FORBIDDEN_QUANTDESK_LEDGER,
    PHILOSOPHY_PATH,
    assert_not_quantdesk_ledger,
)
from ai_trader.researcher import research_ideas
from ai_trader.trader import decide_and_trade


def _hash_if_exists(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_daily(root: Path, symbol: str, closes: list[float]) -> None:
    start = pd.Timestamp("2024-01-02")
    dates = pd.bdate_range(start, periods=len(closes))
    rows = ["date,symbol,open,high,low,close,volume,amount,pct_chg,adjust"]
    prev = closes[0]
    for day, close in zip(dates, closes, strict=True):
        chg = 0.0 if prev == 0 else (close / prev - 1.0) * 100
        rows.append(
            f"{day.date().isoformat()},{symbol},{close},{close},{close},{close},1000,10000,{chg:.4f},raw"
        )
        prev = close
    (root / f"{symbol}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _runtime(tmp_path: Path, daily: Path) -> CyclePaths:
    out = tmp_path / "runtime"
    return CyclePaths(
        daily_dir=daily,
        minute_dir=tmp_path / "minutes",
        ledger=out / "ledger.json",
        hypotheses=out / "hypotheses.json",
        journal_latest=out / "journal_latest.json",
        journal_log=out / "journal.jsonl",
        plan_latest=out / "plan.json",
        review_latest=out / "review.json",
        watchlist=out / "watchlist.json",
        philosophy=out / "philosophy.json",
        cycle_latest=out / "cycle.json",
        snapshot=out / "snapshot.json",
    )


def test_refuses_quantdesk_ledger(tmp_path: Path) -> None:
    before = _hash_if_exists(FORBIDDEN_QUANTDESK_LEDGER)
    with pytest.raises(RuntimeError):
        save_ledger(empty_ledger(), FORBIDDEN_QUANTDESK_LEDGER)
    with pytest.raises(RuntimeError):
        assert_not_quantdesk_ledger(tmp_path / "auto_paper_portfolio.json")
    assert _hash_if_exists(FORBIDDEN_QUANTDESK_LEDGER) == before


def test_cycle_does_not_touch_quantdesk_ledger(tmp_path: Path) -> None:
    before = _hash_if_exists(FORBIDDEN_QUANTDESK_LEDGER)
    philosophy_before = _hash_if_exists(PHILOSOPHY_PATH)
    daily = tmp_path / "daily"
    daily.mkdir()
    for i in range(8):
        closes = [20.0 - n * 0.05 for n in range(80)]
        _write_daily(daily, f"60000{i}", closes)
    runtime = _runtime(tmp_path, daily)
    payload = run_cycle(paths=runtime, lookback=80, universe_limit=8, network=False)
    assert payload["ok"] is True
    assert payload["ledger"]["ledger_id"] == "ai_trader_100k"
    filled = [row for row in payload.get("actions") or [] if row.get("status") == "filled"]
    assert filled == []
    assert abs(float(payload["ledger"]["cash"]) - 100000) < 1e-6
    assert _hash_if_exists(FORBIDDEN_QUANTDESK_LEDGER) == before
    assert str(FORBIDDEN_QUANTDESK_LEDGER) not in str(payload)
    assert payload["conviction"] == "probation"
    assert payload["network_news"] is False
    assert payload["live_method"] is None
    assert payload["plan"]["intents"]
    assert payload["plan"]["intents"][0]["conviction"] == "probation"
    assert runtime.philosophy.parent == tmp_path / "runtime"
    assert _hash_if_exists(PHILOSOPHY_PATH) == philosophy_before


def test_t1_blocks_same_day_sell() -> None:
    ledger = empty_ledger()
    ledger["positions"] = [
        {
            "symbol": "600000",
            "quantity": 100,
            "avg_cost": 10.0,
            "acquired_on": "2026-08-17",
            "hypothesis_id": "reversal_5",
        }
    ]
    ledger["cash"] = 90000.0
    updated, actions = decide_and_trade(
        ledger,
        as_of="2026-08-17",
        prices={"600000": 11.0, "600001": 10.0},
        pct_chg={"600000": 1.0, "600001": 0.0},
        ranked=[("600001", 1.0), ("600002", 0.5)],
        live_id="reversal_5",
    )
    sell_rejects = [
        row for row in actions if row.get("action") == "sell" and row.get("status") == "rejected"
    ]
    assert sell_rejects
    assert "t1_blocked" in str(sell_rejects[0].get("reason"))
    assert any(pos.get("symbol") == "600000" for pos in updated["positions"])


def test_max_two_opens_per_day() -> None:
    prices = {f"60000{i}": 10.0 for i in range(8)}
    ranked = [(f"60000{i}", float(8 - i)) for i in range(8)]
    ledger, actions = decide_and_trade(
        empty_ledger(),
        as_of="2026-08-17",
        prices=prices,
        pct_chg={key: 0.0 for key in prices},
        ranked=ranked,
        live_id="low_vol_20",
    )
    fills = [row for row in actions if row.get("action") == "buy" and row.get("status") == "filled"]
    assert len(fills) == 2
    assert len(ledger["positions"]) == 2


def test_no_live_method_stays_in_cash() -> None:
    ledger, actions = decide_and_trade(
        empty_ledger(),
        as_of="2026-08-17",
        prices={"600000": 10.0},
        pct_chg={"600000": 0.0},
        ranked=[("600000", 1.0)],
        live_id=None,
    )
    assert ledger["cash"] == DEFAULT_MANDATE.initial_cash
    assert ledger["positions"] == []
    assert actions[0]["action"] == "hold_cash"


def test_declining_market_rejects_ideas() -> None:
    dates = pd.bdate_range("2024-01-02", periods=80).strftime("%Y-%m-%d")
    data = {f"60000{i}": pd.Series([20.0 - n * 0.08 for n in range(80)], index=dates) for i in range(8)}
    panel = pd.DataFrame(data)
    result = research_ideas(panel, empty_book(), as_of=str(dates[-1]))
    assert result["live"] is None
    statuses = {item["status"] for item in result["evaluated"]}
    assert "live" not in statuses
    assert statuses & {"rejected", "insufficient_evidence"}


def test_overnight_fill_uses_open_not_high_or_low(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    rows = [
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,updated_at",
        "2024-01-02,600000,10.0,10.2,9.8,10.0,1000,10000,0,raw,fixture,2024-01-02T15:00:00+08:00",
        "2024-01-03,600000,10.15,12.0,9.50,11.0,1000,10000,10,raw,fixture,2024-01-03T15:00:00+08:00",
    ]
    (daily / "600000.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    plan = {
        "trade_date": "2024-01-03",
        "status": "pending_open",
        "live_method": {"hypothesis_id": "demo"},
        "intents": [
            {
                "symbol": "600000",
                "side": "buy",
                "quantity": 100,
                "ref_close": 10.0,
                "predicted_direction": "up",
                "preset_touch_price": 9.6,
                "reason": "test",
            }
        ],
    }
    from ai_trader.overnight import settle_plan

    settled, ledger, actions = settle_plan(plan, empty_ledger(), as_of="2024-01-03", daily_dir=daily)
    fills = [row for row in actions if row.get("status") == "filled"]
    assert len(fills) == 1
    assert fills[0]["price"] == 10.15
    assert fills[0]["fill_model"] == "next_open_only"
    card = settled["scorecard"][0]
    assert card["used_high_low_for_fill"] is False
    assert card["touch_hit"] is True
    assert card["direction_hit"] is True
    assert card["session_high"] == 12.0
    assert abs(float(ledger["cash"]) - 100000) > 1


def test_plan_does_not_trade_same_evening() -> None:
    from ai_trader.overnight import build_plan

    plan = build_plan(
        decision_date="2024-01-02",
        trade_date="2024-01-03",
        ledger=empty_ledger(),
        panel=pd.DataFrame(),
        ranked=[],
        live=None,
        daily_dir=Path("."),
        minute_dir=Path("."),
    )
    assert plan["execution_policy"] == "next_open_only"
    assert plan["intents"] == []
    assert plan["status"] == "pending_open"


def test_settle_fills_at_open_not_high_or_low(tmp_path: Path) -> None:
    from ai_trader.ledger import empty_ledger
    from ai_trader.overnight import settle_plan

    daily = tmp_path / "daily"
    daily.mkdir()
    rows = [
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,updated_at",
        "2024-01-02,600000,10.0,10.2,9.8,10.0,1000,10000,0,raw,fixture,2024-01-02T15:00:00+08:00",
        "2024-01-03,600000,10.3,12.0,9.5,11.0,1000,10000,10,raw,fixture,2024-01-03T15:00:00+08:00",
    ]
    (daily / "600000.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    plan = {
        "trade_date": "2024-01-03",
        "status": "pending_open",
        "live_method": {"hypothesis_id": "reversal_5"},
        "intents": [
            {
                "symbol": "600000",
                "side": "buy",
                "quantity": 100,
                "ref_close": 10.0,
                "predicted_direction": "up",
                "preset_touch_price": 9.6,
                "hypothesis_id": "reversal_5",
                "reason": "test",
            }
        ],
    }
    settled, ledger, actions = settle_plan(plan, empty_ledger(), as_of="2024-01-03", daily_dir=daily)
    fills = [row for row in actions if row.get("status") == "filled"]
    assert len(fills) == 1
    assert fills[0]["price"] == 10.3
    assert fills[0]["fill_model"] == "next_open_only"
    card = settled["scorecard"][0]
    assert card["used_high_low_for_fill"] is False
    assert card["touch_hit"] is True
    assert card["direction_hit"] is True
    assert card["session_high"] == 12.0


def test_plan_does_not_trade_until_next_open() -> None:
    from ai_trader.ledger import empty_ledger
    from ai_trader.overnight import build_plan

    ledger = empty_ledger()
    cash = ledger["cash"]
    plan = build_plan(
        decision_date="2024-01-02",
        trade_date="2024-01-03",
        ledger=ledger,
        panel=pd.DataFrame(),
        ranked=[],
        live=None,
        daily_dir=".",
        minute_dir=".",
    )
    assert plan["execution_policy"] == "next_open_only"
    assert plan["intents"] == []
    assert ledger["cash"] == cash
    assert plan["status"] == "pending_open"


def test_pick_operating_method_prefers_live_then_least_bad() -> None:
    from ai_trader.overnight import pick_operating_method

    live, conv = pick_operating_method(
        {"live": {"hypothesis_id": "reversal_5", "title": "A"}, "evaluated": []}
    )
    assert conv == "live"
    assert live["hypothesis_id"] == "reversal_5"

    method, conv = pick_operating_method(
        {
            "live": None,
            "evaluated": [
                {"hypothesis_id": "reversal_5", "title": "差", "holdout_mean_net": -0.03},
                {"hypothesis_id": "low_vol_20", "title": "稍好", "holdout_mean_net": -0.01},
            ],
        }
    )
    assert conv == "probation"
    assert method["hypothesis_id"] == "low_vol_20"
    assert method["status"] == "probation"

    idle, conv = pick_operating_method({"live": None, "evaluated": []})
    assert conv == "idle"
    assert idle is None


def test_probation_emits_one_half_size_open(tmp_path: Path) -> None:
    from ai_trader.overnight import build_plan

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2024-01-02,600000,10.0,10.2,9.8,10.0,1000,10000,0,raw\n",
        encoding="utf-8",
    )
    (daily / "600001.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2024-01-02,600001,10.0,10.2,9.8,10.0,1000,10000,0,raw\n",
        encoding="utf-8",
    )
    plan = build_plan(
        decision_date="2024-01-02",
        trade_date="2024-01-03",
        ledger=empty_ledger(),
        panel=pd.DataFrame(),
        ranked=[("600000", 1.0), ("600001", 0.5)],
        live={"hypothesis_id": "reversal_5", "title": "5日反转", "status": "probation"},
        daily_dir=daily,
        minute_dir=tmp_path / "minutes",
        network_news=False,
        conviction="probation",
    )
    assert plan["conviction"] == "probation"
    assert len(plan["intents"]) == 1
    intent = plan["intents"][0]
    assert intent["side"] == "buy"
    assert intent["symbol"] == "600000"
    assert intent["conviction"] == "probation"
    assert intent["quantity"] == 800
    assert "观察仓" in intent["reason"]
    assert intent["planned_hold_days"] == 5
    assert intent["abandon_if_open_gap_pct"] == 5.0


def test_does_not_sell_just_because_not_ranked(tmp_path: Path) -> None:
    from ai_trader.overnight import build_plan

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2024-01-02,600000,10.0,10.2,9.8,10.0,1000,10000,0,raw\n"
        "2024-01-03,600000,10.0,10.2,9.8,10.1,1000,10000,1,raw\n",
        encoding="utf-8",
    )
    (daily / "600001.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2024-01-03,600001,10.0,10.2,9.8,10.0,1000,10000,0,raw\n",
        encoding="utf-8",
    )
    ledger = empty_ledger()
    ledger["positions"] = [
        {
            "symbol": "600000",
            "quantity": 100,
            "avg_cost": 10.0,
            "acquired_on": "2024-01-02",
            "hypothesis_id": "reversal_5",
            "planned_hold_days": 5,
            "thesis": "试错仓，故事还在。",
            "market_price": 10.1,
        }
    ]
    plan = build_plan(
        decision_date="2024-01-03",
        trade_date="2024-01-04",
        ledger=ledger,
        panel=pd.DataFrame(),
        ranked=[("600001", 1.0)],
        live={"hypothesis_id": "reversal_5", "title": "5日反转"},
        daily_dir=daily,
        minute_dir=tmp_path / "minutes",
        network_news=False,
        conviction="live",
    )
    sells = [row for row in plan["intents"] if row.get("side") == "sell"]
    assert sells == []


def test_sells_after_planned_hold_days(tmp_path: Path) -> None:
    from ai_trader.overnight import build_plan

    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2024-01-09,600000,10.0,10.2,9.8,10.0,1000,10000,0,raw\n",
        encoding="utf-8",
    )
    ledger = empty_ledger()
    ledger["positions"] = [
        {
            "symbol": "600000",
            "quantity": 100,
            "avg_cost": 10.0,
            "acquired_on": "2024-01-02",
            "hypothesis_id": "reversal_5",
            "planned_hold_days": 5,
            "market_price": 10.0,
        }
    ]
    plan = build_plan(
        decision_date="2024-01-09",
        trade_date="2024-01-10",
        ledger=ledger,
        panel=pd.DataFrame(),
        ranked=[],
        live={"hypothesis_id": "reversal_5", "title": "5日反转"},
        daily_dir=daily,
        minute_dir=tmp_path / "minutes",
        network_news=False,
        conviction="live",
    )
    sells = [row for row in plan["intents"] if row.get("side") == "sell"]
    assert len(sells) == 1
    assert sells[0]["symbol"] == "600000"
    assert "拿了" in sells[0]["reason"]


def test_open_gap_abandons_chase_buy() -> None:
    from ai_trader.trader import execute_intents

    ledger, actions = execute_intents(
        empty_ledger(),
        as_of="2024-01-03",
        intents=[
            {
                "symbol": "600000",
                "side": "buy",
                "quantity": 100,
                "abandon_if_open_gap_pct": 5.0,
            }
        ],
        open_prices={"600000": 10.8},
        prev_closes={"600000": 10.0},
        live_id="reversal_5",
    )
    assert actions[0]["status"] == "abandoned"
    assert ledger["positions"] == []
    assert abs(float(ledger["cash"]) - 100000) < 1e-6
