"""Research/paper user profile contract.

The profile is a local, paper-only preference envelope.  It may tighten risk and
review ceilings, but it must never reset a funded ledger or silently enable
network, LLM, full-A scans, broker routes, real allocation, target/stop prices,
or paper credit.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from quantdesk.paper.portfolio_contracts import PaperCapitalProfile, resolve_paper_capital_profile


DEFAULT_PROFILE_ID = "user_10k"
ACCEPTANCE_PROFILE_ID = "acceptance_100k"
DEFAULT_PAPER_CAPITAL = 10_000.0
ACCEPTANCE_PAPER_CAPITAL = 100_000.0


@dataclass(frozen=True)
class ResearchPaperProfile:
    profile_id: str = DEFAULT_PROFILE_ID
    paper_capital: float = DEFAULT_PAPER_CAPITAL
    research_universe: list[str] = field(default_factory=lambda: ["current_analysis_pool"])
    max_total_exposure_pct: float = 0.80
    max_single_position_pct: float = 0.20
    max_paper_holdings: int = 5
    min_evidence_samples: int = 30
    review_horizon_days: int = 20
    intraday_paper_review_enabled: bool = False
    acceptance_scenario: bool = False
    paper_credit_enabled: bool = False
    credit_limit: float = 0.0
    network_enabled: bool = False
    llm_enabled: bool = False
    full_a_scan_enabled: bool = False
    broker_route: str | None = None
    target_price: float | None = None
    stop_loss_price: float | None = None
    research_only: bool = True
    paper_only: bool = True
    no_broker: bool = True


def _finite_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number


def default_research_paper_profile(**overrides: Any) -> ResearchPaperProfile:
    profile = ResearchPaperProfile()
    if not overrides:
        return profile
    allowed = {field.name for field in ResearchPaperProfile.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    clean = {key: value for key, value in overrides.items() if key in allowed}
    return replace(profile, **clean)


def profile_from_mapping(payload: Mapping[str, Any] | None) -> ResearchPaperProfile:
    data = dict(payload or {})
    universe = data.get("research_universe")
    if isinstance(universe, str):
        universe_list = [universe]
    elif isinstance(universe, list):
        universe_list = [str(item) for item in universe if item]
    else:
        universe_list = ["current_analysis_pool"]
    return ResearchPaperProfile(
        profile_id=str(data.get("profile_id") or DEFAULT_PROFILE_ID),
        paper_capital=_finite_float(data.get("paper_capital"), DEFAULT_PAPER_CAPITAL),
        research_universe=universe_list,
        max_total_exposure_pct=_finite_float(data.get("max_total_exposure_pct"), 0.80),
        max_single_position_pct=_finite_float(data.get("max_single_position_pct"), 0.20),
        max_paper_holdings=_positive_int(data.get("max_paper_holdings"), 5),
        min_evidence_samples=_positive_int(data.get("min_evidence_samples"), 30),
        review_horizon_days=_positive_int(data.get("review_horizon_days"), 20),
        intraday_paper_review_enabled=bool(data.get("intraday_paper_review_enabled", False)),
        acceptance_scenario=bool(data.get("acceptance_scenario", False)),
        paper_credit_enabled=bool(data.get("paper_credit_enabled", False)),
        credit_limit=_finite_float(data.get("credit_limit"), 0.0),
        network_enabled=bool(data.get("network_enabled", False)),
        llm_enabled=bool(data.get("llm_enabled", False)),
        full_a_scan_enabled=bool(data.get("full_a_scan_enabled", False)),
        broker_route=data.get("broker_route"),
        target_price=data.get("target_price"),
        stop_loss_price=data.get("stop_loss_price"),
        research_only=bool(data.get("research_only", True)),
        paper_only=bool(data.get("paper_only", True)),
        no_broker=bool(data.get("no_broker", True)),
    )


def load_research_paper_profile(path: str | Path) -> ResearchPaperProfile:
    profile_path = Path(path)
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("profile_json_must_be_object")
    return profile_from_mapping(payload)


def validate_research_paper_profile(profile: ResearchPaperProfile) -> dict[str, Any]:
    reasons: list[str] = []
    if not math.isfinite(float(profile.paper_capital)) or profile.paper_capital <= 0:
        reasons.append("paper_capital_invalid")
    if profile.paper_capital >= ACCEPTANCE_PAPER_CAPITAL and not profile.acceptance_scenario:
        reasons.append("paper_capital_requires_acceptance_scenario")
    if profile.acceptance_scenario and (
        profile.profile_id != ACCEPTANCE_PROFILE_ID or abs(profile.paper_capital - ACCEPTANCE_PAPER_CAPITAL) > 1e-9
    ):
        reasons.append("acceptance_scenario_profile_mismatch")
    if not profile.acceptance_scenario and (
        profile.profile_id != DEFAULT_PROFILE_ID or abs(profile.paper_capital - DEFAULT_PAPER_CAPITAL) > 1e-9
    ):
        reasons.append("user_profile_must_remain_10k")
    if not profile.research_universe:
        reasons.append("research_universe_empty")
    if not (0 < float(profile.max_total_exposure_pct) <= 1):
        reasons.append("max_total_exposure_pct_invalid")
    if not (0 < float(profile.max_single_position_pct) <= 1):
        reasons.append("max_single_position_pct_invalid")
    if int(profile.max_paper_holdings) <= 0:
        reasons.append("max_paper_holdings_invalid")
    if int(profile.min_evidence_samples) < 30:
        reasons.append("min_evidence_samples_below_30")
    if int(profile.review_horizon_days) <= 0:
        reasons.append("review_horizon_days_invalid")
    if profile.broker_route:
        reasons.append("broker_route_forbidden")
    if profile.target_price is not None:
        reasons.append("target_price_forbidden")
    if profile.stop_loss_price is not None:
        reasons.append("stop_loss_price_forbidden")
    if not profile.research_only:
        reasons.append("research_only_must_be_true")
    if not profile.paper_only:
        reasons.append("paper_only_must_be_true")
    if not profile.no_broker:
        reasons.append("no_broker_must_be_true")
    return {
        "ok": not reasons,
        "status": "passed" if not reasons else "failed",
        "reason_codes": reasons,
        "profile_id": profile.profile_id,
        "paper_capital": float(profile.paper_capital),
        "research_only": profile.research_only,
        "paper_only": profile.paper_only,
        "no_broker": profile.no_broker,
    }


def adapt_profile_to_paper_capital(profile: ResearchPaperProfile) -> PaperCapitalProfile:
    validation = validate_research_paper_profile(profile)
    if not validation.get("ok"):
        raise ValueError("invalid_research_paper_profile:" + ",".join(validation.get("reason_codes") or []))
    scenario = ACCEPTANCE_PROFILE_ID if profile.acceptance_scenario else DEFAULT_PROFILE_ID
    return resolve_paper_capital_profile(
        scenario_id=scenario,
        requested_initial_capital=float(profile.paper_capital),
        paper_credit_enabled=bool(profile.paper_credit_enabled),
        credit_limit=float(profile.credit_limit if profile.paper_credit_enabled else 0.0),
    )


def apply_profile_ceilings(base: Mapping[str, Any], profile: ResearchPaperProfile) -> dict[str, Any]:
    out = dict(base or {})
    out["max_total_exposure_pct"] = min(
        _finite_float(out.get("max_total_exposure_pct"), 1.0),
        float(profile.max_total_exposure_pct),
    )
    out["max_single_position_pct"] = min(
        _finite_float(out.get("max_single_position_pct"), 1.0),
        float(profile.max_single_position_pct),
    )
    out["max_paper_holdings"] = min(
        _positive_int(out.get("max_paper_holdings"), int(profile.max_paper_holdings)),
        int(profile.max_paper_holdings),
    )
    out["min_evidence_samples"] = max(
        _positive_int(out.get("min_evidence_samples"), int(profile.min_evidence_samples)),
        int(profile.min_evidence_samples),
    )
    out["review_horizon_days"] = min(
        _positive_int(out.get("review_horizon_days"), int(profile.review_horizon_days)),
        int(profile.review_horizon_days),
    )
    out["network_enabled"] = bool(out.get("network_enabled")) and bool(profile.network_enabled)
    out["llm_enabled"] = bool(out.get("llm_enabled")) and bool(profile.llm_enabled)
    out["full_a_scan_enabled"] = bool(out.get("full_a_scan_enabled")) and bool(profile.full_a_scan_enabled)
    out["paper_credit_enabled"] = bool(out.get("paper_credit_enabled")) and bool(profile.paper_credit_enabled)
    out["credit_limit"] = min(_finite_float(out.get("credit_limit"), 0.0), float(profile.credit_limit))
    out["research_only"] = True
    out["paper_only"] = True
    out["no_broker"] = True
    return out


__all__ = [
    "ResearchPaperProfile",
    "default_research_paper_profile",
    "profile_from_mapping",
    "load_research_paper_profile",
    "validate_research_paper_profile",
    "adapt_profile_to_paper_capital",
    "apply_profile_ceilings",
]
