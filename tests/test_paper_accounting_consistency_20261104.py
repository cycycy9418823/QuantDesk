"""Accounting transitions must preserve real shares, sellability and cash."""
from __future__ import annotations

import copy
import json

import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper.adjustment_policy import AdjustmentFactorTable, apply_paper_adjustment_policy, apply_suspension_valuation_policy
from quantdesk.paper.position_management_engine import normalize_position_for_t1
from quantdesk.paper import position_management_engine as engine
from quantdesk.paper.ledger_read_contract import strategy_ledger_health
from quantdesk.product_integrity import position_cost_evidence
from tests.test_codex_audit_20261020_ledger import TARGET, isolated, state_with_position, dump, daily, update


def position() -> dict:
    return dict(symbol="600000", strategy_type="long", quantity=100, entry_price=10.0,
                avg_cost=10.0, entry_date="2026-10-13", latest_price=10.0, latest_date="2026-10-13")


def factors() -> AdjustmentFactorTable:
    return AdjustmentFactorTable(rows_by_symbol={"600000": [(20261013, 1.0), (20261019, 2.0)]})


def test_same_day_add_keeps_old_sellable_lot_and_thaws_only_next_day():
    row = dict(position(), quantity=200, sellable_quantity=100, frozen_quantity=100, last_buy_date=TARGET)
    original = copy.deepcopy(row)
    same = normalize_position_for_t1(row, TARGET)
    assert (same["sellable_quantity"], same["frozen_quantity"]) == (100, 100)
    following = normalize_position_for_t1(same, "2026-10-20")
    assert (following["sellable_quantity"], following["frozen_quantity"]) == (200, 0)
    assert row == original


@pytest.mark.parametrize("changes", [
    {"entry_date": TARGET}, {"last_buy_date": "2026-10-20"},
    {"last_buy_date": "not-a-date"}, {"entry_date": "bad"},
    {"sellable_quantity": None}, {"frozen_quantity": None},
    {"sellable_quantity": 200}, {"frozen_quantity": -1},
    {"sellable_quantity": 99.5, "frozen_quantity": 100.5},
    {"entry_date": "", "last_buy_date": ""},
])
def test_unknown_or_inconsistent_same_day_lots_never_sell(changes):
    row = dict(position(), quantity=200, sellable_quantity=100, frozen_quantity=100, last_buy_date=TARGET)
    row.update(changes)
    normalized = normalize_position_for_t1(row, TARGET)
    assert normalized["sellable_quantity"] == 0
    assert normalized["frozen_quantity"] == 200
    assert normalized["t1_status"] != "verified_sellable"


def test_daily_stop_sells_only_old_lot_and_reconciles_partial_profit(isolated):
    root = isolated
    state = state_with_position(root)
    state["positions"][0].update(quantity=200, sellable_quantity=100, frozen_quantity=100,
        last_buy_date=TARGET, avg_cost=5.5427, market_value=1172.0, unrealized_pnl=63.46)
    state.update(cash=98891.46, last_trade_date=TARGET)
    state = paper.reconcile_paper_account(state)
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, state)
    daily(root, price=5.05)
    before = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    result = update()
    sold = [a for a in result["actions"] if a["action"] == "sell"]
    assert len(sold) == 1 and sold[0]["quantity"] == 100
    assert result["closed_positions"] == []
    rest = result["positions"][0]
    assert (rest["quantity"], rest["sellable_quantity"], rest["frozen_quantity"]) == (100, 0, 100)
    assert rest["avg_cost"] == 5.5427
    reduction = result["realized_reductions"][0]
    assert reduction["quantity"] == reduction["exit_quantity"] == 100
    assert reduction["status"] == "reduced"
    summary = result["summary"]
    assert summary["cash"] == pytest.approx(state["cash"] + sold[0]["net_amount"])
    assert summary["total_equity"] == pytest.approx(summary["cash"] + 505)
    assert summary["realized_pnl"] + summary["unrealized_pnl"] == pytest.approx(summary["total_equity"] - state["initial_cash"], abs=.01)
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == before
    # Reusing the actual resulting position on the same day cannot sell the frozen remainder.
    next_state = {**state, "positions": result["positions"], "realized_reductions": result["realized_reductions"],
                  "cash": summary["cash"], "closed_positions": result["closed_positions"]}
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, paper.reconcile_paper_account(next_state))
    again = update()
    assert not [a for a in again["actions"] if a["action"] == "sell"]
    assert again["positions"][0]["quantity"] == 100


