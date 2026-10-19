"""Checkpointed daily reconciliation of the current A-share daily universe.

The existing daily chain refreshes the research pool and the weekly wide-pool
task refreshes existing residual files.  This coordinator makes the remaining
contract explicit: every current-universe symbol is either current, pending, or
explicitly excluded.  It does not run by import and defaults to offline/no
apply, so callers must deliberately authorize canonical writes.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from quantdesk.daily_price_update_engine import update_daily_prices
from quantdesk.data_health_alert_delivery import dispatch_data_health_alerts
from quantdesk.full_a_daily_coverage import build_full_a_daily_coverage
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.suspension_aware_completeness import (
    SuspensionEvidence,
    classify_universe_completeness,
    load_run_session_evidence,
)
from quantdesk.utils import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_RELATIVE = Path("data/universe/a_share_symbols.csv")
RESEARCH_POOL_RELATIVE = Path("data/universe/low_price_learning_symbols.csv")
DAILY_RELATIVE = Path("data/daily")
LOCK_RELATIVE = Path("state/daily_data_chain.lock")
RUNS_RELATIVE = Path("results/data_chain/full_a_daily_reconciliation/runs")
LATEST_RELATIVE = Path("results/data_chain/full_a_daily_reconciliation_latest.json")
# The single published pointer the reconciliation binds its run evidence to. A
# pointer, not a directory scan: it is written once per chain run and carries
# the run id and target date that the evidence belongs to.
CHAIN_LATEST_RELATIVE = Path("results/data_chain/daily_data_chain_latest.json")
# The schema the chain writes for the bounded gap-recovery component. A pointer
# that names another schema is not this contract's evidence.
RUN_SESSION_SCHEMA = "target_day_gap_recovery.v1"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10].replace("/", "-") if len(text) >= 10 else ""


def _read_symbols(path: Path, *, included_only: bool = False) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if included_only and not _truthy(row.get("included")):
                continue
            symbol = str(row.get("symbol") or row.get("code") or "").strip()
            if len(symbol) == 6 and symbol.isdigit() and symbol not in out:
                out.append(symbol)
    return out


def _last_date(path: Path) -> str:
    if not path.is_file():
        return ""
    latest = ""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = _date_text(row.get("date") or row.get("trade_date"))
                if day > latest:
                    latest = day
    except (OSError, UnicodeDecodeError, csv.Error):
        return ""
    return latest


def _binding_contradictions(
    binding: Mapping[str, Any],
    *,
    target: str,
    run_id: str,
    schema_version: str,
) -> list[str]:
    """Fields where an explicit ``evidence_source`` disagrees with the pointer.

    Only *present* fields are compared: an older pointer that omits the binding
    stays compatible, while one that states a different run, day, producer or
    schema is a contradiction and must not be consumed as this run's evidence.
    """
    problems: list[str] = []
    binding_target = _date_text(binding.get("target_date"))
    if binding_target and binding_target != target:
        problems.append("target_date")
    binding_run = str(binding.get("run_id") or "").strip()
    if run_id and binding_run and binding_run != run_id:
        problems.append("run_id")
    binding_source = str(binding.get("source") or "").strip()
    if binding_source and binding_source != "target_day_gap_recovery":
        problems.append("source")
    binding_schema = str(binding.get("schema_version") or "").strip()
    if schema_version and binding_schema and binding_schema != schema_version:
        problems.append("schema_version")
    return problems


def resolve_run_session_evidence(
    project_root: str | Path,
    *,
    target_date: str,
    run_evidence_dir: str | Path | None = None,
    universe_symbols: list[str] | None = None,
) -> SuspensionEvidence:
    """Bind this reconciliation to ONE run's validated no-session evidence.

    Precedence:

    1. an explicit ``run_evidence_dir`` (the caller already knows the run), or
    2. the daily-data-chain *published pointer* for the SAME target date.

    The pointer is a single published artifact, never a history scan, and its
    ``target_date`` must match exactly: a stale pointer left over from another
    day is refused rather than silently reused. Everything the pointer offers is
    still re-validated by the strict session-evidence contract.
    """
    target = _date_text(target_date)
    if run_evidence_dir is not None:
        return load_run_session_evidence(
            target,
            run_evidence_dir=run_evidence_dir,
            expected_symbols=universe_symbols,
        )
    if not target:
        return SuspensionEvidence(target_date=target, present=False, warnings=("run_evidence_target_missing",))
    pointer = Path(project_root).resolve() / CHAIN_LATEST_RELATIVE
    if not pointer.is_file():
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=("run_session_evidence_pointer_missing",),
        )
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=(f"run_session_evidence_pointer_unreadable:{type(exc).__name__}",),
        )
    if not isinstance(payload, Mapping):
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=("run_session_evidence_pointer_not_an_object",),
        )
    pointer_target = _date_text(payload.get("target_date"))
    if pointer_target != target:
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=(f"run_session_evidence_pointer_date_mismatch:{pointer_target or '-'}",),
        )
    # A published pointer is only usable when its own binding is coherent. Absent
    # fields stay compatible with older pointers, but an explicit contradiction
    # is refused rather than re-labelled as this run's evidence.
    if "authoritative" in payload and not _truthy(payload.get("authoritative")):
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=("run_session_evidence_pointer_not_authoritative",),
        )
    components = payload.get("components")
    component = components.get("target_day_gap_recovery") if isinstance(components, Mapping) else None
    if not isinstance(component, Mapping):
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=("run_session_evidence_component_missing",),
        )
    component_target = _date_text(component.get("target_date"))
    if component_target and component_target != target:
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=(f"run_session_evidence_component_date_mismatch:{component_target}",),
        )
    component_schema = str(component.get("schema_version") or "").strip()
    if component_schema and component_schema != RUN_SESSION_SCHEMA:
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=(f"run_session_evidence_component_schema_mismatch:{component_schema}",),
        )
    pointer_run_id = str(payload.get("run_id") or "").strip()
    binding = component.get("evidence_source")
    if isinstance(binding, Mapping):
        contradictions = _binding_contradictions(
            binding,
            target=target,
            run_id=pointer_run_id,
            schema_version=component_schema,
        )
        if contradictions:
            return SuspensionEvidence(
                target_date=target,
                present=False,
                warnings=(
                    "run_session_evidence_binding_contradiction:" + ",".join(contradictions),
                ),
            )
    session_evidence = component.get("session_evidence")
    if not isinstance(session_evidence, Mapping) or not session_evidence:
        return SuspensionEvidence(
            target_date=target,
            present=False,
            warnings=("run_session_evidence_empty",),
        )
    return load_run_session_evidence(
        target,
        run_evidence=session_evidence,
        expected_symbols=universe_symbols,
        run_id=pointer_run_id,
    )


def build_reconciliation_partition(
    project_root: str | Path,
    *,
    target_date: str,
    run_evidence: SuspensionEvidence | None = None,
) -> dict[str, Any]:
    """Split the current universe into mutually exclusive update work classes."""

    root = Path(project_root).resolve()
    target = _date_text(target_date)
    universe = _read_symbols(root / UNIVERSE_RELATIVE)
    if run_evidence is None:
        # Same binding as the full reconciliation: this run's verified
        # no-session evidence, never the legacy file alone.
        run_evidence = resolve_run_session_evidence(
            root, target_date=target, universe_symbols=universe
        )
    research_set = set(_read_symbols(root / RESEARCH_POOL_RELATIVE, included_only=True))
    daily_dir = root / DAILY_RELATIVE
    research: list[str] = []
    residual: list[str] = []
    missing: list[str] = []
    accepted: list[str] = []
    pending: list[str] = []
    latest_by_symbol: dict[str, str] = {}
    for symbol in universe:
        path = daily_dir / f"{symbol}.csv"
        exists = path.is_file()
        latest = _last_date(path) if exists else ""
        latest_by_symbol[symbol] = latest
        if not exists:
            missing.append(symbol)
        elif symbol in research_set:
            research.append(symbol)
        else:
            residual.append(symbol)
        if latest and latest >= target:
            accepted.append(symbol)
        else:
            pending.append(symbol)
    completeness = classify_universe_completeness(
        target, project_root=root, universe_symbols=universe, run_evidence=run_evidence
    )
    verified = [
        symbol
        for symbol in (completeness.get("verified_excluded_symbols") or [])
        if symbol in universe
    ]
    flagged = [
        symbol
        for symbol in (completeness.get("flagged_for_review_symbols") or [])
        if symbol in universe
    ]
    verified_set = set(verified)
    accepted_set = set(accepted) | verified_set
    accepted = [symbol for symbol in universe if symbol in accepted_set]
    pending = [symbol for symbol in universe if symbol not in accepted_set]
    classes = [research, residual, missing]
    flattened = [symbol for group in classes for symbol in group]
    overlap = sorted({symbol for symbol in flattened if flattened.count(symbol) > 1})
    return {
        "target_date": target,
        "universe_symbols": universe,
        "universe_count": len(universe),
        "research_pool_symbols": research,
        "residual_existing_symbols": residual,
        "missing_canonical_symbols": missing,
        "accepted_symbols": accepted,
        "pending_symbols": pending,
        "verified_excluded_symbols": verified,
        "flagged_for_review_symbols": flagged,
        "gap_symbols": list(completeness.get("gap_symbols") or []),
        "completeness": completeness,
        "latest_by_symbol": latest_by_symbol,
        "partition_overlap": overlap,
        "partition_complete": not overlap and set(flattened) == set(universe),
        "research_pool_missing_symbols": sorted(research_set & set(missing)),
    }


def _run_id(value: str | None) -> str:
    if value:
        text = str(value).strip()
        if not text.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid_run_id")
        return text
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"full-a-daily-{stamp}-{uuid4().hex[:10]}"


def _run_root(root: Path) -> Path:
    return root / RUNS_RELATIVE


def _latest_incomplete_run(root: Path, target: str) -> tuple[str, dict[str, Any]]:
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for path in _run_root(root).glob("*/manifest.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        run_target = _date_text(payload.get("target_date"))
        status = str(payload.get("status") or "")
        if run_target <= target and status in {"partial", "blocked", "deferred_upstream_busy"}:
            candidates.append((path.stat().st_mtime, str(payload.get("run_id") or path.parent.name), payload))
    if not candidates:
        return "", {}
    _mtime, run_id, payload = max(candidates)
    return run_id, payload


def _default_health_refresh(*, root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Refresh the actual root's health snapshot only after a writer terminal state."""

    if root != PROJECT_ROOT.resolve():
        return {"data_alerts": {"current": [], "resolved": []}}
    from quantdesk.data_health_dashboard import build_data_health_dashboard

    return build_data_health_dashboard(
        output_path=root / "results/data_health/data_health_dashboard_latest.json",
        write=True,
    )


