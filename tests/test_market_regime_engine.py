from __future__ import annotations

from copy import deepcopy

import numpy as np
import pandas as pd
import pytest
from quantdesk.price_basis import price_data_contract

try:
    from quantdesk.market_regime_engine import (
        MarketRegimeConfig,
        build_market_brain_from_frame,
        build_market_brain_snapshot,
    )
except ModuleNotFoundError:
    MarketRegimeConfig = None  # type: ignore[assignment,misc]
    build_market_brain_from_frame = None  # type: ignore[assignment]
    build_market_brain_snapshot = None  # type: ignore[assignment]


TARGET_DATE = "2026-07-10"
PRICE_CONTRACT = price_data_contract()
PRICE_CONTRACT_VERSION = str(PRICE_CONTRACT.get("version") or "")


def test_market_regime_engine_module_is_available() -> None:
    assert MarketRegimeConfig is not None, "quantdesk.market_regime_engine is not implemented"


@pytest.fixture(autouse=True)
def _skip_behavior_tests_until_module_exists(request: pytest.FixtureRequest) -> None:
    if request.node.name != "test_market_regime_engine_module_is_available" and MarketRegimeConfig is None:
        pytest.skip("market_regime_engine is not implemented yet")


def _price_policy(*, ok: bool = True, basis: str = "raw") -> dict:
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "eligible": ok,
        "contract_version": PRICE_CONTRACT_VERSION,
        "consumer": "feature",
        "consumer_policy": (PRICE_CONTRACT.get("current_enforced") or {}).get("feature") or {},
        "observed_basis": basis,
        "accepted_price_basis": basis,
        "basis_counts": {basis: 1},
        "reason_codes": [] if ok else ["mixed_price_basis"],
    }


def _component(name: str, values: dict, *, data_scope: str, data_date: str) -> dict:
    return {
        **values,
        "data_date": data_date,
        "data_scope": data_scope,
        "price_policy_validation": _price_policy(),
        "provenance": {
            "source_path": f"memory://market-brain/{name}",
            "source_hash": "sha256:" + name.encode("utf-8").hex().ljust(64, "0")[:64],
        },
    }


def _evidence(**overrides) -> dict:
    payload = {
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
        "breadth": {"up_count": 700, "down_count": 250, "flat_count": 50, "sample_count": 1000},
        "volume": {"volume_ratio": 1.2},
        "sector": {"strong_sector_ratio": 0.65},
        "price_policy_validation": _price_policy(),
    }
    payload.update(overrides)
    for component_name in ("breadth", "volume", "sector"):
        values = dict(payload.get(component_name) or {})
        values.setdefault("data_date", str(payload.get("data_date") or TARGET_DATE))
        values.setdefault("data_scope", str(payload.get("data_scope") or "specified_symbols"))
        values.setdefault("price_policy_validation", _price_policy())
        values.setdefault(
            "provenance",
            {
                "source_path": f"memory://market-brain/{component_name}",
                "source_hash": "sha256:" + component_name.encode("utf-8").hex().ljust(64, "0")[:64],
            },
        )
        payload[component_name] = values
    return payload


def _frame(*, trend: str = "up", adjust: str | None = "raw") -> pd.DataFrame:
    dates = pd.date_range("2026-04-01", periods=72, freq="B")
    if trend == "up":
        closes = [100.0 + index * 0.35 for index in range(len(dates))]
    elif trend == "down":
        closes = [125.0 - index * 0.4 for index in range(len(dates))]
    else:
        closes = [100.0 + ((index % 8) - 4) * 0.15 for index in range(len(dates))]
    data = {"date": dates, "close": closes}
    if adjust is not None:
        data["adjust"] = [adjust for _ in dates]
    return pd.DataFrame(data)


def test_bull_snapshot_has_confidence_strategy_compatibility_and_paper_boundary() -> None:
    result = build_market_brain_snapshot(_evidence())

    assert result["regime"] == "BULL"
    assert result["evidence_eligible"] is True
    assert 0.0 <= result["confidence"] <= 1.0
    assert "trend_breakout" in result["recommended_strategies"]
    assert 0.0 <= result["paper_exposure_ceiling"] <= 1.0
    assert result["paper_exposure_ceiling"] == result["max_paper_exposure"]
    assert result["research_only"] is True
    assert result["paper_only"] is True
    assert result["no_broker"] is True
    assert result["is_real_order"] is False
    assert "max_position" not in result