def test_factor_is_diagnostic_not_shares_or_equity_and_exit_only_costs_fees():
    original = position()
    marked = apply_paper_adjustment_policy(original, latest_price=5.0, latest_date=TARGET, factors=factors())
    assert marked["quantity"] == 100 and marked["avg_cost"] == 10
    assert marked["market_value"] == 500 and marked["unrealized_pnl"] == -500
    proxy = marked["factor_research_proxy"]
    assert proxy["factor_ratio"] == 2
    assert proxy["included_in_equity"] is False and proxy["executable_quantity"] is False
    assert marked["price_adjustment_warning"]
    sold = paper._close_position(marked, 5.0, TARGET, "fixture exit")
    assert marked["market_value"] - sold["net_amount"] == pytest.approx(sold["total_fee"])
    assert sold["realized_pnl"] == pytest.approx(marked["unrealized_pnl"] - sold["total_fee"])
    assert original == position()


@pytest.mark.parametrize("later_factors,later_date", [(None,TARGET),(factors(),"2026-10-20")])
def test_new_mark_discards_old_factor_derived_balances(later_factors,later_date):
    old = dict(position(), price_adjustment_status="adjusted", adjusted_quantity_for_valuation=200,
               adjusted_avg_cost=5, adjustment_factor_ratio=2, market_value=1000,
               factor_research_proxy={"factor_ratio": 2})
    marked = apply_paper_adjustment_policy(old, latest_price=5, latest_date=later_date, factors=later_factors)
    assert marked["market_value"] == 500
    for key in ("adjusted_quantity_for_valuation","adjusted_avg_cost","adjustment_factor_ratio","factor_research_proxy"):
        assert key not in marked
    assert old["adjusted_quantity_for_valuation"] == 200


@pytest.mark.parametrize("date,basis,expected_resumed", [(TARGET,"raw",True),("2026-10-14","raw",False),(TARGET,"unknown",False)])
def test_suspension_cleared_only_by_current_trusted_quote(date,basis,expected_resumed):
    old = apply_suspension_valuation_policy(position(), {"suspended": True, "price":10})
    row = paper._mark_position(old,10.2,TARGET,"fixture",quote={"price":10.2,"date":date,
        "price_basis":basis,"bar_valid":True,"volume":1000,"suspended":False,"source":"fixture"})
    if expected_resumed:
        assert row["valuation_status"] == "normal"
        assert row["tradeability_status"] == "tradable" and row["can_sell"] is True
        assert "valuation_warning" not in row
    else:
        assert row["can_sell"] is False
        assert row["valuation_status"] != "normal"


@pytest.mark.parametrize("total,sellable,allowed", [(300,100,False),(400,200,True),(199,199,True),(401,201,True)])
def test_daily_exit_retains_board_rules(total,sellable,allowed):
    row = dict(position(), symbol="688001", quantity=total, sellable_quantity=sellable,
               frozen_quantity=total-sellable, last_buy_date=TARGET if total>sellable else "2026-10-13")
    if not allowed:
        with pytest.raises(ValueError, match="qty_not_valid_for_board"):
            paper._sell_available_position(row,9,TARGET,"fixture")
    else:
        action, sold, rest = paper._sell_available_position(row,9,TARGET,"fixture")
        assert action["quantity"] == sold["exit_quantity"] == sellable
        assert (rest["quantity"] if rest else 0) == total-sellable


def test_illegal_star_partial_does_not_abort_other_daily_exit(isolated):
    state = state_with_position(isolated)
    star = {**state["positions"][0], "symbol":"688001", "quantity":300,
            "sellable_quantity":100, "frozen_quantity":200, "last_buy_date":TARGET,
            "market_value":1758, "unrealized_pnl":round((5.86-5.5427)*300,2)}
    state["positions"].append(star)
    state["cash"] -= 5.5427*300
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON,paper.reconcile_paper_account(state))
    daily(isolated,price=5.05)
    path = isolated/"data/daily/600121.csv"
    (path.parent/"688001.csv").write_text(path.read_text().replace("600121","688001"))
    result = update()
    assert result["ok"]
    assert [a["symbol"] for a in result["actions"] if a["action"] == "sell"] == ["600121"]
    assert any(a["action"]=="block" and "qty_not_valid_for_board" in a["reason"] for a in result["actions"])
    assert result["positions"][0]["quantity"] == 300


@pytest.mark.parametrize("restriction", ["delisted_no_price_pending_manual", "manual_review", "stale"])
def test_fresh_quote_does_not_unlock_unrelated_restrictions(restriction):
    old = dict(position(),valuation_date=TARGET,valuation_status=restriction,
               tradeability_status="blocked",tradable=False,can_sell=False,valuation_warning=restriction)
    result = apply_suspension_valuation_policy(old,{"price":10.2,"date":TARGET,
        "price_basis":"raw","bar_valid":True,"volume":1000})
    assert result == old


@pytest.mark.parametrize("table,status", [(factors(),"factor_reference_only"),
    (AdjustmentFactorTable(rows_by_symbol={"600000":[(20261013,1.)]}),"stale_adj_factor"),
    (None,"missing_adj_factor")])
