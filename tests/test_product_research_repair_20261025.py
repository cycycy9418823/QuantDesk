from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

import pandas as pd
import pytest

from quantdesk.board_rules import classify_board
from quantdesk.product_integrity import build_product_integrity, cost_basis_conflict_evidence, history_coverage, holding_period_evidence, position_cost_evidence, weekly_learning_review
from quantdesk.stock_workbench import build_stock_reference, trade_economics
from quantdesk.strategy_signals import board_signal_from_row, limit_rate_for_symbol


FEES = {"commission_rate": 0.00025, "min_commission": 5, "stamp_duty_rate": 0.0005,
        "transfer_fee_rate": 0, "slippage_bps": 5, "source": "test.fixture"}


@pytest.mark.parametrize("symbol,day", [("600000", "2026-10-23"), ("300001", "2026-10-23"),
    ("300001", "2020-08-21"), ("688001", "2026-10-23"), ("920906", "2026-10-23"), ("830001", "2026-10-23")])
def test_signal_rate_uses_the_shared_board_and_effective_date(symbol, day):
    assert limit_rate_for_symbol(symbol, trade_date=day) == classify_board(symbol, trade_date=day).limit_up_pct / 100


def test_bse_ten_percent_is_not_near_thirty_percent_limit():
    row = {"date": "2026-10-23", "prev_close": 10, "close": 11, "amount_ma20": 40_000_000}
    assert board_signal_from_row(row, symbol="920906", name="fixture") is None
    signal = board_signal_from_row({**row, "close": 12.9}, symbol="920906", name="fixture")
    assert signal["limit_rate"] == 0.3
    assert signal["limit_rule_source"] == "quantdesk.board_rules.classify_board"
    assert signal["validation_status"] == "research_unvalidated"


@pytest.mark.parametrize("updates,name", [({"listing_first_day": True}, "fixture"),
    ({"no_price_limit": True}, "fixture"), ({"listing_date": "2026-10-23"}, "fixture"), ({}, "N新股"), ({}, "C新股")])
def test_no_limit_listing_period_cannot_be_described_as_near_daily_limit(updates, name):
    row = {"date": "2026-10-23", "prev_close": 10, "close": 13, "amount_ma20": 40_000_000, **updates}
    assert board_signal_from_row(row, symbol="920906", name=name) is None


def test_cost_space_and_minimum_lot_do_not_claim_expected_return():
    result = trade_economics(symbol="600000", trade_date="2026-10-23", buy=9.78, stop=9.27, profit=9.79,
                             fee_assumptions=FEES, account_context={"cash": 900, "valuation_date": "2026-10-23"})
    assert result["minimum_quantity"] == 100
    assert result["gross_observation_space"] == 1
    assert result["loss_to_exit_line"] == 51
    assert result["minimum_buy_commission"] == 5
    assert result["estimated_roundtrip_cost_at_observation"] == 11.47
    assert result["observation_space_below_cost"]
    assert result["minimum_lot_affordable"] is False
    assert not result["actionable_order"]


def test_no_fee_or_cash_assumptions_do_not_become_free_and_affordable():
    result = trade_economics(symbol="688001", trade_date="2026-10-23", buy=50, stop=45, profit=55)
    assert result["minimum_quantity"] == 200
    assert result["minimum_lot_gross_amount"] == 10000
    assert result["fee_status"] == "missing"
    assert result["estimated_roundtrip_cost_at_observation"] is None
    assert result["minimum_lot_affordable"] is None
    assert result["observation_space_below_cost"] is None
    assert result["cost_comparison_status"] == "fee_assumptions_incomplete"


def test_missing_transfer_fee_is_partial_instead_of_silently_zero():
    fees = {key: value for key, value in FEES.items() if key != "transfer_fee_rate"}
    result = trade_economics(symbol="600000", trade_date="2026-10-23", buy=9.78, stop=9.27, profit=9.79, fee_assumptions=fees)
    assert result["fee_status"] == "partial"
    assert result["estimated_roundtrip_cost_at_observation"] is None
    assert result["minimum_buy_commission"] == 5
    assert result["gross_observation_space"] == 1
    assert result["observation_space_below_cost"] is None
    assert result["minimum_lot_cash_with_estimated_buy_cost"] is None
    assert result["minimum_lot_affordable"] is None


