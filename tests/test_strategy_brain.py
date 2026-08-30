from __future__ import annotations

from copy import deepcopy
import json

import pytest

from quantdesk.market_regime_engine import build_market_brain_snapshot
from quantdesk.price_basis import PRICE_DATA_CONTRACT_VERSION, price_data_contract
from quantdesk.research.strategy_recommender import recommend_strategy_for_symbol
from quantdesk.strategy_registry import resolve_strategy

try:
    from quantdesk.strategy_brain import (
        StrategyBrainConfig,
        build_strategy_brain_snapshot,
        build_strategy_spec_catalog,
        resolve_strategy_strict,
    )
except ModuleNotFoundError:
    StrategyBrainConfig = None  # type: ignore[assignment,misc]
    build_strategy_brain_snapshot = None  # type: ignore[assignment]
    build_strategy_spec_catalog = None  # type: ignore[assignment]
    resolve_strategy_strict = None  # type: ignore[assignment]


TARGET_DATE = "2026-07-10"
PRICE_CONTRACT = price_data_contract()


def test_strategy_brain_module_is_available() -> None:
    assert StrategyBrainConfig is not None, "quantdesk.strategy_brain is not implemented"


@pytest.fixture(autouse=True)
def _skip_until_module_exists(request: pytest.FixtureRequest) -> None:
    if request.node.name != "test_strategy_brain_module_is_available" and StrategyBrainConfig is None:
        pytest.skip("strategy_brain is not implemented yet")


def _market_snapshot(*, eligible: bool = True) -> dict:
    basis = "raw" if eligible else "mixed_or_unknown"
    price_policy = {
        "ok": eligible,
        "status": "passed" if eligible else "failed",
        "eligible": eligible,
        "contract_version": PRICE_DATA_CONTRACT_VERSION,
        "consumer": "feature",
        "consumer_policy": (PRICE_CONTRACT.get("current_enforced") or {}).get("feature") or {},
        "observed_basis": basis,
        "accepted_price_basis": basis,
        "basis_counts": {basis: 1},
        "reason_codes": [] if eligible else ["mixed_price_basis"],
    }

    def _component(name: str, values: dict) -> dict:
        return {
            **values,
            "data_date": TARGET_DATE,
            "data_scope": "specified_symbols",
            "price_policy_validation": deepcopy(price_policy),
            "provenance": {
                "source_path": f"memory://market/{name}",
                "source_hash": "sha256:" + name.encode("utf-8").hex().ljust(64, "0")[:64],
            },
        }

    evidence = {
        "target_date": TARGET_DATE,
        "data_date": TARGET_DATE,
        "data_scope": "specified_symbols",
        "index": {
            "close": 110.0,
            "ma20": 105.0,
            "ma60": 100.0,
            "return_5d": 0.04,
            "drawdown_20d": -0.01,
            "volatility_20d": 0.015,
        },
        "breadth": _component(
            "breadth",
            {"up_count": 700, "down_count": 250, "flat_count": 50, "sample_count": 1000},
        ),
        "volume": _component("volume", {"volume_ratio": 1.2}),
        "sector": _component("sector", {"strong_sector_ratio": 0.65}),
        "price_policy_validation": deepcopy(price_policy),
        "coverage": {"kind": "specified_symbols", "sample_count": 1000},
        "provenance": {"source_path": "memory://market", "source_hash": "sha256:market"},
        "pit_status": {"status": "not_applicable_benchmark_regime"},
    }
    return build_market_brain_snapshot(evidence)


