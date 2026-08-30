"""Finite, read-only project acceptance aggregation.

This module intentionally keeps engineering readiness, natural observations,
strategy evidence, and cloud deployment as independent dispositions. A
strategy alpha veto or deferred cloud cutover must not erase a functioning
local research-tool acceptance.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from quantdesk import daily_data_chain as _daily_data_chain
from quantdesk.paper.stage4_final_evaluator import (
    validate_stage4_final_evaluation_artifact,
    validate_stage4_final_evaluation_pair,
)

PROJECT_ACCEPTANCE_SCHEMA_VERSION = "project_acceptance_v1"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


#: Daily-chain statuses that count as a successful, authoritative run.
_CHAIN_SUCCESS_STATUSES = frozenset({"completed", "skipped_already_completed"})

#: Statuses where the newest attempt is still open or has failed. These must
#: never be masked by an older successful run: doing so made acceptance look
#: healthy while the current run was actually running, blocked, or failed.
_CHAIN_UNRESOLVED_STATUSES = frozenset({"running", "blocked", "failed", "abandoned"})
_ALPHA_EVIDENCE_SCHEMA_VERSION = "formal_tradable_alpha_adjudication_v1"


def latest_daily_data_chain(root: Path) -> dict[str, Any]:
    """Resolve the chain through the runtime state-aware reader."""

    return dict(_daily_data_chain.latest_daily_data_chain(root))


def _last_successful_daily_chain(root: Path) -> dict[str, Any]:
    """Newest successful chain run found in the append-only run archive."""

    runs_dir = root / "results" / "data_chain" / "runs"
    candidates: list[dict[str, Any]] = []
    if runs_dir.is_dir():
        for path in runs_dir.glob("*/manifest.json"):
            payload = _read_json(path)
            if payload.get("status") in _CHAIN_SUCCESS_STATUSES:
                candidates.append(payload)
    return max(
        candidates,
        key=lambda item: (
            str(item.get("target_date") or ""),
            str(item.get("completed_at") or item.get("generated_at") or ""),
        ),
        default={},
    )


def _daily_chain_states(root: Path, latest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the current attempt and the last success as separate facts.

    The latest attempt is authoritative for "is the loop healthy right now".
    An older successful run may only explain "when did it last succeed"; it can
    never stand in for a `running`, `blocked`, or `failed` newest attempt.
    """

    attempt = (
        dict(latest)
        if latest is not None
        else dict(latest_daily_data_chain(root))
    )
    attempt_status = str(attempt.get("status") or "")
    if attempt_status in _CHAIN_SUCCESS_STATUSES:
        return {
            "latest_attempt": attempt,
            "last_successful": attempt,
            "latest_attempt_is_successful": True,
            "superseded_attempt": {},
        }

    successful = _last_successful_daily_chain(root)
    return {
        "latest_attempt": attempt,
        "last_successful": successful,
        "latest_attempt_is_successful": False,
        # Kept for traceability: the older success that an earlier revision would
        # have silently promoted over this unresolved attempt.
        "superseded_attempt": (
            {
                "status": attempt_status,
                "run_id": attempt.get("run_id"),
                "target_date": attempt.get("target_date"),
            }
            if attempt and successful
            else {}
        ),
    }


def _load_factor_verdicts(root: Path) -> list[dict[str, Any]]:
    verdicts: list[dict[str, Any]] = []
    for relative in (
        "results/round22_factor_adjudication/adjudication_latest.json",
        "results/round23_reversal_factor/adjudication_latest.json",
        "results/alpha_adjudication/alpha_latest.json",
    ):
        payload = _read_json(root / relative)
        if payload.get("task_type") == "formal_tradable_alpha_adjudication":
            verdicts.append(payload)
            continue
        for item in payload.get("verdicts") or []:
            if isinstance(item, Mapping):
                verdicts.append(dict(item))
    return verdicts


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 71 and text.startswith("sha256:") and all(
        char in "0123456789abcdef" for char in text[7:]
    )