@pytest.mark.parametrize("missing", ["commission_rate", "min_commission", "stamp_duty_rate", "transfer_fee_rate", "slippage_bps"])
def test_each_missing_fee_keeps_complete_cost_comparison_unknown(missing):
    fees = {key: value for key, value in FEES.items() if key != missing}
    original = deepcopy(fees)
    result = trade_economics(symbol="600000", trade_date="2026-10-23", buy=9.78, stop=9.27, profit=9.79,
                             fee_assumptions=fees, account_context={"cash": 10000})
    assert fees == original
    assert result["observation_space_below_cost"] is None
    assert result["cost_comparison_status"] == "fee_assumptions_incomplete"
    assert result["fee_assumptions"][missing] is None
    assert result["net_observation_space_after_estimated_cost"] is None
    assert result["minimum_lot_affordable"] is None


def test_explicit_zero_fees_remain_a_valid_complete_comparison():
    fees = {key: 0 for key in FEES if key != "source"}
    result = trade_economics(symbol="600000", trade_date="2026-10-23", buy=9.78, stop=9.27, profit=9.79,
                             fee_assumptions=fees, account_context={"cash": 10000})
    assert result["fee_status"] == "provided"
    assert result["cost_comparison_status"] == "available"
    assert result["estimated_roundtrip_cost_at_observation"] == 0
    assert result["observation_space_below_cost"] is False
    assert result["minimum_lot_affordable"] is True


@pytest.mark.parametrize("invalid", [-0.001, float("nan"), True])
def test_invalid_fee_component_cannot_trigger_partial_lower_bound_warning(invalid):
    result = trade_economics(symbol="600000", trade_date="2026-10-23", buy=9.78, stop=9.27, profit=9.79,
                             fee_assumptions={**FEES, "transfer_fee_rate": invalid})
    assert result["minimum_buy_commission"] == 5
    assert result["fee_status"] == "partial"
    assert result["observation_space_below_cost"] is None


def _reference_frame():
    return pd.DataFrame({"date": pd.bdate_range(end="2026-10-23", periods=90),
        "open": 10., "close": 10., "high": 11., "low": 9., "volume": 1000, "adjust": "raw"})


def _builder(data):
    return {"available": True, "price_band": {"latest": 10}, "support_resistance": {"low_60d": 8, "high_60d": 10.5},
        "scenario_projection": {"five_day_range": {"low": 9, "high": 11}, "scenarios": []}}


def _reference(frame):
    return build_stock_reference(frame, symbol="600000", now=datetime.fromisoformat("2026-10-24T12:00:00+08:00"),
        complete_trade_date=date(2026, 10, 23), calendar={"is_open_by_date": {"2026-10-26": True}},
        research_builder=_builder, quote={}, quote_evidence={}, fee_assumptions=FEES)


def test_reference_anchors_unchanged_and_research_identity_is_separate():
    result = _reference(_reference_frame())
    assert [row["price"] for row in result["levels"]] == [10.5, 9, 11]
    assert result["research_identity"]["validation_status"] == "unvalidated"
    assert all(row["status"] == "not_checked" for row in result["event_checks"])
    assert result["trade_economics"]["fee_status"] == "provided"


def test_reference_with_partial_fees_keeps_anchors_and_warns_about_unknown_comparison():
    fees = {key: value for key, value in FEES.items() if key != "transfer_fee_rate"}
    result = build_stock_reference(_reference_frame(), symbol="600000", now=datetime.fromisoformat("2026-10-24T12:00:00+08:00"),
        complete_trade_date=date(2026, 10, 23), calendar={"is_open_by_date": {"2026-10-26": True}},
        research_builder=_builder, quote={}, quote_evidence={}, fee_assumptions=fees)
    assert [row["price"] for row in result["levels"]] == [10.5, 9, 11]
    assert result["trade_economics"]["observation_space_below_cost"] is None
    assert any("费用假设不完整" in warning for warning in result["warnings"])
    assert not any("不足以覆盖所列费用" in warning for warning in result["warnings"])


def test_old_bad_ohlc_is_explained_but_still_blocks_reference():
    frame = _reference_frame()
    frame.loc[0, "high"] = 9
    result = _reference(frame)
    assert not result["available"]
    assert "daily_ohlc_invalid" in result["reason_codes"]
    assert result["history_quality"]["ohlc_invalid_count"] == 1
    assert result["history_quality"]["current_window_ohlc_invalid_count"] == 0
    assert result["history_quality"]["full_history_guard_retained"]


