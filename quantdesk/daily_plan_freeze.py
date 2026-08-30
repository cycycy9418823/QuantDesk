"""Fail-closed T+1 conditional-plan freeze for the daily data chain.

The module only freezes a paper/research plan after the authoritative data
chain has passed for one complete T-day snapshot.  It never simulates a fill,
mutates a paper ledger, connects to a broker, or backfills a plan after the
scheduled T+1 session has opened.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Mapping

from quantdesk.daily_candidate_refresh import (
    build_candidate_envelope,
    resolve_chain_bound_formal_candidate_batch,
    verify_formal_candidate_batch,
)
from quantdesk.market_clock import to_shanghai_datetime
from quantdesk.shared import now_text
from quantdesk.trading_calendar import calendar_info, next_trade_day
from quantdesk.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DATA_CHAIN_CONTRACT_VERSION = 10
MARKET_CLOSE = time(15, 0)
NEXT_SESSION_OPEN = time(9, 30)
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "QMT Order Route Locked / Not Investment Advice / Not Position Advice."
)

StrategyRunner = Callable[..., dict[str, Any]]
PlanBuilder = Callable[..., dict[str, Any]]


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _existing_plan_evidence(path: Path) -> dict[str, Any]:
    try:
        from quantdesk.intraday.intraday_gap_checker import (
            collect_intraday_required_symbols,
        )

        payload = collect_intraday_required_symbols(
            source="plans",
            target_trade_date="",
            plan_path=path,
        )
        sources = (payload.get("source_manifest") or {}).get("sources") or []
        if sources and isinstance(sources[0], Mapping):
            return dict(sources[0])
    except Exception as exc:  # noqa: BLE001 - evidence failure is itself visible
        return {
            "alignment": "audit_failed",
            "formal_eligible": False,
            "reason_codes": ["previous_plan_evidence_audit_failed"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }
    return {
        "alignment": "missing",
        "formal_eligible": False,
        "reason_codes": ["previous_plan_evidence_missing"],
    }


def _blocked(
    *,
    target_date: str,
    trade_date: str = "",
    reason_codes: list[str],
    output_path: Path,
    previous_exists: bool,
    error: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "status": "blocked",
        "written": False,
        "plan_date": target_date,
        "trade_date": trade_date,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "previous_plan_preserved": bool(previous_exists),
        "path": str(output_path),
        "generated_at": now_text(),
        "next_action": (
            "Keep the previous frozen plan unchanged. A new plan may be frozen only "
            "from a fully passed T-day data-chain run before the T+1 session opens."
        ),
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
            "paper_ledger_mutated": False,
        },
        "disclaimer": DISCLAIMER,
    }
    if error:
        payload["error"] = str(error)[:1000]
    if previous_exists:
        payload["previous_plan_evidence"] = _existing_plan_evidence(output_path)
    return payload


def _freeze_window(
    target_date: str,
    *,
    now: datetime | None,
    calendar_path: Path,
) -> dict[str, Any]:
    info = calendar_info(
        calendar_path,
        target_date=target_date,
        start_date=target_date,
        end_date=target_date,
    )
    reason_codes: list[str] = []
    if info.get("weekend_only") or str(info.get("coverage_warning") or ""):
        reason_codes.append("formal_trading_calendar_unavailable")
    try:
        target = date.fromisoformat(str(target_date)[:10])
    except ValueError:
        return {
            "allowed": False,
            "trade_date": "",
            "reason_codes": ["invalid_target_date"],
            "calendar": info,
        }
    try:
        trade_date = next_trade_day(target, calendar_path=calendar_path)
    except Exception as exc:  # noqa: BLE001 - fail closed on calendar defects
        return {
            "allowed": False,
            "trade_date": "",
            "reason_codes": reason_codes + ["next_trade_date_unavailable"],
            "calendar": info,
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }

    trade_info = calendar_info(
        calendar_path,
        target_date=trade_date,
        start_date=target_date,
        end_date=trade_date,
    )
    if trade_info.get("weekend_only") or str(trade_info.get("coverage_warning") or ""):
        reason_codes.append("formal_next_session_calendar_unavailable")

    current = to_shanghai_datetime(now)
    target_close = datetime.combine(target, MARKET_CLOSE, tzinfo=current.tzinfo)
    next_open = datetime.combine(
        date.fromisoformat(trade_date),
        NEXT_SESSION_OPEN,
        tzinfo=current.tzinfo,
    )
    if current < target_close:
        reason_codes.append("target_session_not_closed")
    if current >= next_open:
        reason_codes.append("retroactive_generation_window_closed")
    return {
        "allowed": not reason_codes,
        "trade_date": trade_date,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "calendar": trade_info,
        "current_time": current.isoformat(),
        "target_close": target_close.isoformat(),
        "next_session_open": next_open.isoformat(),
    }


def _formal_batch_strategy_payload(
    batch: Mapping[str, Any],
    *,
    batch_path: Path,
    target_date: str,
) -> dict[str, Any]:
    """Project one verified dated batch into the legacy plan-builder shape.

    The plan builder still accepts the historical strategy-payload contract,
    so this adapter keeps that interface while binding every row to the
    immutable dated formal batch.  No mutable ``*_latest`` file is consulted.
    """

    rows: list[dict[str, Any]] = []
    for raw in batch.get("candidates") or []:
        if not isinstance(raw, Mapping):
            continue
        candidate = copy.deepcopy(dict(raw))
        candidate["candidate_envelope"] = build_candidate_envelope(
            batch,
            batch_path=batch_path,
            candidate=candidate,
        )
        rows.append(candidate)
    board_rows = [
        row
        for row in rows
        if str(row.get("strategy_type") or "").strip().lower().startswith("board")
    ]
    long_rows = [row for row in rows if row not in board_rows]
    provenance = batch.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    return {
        "ok": True,
        "generated_at": str(provenance.get("captured_at") or target_date),
        "candidates": copy.deepcopy(rows),
        "standard_candidates": copy.deepcopy(rows),
        "long_candidates": copy.deepcopy(long_rows),
        "board_candidates": copy.deepcopy(board_rows),
        "formal_candidate_batch": copy.deepcopy(dict(batch)),
        "formal_candidate_batch_path": str(batch_path),
    }


def freeze_t_plus_one_plan(
    *,
    project_root: Path = PROJECT_ROOT,
    data_chain_manifest: Mapping[str, Any],
    now: datetime | None = None,
    calendar_path: Path | None = None,
    strategy_runner: StrategyRunner | None = None,
    plan_builder: PlanBuilder | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Freeze one T+1 paper-only plan without mutating paper-account state."""

    root = Path(project_root).resolve()
    target_date = str(data_chain_manifest.get("target_date") or "")[:10]
    output_path = root / "results" / "conditional_trade_plans_latest.json"
    previous_exists = output_path.exists()

    if str(data_chain_manifest.get("status") or "") != "completed":
        return _blocked(
            target_date=target_date,
            reason_codes=["upstream_data_chain_not_completed"],
            output_path=output_path,
            previous_exists=previous_exists,
        )

    upstream_reasons: list[str] = []
    if data_chain_manifest.get("authoritative") is not True:
        upstream_reasons.append("authoritative_data_chain_required")
    if data_chain_manifest.get("network") is not True:
        upstream_reasons.append("network_authorized_data_chain_required")
    if data_chain_manifest.get("dry_run") is True:
        upstream_reasons.append("dry_run_cannot_freeze_plan")
    try:
        contract_version = int(
            data_chain_manifest.get("daily_data_chain_contract_version") or 0
        )
    except (TypeError, ValueError):
        contract_version = 0
    if contract_version != REQUIRED_DATA_CHAIN_CONTRACT_VERSION:
        upstream_reasons.append("data_chain_contract_version_mismatch")
    if not str(data_chain_manifest.get("run_id") or "").strip():
        upstream_reasons.append("data_chain_run_id_missing")
    coverage = data_chain_manifest.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    for key in ("daily", "current_analysis_pool", "features"):
        component = coverage.get(key)
        component = component if isinstance(component, Mapping) else {}
        if str(component.get("status") or "") != "passed":
            upstream_reasons.append(f"upstream_{key}_gate_not_passed")
    universe = data_chain_manifest.get("universe")
    universe = universe if isinstance(universe, Mapping) else {}
    analysis_pool = universe.get("current_analysis_pool")
    analysis_pool = analysis_pool if isinstance(analysis_pool, Mapping) else {}
    if not _is_sha256(analysis_pool.get("sha256")):
        upstream_reasons.append("data_chain_universe_sha256_missing")
    components = data_chain_manifest.get("components")
    components = components if isinstance(components, Mapping) else {}
    feature_component = components.get("features")
    feature_component = feature_component if isinstance(feature_component, Mapping) else {}
    feature_gate = coverage.get("features")
    feature_gate = feature_gate if isinstance(feature_gate, Mapping) else {}
    feature_sha256 = str(
        feature_component.get("output_sha256")
        or feature_gate.get("output_sha256")
        or ""
    )
    if not _is_sha256(feature_sha256):
        upstream_reasons.append("data_chain_feature_sha256_missing")
    if upstream_reasons:
        return _blocked(
            target_date=target_date,
            reason_codes=upstream_reasons,
            output_path=output_path,
            previous_exists=previous_exists,
        )

    resolved_calendar_path = Path(
        calendar_path or root / "data" / "calendar" / "a_share_trading_calendar.csv"
    )
    window = _freeze_window(
        target_date,
        now=now,
        calendar_path=resolved_calendar_path,
    )
    trade_date = str(window.get("trade_date") or "")[:10]
    if not window.get("allowed"):
        return {
            **_blocked(
                target_date=target_date,
                trade_date=trade_date,
                reason_codes=list(window.get("reason_codes") or []),
                output_path=output_path,
                previous_exists=previous_exists,
                error=str(window.get("error") or ""),
            ),
            "freeze_window": window,
        }

    # Candidate intake is cryptographically revalidated. The dated snapshot is
    # preferred; an exact run-scoped snapshot is allowed only for the narrow
    # append-only same-day retry conflict handled by the shared resolver.
    # ``strategy_runner`` remains for compatibility but is never invoked.
    formal_load = resolve_chain_bound_formal_candidate_batch(
        root,
        data_chain_manifest=data_chain_manifest,
    )
    formal_path = Path(str(formal_load.get("path") or ""))
    formal_batch = formal_load.get("batch")
    formal_source_mode = str(formal_load.get("source_mode") or "")
    formal_reasons = list(formal_load.get("reason_codes") or [])
    if formal_load.get("status") != "passed" or not isinstance(formal_batch, Mapping):
        return {
            **_blocked(
                target_date=target_date,
                trade_date=trade_date,
                reason_codes=formal_reasons or ["formal_candidate_batch_not_passed"],
                output_path=output_path,
                previous_exists=previous_exists,
            ),
            "freeze_window": window,
            "formal_candidate_batch_path": str(formal_path),
            "formal_candidate_batch_source_mode": formal_source_mode,
        }

    formal_reasons = verify_formal_candidate_batch(formal_batch)
    provenance = formal_batch.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    source_run_id = str(provenance.get("source_run_id") or "")
    source_universe_hash = str(provenance.get("source_universe_hash") or "").lower()
    source_feature_hash = str(provenance.get("source_feature_hash") or "").lower()
    if source_run_id != str(data_chain_manifest.get("run_id") or ""):
        formal_reasons.append("formal_candidate_batch_source_run_mismatch")
    if source_universe_hash != str(analysis_pool.get("sha256") or "").lower():
        formal_reasons.append("formal_candidate_batch_source_universe_mismatch")
    if source_feature_hash != feature_sha256.lower():
        formal_reasons.append("formal_candidate_batch_source_feature_mismatch")
    if formal_reasons:
        return {
            **_blocked(
                target_date=target_date,
                trade_date=trade_date,
                reason_codes=list(dict.fromkeys(formal_reasons)),
                output_path=output_path,
                previous_exists=previous_exists,
            ),
            "freeze_window": window,
            "formal_candidate_batch_path": str(formal_path),
            "formal_candidate_batch_source_mode": formal_source_mode,
        }

    strategy_payload = _formal_batch_strategy_payload(
        formal_batch,
        batch_path=formal_path,
        target_date=target_date,
    )

    if plan_builder is None:
        from quantdesk.paper.conditional_trade_plan import build_conditional_trade_plan

        plan_builder = build_conditional_trade_plan
    try:
        plan = plan_builder(
            auto_strategy_payload=dict(strategy_payload),
            strategy="all",
            quantity_per_stock=100,
            plan_date=target_date,
            trade_date=trade_date,
            source_data_cutoff=target_date,
            write=False,
            output_path=output_path,
            state_path=root / "state" / "auto_paper_portfolio.json",
            daily_dir=root / "data" / "daily",
            daily_feature_path=root / "data" / "features" / "daily_features_latest.csv",
            low_price_universe_path=root / "data" / "universe" / "low_price_learning_symbols.csv",
            session_evidence_metadata_dir=root / "data" / "metadata",
        )
    except Exception as exc:  # noqa: BLE001 - preserve previous plan on any failure
        return {
            **_blocked(
                target_date=target_date,
                trade_date=trade_date,
                reason_codes=["conditional_plan_build_failed"],
                output_path=output_path,
                previous_exists=previous_exists,
                error=f"{type(exc).__name__}:{exc}",
            ),
            "freeze_window": window,
        }

    plan_reasons: list[str] = []
    if not isinstance(plan, Mapping) or plan.get("ok") is not True:
        plan_reasons.append("conditional_plan_not_ok")
    if str(plan.get("plan_date") or "")[:10] != target_date:
        plan_reasons.append("conditional_plan_date_mismatch")
    if str(plan.get("trade_date") or "")[:10] != trade_date:
        plan_reasons.append("conditional_trade_date_mismatch")
    if str(plan.get("source_data_cutoff") or "")[:10] != target_date:
        plan_reasons.append("conditional_plan_cutoff_mismatch")
    price_validation = plan.get("price_data_validation")
    price_validation = (
        price_validation if isinstance(price_validation, Mapping) else {}
    )
    if (
        plan.get("formal_execution_eligible") is not True
        or str(price_validation.get("status") or "") != "passed"
        or price_validation.get("formal_execution_eligible") is not True
        or list(price_validation.get("blocked_symbols") or [])
    ):
        plan_reasons.append("conditional_plan_price_data_not_formal")
    if str(price_validation.get("target_date") or "")[:10] != target_date:
        plan_reasons.append("conditional_plan_price_target_date_mismatch")
    if plan_reasons:
        return {
            **_blocked(
                target_date=target_date,
                trade_date=trade_date,
                reason_codes=plan_reasons,
                output_path=output_path,
                previous_exists=previous_exists,
            ),
            "freeze_window": window,
        }

    frozen = dict(plan)
    frozen.update(
        {
            "freeze_status": "completed",
            "frozen_at": to_shanghai_datetime(now).isoformat(),
            "data_chain_run_id": str(data_chain_manifest.get("run_id") or ""),
            "data_chain_target_date": target_date,
            "data_chain_contract_version": data_chain_manifest.get(
                "daily_data_chain_contract_version"
            ),
            "data_chain_universe_version": str(analysis_pool.get("version") or ""),
            "data_chain_universe_sha256": str(analysis_pool.get("sha256") or ""),
            "data_chain_feature_sha256": str(
                feature_component.get("output_sha256")
                or feature_gate.get("output_sha256")
                or ""
            ),
            "formal_candidate_batch_path": str(formal_path),
            "formal_candidate_batch_source_mode": formal_source_mode,
            "formal_candidate_batch_provenance_hash": str(
                provenance.get("hash") or ""
            ).lower(),
            "freeze_window": window,
            "boundaries": {
                "research_only": True,
                "paper_account_only": True,
                "broker_route": False,
                "live_execution": False,
                "paper_ledger_mutated": False,
            },
            "disclaimer": DISCLAIMER,
        }
    )
    if write:
        atomic_write_json(output_path, frozen)
    return {
        "ok": True,
        "status": "completed" if write else "prepared",
        "prepared": not write,
        "written": bool(write),
        "plan_id": str(frozen.get("plan_id") or ""),
        "plan_date": target_date,
        "trade_date": trade_date,
        "source_data_cutoff": target_date,
        "symbol_count": len(frozen.get("symbols") or []),
        "data_chain_run_id": str(data_chain_manifest.get("run_id") or ""),
        "formal_candidate_batch_path": str(formal_path),
        "formal_candidate_batch_source_mode": formal_source_mode,
        "formal_candidate_batch_provenance_hash": str(
            provenance.get("hash") or ""
        ).lower(),
        "path": str(output_path),
        "reason_codes": [],
        "freeze_window": window,
        **({"prepared_plan": frozen} if not write else {}),
        "boundaries": dict(frozen["boundaries"]),
        "disclaimer": DISCLAIMER,
    }


__all__ = ["REQUIRED_DATA_CHAIN_CONTRACT_VERSION", "freeze_t_plus_one_plan"]
