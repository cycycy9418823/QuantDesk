"""Shared fail-closed consumer gate for downstream QuantDesk artifacts."""

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
    for key in ("daily", "current_analysis_pool", "features"):
        item = coverage.get(key) if isinstance(coverage.get(key), Mapping) else {}
        if key == "features":
            passed = item.get("status") == "passed"
        else:
            passed = item.get("status") == "passed"
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
        "chain": chain,
        "research_only": True,
        "paper_only": True,
        "no_broker_route": True,
        "live_execution_locked": True,
    }


__all__ = ["evaluate_data_chain_consumer_gate"]