def _complete_candidate(**overrides) -> dict:
    row = {
        "symbol": "600744",
        "name": "Sample",
        "sector": "Power",
        "close": 8.8,
        "ma20": 8.0,
        "ma60": 7.6,
        "volume_ratio": 1.6,
        "activity_ratio_20": 1.6,
        "liquidity_score": 78.0,
        "volatility": 0.035,
        "long_score": 72.0,
        "price_policy_validation": {
            "contract_version": PRICE_DATA_CONTRACT_VERSION,
            "consumer": "feature",
            "ok": True,
            "eligible": True,
            "status": "passed",
            "observed_basis": "raw",
            "accepted_price_basis": "raw",
            "basis_counts": {"raw": 1},
            "consumer_policy": {"allowed_basis": ["raw", "forward_adjusted", "back_adjusted"]},
            "price_conversion_performed": False,
            "reason_codes": [],
        },
        "pit_status": {"status": "passed"},
        "as_of": TARGET_DATE,
        "data_cutoff": TARGET_DATE,
        "source_path": "memory://candidate/600744",
        "source_hash": "sha256:candidate",
        "coverage": {"kind": "single_candidate", "feature_count": 5},
    }
    row.update(overrides)
    return row


def test_catalog_wraps_existing_registry_with_versioned_strategy_specs() -> None:
    catalog = build_strategy_spec_catalog()

    assert catalog["status"] in {"passed", "warning"}
    assert catalog["spec_count"] > 0
    ma = catalog["by_id"]["ma_breakout"]
    assert ma["strategy_id"] == "ma_breakout"
    assert ma["version"]
    assert ma["required_features"]
    assert ma["market_fit"]
    assert ma["evidence_kind"] == "registry_strategy_spec"
    assert ma["shadow_only"] is True


def test_strict_resolver_blocks_unknown_without_changing_legacy_fallback() -> None:
    strict = resolve_strategy_strict("definitely_missing_strategy")
    legacy = resolve_strategy("definitely_missing_strategy")

    assert strict["ok"] is False
    assert strict["status"] == "blocked"
    assert strict["strategy"] is None
    assert "unknown_strategy_id" in strict["reason_codes"]
    assert legacy["strategy_id"] == "ma_breakout"
    assert "strategy_warning" in legacy


def test_complete_feature_candidate_keeps_legacy_recommender_result_in_shadow() -> None:
    candidate = _complete_candidate()
    sector = {
        "status": "passed",
        "evidence_eligible": True,
        "pit_status": {"status": "passed"},
        "as_of": TARGET_DATE,
        "data_cutoff": TARGET_DATE,
        "source_path": "memory://sector",
        "source_hash": "sha256:sector",
        "coverage": {"kind": "sector_snapshot", "sector_count": 1},
        "price_policy_validation": deepcopy(candidate["price_policy_validation"]),
        "sectors": {"Power": {"sector": "Power", "sector_status": "leading", "heat_score": 86}},
    }
    legacy = recommend_strategy_for_symbol(
        candidate["symbol"],
        candidate,
        sector["sectors"]["Power"],
    )

    result = build_strategy_brain_snapshot(
        [candidate],
        _market_snapshot(),
        sector_snapshot=sector,
    )
    decision = result["candidate_decisions"][0]

    assert result["shadow_mode"] is True
    assert result["writes_state"] is False
    assert decision["evidence_eligible"] is True
    assert decision["legacy_recommendation"]["primary_strategy"] == legacy["primary_strategy"]
    assert decision["legacy_recommendation"]["confidence"] == legacy["confidence"]
    assert decision["canonical_strategy_id"]
    assert decision["candidate_status"] == "shadow_fit_observation"
    assert legacy["primary_strategy"] == "long_swing"
    assert legacy["confidence"] == 100
    assert legacy["score_breakdown"] == {
        "long_term": 100.0,
        "long_swing": 100.0,
        "board": 52.4,
        "short_swing": 70.0,
        "observe": 52.0,
        "avoid": 10.0,
    }


def test_missing_required_features_are_excluded_pending_research() -> None:
    candidate = _complete_candidate()
    candidate.pop("ma60")
    candidate.pop("volatility")

    result = build_strategy_brain_snapshot([candidate], _market_snapshot())
    decision = result["candidate_decisions"][0]

    assert decision["evidence_eligible"] is False
    assert decision["candidate_status"] == "excluded_pending_research"
    assert decision["canonical_strategy_id"] is None
    assert "missing_required_features" in decision["reason_codes"]
    assert set(decision["missing_features"]) == {"ma60", "volatility"}