def test_conditional_consumer_replaces_legacy_derived_fields(tmp_path,table,status):
    old = dict(position(),adjusted_quantity_for_valuation=200,adjusted_avg_cost=5,
               adjustment_factor_ratio=2,price_adjustment_status="adjusted")
    state = dict(initial_cash=10000,cash=9000,positions=[old],closed_positions=[],realized_reductions=[])
    (tmp_path/"600000.csv").write_text("date,open,high,low,close,volume,adjust\n2026-10-19,5,5,5,5,1000,raw\n")
    result = engine.apply_conditional_execution_to_state(state,{"plan_id":"no-event","trade_date":TARGET,"events":[]},
        daily_dir=tmp_path,adjustment_factors=table,risk_gate_shadow_enabled=False,risk_gate_shadow_write_errors=False)
    marked = result["state"]["positions"][0]
    assert marked["market_value"] == 500 and marked["unrealized_pnl"] == -500
    assert marked["price_adjustment_status"] == status
    assert marked["price_adjustment_warning"] in marked["valuation_warnings"]
    assert not {"adjusted_quantity_for_valuation","adjusted_avg_cost","adjustment_factor_ratio"}.intersection(marked)
    assert old["adjusted_quantity_for_valuation"] == 200


@pytest.mark.parametrize("status,new_policy,shares,cost", [
    ("adjusted",True,100,10),("adjusted",False,200,5),
    ("stale_adj_factor",False,100,10),("missing_adj_factor",False,100,10)])
def test_readers_agree_on_quantity_policy_without_rewriting(status,new_policy,shares,cost):
    row = dict(position(),latest_date=TARGET,latest_price=5,market_value=shares*5,
               unrealized_pnl=(5-cost)*shares,price_adjustment_status=status,
               adjusted_quantity_for_valuation=200,adjusted_avg_cost=5)
    if new_policy:
        row["valuation_quantity_basis"] = "recorded_shares"
    before = copy.deepcopy(row)
    state = dict(initial_cash=10000,cash=9000,positions=[row],closed_positions=[],
                 market_value=row["market_value"],total_equity=9000+row["market_value"],valuation_date=TARGET)
    assert strategy_ledger_health(state,target_date=TARGET)["ok"]
    evidence = position_cost_evidence(row)
    assert evidence["status"] == "consistent" and evidence["valuation_quantity"] == shares
    assert row == before
    row["market_value"] += 1
    assert not strategy_ledger_health(state,target_date=TARGET)["ok"]
    row["unrealized_pnl"] += 1
    assert position_cost_evidence(row)["status"] == "mismatch"


def test_daily_partial_exit_persists_and_rerun_never_duplicates(isolated):
    state = state_with_position(isolated)
    state["positions"][0].update(quantity=200,sellable_quantity=100,frozen_quantity=100,
        last_buy_date=TARGET,avg_cost=5.5427,market_value=1172,unrealized_pnl=63.46)
    state.update(cash=98891.46,last_trade_date=TARGET)
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON,paper.reconcile_paper_account(state))
    daily(isolated,price=5.05)
    result = paper.run_auto_paper_portfolio(mode="update",initial_cash=100000,plan_date=TARGET,
        review_target_date=TARGET,risk_gate_shadow_enabled=False,dry_run=False)
    assert result["ok"]
    saved = json.loads(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_text(encoding="utf-8"))
    loaded = paper._load_state(100000)
    assert len(loaded["realized_reductions"]) == len(saved["realized_reductions"]) == 1
    assert loaded["positions"][0]["quantity"] == 100
    again = paper.run_auto_paper_portfolio(mode="update",initial_cash=100000,plan_date=TARGET,
        review_target_date=TARGET,risk_gate_shadow_enabled=False,dry_run=False)
    assert again["ok"]
    reloaded = paper._load_state(100000)
    assert reloaded["cash"] == loaded["cash"]
    assert reloaded["realized_reductions"] == loaded["realized_reductions"]
    assert reloaded["realized_pnl"] == loaded["realized_pnl"]


