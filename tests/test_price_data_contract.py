from __future__ import annotations

from pathlib import Path

import pandas as pd

import quantdesk.price_basis as price_basis

PRICE_BASIS_BACK = price_basis.PRICE_BASIS_BACK
PRICE_BASIS_FORWARD = price_basis.PRICE_BASIS_FORWARD
PRICE_BASIS_RAW = price_basis.PRICE_BASIS_RAW
PRICE_BASIS_UNKNOWN = price_basis.PRICE_BASIS_UNKNOWN


def _frame(*adjustments: object) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-02", periods=len(adjustments), freq="B"),
            "close": [10.0 + index for index in range(len(adjustments))],
            "adjust": list(adjustments),
        }
    )


def test_classify_frame_price_basis_accepts_single_known_basis() -> None:
    assert price_basis.classify_frame_price_basis(_frame("raw", "raw")) == (PRICE_BASIS_RAW, "")
    assert price_basis.classify_frame_price_basis(_frame("qfq", "qfq")) == (PRICE_BASIS_FORWARD, "")
    assert price_basis.classify_frame_price_basis(_frame("hfq", "hfq")) == (PRICE_BASIS_BACK, "")


def test_classify_frame_price_basis_rejects_mixed_basis() -> None:
    assert price_basis.classify_frame_price_basis(_frame("raw", "qfq")) == (
        PRICE_BASIS_UNKNOWN,
        "mixed_price_basis",
    )


def test_classify_frame_price_basis_rejects_missing_or_partial_unknown_basis() -> None:
    assert price_basis.classify_frame_price_basis(pd.DataFrame({"date": ["2026-01-02"], "close": [10.0]})) == (
        PRICE_BASIS_UNKNOWN,
        "unknown_price_basis",
    )
    assert price_basis.classify_frame_price_basis(_frame(None, None)) == (
        PRICE_BASIS_UNKNOWN,
        "unknown_price_basis",
    )
    assert price_basis.classify_frame_price_basis(_frame("raw", None)) == (
        PRICE_BASIS_UNKNOWN,
        "unknown_price_basis",
    )


def test_validate_price_frame_set_requires_one_known_basis_across_symbols() -> None:
    passed = price_basis.validate_price_frame_set(
        {"000001": _frame("raw", "raw"), "000002": _frame("raw", "raw")},
        consumer="backtest",
    )
    assert passed["status"] == "passed"
    assert passed["eligible"] is True
    assert passed["accepted_price_basis"] == PRICE_BASIS_RAW
    assert passed["blocked_symbols"] == []

    blocked = price_basis.validate_price_frame_set(
        {"000001": _frame("raw", "raw"), "000002": _frame("qfq", "qfq")},
        consumer="backtest",
    )
    assert blocked["status"] == "blocked"
    assert blocked["eligible"] is False
    assert blocked["accepted_price_basis"] == PRICE_BASIS_UNKNOWN
    assert blocked["reason_codes"] == ["cross_symbol_price_basis_mismatch"]
    assert blocked["basis_counts"] == {PRICE_BASIS_FORWARD: 1, PRICE_BASIS_RAW: 1}


def test_validate_price_frame_set_surfaces_per_symbol_unknown_reason() -> None:
    result = price_basis.validate_price_frame_set(
        {"000001": _frame("raw", "raw"), "000002": _frame("raw", None)},
        consumer="feature",
    )
    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["unknown_price_basis"]
    assert result["blocked_symbols"] == ["000002"]
    assert result["per_symbol"]["000002"]["reason_code"] == "unknown_price_basis"