def test_invalid_market_snapshot_blocks_formal_strategy_fit() -> None:
    result = build_strategy_brain_snapshot([_complete_candidate()], _market_snapshot(eligible=False))

    assert result["evidence_eligible"] is False
    assert result["formal_strategy_fits"] == []
    assert "market_evidence_ineligible" in result["reason_codes"]
    assert result["candidate_decisions"][0]["candidate_status"] == "excluded_pending_research"


def test_non_pit_sector_evidence_is_visible_but_not_used_for_formal_fit() -> None:
    candidate = _complete_candidate()
    non_pit = {
        "status": "passed",
        "as_of": TARGET_DATE,
        "pit_status": {"status": "proxy_non_pit"},
        "sectors": {"Power": {"sector": "Power", "sector_status": "leading", "heat_score": 99}},
    }

    result = build_strategy_brain_snapshot(
        [candidate],
        _market_snapshot(),
        sector_snapshot=non_pit,
    )
    decision = result["candidate_decisions"][0]

    assert result["sector_evidence"]["used_for_formal_fit"] is False
    assert result["sector_evidence"]["status"] == "proxy_non_pit"
    assert "sector_evidence_non_pit" in result["reason_codes"]
    assert decision["legacy_recommendation"]["sector_context"]["sector_status"] == "unknown"


def test_shadow_builder_is_deterministic_does_not_mutate_inputs_and_has_boundaries() -> None:
    candidates = [_complete_candidate()]
    market = _market_snapshot()
    original_candidates = deepcopy(candidates)
    original_market = deepcopy(market)

    first = build_strategy_brain_snapshot(candidates, market)
    second = build_strategy_brain_snapshot(candidates, market)

    assert candidates == original_candidates
    assert market == original_market
    assert first == second
    assert first["research_only"] is True
    assert first["paper_only"] is True
    assert first["no_broker"] is True
    assert first["is_real_order"] is False
    assert first["not_investment_advice"] is True
    assert first["not_position_advice"] is True
    assert "quantity" not in json.dumps(first, ensure_ascii=False).lower()


def test_candidate_decision_carries_common_evidence_envelope() -> None:
    candidate = _complete_candidate()

    decision = build_strategy_brain_snapshot([candidate], _market_snapshot())["candidate_decisions"][0]

    assert decision["schema_version"]
    assert decision["as_of"] == TARGET_DATE
    assert decision["data_cutoff"] == TARGET_DATE
    assert decision["source_path"] == candidate["source_path"]
    assert decision["source_hash"] == candidate["source_hash"]
    assert decision["price_data_contract_version"] == PRICE_DATA_CONTRACT_VERSION
    assert decision["price_policy_validation"]["eligible"] is True
    assert decision["pit_status"]["status"] == "passed"
    assert decision["coverage"] == candidate["coverage"]
    assert isinstance(decision["reason_codes"], list)
    assert isinstance(decision["degraded_reasons"], list)


@pytest.mark.parametrize(
    "policy_patch",
    [
        {"eligible": "false", "ok": "false"},
        {"observed_basis": "mixed_or_unknown", "accepted_price_basis": "raw"},
        {"basis_counts": {"raw": 1, "forward_adjusted": 1}},
        {"contract_version": "legacy"},
        {"price_conversion_performed": True},
        {"eligible": True, "ok": False},
        {"blocked_symbols": ["600744"]},
    ],
)
def test_candidate_price_policy_fail_closed_on_malformed_or_contradictory_claims(
    policy_patch: dict,
) -> None:
    candidate = _complete_candidate()
    candidate["price_policy_validation"].update(policy_patch)

    decision = build_strategy_brain_snapshot([candidate], _market_snapshot())["candidate_decisions"][0]

    assert decision["evidence_eligible"] is False
    assert "candidate_price_policy_ineligible" in decision["reason_codes"]


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"data_cutoff": "2026-07-11"}, "candidate_data_cutoff_mismatch"),
        ({"source_path": ""}, "candidate_source_path_missing"),
        ({"symbol": ""}, "candidate_symbol_missing"),
        ({"coverage": {}}, "candidate_coverage_missing"),
    ],
)
def test_candidate_identity_and_cutoff_fail_closed(patch: dict, reason: str) -> None:
    candidate = _complete_candidate(**patch)

    decision = build_strategy_brain_snapshot([candidate], _market_snapshot())["candidate_decisions"][0]

    assert decision["evidence_eligible"] is False
    assert reason in decision["reason_codes"]


