"""B3 truth regressions: real source windows, metadata and complete plan copy."""
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from ai_trader import decision_log, llm, market, overnight
from ai_trader.ledger import empty_ledger

DAYS = ["2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]
CLOSES = [10, 10.1, 10.2, 10.5, 10.8, 11.37]


def daily(root: Path, days=DAYS, closes=CLOSES, pct_override=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    previous = closes[0]
    for index, (day, close) in enumerate(zip(days, closes)):
        rows.append(dict(date=day, symbol="920371", open=close, high=close, low=close,
                         close=close, volume=1000, amount=10000,
                         pct_chg=(pct_override or {}).get(index, (close / previous - 1) * 100),
                         adjust="raw", source="synthetic_b3_source", fetched_at=day + "T16:00:00+08:00"))
        previous = close
    path = root / "920371.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def context(root: Path):
    panel, _, _ = market.load_close_panel(["920371"], root)
    return overnight._build_llm_context(as_of=DAYS[-1], trade_date="2026-09-11",
        ledger=empty_ledger(), panel=panel, recommended=[], research={"evaluated": []}, daily_dir=root), panel


def test_metadata_reaches_real_prompt_without_changing_the_system(tmp_path):
    path = daily(tmp_path)
    before = path.read_bytes()
    ctx, _ = context(tmp_path)
    fact = ctx["candidate_facts"][0]
    assert fact["change"] == pytest.approx(.137)
    assert fact["evidence_ref"]["sha256"]
    assert fact["source"] == "synthetic_b3_source"
    system, user = llm._build_prompt(ctx)
    old_system, _ = llm._build_prompt({**ctx, "candidate_facts": []})
    assert system == old_system
    assert fact["start"] in user and fact["end"] in user
    assert fact["source"] in user and fact["evidence_ref"]["sha256"] in user
    assert before == path.read_bytes()


def test_missing_session_keeps_observed_value_but_never_calls_it_five_sessions(tmp_path):
    daily(tmp_path, days=["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-09", "2026-09-10"])
    ctx, _ = context(tmp_path)
    fact = ctx["candidate_facts"][0]
    assert fact["change"] == pytest.approx(.137)
    assert fact["missing_sessions"] == ["2026-09-08"]
    assert fact["period_label"] != "近5交易日"
    assert fact["observed_intervals"] == 5 and fact["sessions"] == 6
    assert fact["validation_status"] == "diagnostic_only"
    assert decision_log.verify_market_note("近5交易日920371+13.7%", decision_date=DAYS[-1], daily_dir=tmp_path)


def test_repaired_research_value_is_not_relabelled_raw(tmp_path):
    path = daily(tmp_path, closes=[10, 10.1, 5.1, 5.25, 5.4, 5.685],
                 pct_override={2: (10.2 / 10.1 - 1) * 100})
    original = path.read_bytes()
    ctx, panel = context(tmp_path)
    fact = ctx["candidate_facts"][0]
    assert fact["change"] == pytest.approx(.137)
    assert fact["original_price_basis"] == "raw"
    assert fact["price_basis"] != "raw"
    assert fact["calculation_basis"] == "corporate_action_repaired_research_proxy"
    assert fact["validation_status"] == "diagnostic_only"
    assert panel.iloc[0, 0] == pytest.approx(5.0)
    assert path.read_bytes() == original
    hits = decision_log.verify_market_note("近5交易日920371+13.7%", decision_date=DAYS[-1], daily_dir=tmp_path)
    assert hits[0]["actual"] == pytest.approx(-43.15)


def test_source_version_change_is_visible_in_context(tmp_path):
    daily(tmp_path)
    _, panel = context(tmp_path)
    daily(tmp_path, closes=CLOSES[:-1] + [12.0])
    ctx = overnight._build_llm_context(as_of=DAYS[-1], trade_date="2026-09-11",
        ledger=empty_ledger(), panel=panel, recommended=[], research={"evaluated": []}, daily_dir=tmp_path)
    assert "source_version_unverified" in ctx["candidate_facts"][0]["warnings"]


def test_summary_keeps_no_intent_and_unverified_separate(tmp_path):
    daily(tmp_path)
    no_intents = {"decision_date": DAYS[-1], "intents": [],
                  "llm": {"used": True, "market_note": "近5交易日920371+99%"}}
    result = decision_log.apply_gate_to_plan(no_intents, daily_dir=tmp_path)
    headline = overnight.llm_journal_headline("2026-09-11", proposed=[],
        committed_buy_symbols=[], plan_committed=True, held_count=8,
        gate_rejected=bool(result.rejections), statement_review=no_intents["statement_review"])
    assert "买入" not in headline and "盘面陈述" in headline
    assert no_intents["statement_review"]["original_intent_count"] == 0
    # 2AI-1：点评只标注，所以把核不了的说法放进这笔买入自己的理由里。
    plan = {"decision_date": DAYS[-1], "intents": [{"symbol": "920371", "side": "buy", "ref_close": 11.37,
                                                     "llm_reason": "920371+13.7%"}],
            "llm": {"used": True, "market_note": "观望"}}
    original = deepcopy(plan["intents"])
    result = decision_log.apply_gate_to_plan(plan, daily_dir=tmp_path)
    assert plan["statement_review"]["original_intents"] == original
    assert plan["statement_review"]["status"] == "unverified"
    headline = overnight.llm_journal_headline("2026-09-11", proposed=["920371"],
        committed_buy_symbols=[], plan_committed=True,
        gate_rejected=bool(result.rejections), statement_review=plan["statement_review"])
    assert "事实不符" not in headline and "未核验" in headline


def test_sell_only_and_mixed_plans_describe_waiting_intents():
    headline = overnight.llm_journal_headline("2026-09-11", proposed=[],
        committed_buy_symbols=[], committed_sell_symbols=["600000"],
        plan_committed=True, held_count=8)
    assert "提交卖出 600000" in headline
    assert "无新增交易" not in headline and "尚未成交" in headline
    mixed = overnight.llm_journal_headline("2026-09-11", proposed=["920371"],
        committed_buy_symbols=["920371"], committed_sell_symbols=["600000"],
        plan_committed=True, held_count=8)
    assert "提交买入 920371" in mixed and "提交卖出 600000" in mixed

