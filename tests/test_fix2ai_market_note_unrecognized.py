"""2AI-1: re-applied 0c7ad90 (unrecognised number claims are "未核验") on the per-side gate.

The reason 0c7ad90 was backed out: on 10-09 kimi's market note said 「920627一周68%」.
The catch-all marks that "未核验", and the old gate merged market-note hits into every
intent, so one sentence of commentary blocked every buy and every sell of the night.
With the per-side gate the note is only labelled; each intent answers for its own words.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_trader import decision_log as dl
from ai_trader.llm import propose_picks

DAY = "2026-10-09"
NEXT = "2026-10-12"

REPLY = json.dumps({
    "stance": {"summary": "波段为主", "preferred_styles": ["波段"], "reasoning": "x"},
    "buys": [{"symbol": "600001", "reason": "蓝筹底仓，估值不高", "style": "波段", "hold_days": 10,
              "conviction": "live"}],
    "sells": [{"symbol": "600000", "reason": "逻辑变了，先走"}],
    "avoid": [],
    "lesson": "",
    "market_note": "小盘情绪过热，920627一周68%，追高要小心",
}, ensure_ascii=False)


def _daily(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for symbol in ("600000", "600001"):
        (root / f"{symbol}.csv").write_text(
            "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
            f"2026-10-08,{symbol},10,10,10,10,1000,10000,0,raw\n"
            f"{DAY},{symbol},10,10.04,10,10.04,1000,10000,0.4,raw\n",
            encoding="utf-8",
        )
    return root


def _plan_from_reply(buy_reason: str | None = None, sell_reason: str | None = None) -> dict:
    proposal = propose_picks({}, {}, reuse_text=REPLY, reuse_model="kimi")
    assert proposal is not None
    buy, sell = proposal["buys"][0], proposal["sells"][0]
    buy_reason = buy["reason"] if buy_reason is None else buy_reason
    sell_reason = sell["reason"] if sell_reason is None else sell_reason
    return {
        "decision_date": DAY,
        "trade_date": NEXT,
        "llm": {"used": True, "model": "kimi", "market_note": proposal["market_note"]},
        "intents": [
            {"symbol": buy["symbol"], "side": "buy", "ref_close": 10.04, "llm_reason": buy_reason,
             "reason": buy_reason, "style": buy["style"], "planned_hold_days": buy["hold_days"]},
            {"symbol": sell["symbol"], "side": "sell", "ref_close": 10.04, "reason": sell_reason,
             "planned_hold_days": 5},
        ],
    }


def test_unrecognised_number_in_market_note_no_longer_blocks_the_night(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = _plan_from_reply()
    assert "920627一周68%" in plan["llm"]["market_note"]
    gate = dl.apply_gate_to_plan(plan, daily_dir=daily)
    assert [(row["symbol"], row["side"]) for row in plan["intents"]] == [("600001", "buy"), ("600000", "sell")]
    assert gate.rejections == []
    # The commentary is labelled "未核验", never passed off as checked.
    assert plan["statement_review"]["market_note_status"] == dl.RESULT_UNVERIFIED
    assert [hit["kind"] for hit in gate.market_note_rejections] == ["claimed_unrecognized"]
    records = dl.records_from_plan(plan, daily_dir=daily, rejections=gate.rejections)
    assert sorted((row["side"], row["result"]) for row in records) == [
        ("buy", dl.RESULT_PENDING), ("sell", dl.RESULT_PENDING)]
    assert all(row["committed"] for row in records)


def test_same_claim_in_an_intents_own_reason_blocks_the_buy_and_labels_the_sell(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = _plan_from_reply(buy_reason="920627一周68%，跟一把", sell_reason="920627一周68%，换股")
    gate = dl.apply_gate_to_plan(plan, daily_dir=daily)
    assert [(row["symbol"], row["side"]) for row in plan["intents"]] == [("600000", "sell")]
    assert plan["intents"][0]["statement_label"] == dl.STATEMENT_FLAG_UNVERIFIED
    assert [(row["symbol"], row["side"]) for row in gate.rejections] == [("600001", "buy")]
    hits = gate.rejections[0]["statement_rejections"]
    assert hits and all(hit["evidence_status"] == "unverified" for hit in hits)
    records = dl.records_from_plan(plan, daily_dir=daily, rejections=gate.rejections)
    by_side = {row["side"]: row for row in records}
    assert by_side["buy"]["result"] == dl.RESULT_UNVERIFIED and by_side["buy"]["committed"] is False
    assert by_side["sell"]["result"] == dl.RESULT_PENDING and by_side["sell"]["committed"] is True
    assert by_side["sell"]["statement_label"] == dl.STATEMENT_FLAG_UNVERIFIED