def test_market_string_false_eligibility_cannot_enable_strategy_fit() -> None:
    market = _market_snapshot()
    market["evidence_eligible"] = "false"

    result = build_strategy_brain_snapshot([_complete_candidate()], market)

    assert result["evidence_eligible"] is False
    assert result["formal_strategy_fits"] == []
    assert "market_evidence_ineligible" in result["reason_codes"]


def test_market_boolean_alone_cannot_bypass_common_evidence_envelope() -> None:
    result = build_strategy_brain_snapshot(
        [_complete_candidate()],
        {"evidence_eligible": True},
    )

    assert result["evidence_eligible"] is False
    assert result["formal_strategy_fits"] == []
    assert "market_as_of_missing_or_invalid" in result["reason_codes"]
    assert "market_source_hash_missing" in result["reason_codes"]
    assert "market_price_policy_ineligible" in result["reason_codes"]


def test_catalog_and_resolver_carry_static_contract_provenance() -> None:
    catalog = build_strategy_spec_catalog()
    resolved = resolve_strategy_strict("ma_breakout")

    for payload in (catalog, resolved):
        assert payload["schema_version"]
        assert "as_of" in payload
        assert "data_cutoff" in payload
        assert payload["source_path"]
        assert payload["source_hash"].startswith("sha256:")
        assert payload["price_data_contract_version"] == PRICE_DATA_CONTRACT_VERSION
        assert payload["pit_status"]["status"] == "not_applicable_static_strategy_registry"
        assert isinstance(payload["reason_codes"], list)
        assert isinstance(payload["degraded_reasons"], list)


def test_sector_evidence_without_as_of_is_visible_but_not_used() -> None:
    sector = {
        "status": "passed",
        "pit_status": {"status": "passed"},
        "sectors": {"Power": {"sector": "Power", "sector_status": "leading", "heat_score": 99}},
    }

    result = build_strategy_brain_snapshot(
        [_complete_candidate()],
        _market_snapshot(),
        sector_snapshot=sector,
    )

    assert result["sector_evidence"]["used_for_formal_fit"] is False
    assert "sector_evidence_as_of_missing" in result["reason_codes"]
    assert result["candidate_decisions"][0]["legacy_recommendation"]["sector_context"]["sector_status"] == "unknown"


def test_mainline_mismatched_cutoff_is_not_used_for_formal_fit() -> None:
    mainline = {
        "status": "passed",
        "evidence_eligible": True,
        "as_of": TARGET_DATE,
        "target_date": TARGET_DATE,
        "data_cutoff": "2026-07-09",
        "reason_codes": [],
        "current_mainlines": [{"name": "Power"}],
    }

    result = build_strategy_brain_snapshot(
        [_complete_candidate()],
        _market_snapshot(),
        mainline_snapshot=mainline,
    )

    assert result["mainline_evidence"]["used_for_formal_fit"] is False
    assert "mainline_evidence_data_cutoff_mismatch" in result["reason_codes"]


def test_market_future_cutoff_blocks_all_formal_fits() -> None:
    market = _market_snapshot()
    market["data_cutoff"] = "2099-01-01"

    result = build_strategy_brain_snapshot([_complete_candidate()], market)

    assert result["evidence_eligible"] is False
    assert result["formal_strategy_fits"] == []
    assert "market_data_cutoff_after_as_of" in result["reason_codes"]