def _safe_ratio(value: Any) -> float | None:
    """Parse a finite coverage ratio without letting malformed evidence raise."""

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _is_formal_tradable_alpha_verdict(
    item: Mapping[str, Any], *, project_root: Path
) -> bool:
    """Require an explicit, provenance-bound alpha evidence contract.

    A factor ``keep`` verdict is a statistical dimension result, not by itself
    proof of a tradable alpha signal.  The producer must explicitly opt into
    this acceptance disposition and bind the row to a versioned, hashed
    source artifact.
    """

    if (
        str(item.get("verdict") or "") != "keep"
        or str(item.get("dimension") or "") in {"", "risk"}
        or item.get("formal_evidence_eligible") is not True
        or str(item.get("evidence_schema_version") or "")
        != _ALPHA_EVIDENCE_SCHEMA_VERSION
        or not str(item.get("adjudication_id") or "").strip()
        or str(item.get("publication_status") or "") != "approved"
    ):
        return False
    holdout = item.get("holdout")
    cost_model = item.get("cost_model")
    tradability = item.get("tradability")
    gates = item.get("gates")
    if not (
        isinstance(holdout, Mapping)
        and holdout.get("independent") is True
        and str(holdout.get("status") or "") == "passed"
        and isinstance(cost_model, Mapping)
        and cost_model.get("net_of_costs") is True
        and isinstance(tradability, Mapping)
        and tradability.get("checked") is True
        and (_safe_ratio(tradability.get("coverage")) or 0.0) >= 0.95
        and isinstance(gates, Mapping)
        and all(gates.get(name) is True for name in ("holdout", "cost", "tradability"))
    ):
        return False
    provenance = item.get("provenance")
    if not isinstance(provenance, Mapping):
        return False
    source_path = str(provenance.get("source_path") or "").strip()
    declared_hash = str(provenance.get("source_hash") or "").strip().lower()
    if not source_path or not _is_sha256(declared_hash):
        return False
    candidate = Path(source_path)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        candidate = candidate.resolve(strict=True)
        if not candidate.is_relative_to(project_root.resolve()):
            return False
        actual_hash = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    except (OSError, RuntimeError):
        return False
    if actual_hash != declared_hash:
        return False
    preregistration = item.get("preregistration")
    if not isinstance(preregistration, Mapping):
        return False
    prereg_path = str(preregistration.get("path") or "").strip()
    prereg_hash = str(
        preregistration.get("source_hash")
        or preregistration.get("preregistration_hash")
        or ""
    ).strip().lower()
    if not prereg_path or not _is_sha256(prereg_hash):
        return False
    prereg_candidate = Path(prereg_path)
    if not prereg_candidate.is_absolute():
        prereg_candidate = project_root / prereg_candidate
    try:
        prereg_candidate = prereg_candidate.resolve(strict=True)
        if not prereg_candidate.is_relative_to(project_root.resolve()):
            return False
        prereg_actual_hash = "sha256:" + hashlib.sha256(
            prereg_candidate.read_bytes()
        ).hexdigest()
    except (OSError, RuntimeError):
        return False
    if prereg_actual_hash != prereg_hash:
        return False
    if str(preregistration.get("schema_version") or "") != "formal_alpha_preregistration_v1":
        return False
    alpha_evidence = item.get("alpha_evidence")
    return isinstance(alpha_evidence, Mapping) and (
        str(alpha_evidence.get("evidence_kind") or "") == "formal"
        and alpha_evidence.get("tradable") is True
        and alpha_evidence.get("independent_holdout") is True
        and alpha_evidence.get("net_of_costs") is True
        and alpha_evidence.get("tradeability_checked") is True
    )


def _mechanics_acceptance() -> dict[str, Any]:
    from quantdesk.paper.acceptance import (
        build_deterministic_acceptance_fixture,
        run_paper_acceptance_suite,
    )

    scenarios = [
        build_deterministic_acceptance_fixture(
            profile_id="user_10k",
            completed_trades=32,
            seed=42,
        ),
        build_deterministic_acceptance_fixture(
            profile_id="acceptance_100k",
            completed_trades=32,
            seed=42,
        ),
    ]
    suite = run_paper_acceptance_suite(scenarios)
    return {
        "status": suite.get("status"),
        "acceptance_passed": suite.get("acceptance_passed"),
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
    }