@contextmanager
def _default_lock(path: Path, *, timeout_seconds: float) -> Iterator[Path]:
    with interprocess_file_lock(path, timeout_seconds=timeout_seconds) as locked:
        yield locked


def _effective_coverage_from_partition(
    raw: Mapping[str, Any],
    partition: Mapping[str, Any],
    *,
    target_date: str,
) -> dict[str, Any]:
    """Formal coverage deducts verified suspensions; raw keeps physical gaps."""

    target = _date_text(target_date)
    verified = set(partition.get("verified_excluded_symbols") or [])
    latest = dict(partition.get("latest_by_symbol") or {})
    universe = list(partition.get("universe_symbols") or [])
    physical_missing = [symbol for symbol in universe if not latest.get(symbol)]
    physical_stale = [
        symbol
        for symbol in universe
        if latest.get(symbol) and target and str(latest.get(symbol)) < target
    ]
    effective_missing = [symbol for symbol in physical_missing if symbol not in verified]
    effective_stale = [symbol for symbol in physical_stale if symbol not in verified]
    effective = dict(raw)
    effective["missing_count"] = len(effective_missing)
    effective["stale_count"] = len(effective_stale)
    effective["missing_symbols_sample"] = effective_missing[:50]
    effective["stale_symbols_sample"] = effective_stale[:50]
    effective["verified_excluded_count"] = len(verified)
    covered = [
        symbol
        for symbol in universe
        if symbol not in set(effective_missing) and symbol not in set(effective_stale)
    ]
    effective["at_target_date_count"] = len(covered)
    if universe:
        effective["at_target_date_text"] = f"{len(covered)}/{len(universe)}"
        effective["coverage_text"] = f"{len(covered)}/{len(universe)}"
    return effective