def test_old_unknown_basis_is_not_allowed_by_window_explanation():
    frame = _reference_frame()
    frame.loc[0, "adjust"] = "unknown"
    result = _reference(frame)
    assert result["status"] == "blocked"
    assert not result["available"]


def _state():
    return {"initial_cash": 1000, "cash": 600, "total_equity": 1050, "realized_pnl": 0, "unrealized_pnl": 50,
            "legacy_realized_adjustment": 0, "valuation_date": "2026-09-03", "created_at": "2026-05-01",
            "positions": [{"symbol": "600000", "quantity": 10, "entry_price": 40, "avg_cost": 38,
                           "latest_price": 45, "unrealized_pnl": 50, "market_value": 450,
                           "sector": "未知板块", "industry": "银行Ⅱ", "price_adjustment_status": "stale_adj_factor"}],
            "closed_positions": [], "realized_reductions": [], "warnings": ["paper_account_pnl_identity_mismatch", "paper_account_top_level_drift_detected"]}


def test_cost_projection_exposes_legacy_entry_basis_without_adjusting_ledger():
    state = _state()
    original = deepcopy(state)
    result = build_product_integrity(state)
    assert state == original
    assert result["position_costs"][0]["pnl_cost_price"] == 38
    assert result["position_costs"][0]["entry_cost_differs"]
    assert result["position_costs"][0]["recomputed_unrealized_pnl"] == 70
    assert result["position_costs"][0]["pnl_difference"] == -20
    assert result["position_costs"][0]["status"] == "mismatch"
    assert result["capital_cost_bridge"]["difference"] == -20
    assert result["pnl_identity"]["status"] == "consistent"
    assert result["industry_coverage"]["known"] == 1
    assert result["corporate_action_evidence"]["stale_symbols"] == ["600000"]
    assert result["warnings"]["current_count"] == 1
    assert result["warnings"]["historical_count"] == 1


def test_pnl_difference_is_preserved_not_balanced_with_a_new_adjustment():
    state = _state()
    state["cash"] += 3.03
    state["total_equity"] += 3.03
    result = build_product_integrity(state)
    assert result["pnl_identity"]["difference"] == 3.03
    assert result["capital_cost_bridge"]["source_of_difference"] == "unresolved"
    assert result["loss_contribution"]["reconciled_total"] is False
    assert state["legacy_realized_adjustment"] == 0


def test_incomplete_numbers_do_not_become_zero_money():
    result = build_product_integrity({})
    assert result["pnl_identity"]["status"] == "missing"
    assert result["realized_components"]["closed"] is None
    assert result["loss_contribution"]["held_unrealized"]["amount"] is None
    assert not result["loss_contribution"]["reconciled_total"]
    assert position_cost_evidence({"quantity": True})["recomputed_unrealized_pnl"] is None


def test_history_gaps_duplicates_and_zero_holding_cannot_be_called_complete():
    calendar = {"is_open_by_date": {"2026-09-01": True, "2026-09-02": True, "2026-09-03": True}}
    history = {"items": [{"valuation_date": value, "generated_at": value, "cash": 1000, "market_value": 0,
                          "total_equity": 1000, "portfolio_review_valid": True}
                         for value in ("2026-09-01", "2026-09-03", "2026-09-03")]}
    result = history_coverage(history, calendar=calendar, created_at="2026-05-01", target_date="2026-09-03")
    assert result["missing_dates"] == ["2026-09-02"]
    assert result["duplicate_dates"] == ["2026-09-03"]
    assert result["before_first_history_unverified"]
    assert not result["continuous_statistics_eligible"]
    holding = holding_period_evidence({"entry_date": "2026-09-01", "exit_date": "2026-09-03", "holding_days": 0}, calendar=calendar)
    assert holding["recorded_zero_conflicts_with_dates"]
    assert holding["elapsed_trading_days"] == 2


def test_no_calendar_does_not_invent_missing_trading_days():
    result = history_coverage({"items": [{"valuation_date": "2026-09-01", "cash": 1000, "market_value": 0,
                                         "total_equity": 1000, "portfolio_review_valid": True}]}, target_date="2026-09-03")
    assert result["expected_trading_days"] is None
    assert not result["continuous_statistics_eligible"]