def _stage4_disposition(
    stage4: Mapping[str, Any],
    stage4_final: Mapping[str, Any],
    as_of: str,
    *,
    project_root: Path,
) -> dict[str, Any]:
    milestone_dates = stage4.get("milestone_dates")
    terminal_date = (
        str(milestone_dates.get("T0+18") or "")[:10]
        if isinstance(milestone_dates, Mapping)
        else ""
    )
    try:
        terminal_reached = bool(terminal_date and date.fromisoformat(as_of) >= date.fromisoformat(terminal_date))
    except ValueError:
        terminal_reached = False
    if not terminal_reached:
        return {
            "status": "pending",
            "reason_codes": list(stage4.get("reason_codes") or []),
            "maintenance_disposition": stage4.get(
                "maintenance_disposition", "pending"
            ),
            "sample_disposition": stage4.get(
                "sample_disposition", "not_evaluable"
            ),
            "evaluator_verdict": None,
        }
    # Acceptance must use the dated artifact + latest pointer + raw adapter hash.
    # The optional mapping is retained only as a read-only caller override after
    # the on-disk provenance gate succeeds; it can never promote an unpaired file.
    valid_pair, _reason = validate_stage4_final_evaluation_pair(
        project_root, terminal_date
    )
    valid_final = valid_pair
    if valid_pair:
        stage4_final = _read_json(
            project_root
            / "results"
            / "data_health"
            / "stage4_final_evaluation_latest.json"
        )
        valid_final, _reason = validate_stage4_final_evaluation_artifact(
            stage4_final, terminal_date
        )
    if not valid_final:
        return {
            "status": "terminal_artifact_pending",
            "reason_codes": ["stage4_terminal_evaluation_missing_or_stale"],
            "maintenance_disposition": "pending",
            "sample_disposition": "not_evaluable",
            "evaluator_verdict": None,
        }
    return {
        "status": str(stage4_final.get("terminal_status") or "terminal_artifact_pending"),
        "reason_codes": list(stage4_final.get("reason_codes") or []),
        "maintenance_disposition": stage4_final.get("maintenance_disposition"),
        "sample_disposition": stage4_final.get("sample_disposition"),
        "evaluator_verdict": stage4_final.get("evaluator_verdict"),
    }