def _manifest(
    *,
    run_id: str,
    target: str,
    partition: Mapping[str, Any],
    status: str,
    resumed_from_run_id: str = "",
    update: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    canonical_write: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "full_a_daily_reconciliation_v1",
        "task_type": "full_a_daily_reconciliation",
        "run_id": run_id,
        "target_date": target,
        "status": status,
        "resumed_from_run_id": resumed_from_run_id,
        "partition": dict(partition),
        "update": dict(update or {}),
        "coverage": dict(coverage or {}),
        "canonical_write": bool(canonical_write),
        "network": False,
        "apply": False,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }


def _write_manifest(root: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    run_dir = _run_root(root) / str(payload["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    atomic_write_json(manifest_path, payload)
    atomic_write_json(root / LATEST_RELATIVE, payload)
    return manifest_path, run_dir


def run_full_a_daily_reconciliation(
    *,
    project_root: str | Path,
    target_date: str,
    network: bool = False,
    apply: bool = False,
    audit_only: bool = False,
    run_id: str | None = None,
    lock_timeout_seconds: float = 0,
    update_runner: Callable[..., Mapping[str, Any]] = update_daily_prices,
    health_refresher: Callable[..., Mapping[str, Any]] = _default_health_refresh,
    alert_dispatcher: Callable[..., Mapping[str, Any]] = dispatch_data_health_alerts,
    lock_factory: Callable[..., Any] = _default_lock,
    alert_network: bool = False,
    alert_push: bool = False,
    alert_dry_run: bool = True,
    run_evidence_dir: str | Path | None = None,
    run_evidence: SuspensionEvidence | None = None,
) -> dict[str, Any]:
    """Reconcile only current-universe daily gaps through existing safeguards."""

    root = Path(project_root).resolve()
    target = _date_text(target_date)
    current_run_id = _run_id(run_id)
    if run_evidence is None:
        # Bind to ONE run's verified no-session evidence, so a symbol the chain
        # already proved has no session is not reported here as a gap.
        run_evidence = resolve_run_session_evidence(
            root,
            target_date=target,
            run_evidence_dir=run_evidence_dir,
            universe_symbols=_read_symbols(root / UNIVERSE_RELATIVE),
        )
    partition = build_reconciliation_partition(root, target_date=target, run_evidence=run_evidence)
    if not target or not partition["universe_symbols"]:
        payload = _manifest(run_id=current_run_id, target=target, partition=partition, status="blocked")
        payload["reason_codes"] = ["universe_or_target_missing"]
        manifest_path, _run_dir = _write_manifest(root, payload)
        payload["manifest_path"] = str(manifest_path.relative_to(root)).replace("\\", "/")
        return payload
    if not partition["partition_complete"]:
        payload = _manifest(run_id=current_run_id, target=target, partition=partition, status="blocked")
        payload["reason_codes"] = ["partition_invalid"]
        manifest_path, _run_dir = _write_manifest(root, payload)
        payload["manifest_path"] = str(manifest_path.relative_to(root)).replace("\\", "/")
        return payload
    if audit_only:
        raw_coverage = build_full_a_daily_coverage(
            target_date=target, universe_path=root / UNIVERSE_RELATIVE,
            daily_dir=root / DAILY_RELATIVE,
        )
        coverage = _effective_coverage_from_partition(raw_coverage, partition, target_date=target)
        pending = list(partition["pending_symbols"])
        payload = _manifest(
            run_id=current_run_id, target=target, partition=partition,
            status="completed_with_warnings" if pending else "completed", coverage=coverage,
        )
        payload.update({
            "audit_only": True, "accepted_symbols": list(partition["accepted_symbols"]),
            "retry_symbols": pending, "raw_coverage": raw_coverage, "effective_coverage": coverage,
            "reason_codes": ["daily_gaps_retained"] if pending else [],
            "coverage_contract": "Date coverage only; price validity is checked by each consumer.",
        })
        manifest_path, _ = _write_manifest(root, payload)
        payload["manifest_path"] = manifest_path.relative_to(root).as_posix()
        return payload
    if apply and not network:
        payload = _manifest(run_id=current_run_id, target=target, partition=partition, status="blocked")
        payload["reason_codes"] = ["apply_requires_network"]
        manifest_path, _run_dir = _write_manifest(root, payload)
        payload["manifest_path"] = str(manifest_path.relative_to(root)).replace("\\", "/")
        return payload
    resumed_from_run_id, _prior = _latest_incomplete_run(root, target)
    if not (network and apply):
        payload = _manifest(
            run_id=current_run_id,
            target=target,
            partition=partition,
            status="planned_offline",
            resumed_from_run_id=resumed_from_run_id,
        )
        payload["reason_codes"] = ["network_or_apply_not_authorized"]
        manifest_path, _run_dir = _write_manifest(root, payload)
        payload["manifest_path"] = str(manifest_path.relative_to(root)).replace("\\", "/")
        return payload
    try:
        with lock_factory(root / LOCK_RELATIVE, timeout_seconds=float(lock_timeout_seconds)):
            partition = build_reconciliation_partition(
                root, target_date=target, run_evidence=run_evidence
            )
            pending = list(partition["pending_symbols"])
            checkpoint = {
                "schema_version": "full_a_daily_reconciliation_checkpoint_v1",
                "run_id": current_run_id,
                "target_date": target,
                "initial_pending_symbols": pending,
                "resumed_from_run_id": resumed_from_run_id,
            }
            run_dir = _run_root(root) / current_run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(run_dir / "checkpoint.json", checkpoint)
            update: Mapping[str, Any] = {}
            if pending:
                update = update_runner(
                    only_low_price=False,
                    target_date=target,
                    symbols=pending,
                    workers=1,
                    sleep_seconds=1.0,
                    network=True,
                    dry_run=False,
                    rebuild_features=False,
                    write_result=False,
                    run_id=current_run_id,
                    require_raw_price_basis=True,
                    project_root=root,
                    daily_dir=root / DAILY_RELATIVE,
                    metadata_dir=root / "data/metadata",
                )
            raw_coverage = build_full_a_daily_coverage(
                target_date=target,
                universe_path=root / UNIVERSE_RELATIVE,
                daily_dir=root / DAILY_RELATIVE,
            )
            partition = build_reconciliation_partition(
                root, target_date=target, run_evidence=run_evidence
            )
            retry_symbols = list(partition["pending_symbols"])
            status = "completed" if not retry_symbols else "partial"
            effective_coverage = _effective_coverage_from_partition(
                raw_coverage, partition, target_date=target
            )
            payload = _manifest(
                run_id=current_run_id,
                target=target,
                partition=partition,
                status=status,
                resumed_from_run_id=resumed_from_run_id,
                update=update,
                coverage=effective_coverage,
                canonical_write=bool(pending),
            )
            payload["network"] = True
            payload["apply"] = True
            payload["accepted_symbols"] = list(partition["accepted_symbols"])
            payload["retry_symbols"] = retry_symbols
            payload["raw_coverage"] = raw_coverage
            payload["effective_coverage"] = effective_coverage
            payload["reason_codes"] = []
            if partition.get("verified_excluded_symbols"):
                payload["reason_codes"].append("verified_suspension_excluded_from_retry")
            manifest_path, _run_dir = _write_manifest(root, payload)
            health = health_refresher(root=root, manifest=payload)
            delivery = alert_dispatcher(
                root,
                health_payload=health,
                network=bool(alert_network),
                push=bool(alert_push),
                dry_run=bool(alert_dry_run),
            )
            payload["data_health_alert_delivery"] = dict(delivery)
            atomic_write_json(manifest_path, payload)
            atomic_write_json(root / LATEST_RELATIVE, payload)
            payload["manifest_path"] = str(manifest_path.relative_to(root)).replace("\\", "/")
            return payload
    except TimeoutError:
        payload = _manifest(
            run_id=current_run_id,
            target=target,
            partition=partition,
            status="deferred_upstream_busy",
            resumed_from_run_id=resumed_from_run_id,
        )
        payload["reason_codes"] = ["daily_data_chain_lock_busy"]
        manifest_path, _run_dir = _write_manifest(root, payload)
        payload["manifest_path"] = str(manifest_path.relative_to(root)).replace("\\", "/")
        return payload


__all__ = [
    "build_reconciliation_partition",
    "resolve_run_session_evidence",
    "run_full_a_daily_reconciliation",
]