def test_early_ai_receipt_gaps_are_counted_without_claiming_all_evidence_lost():
    result = build_product_integrity(_state(), ai_ledgers={"default": {"trades": [
        {"price": 10, "plan_id": "p1"}, {"price_evidence": {"source": "fixture"}, "plan_id": "p2"}]}})
    coverage = result["ai_evidence_coverage"]["default"]
    assert coverage["price_evidence_known"] == 1
    assert coverage["missing_price_evidence_indices"] == [0]
    assert coverage["archive_search_exhaustive"] is False


def test_invalid_records_are_visible_and_do_not_become_a_complete_ledger():
    state = _state()
    state["positions"].append("malformed")
    state["closed_positions"].append(None)
    result = build_product_integrity(state)
    assert result["collection_validity"]["positions"]["invalid_rows"] == 1
    assert result["capital_cost_bridge"]["status"] == "unverified"
    assert result["realized_components"]["closed"] is None
    assert not result["loss_contribution"]["held_unrealized"]["complete"]


def test_future_or_malformed_equity_records_cannot_pass_coverage():
    calendar = {"is_open_by_date": {"2026-09-01": True, "2026-09-02": True}}
    result = history_coverage({"items": [{"valuation_date": "2026-09-01", "cash": 1000, "market_value": 0, "total_equity": 1000},
                                          {"valuation_date": "2026-09-02", "cash": 1001, "market_value": 0, "total_equity": 1001}, "bad"]},
                              calendar=calendar, target_date="2026-09-01")
    assert result["invalid_record_rows"] == 1
    assert result["dates_after_target"] == ["2026-09-02"]
    assert not result["continuous_statistics_eligible"]


def test_weekly_review_groups_real_exit_dates_and_retains_original_hypotheses():
    records = [{"symbol": str(index), "entry_date": "2026-10-19", "exit_date": "2026-10-23", "realized_pnl": pnl,
                "holding_days": 0, "total_fee": 5, "entry_reason": f"original-{index}",
                "invalidation_condition": f"invalid-if-{index}"} for index, pnl in enumerate((-30, -5, -10, 20))]
    records.append({"symbol": "next", "entry_date": "2026-10-23", "exit_date": "2026-10-26", "realized_pnl": -2, "total_fee": 5})
    before = deepcopy(records)
    result = weekly_learning_review(records, as_of="2026-10-26")
    assert records == before
    weeks = result["weekly_review"]["weeks"]
    assert [week["week_id"] for week in weeks] == ["2026-W44", "2026-W43"]
    week = weeks[1]
    assert week["closed_trade_count"] == 4
    assert week["realized_pnl"]["amount"] == -25
    assert week["recorded_exit_fees"]["amount"] == 20
    assert week["holding_period"]["zero_day_conflicts"] == 4
    assert [row["realized_pnl"] for row in week["top_loss_examples"]] == [-30, -10, -5]
    assert week["top_loss_examples"][0]["original_entry_reason"]["text"] == "original-0"
    assert week["top_loss_examples"][0]["invalidation_condition"]["text"] == "invalid-if-0"
    assert not week["outcome_is_judgment"]


def test_weekly_review_never_invents_a_hypothesis_from_exit_reason_or_pnl():
    records = [{"symbol": "loss", "exit_date": "2026-10-23", "realized_pnl": -5, "exit_reason": "price fell",
                "judgment_review": {"status": "correct", "verified": True}},
               {"symbol": "profit", "exit_date": "2026-10-23", "realized_pnl": 10, "total_fee": 0}]
    result = weekly_learning_review(records, as_of="2026-10-23")
    week = result["weekly_review"]["weeks"][0]
    loss = week["top_loss_examples"][0]
    assert loss["original_entry_reason"] == {"status": "unknown", "text": None, "source_field": None}
    assert loss["invalidation_condition"]["status"] == "unknown"
    assert loss["original_exit_reason"]["text"] == "price fell"
    assert loss["judgment_evidence"]["recorded_status"] == "correct"
    assert loss["judgment_evidence"]["recorded_verified_flag"] is True
    assert not loss["judgment_evidence"]["independently_verified_here"]
    assert not loss["judgment_evidence"]["inferred_from_pnl"]
    assert week["evidence_coverage"]["judgment_review_recorded"] == 1
    assert week["recorded_exit_fees"]["known_count"] == 1
    assert not week["recorded_exit_fees"]["complete"]
    assert result["weekly_review"]["status"] == "partial"