def test_historical_candidate_cannot_use_current_snapshot_pit_exception() -> None:
    candidate = _complete_candidate(
        as_of="2020-01-02",
        data_cutoff="2020-01-02",
        pit_status={"status": "not_applicable_current_snapshot"},
    )
    market = _market_snapshot()
    market["as_of"] = "2020-01-02"
    market["data_cutoff"] = "2020-01-02"

    decision = build_strategy_brain_snapshot([candidate], market)["candidate_decisions"][0]

    assert decision["evidence_eligible"] is False
    assert "candidate_pit_ineligible" in decision["reason_codes"]


def test_config_cannot_widen_pit_or_price_contract_fail_closed_rules() -> None:
    candidate = _complete_candidate(
        pit_status={"status": "not_applicable_current_snapshot"},
    )
    candidate["price_policy_validation"].update(
        {
            "observed_basis": "mixed_or_unknown",
            "accepted_price_basis": "mixed_or_unknown",
            "basis_counts": {"mixed_or_unknown": 1},
            "consumer_policy": {"allowed_basis": ["mixed_or_unknown"]},
        }
    )
    config = StrategyBrainConfig(
        accepted_pit_statuses=("passed", "not_applicable_current_snapshot"),
        accepted_candidate_price_bases=("raw", "mixed_or_unknown"),
    )

    decision = build_strategy_brain_snapshot(
        [candidate],
        _market_snapshot(),
        config=config,
    )["candidate_decisions"][0]

    assert decision["evidence_eligible"] is False
    assert "candidate_pit_ineligible" in decision["reason_codes"]
    assert "candidate_price_policy_ineligible" in decision["reason_codes"]


def test_non_shadow_or_state_writing_config_is_blocked() -> None:
    result = build_strategy_brain_snapshot(
        [_complete_candidate()],
        _market_snapshot(),
        config=StrategyBrainConfig(
            shadow_mode=False,
            allow_state_writes=True,
            strict_resolver=False,
        ),
    )

    assert result["status"] == "blocked"
    assert result["evidence_eligible"] is False
    assert result["formal_strategy_fits"] == []
    assert "strategy_brain_unsafe_config" in result["reason_codes"]


def test_sector_claim_missing_common_envelope_cannot_influence_score() -> None:
    sector = {
        "status": "passed",
        "evidence_eligible": True,
        "pit_status": {"status": "passed"},
        "as_of": TARGET_DATE,
        "data_cutoff": TARGET_DATE,
        "sectors": {"Power": {"sector": "Power", "sector_status": "leading", "heat_score": 99}},
    }

    result = build_strategy_brain_snapshot(
        [_complete_candidate()],
        _market_snapshot(),
        sector_snapshot=sector,
    )

    assert result["sector_evidence"]["used_for_formal_fit"] is False
    assert "sector_evidence_source_missing" in result["reason_codes"]
    assert result["candidate_decisions"][0]["legacy_recommendation"]["sector_context"]["sector_status"] == "unknown"


def test_selected_strategy_requires_its_complete_feature_set() -> None:
    candidate = _complete_candidate()
    candidate.pop("activity_ratio_20")

    decision = build_strategy_brain_snapshot([candidate], _market_snapshot())["candidate_decisions"][0]

    assert decision["legacy_recommendation"]["primary_strategy"] == "long_swing"
    assert decision["evidence_eligible"] is False
    assert "strategy_required_features_missing" in decision["reason_codes"]
    assert "activity_ratio_20" in decision["missing_features"]


def test_selected_strategy_must_fit_formal_market_regime() -> None:
    market = _market_snapshot()
    market["formal_regime"] = "CRASH"

    decision = build_strategy_brain_snapshot([_complete_candidate()], market)["candidate_decisions"][0]

    assert decision["evidence_eligible"] is False
    assert "strategy_market_fit_mismatch" in decision["reason_codes"]
