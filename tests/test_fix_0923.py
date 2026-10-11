"""FIX-0923 factual statements, sell provenance, health truth and test collection."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from ai_trader import decision_log as dl
from ai_trader.ledger import empty_ledger
from ai_trader.overnight import build_plan
from quantdesk.maintenance.test_inventory import _runner_kind

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-09-22"


def daily(root: Path, symbol: str = "600000", previous: float = 10, close: float = 10) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{symbol}.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,updated_at\n"
        f"2026-09-21,{symbol},{previous},{previous},{previous},{previous},1000,10000,0,raw,fixture,2026-09-21T15:01:00\n"
        f"{DAY},{symbol},{close},{close},{close},{close},1000,10000,{(close / previous - 1) * 100},raw,fixture,{DAY}T15:01:00\n",
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("note", ["今天600000涨99%", "今天600000收盘999元", "今天浦发银行涨99%"])
@pytest.mark.parametrize("with_names", [False, True])
def test_f2_false_natural_market_claims_never_pass(tmp_path: Path, note: str, with_names: bool) -> None:
    folder = daily(tmp_path / "daily")
    names = {"浦发银行": "600000"} if with_names else None
    plan = {"decision_date": DAY, "llm": {"used": True, "market_note": note},
            "intents": [{"symbol": "600000", "side": "buy", "reason": "观望", "ref_close": 10}]}
    gate = dl.gate_plan_statements(plan, daily_dir=folder, name_to_symbol=names)
    assert gate.market_note_ok is False
    assert gate.accepted_intents == []
    hits = dl.verify_market_note(note, decision_date=DAY, daily_dir=folder, name_to_symbol=names)
    assert hits == gate.market_note_rejections
    assert hits == dl.verify_intent_against_daily(plan["intents"][0], decision_date=DAY,
                                                daily_dir=folder, market_note=note, name_to_symbol=names)
    if "浦发银行" in note and not with_names:
        assert gate.market_note_status == "未核验"
        assert hits[0]["evidence_status"] == "unverified"
    else:
        assert gate.market_note_status == "陈述与数据不符"
        assert hits[0]["actual"] == (10 if "收盘" in note else 0)


@pytest.mark.parametrize("note", ["600000涨99%", "收盘999元", "浦发银行涨99%", "观望", "", "明天600000涨99%"])
def test_f2_unverified_is_not_verified(tmp_path: Path, note: str) -> None:
    gate = dl.gate_plan_statements({"decision_date": DAY, "llm": {"used": True, "market_note": note},
                                   "intents": []}, daily_dir=daily(tmp_path))
    assert gate.market_note_ok is False
    assert dl.statement_gate_summary(gate)["market_note_status"] == "未核验"


def test_f2_known_facts_and_cross_symbol_prices(tmp_path: Path) -> None:
    daily(tmp_path, "600000", 10, 10)
    daily(tmp_path, "600001", 20, 20)
    note = "今天600000涨0%；今天600001收盘20元"
    gate = dl.gate_plan_statements({"decision_date": DAY, "llm": {"market_note": note}, "intents": []}, daily_dir=tmp_path)
    assert gate.market_note_ok and gate.market_note_status == "已核对"
    intent = {"symbol": "600000", "side": "buy", "ref_close": 10, "reason": note}
    assert dl.verify_intent_against_daily(intent, decision_date=DAY, daily_dir=tmp_path) == []
    assert dl.verify_market_note("今天浦发银行涨0%", decision_date=DAY, daily_dir=tmp_path,
                                 name_to_symbol={"浦发银行": "600000"}) == []


def test_f2_named_note_mapping_is_shared_by_empty_plan_and_records(tmp_path: Path) -> None:
    folder = daily(tmp_path)
    names = {"浦发银行": "600000"}
    plan = {"decision_date": DAY, "intents": [], "llm": {"used": True, "market_note": "今天浦发银行涨0%"}}
    gate = dl.apply_gate_to_plan(plan, daily_dir=folder, name_to_symbol=names)
    assert gate.market_note_ok
    records = dl.records_from_plan(plan, daily_dir=folder, rejections=gate.rejections, name_to_symbol=names)
    assert len(records) == 1 and records[0]["result"] == dl.RESULT_NA
    assert records[0]["result_evidence"] == []


def test_f3_old_entry_thesis_cannot_veto_a_correct_sell(tmp_path: Path) -> None:
    folder = daily(tmp_path / "daily", close=9.6)
    ledger = empty_ledger()
    ledger["positions"] = [{"symbol": "600000", "quantity": 100, "avg_cost": 10,
                             "acquired_on": "2026-09-15", "planned_hold_days": 250,
                             "style": "长期", "market_price": 9.6, "thesis": "今日涨2%"}]
    plan = build_plan(decision_date=DAY, trade_date="2026-09-23", ledger=ledger,
                      panel=pd.DataFrame(), ranked=[], live=None, daily_dir=folder,
                      minute_dir=tmp_path / "minutes", network_news=False, conviction="live",
                      llm_sells=[{"symbol": "600000", "reason": "今日跌4%，逻辑已破坏"}], llm_directed=True)
    plan["llm"] = {"used": True, "market_note": "观望"}
    sell = plan["intents"][0]
    assert sell["entry_thesis"] == {"text": "今日涨2%", "date": None}
    assert "今日涨2%" not in sell["thesis"]
    assert "今日跌4%" in sell["thesis"]
    # Even a legacy/stale thesis field must not be used as today's sell facts.
    sell["thesis"] = "今日涨2%"
    sell["llm_reason"] = "今日涨99%"
    gate = dl.gate_plan_statements(plan, daily_dir=folder)
    assert gate.accepted_intents == [sell] and gate.rejections == []
    record = dl.build_decision_record(sell, plan=plan, daily_dir=folder)
    assert record["judgment"] == sell["reason"]
    assert record["entry_thesis"] == sell["entry_thesis"]
    original_id = record["decision_id"]
    sell["entry_thesis"] = {"text": "其他历史背景", "date": "2026-09-14"}
    assert dl.build_decision_record(sell, plan=plan, daily_dir=folder)["decision_id"] == original_id
    sell["reason"] = "今日涨99%"
    assert dl.gate_plan_statements(plan, daily_dir=folder).accepted_intents == []


def health_module():
    spec = importlib.util.spec_from_file_location("health_fix_0923", ROOT / "scripts/ops/write_health_line.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def health_fixture(root: Path, default_used: bool, glm_used: bool) -> None:
    for label, used in (("ai_trader", default_used), ("ai_trader_glm", glm_used)):
        folder = root / "state" / label
        folder.mkdir(parents=True)
        for name, data in (("ledger.json", {"as_of": DAY, "trades": [{"date": DAY}]}),
                           ("journal_latest.json", {"as_of": DAY, "llm": {"used": used}})):
            (folder / name).write_text(json.dumps(data), encoding="utf-8")
    (root / "STATUS.md").write_text("# STATUS\n原文\n", encoding="utf-8")


@pytest.mark.parametrize("default_used,glm_used", [(True, True), (False, True), (True, False), (False, False)])
def test_f4_four_dry_run_scenarios(tmp_path: Path, capsys, default_used: bool, glm_used: bool) -> None:
    health_fixture(tmp_path, default_used, glm_used)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    assert health_module().main(["--root", str(tmp_path), "--target-date", DAY, "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert f"AI账 {'✓' if default_used and glm_used else '△'}2笔" in payload["line"]
    explanation = " · ".join(payload["explains"])
    for label, used in (("默认", default_used), ("glm", glm_used)):
        assert (f"{label} 规则回退" in explanation) is (not used)
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("journal", [None, {"as_of": "2026-09-21", "llm": {"used": True}}, {"as_of": DAY, "llm": {"used": "true"}}])
def test_f4_missing_stale_or_untyped_model_evidence_is_not_green(tmp_path: Path, journal) -> None:
    health_fixture(tmp_path, True, True)
    path = tmp_path / "state/ai_trader_glm/journal_latest.json"
    if journal is None:
        path.unlink()
    else:
        path.write_text(json.dumps(journal), encoding="utf-8")
    cell, explanation = health_module().ai_cell(tmp_path, DAY)
    assert cell == "AI账 △2笔"
    assert "glm 当日模型参与未核验" in explanation


def test_f4_status_keeps_all_explanations(tmp_path: Path, capsys) -> None:
    health_fixture(tmp_path, True, False)
    assert health_module().main(["--root", str(tmp_path), "--target-date", DAY]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["explains"]) > 1
    assert " · ".join(payload["explains"]) in (tmp_path / "STATUS.md").read_text(encoding="utf-8")


def test_f5_full_jobs_and_mixed_files_are_collected_by_pytest() -> None:
    config = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    for job in ("full_regression", "full_regression_scheduled"):
        assert config[job]["script"] == ["python -m pytest -q -p no:cacheprovider tests"]
    assert _runner_kind("class Tests(unittest.TestCase):\n def test_method(self): pass\ndef test_function(): pass\n") == "pytest"
    assert _runner_kind("class Tests(unittest.TestCase):\n def test_method(self): pass\n") == "unittest"
    for filename in ("test_core_logic.py", "test_round9s_stage4_final_evaluator.py"):
        assert _runner_kind((ROOT / "tests" / filename).read_text(encoding="utf-8")) == "pytest"