def test_crash_requires_drawdown_and_stress_confirmation() -> None:
    evidence = _evidence(
        index={
            "close": 82.0,
            "ma20": 96.0,
            "ma60": 105.0,
            "return_5d": -0.09,
            "drawdown_20d": -0.14,
            "volatility_20d": 0.065,
        },
        breadth={"up_count": 90, "down_count": 860, "flat_count": 50, "sample_count": 1000},
        volume={"volume_ratio": 1.8},
        sector={"strong_sector_ratio": 0.05},
    )

    result = build_market_brain_snapshot(evidence)

    assert result["regime"] == "CRASH"
    assert result["paper_exposure_ceiling"] <= 0.15
    assert "crash_drawdown_confirmed" in result["reason_codes"]


def test_recovery_requires_a_prior_bear_or_crash_state() -> None:
    recovery = _evidence(
        index={
            "close": 103.0,
            "ma20": 101.0,
            "ma60": 106.0,
            "return_5d": 0.045,
            "drawdown_20d": -0.07,
            "volatility_20d": 0.028,
        },
        breadth={"up_count": 620, "down_count": 330, "flat_count": 50, "sample_count": 1000},
        volume={"volume_ratio": 1.25},
        sector={"strong_sector_ratio": 0.55},
        previous_regime="CRASH",
    )
    without_prior = deepcopy(recovery)
    without_prior.pop("previous_regime")

    assert build_market_brain_snapshot(recovery)["regime"] == "RECOVERY"
    assert build_market_brain_snapshot(without_prior)["regime"] != "RECOVERY"


def test_bear_and_sideways_are_distinct() -> None:
    bear = _evidence(
        index={
            "close": 88.0,
            "ma20": 96.0,
            "ma60": 104.0,
            "return_5d": -0.025,
            "drawdown_20d": -0.06,
            "volatility_20d": 0.03,
        },
        breadth={"up_count": 240, "down_count": 700, "flat_count": 60, "sample_count": 1000},
        volume={"volume_ratio": 0.85},
        sector={"strong_sector_ratio": 0.15},
    )
    sideways = _evidence(
        index={
            "close": 100.0,
            "ma20": 100.2,
            "ma60": 99.8,
            "return_5d": 0.001,
            "drawdown_20d": -0.025,
            "volatility_20d": 0.018,
        },
        breadth={"up_count": 480, "down_count": 470, "flat_count": 50, "sample_count": 1000},
        volume={"volume_ratio": 0.98},
        sector={"strong_sector_ratio": 0.5},
    )

    assert build_market_brain_snapshot(bear)["regime"] == "BEAR"
    assert build_market_brain_snapshot(sideways)["regime"] == "SIDEWAYS"


def test_missing_required_evidence_is_unknown_and_not_formally_eligible() -> None:
    result = build_market_brain_snapshot(
        {
            "target_date": TARGET_DATE,
            "data_date": TARGET_DATE,
            "data_scope": "specified_symbols",
            "price_policy_validation": _price_policy(),
        }
    )

    assert result["regime"] == "UNKNOWN"
    assert result["evidence_eligible"] is False
    assert result["recommended_strategies"] == []
    assert "missing_index_evidence" in result["reason_codes"]
    assert "missing_breadth_evidence" in result["reason_codes"]


def test_invalid_or_missing_price_policy_fails_closed() -> None:
    mixed = build_market_brain_snapshot(
        _evidence(price_policy_validation=_price_policy(ok=False, basis="mixed_or_unknown"))
    )
    missing = _evidence()
    missing.pop("price_policy_validation")
    missing_result = build_market_brain_snapshot(missing)

    assert mixed["regime"] == "UNKNOWN"
    assert mixed["evidence_eligible"] is False
    assert "price_policy_validation_failed" in mixed["reason_codes"]
    assert missing_result["regime"] == "UNKNOWN"
    assert "missing_price_policy_validation" in missing_result["reason_codes"]


def test_future_or_stale_market_evidence_cannot_be_formally_eligible() -> None:
    future = build_market_brain_snapshot(_evidence(data_date="2026-07-13"))
    stale = build_market_brain_snapshot(_evidence(data_date="2026-07-09"))

    assert future["regime"] == "UNKNOWN"
    assert future["evidence_eligible"] is False
    assert "future_market_evidence" in future["reason_codes"]
    assert stale["evidence_eligible"] is False
    assert "stale_market_evidence" in stale["reason_codes"]


