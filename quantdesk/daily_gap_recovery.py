"""Bounded recovery of missing target-day bars under the existing daily writer.

Existing dates are never replaced.  Provider workers have no daily-write
authority; only the parent appends validated rows using the original appender.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_session_evidence import validate_baostock_session_evidence
from quantdesk.data_truth.daily_bar_validity import daily_bar_validity_from_row
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.pipeline_process_guard import assert_canonical_daily_write, daily_writer_proof
from quantdesk.price_basis import classify_frame_price_basis
from quantdesk.process_deadline import run_process_tasks_until_deadline
from quantdesk.shared import normalize_symbol
from quantdesk.time_utils import now_shanghai_iso
from quantdesk.utils import atomic_write_json


# A single worker may need terminate/join/kill after its fetch deadline.
_WORKER_CLEANUP_SECONDS = 6.0
_PARENT_COMMIT_SECONDS = 10.0


@dataclass(frozen=True)
class GapRecoveryBudget:
    max_symbols: int = 20
    total_seconds: float = 120.0
    provider_order: tuple[str, ...] = ("baostock_daily", "tencent_daily")
    inter_call_seconds: float = 1.0


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _target(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("target_day_must_be_iso_date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("target_day_must_be_iso_date")
    return value


def _attempt_path(directory: Path, provider: str, symbol: str) -> Path:
    return directory / f"{provider}_{symbol}.json"


def _fetch_target_day(task: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """Spawned read-only provider call; the marker does not prove an HTTP send."""
    if daily_writer_proof() is not None:
        raise RuntimeError("fetch_worker_must_not_have_daily_writer_authority")
    provider, symbol, target = task["provider"], task["symbol"], context["target_date"]
    marker = {"provider": provider, "symbol": symbol, "target_date": target,
              "status": "provider_call_started", "started_at": now_shanghai_iso()}
    atomic_write_json(_attempt_path(Path(context["attempt_dir"]), provider, symbol), marker)
    from quantdesk.daily_price_providers import fetch_daily

    try:
        frame, warnings = fetch_daily(provider, symbol, target, target, network=True, strict_input=True)
        return {"frame": frame, "warnings": list(warnings), "provider": provider, "symbol": symbol}
    finally:
        # Keep pacing inside the killable worker, including its last request.
        time.sleep(float(context["inter_call_seconds"]))


@dataclass(frozen=True)
class _ParentLease:
    root: Path
    target: str
    deadline: float

    def assert_active(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("target_day_recovery_deadline")
        assert_canonical_daily_write(self.root / "data" / "daily")
        proof = daily_writer_proof()
        if proof is not None and (
            Path(str(proof.get("root") or "")).resolve() != self.root
            or str(proof.get("target") or "") != self.target
        ):
            raise RuntimeError("target_day_recovery_writer_mismatch")


def _missing_candidates(
    root: Path, target: str, daily: Mapping[str, Any], limit: int,
) -> tuple[list[str], dict[str, str], list[str]]:
    latest = dict(daily.get("latest_by_symbol_after") or {})
    known_missing = set(daily.get("missing_symbol_list_after") or [])
    invalid = set(daily.get("invalid_price_basis_symbols") or [])
    symbols = sorted({normalize_symbol(s) for s in daily.get("processed_symbols", []) if normalize_symbol(s)})
    candidates: list[str] = []
    rejected: dict[str, str] = {}
    deferred: list[str] = []
    examined = 0
    for symbol in symbols:
        if symbol in invalid:
            rejected[symbol] = "existing_price_basis_invalid"
            continue
        last = latest.get(symbol)
        if last in (None, ""):
            if symbol not in known_missing:
                rejected[symbol] = "latest_date_evidence_missing"
                continue
        else:
            try:
                if _target(last) >= target:
                    continue
            except (TypeError, ValueError):
                rejected[symbol] = "latest_date_evidence_invalid"
                continue
        if examined >= limit:
            deferred.append(symbol)
            continue
        examined += 1
        path = root / "data" / "daily" / f"{symbol}.csv"
        if path.is_file() and path.stat().st_size:
            frame = read_csv_frame(path, dtype=str)
            basis, reason = classify_frame_price_basis(frame)
            if basis != "raw" or reason or frame.attrs.get("warnings"):
                rejected[symbol] = "original_frame_not_verified_raw"
                continue
            if "date" not in frame.columns:
                rejected[symbol] = "original_frame_missing_date"
                continue
            dates = pd.to_datetime(frame["date"], errors="coerce")
            if dates.isna().any() or dates.dt.strftime("%Y-%m-%d").ge(target).any():
                rejected[symbol] = "original_frame_not_appendable_for_target"
                continue
        candidates.append(symbol)
    return candidates, rejected, deferred


def _candidate_row(frame: Any, symbol: str, target: str, provider: str) -> dict[str, Any]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("provider_has_no_target_bar")
    # Classify the complete supplied frame before inspecting dates or symbols.
    basis, reason = classify_frame_price_basis(frame)
    if basis != "raw" or reason:
        raise ValueError("provider_frame_not_verified_raw")
    if "date" not in frame.columns or "symbol" not in frame.columns:
        raise ValueError("provider_frame_missing_identity")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any() or not frame["symbol"].map(normalize_symbol).eq(symbol).all():
        raise ValueError("provider_frame_invalid_date_or_symbol")
    rows = frame.loc[dates.dt.strftime("%Y-%m-%d").eq(target)]
    if len(rows) != 1:
        raise ValueError("provider_target_bar_not_unique")
    row = rows.iloc[0].to_dict()
    for column in ("open", "high", "low", "close", "volume"):
        value = row.get(column)
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"provider_nonfinite_{column}")
    valid, invalid_reason = daily_bar_validity_from_row(row)
    if not valid:
        raise ValueError(invalid_reason)
    for column in ("amount", "change", "pct_chg"):
        value = row.get(column)
        if value is None or pd.isna(value):
            row[column] = ""
        elif isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"provider_nonfinite_{column}")
    if row.get("amount") != "" and float(row["amount"]) < 0:
        raise ValueError("provider_negative_amount")
    row.update(date=target, symbol=symbol, adjust="raw", source=provider, updated_at=now_shanghai_iso())
    return row


def recover_target_day_gaps(
    *, project_root: str | Path, target_date: str, daily_payload: Mapping[str, Any],
    output_dir: str | Path, network: bool, dry_run: bool = False,
    budget: GapRecoveryBudget = GapRecoveryBudget(),
) -> dict[str, Any]:
    """Try independent existing sources for at most twenty missing target rows."""
    root, output = Path(project_root).resolve(), Path(output_dir).resolve()
    target = _target(target_date)
    if daily_payload.get("target_date") != target or not isinstance(daily_payload.get("processed_symbols"), (list, tuple)):
        raise ValueError("target_day_recovery_input_scope_invalid")
    if budget.max_symbols < 1 or budget.max_symbols > 20 or not 0 < budget.total_seconds <= 120:
        raise ValueError("target_day_recovery_budget_out_of_range")
    if budget.provider_order != ("baostock_daily", "tencent_daily") or budget.inter_call_seconds < 1:
        raise ValueError("target_day_recovery_provider_contract_invalid")
    report: dict[str, Any] = {
        "schema_version": "target_day_gap_recovery.v1", "target_date": target,
        "network_requested": bool(network), "status": "skipped", "provider_calls_started": 0,
        "provider_call_limit": budget.max_symbols * len(budget.provider_order),
        "candidates": [], "rejected": {}, "deferred_symbols": [], "attempts": [],
        "appended_symbols": [], "verified_no_session_symbols": [], "session_evidence": {},
        "before_sha256": {}, "after_sha256": {}, "remaining_symbols": [],
        "total_budget_seconds": budget.total_seconds,
        "worker_cleanup_reserve_seconds": _WORKER_CLEANUP_SECONDS,
        "parent_commit_reserve_seconds": _PARENT_COMMIT_SECONDS,
    }
    if not network or dry_run:
        report["reason"] = "dry_run" if dry_run else "network_disabled"
        return report
    deadline = time.monotonic() + budget.total_seconds
    lease = _ParentLease(root, target, deadline)
    lease.assert_active()
    candidates, rejected, deferred = _missing_candidates(root, target, daily_payload, budget.max_symbols)
    report.update(candidates=candidates, rejected=rejected, deferred_symbols=deferred)
    if not candidates:
        report.update(status="partial" if rejected else "no_target_day_gaps", reason="input_evidence_requires_review" if rejected else "")
        return report
    output.mkdir(parents=True, exist_ok=True)
    attempt_dir = output / "attempts"
    attempt_dir.mkdir(exist_ok=True)
    if any(attempt_dir.iterdir()):
        raise ValueError("target_day_recovery_requires_new_attempt_directory")
    for symbol in candidates:
        report["before_sha256"][symbol] = _hash(root / "data" / "daily" / f"{symbol}.csv")
    pending = set(candidates)
    from scripts.run_daily_data_refresh import DAILY_HEADER, _append_daily_row

    for provider_index, provider in enumerate(budget.provider_order):
        remaining = deadline - time.monotonic()
        if not pending:
            break
        # Include process shutdown and parent-side validation/append in the
        # same budget. A final hung symbol must not consume the save window
        # for valid results already returned by that provider.
        provider_window = remaining / (len(budget.provider_order) - provider_index)
        slice_seconds = provider_window - _WORKER_CLEANUP_SECONDS - _PARENT_COMMIT_SECONDS
        if slice_seconds <= 0:
            report["stop_reason"] = "insufficient_finish_budget"
            break
        tasks = [{"provider": provider, "symbol": symbol} for symbol in sorted(pending)]
        batch = run_process_tasks_until_deadline(
            _fetch_target_day, tasks,
            context={"target_date": target, "attempt_dir": str(attempt_dir), "inter_call_seconds": budget.inter_call_seconds},
            max_workers=1, timeout_seconds=slice_seconds,
        )
        for index, task in enumerate(tasks):
            symbol = task["symbol"]
            marker = _attempt_path(attempt_dir, provider, symbol)
            started = marker.is_file()
            attempt = {**task, "provider_call_started": started, "status": "not_started"}
            report["attempts"].append(attempt)
            report["provider_calls_started"] += int(started)
            if index not in batch.results:
                attempt["status"] = "failed" if index in batch.failures else "unfinished" if started else "not_started"
                if index in batch.failures:
                    attempt["reason"] = str(batch.failures[index])[:400]
                continue
            if not started:
                attempt.update(status="rejected", reason="missing_provider_start_receipt")
                continue
            result = batch.results[index]
            if not isinstance(result, Mapping) or result.get("symbol") != symbol or result.get("provider") != provider:
                attempt.update(status="rejected", reason="provider_result_identity_mismatch")
                continue
            attempt["warnings"] = [str(value)[:300] for value in list(result.get("warnings") or [])[:20]]
            frame = result.get("frame")
            session = getattr(frame, "attrs", {}).get("suspension_evidence")
            verified = validate_baostock_session_evidence(
                session, expected_symbol=symbol, expected_start_date=target, expected_end_date=target,
            ) if provider == "baostock_daily" else {"valid": False, "suspended_dates": []}
            no_session = verified["valid"] and target in verified["suspended_dates"]
            try:
                row = _candidate_row(frame, symbol, target, provider)
            except (TypeError, ValueError, OverflowError) as exc:
                if no_session and isinstance(frame, pd.DataFrame) and frame.empty:
                    try:
                        lease.assert_active()
                        evidence_file = output / f"session_{symbol}.json"
                        atomic_write_json(evidence_file, verified["evidence"])
                    except (OSError, RuntimeError) as save_error:
                        attempt.update(status="rejected", reason=type(save_error).__name__)
                        continue
                    report["session_evidence"][symbol] = verified["evidence"]
                    report["verified_no_session_symbols"].append(symbol)
                    pending.discard(symbol)
                    attempt.update(status="verified_no_session", evidence_path=str(evidence_file))
                else:
                    attempt.update(status="rejected", reason=str(exc))
                continue
            if no_session:
                attempt.update(status="rejected", reason="provider_trade_and_halt_conflict")
                continue
            path = root / "data" / "daily" / f"{symbol}.csv"
            try:
                lease.assert_active()
                with interprocess_file_lock(path.parent / ".locks" / f"{symbol}.lock", timeout_seconds=max(0.01, min(1.0, deadline - time.monotonic()))):
                    lease.assert_active()
                    if _hash(path) != report["before_sha256"][symbol]:
                        raise RuntimeError("target_daily_file_changed_during_fetch")
                    stream = io.StringIO(newline="")
                    csv.writer(stream, lineterminator="").writerow([row.get(column, "") for column in DAILY_HEADER.split(",")])
                    appended, reason = _append_daily_row(path, stream.getvalue(), header=DAILY_HEADER, lease=lease)
                if not appended:
                    attempt.update(status="rejected", reason=reason)
                    continue
            except (OSError, RuntimeError, ValueError) as exc:
                attempt.update(status="rejected", reason=type(exc).__name__ + ":" + str(exc)[:200])
                continue
            pending.discard(symbol)
            report["appended_symbols"].append(symbol)
            report["after_sha256"][symbol] = _hash(path)
            attempt.update(status="appended", after_sha256=report["after_sha256"][symbol], bar_evidence=row)
    report["remaining_symbols"] = sorted(pending)
    report["status"] = "partial" if pending or deferred or rejected else "completed"
    report["completed_at"] = now_shanghai_iso()
    report["deadline_exhausted"] = time.monotonic() >= deadline
    atomic_write_json(output / "receipt.json", report)
    return report


def merge_recovered_daily_status(
    original: Mapping[str, Any], refreshed: Mapping[str, Any], touched: Sequence[str],
    recovery: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace only touched securities; preserve the complete universe scope."""
    result, changed = dict(original), set(touched)
    count_fields = {
        "fresh_symbol_list_after": "fresh_count_after", "stale_symbol_list_after": "stale_count_after",
        "missing_symbol_list_after": "missing_count_after", "failed_symbol_list": "failed_symbols",
        "invalid_price_basis_symbols": None,
        "raw_positive_volume_target_symbol_list": "raw_positive_volume_target_symbols",
        "raw_zero_volume_target_symbol_list": "raw_zero_volume_target_symbols",
    }
    for key, count_key in count_fields.items():
        result[key] = sorted((set(original.get(key) or []) - changed) | (set(refreshed.get(key) or []) & changed))
        if count_key:
            result[count_key] = len(result[key])
    for key in ("latest_by_symbol_after", "invalid_price_basis_reasons", "missing_recent_trade_dates_by_symbol"):
        value = {s: v for s, v in dict(original.get(key) or {}).items() if s not in changed}
        value.update({s: v for s, v in dict(refreshed.get(key) or {}).items() if s in changed})
        result[key] = value
    result["target_day_gap_recovery"] = dict(recovery)
    calls = int(recovery.get("provider_calls_started") or 0)
    result["provider_calls"] = int(original.get("provider_calls") or 0) + calls
    result["network_effective"] = bool(original.get("network_effective") or calls)
    result["network_scope"] = "verified_upstream_reuse_with_target_gap_recovery"
    result["operation"] = "local_input_audit_with_target_gap_recovery"
    result["ok"] = not any(result.get(key) for key in ("stale_symbol_list_after", "missing_symbol_list_after", "failed_symbol_list"))
    result["status"] = "completed" if result["ok"] else "partial"
    return result
