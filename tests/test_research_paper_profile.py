from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_default_research_paper_profile_is_user_10k_research_only() -> None:
    from quantdesk.trader_profile import default_research_paper_profile, validate_research_paper_profile

    profile = default_research_paper_profile()
    result = validate_research_paper_profile(profile)

    assert result["ok"] is True
    assert profile.profile_id == "user_10k"
    assert profile.paper_capital == 10_000.0
    assert profile.paper_credit_enabled is False
    assert profile.network_enabled is False
    assert profile.llm_enabled is False
    assert profile.full_a_scan_enabled is False
    assert profile.broker_route is None
    assert profile.target_price is None
    assert profile.stop_loss_price is None
    assert profile.research_only is True
    assert profile.paper_only is True
    assert profile.no_broker is True


def test_load_profile_from_json_and_adapter_preserve_10k_and_credit_off(tmp_path: Path) -> None:
    from quantdesk.paper.portfolio_contracts import PaperCapitalProfile
    from quantdesk.trader_profile import adapt_profile_to_paper_capital, load_research_paper_profile

    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "profile_id": "user_10k",
                "paper_capital": 10000,
                "research_universe": ["current_analysis_pool"],
                "max_total_exposure_pct": 0.8,
                "max_single_position_pct": 0.2,
                "max_paper_holdings": 5,
                "min_evidence_samples": 30,
                "review_horizon_days": 20,
                "intraday_paper_review_enabled": True,
            }
        ),
        encoding="utf-8",
    )

    profile = load_research_paper_profile(path)
    capital = adapt_profile_to_paper_capital(profile)

    assert isinstance(capital, PaperCapitalProfile)
    assert capital.profile_id == "user_10k"
    assert capital.initial_capital == 10_000.0
    assert capital.paper_credit_enabled is False
    assert capital.credit_limit == 0.0


def test_profile_may_only_tighten_market_and_risk_ceilings() -> None:
    from quantdesk.trader_profile import apply_profile_ceilings, default_research_paper_profile

    profile = default_research_paper_profile(
        max_total_exposure_pct=0.6,
        max_single_position_pct=0.12,
        max_paper_holdings=4,
    )
    base = {
        "max_total_exposure_pct": 0.9,
        "max_single_position_pct": 0.2,
        "max_paper_holdings": 10,
        "network_enabled": False,
    }

    tightened = apply_profile_ceilings(base, profile)

    assert tightened["max_total_exposure_pct"] == 0.6
    assert tightened["max_single_position_pct"] == 0.12
    assert tightened["max_paper_holdings"] == 4
    assert tightened["network_enabled"] is False


def test_profile_cannot_loosen_existing_ceilings_or_enable_network_llm_full_a_or_credit() -> None:
    from quantdesk.trader_profile import apply_profile_ceilings, default_research_paper_profile

    profile = default_research_paper_profile(
        max_total_exposure_pct=1.0,
        max_single_position_pct=0.5,
        max_paper_holdings=99,
        network_enabled=True,
        llm_enabled=True,
        full_a_scan_enabled=True,
        paper_credit_enabled=True,
        credit_limit=5000,
    )
    base = {
        "max_total_exposure_pct": 0.4,
        "max_single_position_pct": 0.1,
        "max_paper_holdings": 3,
        "network_enabled": False,
        "llm_enabled": False,
        "full_a_scan_enabled": False,
        "paper_credit_enabled": False,
        "credit_limit": 0.0,
    }

    tightened = apply_profile_ceilings(base, profile)

    assert tightened["max_total_exposure_pct"] == 0.4
    assert tightened["max_single_position_pct"] == 0.1
    assert tightened["max_paper_holdings"] == 3
    assert tightened["network_enabled"] is False
    assert tightened["llm_enabled"] is False
    assert tightened["full_a_scan_enabled"] is False
    assert tightened["paper_credit_enabled"] is False
    assert tightened["credit_limit"] == 0.0


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"paper_capital": 100_000}, "paper_capital_requires_acceptance_scenario"),
        ({"min_evidence_samples": 29}, "min_evidence_samples_below_30"),
        ({"max_total_exposure_pct": 1.2}, "max_total_exposure_pct_invalid"),
        ({"max_single_position_pct": 0}, "max_single_position_pct_invalid"),
        ({"max_paper_holdings": 0}, "max_paper_holdings_invalid"),
        ({"review_horizon_days": 0}, "review_horizon_days_invalid"),
        ({"broker_route": "qmt"}, "broker_route_forbidden"),
        ({"target_price": 12.3}, "target_price_forbidden"),
        ({"stop_loss_price": 9.9}, "stop_loss_price_forbidden"),
    ],
)
def test_invalid_profile_fails_closed(overrides: dict, reason: str) -> None:
    from quantdesk.trader_profile import default_research_paper_profile, validate_research_paper_profile

    profile = default_research_paper_profile(**overrides)
    result = validate_research_paper_profile(profile)

    assert result["ok"] is False
    assert reason in result["reason_codes"]


def test_acceptance_100k_requires_explicit_acceptance_scenario() -> None:
    from quantdesk.trader_profile import adapt_profile_to_paper_capital, default_research_paper_profile, validate_research_paper_profile

    profile = default_research_paper_profile(
        profile_id="acceptance_100k",
        paper_capital=100_000,
        acceptance_scenario=True,
        max_total_exposure_pct=0.5,
    )

    assert validate_research_paper_profile(profile)["ok"] is True
    capital = adapt_profile_to_paper_capital(profile)
    assert capital.scenario_id == "acceptance_100k"
    assert capital.initial_capital == 100_000.0