def test_weekly_review_reports_unassigned_dates_and_bad_records_separately():
    records = [None, {"symbol": "unknown", "exit_date": "broken", "realized_pnl": -1},
               {"symbol": "future", "exit_date": "2026-10-26", "realized_pnl": -2}]
    result = weekly_learning_review(records, as_of="2026-10-23")["weekly_review"]
    assert result["invalid_record_rows"] == 1
    assert result["weeks"] == []
    assert [row["source_row"] for row in result["undated_or_future_records"]] == [1, 2]
    assert [row["reason"] for row in result["undated_or_future_records"]] == ["exit_date_missing_or_invalid", "exit_date_after_as_of"]
    assert not result["strategy_changes"]


def test_integrity_exposes_weekly_learning_without_changing_existing_fields():
    state = _state()
    result = build_product_integrity(state)
    assert isinstance(result["learning"]["summary"], str)
    assert all(isinstance(check, str) for check in result["learning"]["checks"])
    assert result["learning"]["weekly_review"]["status"] == "missing"
    assert result["pnl_identity"]["status"] == "consistent"


def _cost_conflict_state():
    # Different identifiers demonstrate a field rule, not symbol-specific fixes.
    return {"initial_cash": 100000, "total_equity": 82515.64, "cash": 15176.64,
        "realized_pnl": -19030.59, "unrealized_pnl": 1543.20,
        "positions": [{"symbol": "111111", "entry_date": "2026-08-05", "quantity": 500,
            "entry_price": 9.4047, "avg_cost": 9.3186, "market_value": 4765, "unrealized_pnl": 62.65}],
        "closed_positions": [{"symbol": "222222", "entry_date": "2026-08-06", "exit_date": "2026-09-24",
            "quantity": 200, "entry_price": 4.8024, "avg_cost": 5.0025,
            "gross_amount": 882, "total_fee": 5.88, "net_amount": 876.12, "realized_pnl": -84.36}],
        "realized_reductions": [], "legacy_realized_adjustment": -20}


def test_cost_conflicts_explain_exact_reported_difference_without_replacing_original_pnl():
    state = _cost_conflict_state()
    original = deepcopy(state)
    result = build_product_integrity(state)
    explanation = result["capital_cost_bridge"]["cost_basis_explanation"]
    assert state == original
    assert result["pnl_identity"]["difference"] == 3.03
    assert result["pnl_identity"]["status"] == "mismatch"
    assert explanation["status"] == "explains_reported_difference"
    assert explanation["status_label"] == "成本字段冲突解释了报告差额"
    assert explanation["candidate_adjustment_sum"] == 3.03
    assert explanation["remaining_reported_difference"] == 0
    assert explanation["explains_reported_difference"]
    assert [item["alternative_pnl_delta"] for item in explanation["candidates"]] == [43.05, -40.02]
    assert [item["source_fields"]["amount"] for item in explanation["candidates"]] == ["market_value", "net_amount"]
    assert explanation["candidates"][1]["original_values"]["recorded_pnl"] == -84.36
    assert explanation["candidates"][1]["pnl_using_avg_cost"] == -124.38
    assert not explanation["authoritative_pnl_replaced"]
    assert not explanation["full_historical_cashflow_verified"]
    assert not result["loss_contribution"]["reconciled_total"]


def test_cost_conflict_does_not_call_an_existing_avg_cost_match_a_new_entry_cost_bug():
    state = _cost_conflict_state()
    state["positions"] = []
    state["closed_positions"] = [{"symbol": "333333", "quantity": 0, "exit_quantity": 100,
        "entry_price": 8.89, "avg_cost": 8.2419, "net_amount": 754.24, "realized_pnl": -69.95}]
    result = cost_basis_conflict_evidence(state)
    row = result["items"][0]
    assert row["source_fields"]["quantity"] == "exit_quantity"
    assert row["match_status"] == "matches_avg_only"
    assert row["pnl_using_entry_price"] == -134.76
    assert row["pnl_using_avg_cost"] == -69.95
    assert result["candidates"] == []
    assert not row["explanation_candidate"]
    assert not result["explains_reported_difference"]


def test_cost_conflict_uses_net_sale_proceeds_without_charging_fees_twice():
    state = _cost_conflict_state()
    state["positions"] = []
    state["closed_positions"] = [{"symbol": "sale", "quantity": 10, "entry_price": 5, "avg_cost": 6,
        "gross_amount": 100, "total_fee": 5, "net_amount": 95, "realized_pnl": 45}]
    row = cost_basis_conflict_evidence(state)["candidates"][0]
    assert row["pnl_using_entry_price"] == 45
    assert row["pnl_using_avg_cost"] == 35
    assert row["alternative_pnl_delta"] == -10
    assert row["net_proceeds_include_recorded_exit_fees"]