def test_validate_price_frame_set_enforces_consumer_specific_basis_policy() -> None:
    raw_execution = price_basis.validate_price_frame_set(
        {"000001": _frame("raw", "raw")},
        consumer="execution",
    )
    assert raw_execution["status"] == "passed"
    assert raw_execution["accepted_price_basis"] == PRICE_BASIS_RAW

    adjusted_execution = price_basis.validate_price_frame_set(
        {"000001": _frame("qfq", "qfq")},
        consumer="execution",
    )
    assert adjusted_execution["status"] == "blocked"
    assert adjusted_execution["reason_codes"] == ["consumer_price_basis_not_allowed"]
    assert adjusted_execution["per_symbol"]["000001"]["reason_code"] == "consumer_price_basis_not_allowed"

    adjusted_intraday_replay = price_basis.validate_price_frame_set(
        {"000001": _frame("qfq", "qfq")},
        consumer="historical_intraday_replay",
    )
    assert adjusted_intraday_replay["status"] == "blocked"
    assert adjusted_intraday_replay["reason_codes"] == ["consumer_price_basis_not_allowed"]

    adjusted_rule_replay = price_basis.validate_price_frame_set(
        {"000001": _frame("qfq", "qfq")},
        consumer="rule_replay",
    )
    assert adjusted_rule_replay["status"] == "passed"
    assert adjusted_rule_replay["accepted_price_basis"] == PRICE_BASIS_FORWARD


def test_validate_price_frame_set_rejects_unknown_consumer() -> None:
    result = price_basis.validate_price_frame_set(
        {"000001": _frame("raw", "raw")},
        consumer="invented_consumer",
    )
    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["unknown_price_consumer"]
    assert result["accepted_price_basis"] == PRICE_BASIS_UNKNOWN


def test_observed_price_basis_labels_do_not_apply_requested_directory_fallback(tmp_path) -> None:
    labels = price_basis.observed_price_basis_labels(
        PRICE_BASIS_FORWARD,
        reason_codes=[],
    )
    assert labels["price_basis"] == PRICE_BASIS_FORWARD
    assert labels["adjustment_basis"]["effective"] == PRICE_BASIS_FORWARD
    assert labels["adjustment_basis"]["observation_mode"] is True
    assert labels["adjustment_basis"]["forward_fallback_to_raw"] is False
    assert price_basis.FORWARD_FALLBACK_WARNING not in labels["adjustment_warnings"]


def test_price_data_contract_separates_current_enforcement_from_future_dual_track() -> None:
    contract = price_basis.price_data_contract()
    policy = contract["current_enforced"]

    for consumer in ("feature", "backtest", "rule_replay"):
        assert policy[consumer]["required_basis"] == "single_known_basis"
        assert policy[consumer]["cross_symbol_consistency"] is True
        assert policy[consumer]["mixed_or_unknown"] in {"block", "block_before_simulation", "block_formal_metrics"}

    assert policy["backtest"]["architecture"] == "single_frame_compatibility"
    assert policy["backtest"]["fill_price_basis"] == "inherits_validated_input_basis"
    assert policy["backtest"]["real_execution_price_claim"] is False
    assert policy["historical_intraday_replay"]["required_basis"] == PRICE_BASIS_RAW
    assert policy["trade_replay"]["required_basis"] == PRICE_BASIS_RAW
    assert policy["paper_execution"]["required_basis"] == PRICE_BASIS_RAW
    assert policy["paper_execution"]["price_role"] == "real_execution_price"

    target = contract["future_target"]
    assert target["feature"] == PRICE_BASIS_FORWARD
    assert target["backtest_signal"] == PRICE_BASIS_FORWARD
    assert target["execution"] == "raw_real_execution_price"
    assert target["simulation"] == "dual_track_adjusted_signal_plus_raw_execution"
    assert target["signal_to_execution_bridge"]["required"] is True


def test_price_data_contract_is_durable_for_future_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    contract_doc = (root / "docs" / "price_data_contract.md").read_text(encoding="utf-8")
    agent_rules = (root / "AGENTS.md").read_text(encoding="utf-8")

    for role in (
        "feature",
        "backtest",
        "historical_intraday_replay",
        "trade_replay",
        "paper_execution",
        "future_target",
    ):
        assert role in contract_doc
    assert "current_enforced" in contract_doc
    assert "single_frame_compatibility" in contract_doc
    assert "dual_track_adjusted_signal_plus_raw_execution" in contract_doc
    assert "Price Data Contract" in agent_rules
    assert "docs/price_data_contract.md" in agent_rules
