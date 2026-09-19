"""Admission for daily research readers; execution still verifies its own prices."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from quantdesk.daily_data_chain import latest_daily_data_chain


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def evaluate_data_chain_consumer_gate(
    chain_payload: Mapping[str, Any] | None = None,
    *,
    project_root: str | Path | None = None,
    target_date: str | None = None,
    required_universe_sha256: str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve() if project_root is not None else PROJECT_ROOT
    chain = (
        dict(chain_payload)
        if isinstance(chain_payload, Mapping)
        else latest_daily_data_chain(root)
    )
    reasons: list[str] = []
    status = str(chain.get("status") or "not_run")
    if not chain.get("exists", bool(chain)):
        reasons.append("daily_chain_not_run")
    if status != "completed":
        reasons.append(f"daily_chain_status_{status}")
    if chain.get("authoritative") is not True:
        reasons.append("daily_chain_not_authoritative")
    if chain.get("published_latest") is not True:
        reasons.append("daily_chain_latest_not_published")
    if target_date and str(chain.get("target_date") or "")[:10] != str(target_date)[:10]:
        reasons.append("daily_chain_target_date_mismatch")
    if required_universe_sha256:
        actual = (
            chain.get("universe", {})
            .get("current_analysis_pool", {})
            .get("sha256")
        )
        if actual != required_universe_sha256:
            reasons.append("daily_chain_universe_digest_mismatch")
    coverage = chain.get("coverage") if isinstance(chain.get("coverage"), Mapping) else {}
    research_eligibility: dict[str, Any] = {}
    if any(
        isinstance(coverage.get(key), Mapping) and coverage[key].get("status") != "passed"
        for key in ("daily", "features")
    ):
        from quantdesk.daily_candidate_refresh import candidate_refresh_gate

        research_eligibility = candidate_refresh_gate(chain)
    for key in ("daily", "current_analysis_pool", "features"):
        item = coverage.get(key) if isinstance(coverage.get(key), Mapping) else {}
        passed = item.get("status") == "passed"
        if key in {"daily", "features"} and not passed:
            # Published per-symbol gaps remain visible. Reuse the same structural,
            # raw-price and provenance checks as formal research admission.
            passed = research_eligibility.get("formal_eligible") is True
        if key == "current_analysis_pool" and item.get("status") == "not_requested":
            passed = (
                set(item.get("reason_codes") or []) == {"analysis_pool_minute_on_demand"}
                and item.get("requested_count") == 0 and item.get("audited_count") == 0
            )
        if not passed:
            reasons.append(f"daily_chain_coverage_{key}_not_passed")
    if chain.get("protected_state_unchanged") is not True:
        reasons.append("daily_chain_protected_state_not_verified")
    reasons = list(dict.fromkeys(reasons))
    return {
        "status": "passed" if not reasons else "blocked",
        "allowed": not reasons,
        "authoritative": chain.get("authoritative") is True,
        "run_id": str(chain.get("run_id") or ""),
        "target_date": str(chain.get("target_date") or "")[:10],
        "reasons": reasons,
        "chain_status": status,
        "scope": "daily_research",
        "execution_authorized": False,
        "research_eligibility": research_eligibility,
        "chain": chain,
        "research_only": True,
        "paper_only": True,
        "no_broker_route": True,
        "live_execution_locked": True,
    }


__all__ = ["evaluate_data_chain_consumer_gate"]