def test_local_sample_caps_confidence_and_keeps_scope_warning_visible() -> None:
    result = build_market_brain_snapshot(_evidence(data_scope="local_sample"))

    assert result["regime"] == "BULL"
    assert result["confidence"] <= MarketRegimeConfig().local_sample_confidence_cap
    assert "local_sample_not_full_market" in result["reason_codes"]
    assert result["status"] in {"warning", "partial"}


def test_frame_facade_validates_original_price_basis_before_cutoff() -> None:
    frame = _frame()
    future = pd.DataFrame(
        [{"date": "2026-07-13", "close": 999.0, "adjust": "raw"}]
    )
    original = pd.concat([frame, future], ignore_index=True)

    result = build_market_brain_from_frame(
        original,
        target_date=TARGET_DATE,
        breadth=_component("breadth", {"up_count": 650, "down_count": 300, "flat_count": 50, "sample_count": 1000}, data_scope="specified_symbols", data_date=TARGET_DATE),
        volume=_component("volume", {"volume_ratio": 1.1}, data_scope="specified_symbols", data_date=TARGET_DATE),
        sector=_component("sector", {"strong_sector_ratio": 0.6}, data_scope="specified_symbols", data_date=TARGET_DATE),
        data_scope="specified_symbols",
    )

    assert result["regime"] == "BULL"
    assert result["data_date"] <= TARGET_DATE
    assert result["price_policy"]["observed_basis"] == "raw"
    assert result["price_policy"]["consumer"] == "market_brain"
    assert result["source_row_count"] == len(original)
    assert result["as_of_row_count"] == len(frame)


def test_frame_facade_rejects_unknown_and_mixed_basis() -> None:
    unknown = build_market_brain_from_frame(
        _frame(adjust=None),
        target_date=TARGET_DATE,
        breadth=_component("breadth", {"up_count": 6, "down_count": 3, "sample_count": 9}, data_scope="local_sample", data_date=TARGET_DATE),
        volume=_component("volume", {"volume_ratio": 1.0}, data_scope="local_sample", data_date=TARGET_DATE),
        sector=_component("sector", {"strong_sector_ratio": 0.5}, data_scope="local_sample", data_date=TARGET_DATE),
    )
    mixed_frame = _frame()
    mixed_frame.loc[mixed_frame.index[-1], "adjust"] = "qfq"
    mixed = build_market_brain_from_frame(
        mixed_frame,
        target_date=TARGET_DATE,
        breadth=_component("breadth", {"up_count": 6, "down_count": 3, "sample_count": 9}, data_scope="local_sample", data_date=TARGET_DATE),
        volume=_component("volume", {"volume_ratio": 1.0}, data_scope="local_sample", data_date=TARGET_DATE),
        sector=_component("sector", {"strong_sector_ratio": 0.5}, data_scope="local_sample", data_date=TARGET_DATE),
    )

    assert unknown["regime"] == "UNKNOWN"
    assert "unknown_price_basis" in unknown["reason_codes"]
    assert mixed["regime"] == "UNKNOWN"
    assert "mixed_price_basis" in mixed["reason_codes"]


def test_same_evidence_produces_identical_decision_fields() -> None:
    first = build_market_brain_snapshot(_evidence())
    second = build_market_brain_snapshot(_evidence())
    keys = (
        "regime",
        "confidence",
        "paper_exposure_ceiling",
        "recommended_strategies",
        "component_scores",
        "reason_codes",
    )

    assert {key: first[key] for key in keys} == {key: second[key] for key in keys}


def test_snapshot_carries_common_evidence_envelope_without_mutating_input() -> None:
    evidence = _evidence(
        coverage={"kind": "specified_symbols", "sample_count": 1000, "ratio": 0.37},
        provenance={"source_path": "memory://benchmark", "source_hash": "sha256:test"},
        pit_status={"status": "not_applicable_benchmark_regime"},
    )
    original = deepcopy(evidence)

    result = build_market_brain_snapshot(evidence)

    assert evidence == original
    assert result["as_of"] == TARGET_DATE
    assert result["data_cutoff"] == TARGET_DATE
    assert result["coverage"] == evidence["coverage"]
    assert result["provenance"]["source_path"] == "memory://benchmark"
    assert result["provenance"]["source_hash"] == "sha256:test"
    assert result["provenance"]["network_used"] is False
    assert result["provenance"]["canonical_write"] is False
    assert result["pit_status"] == evidence["pit_status"]
    assert result["degraded_reasons"] == result["reason_codes"]


