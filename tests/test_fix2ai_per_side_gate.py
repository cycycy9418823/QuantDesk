"""2AI-1: the statement gate is per side.

Owner rule 2: an unverifiable buy does not fill; a sell always passes, labelled when
its own reason carries an unverified or wrong claim; the market note is only labelled.

Before the fix, gate_plan_statements merged every market-note hit into every intent
of an LLM night, so one bad number in the note blocked all buys and all sells, and a
sell with a wrong number in its own reason was blocked too.
"""

from __future__ import annotations

from pathlib import Path

from ai_trader import decision_log as dl

DAY = "2026-09-04"


def _daily(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for symbol in ("600000", "600001"):
        (root / f"{symbol}.csv").write_text(
            "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
            f"2026-09-03,{symbol},10,10,10,10,1000,10000,0,raw\n"
            f"{DAY},{symbol},10,10.04,10,10.04,1000,10000,0.4,raw\n",
            encoding="utf-8",
        )
    return root


def _plan(note: str, *, buy_reason: str, sell_reason: str) -> dict:
    return {
        "decision_date": DAY,
        "trade_date": "2026-09-07",
        "llm": {"used": True, "model": "m", "market_note": note},
        "intents": [
            {"symbol": "600001", "side": "buy", "ref_close": 10.04, "llm_reason": buy_reason,
             "reason": buy_reason, "planned_hold_days": 5},
            {"symbol": "600000", "side": "sell", "ref_close": 10.04, "reason": sell_reason,
             "planned_hold_days": 5},
        ],
    }


def test_wrong_number_in_market_note_blocks_neither_buy_nor_sell(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = _plan("600000今天涨40%，情绪过热", buy_reason="蓝筹底仓，估值不高", sell_reason="不想拿了")
    gate = dl.apply_gate_to_plan(plan, daily_dir=daily)
    assert [(row["symbol"], row["side"]) for row in plan["intents"]] == [("600001", "buy"), ("600000", "sell")]
    assert gate.rejections == []
    # The note is still labelled, never silently "checked".
    assert gate.market_note_rejections
    assert plan["statement_review"]["market_note_status"] == "陈述与数据不符"
    assert all(not row.get("statement_flags") for row in plan["intents"])


def test_buy_with_its_own_wrong_claim_is_blocked_sell_passes_with_label(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = _plan("", buy_reason="今天涨40%", sell_reason="今天收盘18.88")
    gate = dl.apply_gate_to_plan(plan, daily_dir=daily)
    assert [(row["symbol"], row["side"]) for row in plan["intents"]] == [("600000", "sell")]
    sell = plan["intents"][0]
    assert sell["statement_label"] == dl.STATEMENT_FLAG_MISMATCH
    assert sell["statement_flags"]
    assert [(row["symbol"], row["side"]) for row in gate.rejections] == [("600001", "buy")]
    assert gate.rejections[0]["statement_rejections"]
    review = plan["statement_review"]
    assert review["accepted_intent_count"] == 1
    assert review["rejected_intent_count"] == 1
    assert review["flagged_intents"] == [
        {"symbol": "600000", "side": "sell", "label": dl.STATEMENT_FLAG_MISMATCH}
    ]


def test_flagged_sell_record_is_committed_and_carries_the_label(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = _plan("", buy_reason="蓝筹底仓", sell_reason="今天收盘18.88")
    gate = dl.apply_gate_to_plan(plan, daily_dir=daily)
    records = dl.records_from_plan(plan, daily_dir=daily, rejections=gate.rejections)
    sell = next(row for row in records if row["side"] == "sell")
    assert sell["result"] == dl.RESULT_PENDING
    assert sell["committed"] is True
    assert sell["statement_label"] == dl.STATEMENT_FLAG_MISMATCH
    buy = next(row for row in records if row["side"] == "buy")
    assert buy["statement_label"] == ""


def test_hold_night_with_wrong_note_is_still_recorded_as_mismatch(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = {"decision_date": DAY, "trade_date": "2026-09-07", "intents": [],
            "llm": {"used": True, "market_note": "600000今天涨40%"}}
    gate = dl.apply_gate_to_plan(plan, daily_dir=daily)
    records = dl.records_from_plan(plan, daily_dir=daily, rejections=gate.rejections)
    assert [row["side"] for row in records] == ["hold"]
    assert records[0]["result"] == dl.RESULT_MISMATCH
