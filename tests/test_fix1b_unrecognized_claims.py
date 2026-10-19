"""1B-2: a number-bearing claim the recognisers cannot parse is "未核验", never "checked".

Before the fix these sentences matched no regex, produced no hit, and the buy
went through as if its statement had been verified against data/daily.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_trader import decision_log as dl

DAY = "2026-09-04"


def _daily(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "600000.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust\n"
        "2026-09-03,600000,10,10,10,10,1000,10000,0,raw\n"
        f"{DAY},600000,10,10.04,10,10.04,1000,10000,0.4,raw\n",
        encoding="utf-8",
    )
    return root


def _gate(daily: Path, reason: str, side: str = "buy"):
    plan = {"decision_date": DAY,
            "intents": [{"symbol": "600000", "side": side, "ref_close": 10.04, "llm_reason": reason,
                         "reason": reason}]}
    return plan, dl.apply_gate_to_plan(plan, daily_dir=daily)


@pytest.mark.parametrize("reason", [
    "今天涨了5%",          # 了 between verb and number
    "今日涨幅达5%",        # 达 between verb and number
    "今天涨５％",          # full-width digit and percent
    "收盘价：12.30元",     # full-width colon
    "今日收于12.30元",     # 收于 is not a recognised close verb
    "920627一周68%",       # period word between code and number
])
def test_unparseable_number_claim_is_unverified_and_blocks_the_buy(tmp_path: Path, reason: str) -> None:
    plan, gate = _gate(_daily(tmp_path / "daily"), reason)
    assert plan["intents"] == [], f"{reason!r} must not pass as checked"
    hits = gate.rejections[0]["statement_rejections"]
    assert hits and all(hit["evidence_status"] == "unverified" for hit in hits), hits
    assert plan["statement_review"]["status"] == "unverified"


@pytest.mark.parametrize("reason", [
    "今天涨0.4%",                       # recognised and correct
    "预计明天涨5%",                     # forecast, not a fact
    "打算拿5个交易日，高开超5%放弃",     # plan condition, not a fact
    "目标价15元",                       # target, not a fact
    "蓝筹底仓，估值不高",               # no number
])
def test_checked_or_non_factual_text_still_passes(tmp_path: Path, reason: str) -> None:
    plan, gate = _gate(_daily(tmp_path / "daily"), reason)
    assert len(plan["intents"]) == 1, gate.rejections
    assert gate.rejections == []


def test_news_headline_appended_to_reason_is_not_checked_as_ai_words(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = {"decision_date": DAY,
            "intents": [{"symbol": "600000", "side": "buy", "ref_close": 10.04,
                         "reason": "今晚看上这只。消息：公司公告净利润增长35%。"}]}
    dl.apply_gate_to_plan(plan, daily_dir=daily)
    assert len(plan["intents"]) == 1


def test_hold_night_with_unparseable_note_is_unverified_not_na(tmp_path: Path) -> None:
    daily = _daily(tmp_path / "daily")
    plan = {"decision_date": DAY, "trade_date": "2026-09-07", "intents": [],
            "llm": {"used": True, "market_note": "920627一周68%，情绪过热"}}
    gate = dl.apply_gate_to_plan(plan, daily_dir=daily)
    records = dl.records_from_plan(plan, daily_dir=daily, rejections=gate.rejections)
    assert records[0]["result"] == dl.RESULT_UNVERIFIED, records[0]["result"]