def build_project_acceptance(
    project_root: Path,
    *,
    as_of: str,
    paper_mechanics: Mapping[str, Any] | None = None,
    daily_chain: Mapping[str, Any] | None = None,
    stage4: Mapping[str, Any] | None = None,
    stage4_final: Mapping[str, Any] | None = None,
    factor_verdicts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable acceptance payload without changing project state."""

    root = Path(project_root).resolve()
    mechanics = dict(paper_mechanics or _mechanics_acceptance())
    chain_states = _daily_chain_states(root, daily_chain)
    # `chain` stays the *current* attempt so a running/blocked/failed run is
    # never hidden behind an older success.
    chain = dict(chain_states["latest_attempt"])
    last_successful_chain = dict(chain_states["last_successful"])
    stage4_payload = dict(
        stage4
        or _read_json(
            root
            / "results"
            / "data_health"
            / "stage4_maintenance_evidence_latest.json"
        )
    )
    stage4_final_payload = dict(
        stage4_final
        or _read_json(
            root
            / "results"
            / "data_health"
            / "stage4_final_evaluation_latest.json"
        )
    )
    verdicts = [
        dict(item)
        for item in (factor_verdicts if factor_verdicts is not None else _load_factor_verdicts(root))
        if isinstance(item, Mapping)
    ]

    mechanics_ok = (
        mechanics.get("status") in {"passed", "insufficient_sample"}
        and mechanics.get("acceptance_passed") is True
        and mechanics.get("research_only") is True
        and mechanics.get("paper_only") is True
        and mechanics.get("no_broker") is True
    )
    chain_status = str(chain.get("status") or "")
    chain_ok = (
        chain_status in _CHAIN_SUCCESS_STATUSES
        and chain.get("authoritative") is not False
    )
    local_status = "accepted" if mechanics_ok and chain_ok else "pending"

    chain_reason_codes: list[str] = []
    if not chain_ok:
        if chain_status == "running":
            chain_reason_codes.append("latest_daily_chain_attempt_still_running")
        elif chain_status in _CHAIN_UNRESOLVED_STATUSES:
            chain_reason_codes.append(f"latest_daily_chain_attempt_{chain_status}")
        elif not chain_status:
            chain_reason_codes.append("latest_daily_chain_attempt_missing")
        else:
            chain_reason_codes.append(f"latest_daily_chain_attempt_status_{chain_status}")
        if chain.get("authoritative") is False:
            chain_reason_codes.append("latest_daily_chain_attempt_not_authoritative")
        if last_successful_chain:
            # Explicit: an older success exists but must not be read as current health.
            chain_reason_codes.append("older_successful_run_does_not_supersede_current_attempt")
    ops = _stage4_disposition(
        stage4_payload,
        stage4_final_payload,
        as_of,
        project_root=root,
    )
    alpha_validated = any(
        _is_formal_tradable_alpha_verdict(item, project_root=root) for item in verdicts
    )
    strategy_status = "validated" if alpha_validated else "not_validated"
    strategy_reasons = (
        []
        if alpha_validated
        else ["no_formal_tradable_alpha_signal_passed_adjudication"]
    )

    return {
        "schema_version": PROJECT_ACCEPTANCE_SCHEMA_VERSION,
        "task_type": "project_acceptance",
        "generated_for": str(as_of)[:10],
        "status": (
            "local_research_tool_accepted"
            if local_status == "accepted"
            else "local_research_tool_pending"
        ),
        "ok": local_status == "accepted",
        "dispositions": {
            "local_research_tool": {
                "status": local_status,
                "mechanics_status": mechanics.get("status"),
                # Compatibility: unchanged key, still the *current* attempt status.
                "daily_chain_status": chain.get("status"),
                "daily_chain_latest_attempt_status": chain.get("status"),
                "daily_chain_last_successful_status": last_successful_chain.get("status"),
                "daily_chain_last_successful_target_date": last_successful_chain.get("target_date"),
                "latest_attempt_is_successful": bool(
                    chain_states.get("latest_attempt_is_successful")
                ),
                "reason_codes": []
                if local_status == "accepted"
                else (["engineering_or_runtime_acceptance_not_complete"] + chain_reason_codes),
            },
            "operations_observation": {
                "status": ops.get("status", "pending"),
                "reason_codes": list(ops.get("reason_codes") or []),
                "maintenance_disposition": ops.get("maintenance_disposition"),
                "sample_disposition": ops.get("sample_disposition"),
                "evaluator_verdict": ops.get("evaluator_verdict"),
            },
            "strategy_alpha": {
                "status": strategy_status,
                "reason_codes": strategy_reasons,
                "formal_evidence": bool(alpha_validated),
            },
            "cloud_deployment": {
                "status": "deferred",
                "reason_codes": ["local_authoritative_writer_cloud_read_only"],
                "writer_authority": "local_only",
            },
        },
        "boundaries": {
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "live_execution_locked": True,
        },
        "evidence": {
            "daily_chain": {
                # Compatibility keys: these describe the current attempt.
                "run_id": chain.get("run_id"),
                "status": chain.get("status"),
                "target_date": chain.get("target_date"),
                "authoritative": chain.get("authoritative"),
                "latest_attempt": {
                    "run_id": chain.get("run_id"),
                    "status": chain.get("status"),
                    "target_date": chain.get("target_date"),
                    "authoritative": chain.get("authoritative"),
                    "is_successful": bool(chain_states.get("latest_attempt_is_successful")),
                },
                "last_successful": {
                    "run_id": last_successful_chain.get("run_id"),
                    "status": last_successful_chain.get("status"),
                    "target_date": last_successful_chain.get("target_date"),
                    "completed_at": last_successful_chain.get("completed_at")
                    or last_successful_chain.get("generated_at"),
                },
                "superseded_attempt": dict(chain_states.get("superseded_attempt") or {}),
                "acceptable": chain_ok,
                "reason_codes": chain_reason_codes,
            },
            # Keep the pre-state-machine top-level keys for existing readers;
            # the nested `daily_chain` object above remains authoritative.
            "daily_chain_last_successful": {
                "run_id": last_successful_chain.get("run_id"),
                "status": last_successful_chain.get("status"),
                "target_date": last_successful_chain.get("target_date"),
                "completed_at": last_successful_chain.get("completed_at")
                or last_successful_chain.get("generated_at"),
            },
            "daily_chain_superseded_attempt": dict(
                chain_states.get("superseded_attempt") or {}
            ),
            "stage4": {
                "status": stage4_payload.get("status"),
                "as_of": stage4_payload.get("as_of"),
                "t0": stage4_payload.get("t0"),
                "milestone_dates": stage4_payload.get("milestone_dates") or {},
                "reason_codes": list(stage4_payload.get("reason_codes") or []),
                "missing_shadow_dates": [
                    str(row.get("date"))
                    for row in (stage4_payload.get("daily_counts") or [])
                    if isinstance(row, Mapping)
                    and row.get("shadow_status") != "observed"
                    and row.get("date")
                ],
                "final_evaluation": {
                    "schema_version": stage4_final_payload.get("schema_version"),
                    "as_of": stage4_final_payload.get("as_of"),
                    "terminal_status": stage4_final_payload.get("terminal_status"),
                    "maintenance_disposition": stage4_final_payload.get(
                        "maintenance_disposition"
                    ),
                    "sample_disposition": stage4_final_payload.get(
                        "sample_disposition"
                    ),
                    "evaluator_verdict": stage4_final_payload.get(
                        "evaluator_verdict"
                    ),
                    "source_adapter": dict(
                        stage4_final_payload.get("source_adapter") or {}
                    ),
                },
            },
            "factor_verdict_count": len(verdicts),
        },
        "disclaimer": (
            "Research Only / Paper Account Only / No Broker Route / "
            "Not Investment Advice / Not Position Advice."
        ),
    }


__all__ = ["PROJECT_ACCEPTANCE_SCHEMA_VERSION", "build_project_acceptance"]