@pytest.mark.parametrize(
    ("missing_field", "reason"),
    [
        ("contract_version", "missing_price_contract_version"),
        ("consumer_policy", "missing_consumer_price_policy"),
    ],
)
def test_price_policy_missing_required_validator_fields_fails_closed(
    missing_field: str,
    reason: str,
) -> None:
    policy = _price_policy()
    policy.pop(missing_field)

    result = build_market_brain_snapshot(
        _evidence(price_policy_validation=policy)
    )

    assert result["formal_regime"] == "UNKNOWN"
    assert result["evidence_eligible"] is False
    assert reason in result["reason_codes"]


def test_next_local_calendar_day_timezone_evidence_is_future() -> None:
    result = build_market_brain_snapshot(
        _evidence(data_date="2026-07-11T00:30:00+08:00")
    )

    assert result["formal_regime"] == "UNKNOWN"
    assert result["evidence_eligible"] is False
    assert "future_market_evidence" in result["reason_codes"]


@pytest.mark.parametrize(
    ("component_name", "missing_field", "reason"),
    [
        ("breadth", "price_policy_validation", "missing_breadth_price_policy_validation"),
        ("volume", "price_policy_validation", "missing_volume_price_policy_validation"),
        ("sector", "price_policy_validation", "missing_sector_price_policy_validation"),
        ("breadth", "provenance", "missing_breadth_provenance"),
        ("volume", "provenance", "missing_volume_provenance"),
        ("sector", "provenance", "missing_sector_provenance"),
    ],
)
def test_auxiliary_component_requires_price_policy_and_provenance(
    component_name: str,
    missing_field: str,
    reason: str,
) -> None:
    evidence = _evidence()
    evidence[component_name].pop(missing_field)

    result = build_market_brain_snapshot(evidence)

    assert result["formal_regime"] == "UNKNOWN"
    assert result["evidence_eligible"] is False
    assert reason in result["reason_codes"]


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (MarketRegimeConfig(bull_return_5d_min=None), "invalid_config_bull_return_5d_min"),
        (MarketRegimeConfig(bear_confirmation_min="2"), "invalid_config_bear_confirmation_min"),
        (MarketRegimeConfig(full_market_scopes=None), "invalid_config_full_market_scopes"),
    ],
)
def test_malformed_config_returns_unknown_instead_of_raising(
    config: MarketRegimeConfig,
    reason: str,
) -> None:
    result = build_market_brain_snapshot(_evidence(), config=config)

    assert result["formal_regime"] == "UNKNOWN"
    assert result["evidence_eligible"] is False
    assert reason in result["reason_codes"]


def test_numpy_boolean_numeric_evidence_fails_closed() -> None:
    evidence = _evidence()
    evidence["index"]["return_5d"] = np.bool_(True)

    result = build_market_brain_snapshot(evidence)

    assert result["formal_regime"] == "UNKNOWN"
    assert "missing_index_evidence" in result["reason_codes"]


def test_impossible_minus_one_return_and_drawdown_fails_closed() -> None:
    evidence = _evidence()
    evidence["index"].update({"return_5d": -1.0, "drawdown_20d": -1.0})

    result = build_market_brain_snapshot(evidence)

    assert result["formal_regime"] == "UNKNOWN"
    assert "invalid_index_evidence" in result["reason_codes"]


def test_full_market_claim_without_coverage_and_provenance_is_blocked() -> None:
    evidence = _evidence(data_scope="full_market")
    evidence.pop("coverage", None)
    evidence.pop("provenance", None)

    result = build_market_brain_snapshot(evidence)

    assert result["formal_regime"] == "UNKNOWN"
    assert result["evidence_eligible"] is False
    assert "full_market_coverage_unsubstantiated" in result["reason_codes"]


def test_substantiated_full_market_claim_remains_formally_eligible() -> None:
    evidence = _evidence(
        data_scope="full_market",
        coverage={
            "kind": "full_market",
            "sample_count": 1000,
            "ratio": 1.0,
        },
        provenance={
            "source_path": "memory://market-brain/full-market",
            "source_hash": "sha256:" + "f" * 64,
            "coverage_verified": True,
        },
    )

    result = build_market_brain_snapshot(evidence)

    assert result["formal_regime"] == "BULL"
    assert result["evidence_eligible"] is True