def test_conditional_add_then_separate_exit_sells_old_lots_once(tmp_path):
    from tests.test_paper_account_20260928 import (
        _holding_state,_timed_market,_buy_plan,_plan,_apply_timed,PARTIAL_DAY,SYMBOL)
    intraday,daily_dir,window = _timed_market(tmp_path,PARTIAL_DAY,{SYMBOL:[("09:31:00",10.)]})
    add = _buy_plan(SYMBOL,PARTIAL_DAY,10.)
    add.update(position_state="holding",current_quantity=200,sellable_quantity=200)
    add["rules"]["add_rules"] = add["rules"].pop("entry_rules")
    add["rules"]["add_rules"][0]["action"] = "add"
    def simulate(plan_id,item):
        result = engine.simulate_conditional_plan_file({"plan_id":plan_id,"trade_date":PARTIAL_DAY,
            "plan_date":"2026-09-21","symbols":[item]},intraday_dir=intraday,
            window_snapshot_path=window,include_risk_overlays=False,write=False)
        assert result["ok"] and len(result["events"]) == 1
        return result
    original = _holding_state(8000.)
    first = _apply_timed(original,simulate("first-add",add),daily_dir)["state"]
    second_execution = simulate("second-add",add)
    second = _apply_timed(first,second_execution,daily_dir)["state"]
    assert (second["positions"][0]["quantity"],second["positions"][0]["sellable_quantity"],
            second["positions"][0]["frozen_quantity"]) == (400,200,200)
    assert _apply_timed(second,second_execution,daily_dir)["already_updated_today"]
    exit_execution = simulate("exit-old",_plan(PARTIAL_DAY,"sell_all",10.,400))
    sold = _apply_timed(second,exit_execution,daily_dir)["state"]
    assert (sold["positions"][0]["quantity"],sold["positions"][0]["sellable_quantity"],
            sold["positions"][0]["frozen_quantity"]) == (200,0,200)
    assert sold["positions"][0]["avg_cost"] == second["positions"][0]["avg_cost"]
    assert len(sold["realized_reductions"]) == 1 and not sold["closed_positions"]
    repeated = _apply_timed(sold,exit_execution,daily_dir)
    assert repeated["already_updated_today"] and repeated["state"]["cash"] == sold["cash"]
    assert sold["realized_pnl"]+sold["unrealized_pnl"] == pytest.approx(sold["total_equity"]-10000,abs=.03)


def test_next_open_fixed_sell_rejects_partly_frozen_inherited_position(tmp_path,monkeypatch):
    from tests.test_snapshot_open_fill_20261012 import (
        setup_book,daily as snapshot_daily,intent,write_snapshot,source_row,run_day)
    from quantdesk.paper import strategy_fill_journal as journal
    setup_book(tmp_path,monkeypatch)
    snapshot_daily(tmp_path,"600000",{"2026-10-12":10,"2026-10-13":10})
    held = paper._make_position({"symbol":"600000","name":"浦发银行","strategy_type":"long"},200,10,"2026-10-08")
    held.update(last_buy_date="2026-10-13",sellable_quantity=100,frozen_quantity=100)
    order = intent("sell",quantity=200)
    seed = paper._empty_state(10000)
    seed.update(paper.policy_header(paper.POLICY_NEXT_OPEN))
    seed.update(positions=[held],cash=10000-held["entry_cash_out"],pending_orders=[order],
        strategy_fill_mode="next_open_snapshot",fill_policy_scope="future_fills_since_opening_checkpoint",
        snapshot_decision_date="2026-10-12",updated_at="2026-10-13T01:00:00+00:00")
    seed = paper.reconcile_paper_account(seed)
    path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON
    journal_path = path.with_suffix(".fills.jsonl")
    # Inherited history is explicitly unverified; the next-open route does not
    # itself support additions. Seal the fixture rather than altering its journal.
    committed = journal.commit_transaction(journal_path,before=seed,after=copy.deepcopy(seed),events=[],
        transaction_id="fixture:inherited-partly-frozen",recorded_at=seed["updated_at"],
        result={"fixture_only":True},ledger_existed=True)
    dump(path,committed["state"])
    assert journal.replay_journal(journal_path)["records"][0]["inherited_history_status"] == "legacy_opening_balance_unverified"
    write_snapshot(tmp_path/"data/snapshots",rows=[source_row()])
    result = run_day(tmp_path,monkeypatch,"2026-10-13")
    assert result["ok"], result
    assert len(result["snapshot_outcomes"]) == 1
    outcome = result["snapshot_outcomes"][0]
    assert outcome["order"] == order and outcome["order"]["quantity"] == 200
    assert outcome["status"] == "rejected" and outcome["reason_code"] == "t_plus_one_sell_blocked"
    assert outcome["quantity"] == 0 and outcome["fill_price"] is None and outcome["amounts"] == {}
    assert result["summary"]["cash"] == seed["cash"]
    assert result["positions"][0]["quantity"] == 200
    assert not result["closed_positions"] and not result["pending_orders"]
    saved_state,saved_journal = path.read_bytes(),journal_path.read_bytes()
    assert journal.replay_journal(journal_path)["state"] == json.loads(saved_state)
    rerun = run_day(tmp_path,monkeypatch,"2026-10-13")
    assert rerun["ok"] and rerun["idempotent_replay"]
    assert path.read_bytes() == saved_state and journal_path.read_bytes() == saved_journal