def test_cost_conflict_retains_zero_proceeds_and_zero_recorded_pnl_as_known_values():
    state = _cost_conflict_state()
    state["positions"] = [{"symbol": "flat", "quantity": 10, "entry_price": 10, "avg_cost": 8,
        "market_value": 100, "unrealized_pnl": 0}]
    state["closed_positions"] = [{"symbol": "zero-sale", "quantity": 10, "entry_price": 1, "avg_cost": 2,
        "net_amount": 0, "realized_pnl": -10}]
    rows = cost_basis_conflict_evidence(state)["candidates"]
    assert len(rows) == 2
    assert rows[0]["original_values"]["recorded_pnl"] == 0
    assert rows[0]["alternative_pnl_delta"] == 20
    assert rows[1]["original_values"]["amount"] == 0
    assert rows[1]["alternative_pnl_delta"] == -10


def test_cost_conflict_unknown_fields_do_not_fall_back_to_gross_or_zero():
    state = _cost_conflict_state()
    state["positions"] = [{"symbol": "no-avg", "quantity": 10, "entry_price": 10,
        "market_value": 100, "unrealized_pnl": 0},
        {"symbol": "bool-quantity", "quantity": True, "entry_price": 10, "avg_cost": 8,
         "market_value": 100, "unrealized_pnl": 0}]
    state["closed_positions"] = [{"symbol": "no-net", "quantity": 10, "entry_price": 1, "avg_cost": 2,
        "gross_amount": 100, "total_fee": 5, "realized_pnl": 90}]
    result = cost_basis_conflict_evidence(state)
    assert result["unknown_count"] == 3
    assert result["candidates"] == []
    assert [row["missing_fields"] for row in result["items"]] == [["avg_cost"], ["quantity"], ["net_amount"]]
    assert result["items"][2]["pnl_using_entry_price"] is None
    assert not result["explains_reported_difference"]


def test_cost_conflict_excludes_both_and_neither_matches():
    state = _cost_conflict_state()
    state["closed_positions"] = []
    state["positions"] = [{"symbol": "same", "quantity": 10, "entry_price": 10, "avg_cost": 10,
        "market_value": 100, "unrealized_pnl": 0},
        {"symbol": "neither", "quantity": 10, "entry_price": 10, "avg_cost": 8,
         "market_value": 100, "unrealized_pnl": 99}]
    result = cost_basis_conflict_evidence(state)
    assert [row["match_status"] for row in result["items"]] == ["matches_both", "matches_neither"]
    assert result["candidates"] == []


def test_one_cent_residual_cannot_be_called_an_exact_explanation():
    state = _cost_conflict_state()
    state["total_equity"] = 82515.65
    result = cost_basis_conflict_evidence(state)
    assert result["candidate_adjustment_sum"] == 3.03
    assert result["remaining_reported_difference"] == 0.01
    assert result["status"] == "partial_explanation"
    assert not result["explains_reported_difference"]


def test_invalid_collections_and_missing_identity_cannot_certify_cost_explanation():
    state = _cost_conflict_state()
    state["positions"].append(None)
    result = cost_basis_conflict_evidence(state)
    assert result["invalid_record_count"] == 1
    assert result["status"] == "unavailable"
    assert not result["explains_reported_difference"]
    del state["total_equity"]
    result = cost_basis_conflict_evidence(state)
    assert result["reported_pnl_difference"] is None
    assert result["remaining_reported_difference"] is None
    assert not result["explains_reported_difference"]


def test_nonfinite_cost_or_unverified_financing_remains_unknown():
    state = _cost_conflict_state()
    state["positions"][0]["avg_cost"] = float("nan")
    state["credit_used"] = 1
    result = cost_basis_conflict_evidence(state)
    assert result["items"][0]["match_status"] == "unknown"
    assert result["reported_pnl_difference"] is None
    assert result["status"] == "unavailable"
    state = _cost_conflict_state()
    state["total_equity"] = 1e300
    result = cost_basis_conflict_evidence(state)
    assert result["status"] == "unavailable"
    assert not result["explains_reported_difference"]
