"""Fail-closed formal candidate refresh and daily plan rollover decisions.

The module is deliberately independent from the legacy v420 candidate CSV.
It consumes an already-authoritative daily-data-chain manifest and an in-memory
strategy payload, then writes a separate append-only dated candidate snapshot.
It never fetches data, mutates a paper ledger, or routes an order.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from quantdesk.candidate_contract import candidate_rows_from_strategy_payload
from quantdesk.data_update_staging import atomic_write_text
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.shared import normalize_symbol
from quantdesk.shared import read_json_strict as _read_json_strict
from quantdesk.utils import atomic_write_json


CANDIDATE_REFRESH_SCHEMA_VERSION = "round13_formal_candidate_refresh_v1"
CANDIDATE_ENVELOPE_SCHEMA_VERSION = "formal_candidate_envelope_v1"
PLAN_ROLLOVER_SCHEMA_VERSION = "round13_plan_rollover_v1"
REPLAY_RECEIPT_SCHEMA_VERSION = "round13_conditional_replay_receipt_v1"
REQUIRED_DATA_CHAIN_CONTRACT_VERSION = 10
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "Not Investment Advice / Not Position Advice."
)

StrategyRunner = Callable[..., dict[str, Any]]

# The minute-evidence producer (``intraday_gap_checker.build_required_minute_status``
# via ``intraday_schema.intraday_status_for_file``) emits exactly these per-symbol
# statuses, and buckets each non-available one into its own count field. Keeping the
# mapping explicit is what lets us verify that a partial report is internally
# consistent instead of trusting its own summary numbers.
MINUTE_STATUS_AVAILABLE = "available"
# Status -> bucket family name. The producer emits one count field and one
# symbol list per family (``<family>_count`` / ``<family>_symbols``), so the
# trust check derives both field names from the family name.
MINUTE_STATUS_BUCKETS: dict[str, str] = {
    "missing_intraday": "missing",
    "stale_intraday": "stale",
    "insufficient_intraday": "insufficient",
    "invalid_schema": "invalid",
}
# Bucket family names covering every bucket the status map knows about, in the
# order the producer emits them. The trust check requires the declared counts,
# the per-bucket symbol lists, and the per-item statuses to agree with each
# other, so this list must never silently miss a bucket.
_MINUTE_BUCKETS: tuple[str, ...] = (
    "available",
    "missing",
    "stale",
    "insufficient",
    "invalid",
)


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _iso_day(value: Any) -> str:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _iso_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.isoformat()


def _safe_run_id(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or Path(text).name != text
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", text)
    ):
        return ""
    return text


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strict_symbol_list(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    symbols: list[str] = []
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"[0-9]{6}", item):
            return [], False
        symbols.append(item)
    return symbols, len(symbols) == len(set(symbols))


def _strict_nonnegative_counts(value: Any) -> tuple[dict[str, int], bool]:
    if not isinstance(value, Mapping) or not value:
        return {}, False
    result: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if (
            not key
            or key in result
            or isinstance(raw_value, bool)
            or not isinstance(raw_value, int)
            or raw_value < 0
        ):
            return {}, False
        result[key] = raw_value
    return result, True


def _nested_value(row: Mapping[str, Any], key: str) -> Any:
    queue: list[tuple[Mapping[str, Any], int]] = [(row, 0)]
    seen: set[int] = set()
    while queue:
        current, depth = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        value = current.get(key)
        if value not in (None, ""):
            return value
        if depth >= 4:
            continue
        for nested_key in ("raw", "source_item", "features", "payload"):
            nested = current.get(nested_key)
            if isinstance(nested, Mapping):
                queue.append((nested, depth + 1))
    return None


def candidate_refresh_gate(data_chain_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the independent, daily-only eligibility gate for observation.

    Minute coverage is intentionally absent from this gate.  A symbol must have
    explicit current-day raw daily feature evidence to enter the research
    candidate input; structural/provenance failures still block the whole
    refresh.
    """

    manifest = _mapping(data_chain_manifest)
    target_date = _iso_day(manifest.get("target_date"))
    run_id = _safe_run_id(manifest.get("run_id"))
    coverage = _mapping(manifest.get("coverage"))
    universe = _mapping(manifest.get("universe"))
    analysis_pool = _mapping(universe.get("current_analysis_pool"))
    research_universe = _mapping(universe.get("research_universe"))
    critical_pool = _mapping(universe.get("critical"))
    components = _mapping(manifest.get("components"))
    features = _mapping(components.get("features"))
    feature_gate = _mapping(coverage.get("features"))
    universe_hash = str(analysis_pool.get("sha256") or "").strip().lower()
    feature_hash = str(
        features.get("output_sha256") or feature_gate.get("output_sha256") or ""
    ).strip().lower()

    reasons: list[str] = []
    if not target_date:
        reasons.append("upstream_target_date_invalid")
    if not run_id:
        reasons.append("upstream_run_id_invalid")
    daily_coverage = _mapping(coverage.get("daily"))
    declared_universe_symbols, universe_symbols_valid = _strict_symbol_list(
        analysis_pool.get("symbols")
    )
    universe_symbols = list(dict.fromkeys(declared_universe_symbols))
    universe_set = set(universe_symbols)
    critical_symbols: list[str] = []
    critical_symbols_valid = True
    if critical_pool:
        critical_symbols, critical_symbols_valid = _strict_symbol_list(
            critical_pool.get("symbols")
        )
    expected_session_symbols = universe_set | set(critical_symbols)

    def normalized_daily_list(name: str) -> tuple[list[str], bool]:
        return _strict_symbol_list(daily_coverage.get(name, []))

    daily_stale, daily_stale_valid = normalized_daily_list("stale_symbols")
    daily_missing, daily_missing_valid = normalized_daily_list("missing_symbols")
    daily_failed, daily_failed_valid = normalized_daily_list("failed_symbols")
    daily_invalid_basis, daily_invalid_basis_valid = normalized_daily_list(
        "invalid_price_basis_symbols"
    )
    daily_legitimate, daily_legitimate_valid = normalized_daily_list(
        "legitimate_no_data_symbols"
    )
    daily_excluded = set(
        daily_stale + daily_missing + daily_failed + daily_legitimate + daily_invalid_basis
    )
    # A missing symbol is a per-symbol research exclusion, not a failure of
    # the feature stage for every other member of the all-A universe.
    feature_component_passed = str(features.get("status") or "") in {
        "passed",
        "partial",
    }
    feature_per_symbol = {
        normalize_symbol(symbol): _mapping(entry)
        for symbol, entry in _mapping(features.get("per_symbol")).items()
        if normalize_symbol(symbol)
    }
    verified_exclusions = _mapping(feature_gate.get("excluded_symbol_evidence"))
    verified_exclusion_symbols = {
        symbol for symbol, evidence in feature_per_symbol.items()
        if evidence.get("eligible") is False
        and str(evidence.get("reason_code") or "")
        and dict(_mapping(verified_exclusions.get(symbol))) == dict(evidence)
        and str(feature_gate.get("exclusion_output_sha256") or "") == feature_hash
        and _is_sha256(feature_hash)
    }

    if manifest.get("authoritative") is not True:
        reasons.append("upstream_data_chain_not_authoritative")
    if manifest.get("network") is not True:
        reasons.append("upstream_network_run_required")
    if manifest.get("dry_run") is True:
        reasons.append("upstream_dry_run_not_formal")
    if str(manifest.get("status") or "") not in {"completed", "blocked"}:
        reasons.append("upstream_data_chain_status_invalid")
    try:
        contract_version = int(manifest.get("daily_data_chain_contract_version") or 0)
    except (TypeError, ValueError):
        contract_version = 0
    if contract_version != REQUIRED_DATA_CHAIN_CONTRACT_VERSION:
        reasons.append("upstream_data_chain_contract_version_mismatch")
    if manifest.get("protected_state_unchanged") is not True:
        reasons.append("upstream_protected_state_not_unchanged")
    session_gate = _mapping(components.get("session_expectation"))
    session_unknown, session_unknown_valid = _strict_symbol_list(
        session_gate.get("unknown_symbols")
    )
    session_symbols, session_symbols_valid = _strict_symbol_list(
        session_gate.get("symbols")
    )
    classification_counts, classification_counts_valid = (
        _strict_nonnegative_counts(session_gate.get("classification_counts"))
    )
    declared_counts, declared_counts_valid = _strict_nonnegative_counts(
        session_gate.get("counts")
    )
    session_counts_consistent = bool(
        classification_counts_valid
        and declared_counts_valid
        and declared_counts == classification_counts
    )
    session_counts = classification_counts
    session_coverage = _mapping(coverage.get("session_expectation"))
    coverage_unknown, coverage_unknown_valid = _strict_symbol_list(
        session_coverage.get("unknown_symbols")
    )
    daily_session_unknown = (
        set(daily_stale + daily_missing + daily_failed + daily_invalid_basis)
        | verified_exclusion_symbols
    )
    session_per_symbol = _mapping(session_gate.get("per_symbol"))
    witnessed_session_unknown = {
        symbol for symbol in session_unknown
        if (entry := _mapping(session_per_symbol.get(symbol))).get("symbol") == symbol
        and entry.get("classification") == "unknown"
        and entry.get("status") == "unknown"
        and isinstance(entry.get("reason_code"), str)
        and entry["reason_code"].strip()
        and isinstance(entry.get("evidence"), list)
        and bool(entry["evidence"])
        and all(isinstance(item, Mapping) and item.get("source") for item in entry["evidence"])
    }
    session_coverage_unknown_count = _integer_or_none(
        session_coverage.get("unknown_count")
    )
    session_structure_valid = bool(
        session_gate
        and _iso_day(session_gate.get("target_date")) == target_date
        and session_unknown_valid
        and session_symbols_valid
        and critical_symbols_valid
        and set(session_symbols) == expected_session_symbols
        and set(session_unknown).issubset(set(session_symbols))
        and session_counts_consistent
        and session_counts.get("unknown") == len(session_unknown)
        and sum(session_counts.values()) == len(session_symbols)
    )
    session_passed = bool(
        session_structure_valid
        and str(session_gate.get("status") or "") == "passed"
        and session_gate.get("formal_eligible") is True
        and not session_unknown
    )
    session_unknown_degradation = bool(
        session_structure_valid
        and str(session_gate.get("status") or "") == "blocked"
        and session_gate.get("formal_eligible") is False
        and bool(session_unknown)
        and set(session_unknown).issubset(daily_session_unknown | witnessed_session_unknown)
        and (not session_per_symbol or set(session_unknown) == witnessed_session_unknown)
        and str(session_coverage.get("status") or "") == "blocked"
        and session_coverage_unknown_count == len(session_unknown)
        and coverage_unknown_valid
        and sorted(coverage_unknown) == sorted(session_unknown)
    )
    if not session_passed and not session_unknown_degradation:
        reasons.append("upstream_session_expectation_not_passed")
    critical_sources = _mapping(components.get("critical_sources"))
    failed_sources = critical_sources.get("failed_sources")
    failed_sources_valid = isinstance(failed_sources, list)
    if not critical_sources:
        reasons.append("upstream_critical_sources_missing")
    elif not (
        not critical_symbols
        and str(critical_sources.get("status") or "") == "not_requested"
        and critical_sources.get("reason_codes") == ["minute_processing_not_requested"]
    ) and (
        str(critical_sources.get("status") or "") not in {"passed", "completed"}
        or critical_sources.get("formal_eligible") is not True
        or not failed_sources_valid
        or bool(failed_sources)
    ):
        reasons.append("upstream_critical_sources_not_formal")

    daily_hole_codes = {
        "daily_symbols_stale",
        "daily_symbols_missing",
        "daily_symbols_failed",
        "daily_price_basis_not_raw",
    }
    daily_status = str(daily_coverage.get("status") or "")
    daily_reasons = {
        str(item) for item in (daily_coverage.get("reason_codes") or []) if item
    }
    if daily_status not in {"passed", "partial"} and not (
        daily_excluded and daily_reasons.issubset(daily_hole_codes)
    ):
        reasons.append("upstream_daily_gate_not_passed_without_symbol_evidence")
    for list_name, values, values_valid in (
        ("stale", daily_stale, daily_stale_valid),
        ("missing", daily_missing, daily_missing_valid),
        ("failed", daily_failed, daily_failed_valid),
    ):
        if not values_valid:
            reasons.append(f"upstream_daily_{list_name}_symbols_invalid")
        count_key = f"{list_name}_count"
        if count_key in daily_coverage:
            parsed_count = _integer_or_none(daily_coverage.get(count_key))
            if parsed_count is None or parsed_count < 0:
                reasons.append(f"upstream_daily_{list_name}_count_invalid")
            elif parsed_count != len(values):
                reasons.append(f"upstream_daily_{list_name}_evidence_mismatch")
    if not daily_invalid_basis_valid:
        reasons.append("upstream_daily_invalid_price_basis_symbols_invalid")
    if not daily_legitimate_valid:
        reasons.append("upstream_daily_legitimate_no_data_symbols_invalid")
    invalid_basis_count = 0
    if "invalid_price_basis_count" in daily_coverage:
        parsed_invalid_basis_count = _integer_or_none(
            daily_coverage.get("invalid_price_basis_count")
        )
        if parsed_invalid_basis_count is None or parsed_invalid_basis_count < 0:
            reasons.append("upstream_daily_invalid_price_basis_count_invalid")
        else:
            invalid_basis_count = parsed_invalid_basis_count
    if invalid_basis_count != len(daily_invalid_basis):
        reasons.append("upstream_daily_invalid_price_basis_evidence_mismatch")
    if (daily_invalid_basis or invalid_basis_count) and not set(daily_invalid_basis).issubset(verified_exclusion_symbols):
        reasons.append("upstream_daily_price_basis_not_raw")

    if not feature_component_passed:
        reasons.append("upstream_feature_component_not_passed")
    if set(feature_gate.get("reason_codes") or []) & {
        "feature_output_integrity_invalid", "feature_input_snapshot_unstable",
        "feature_output_digest_missing", "feature_price_policy_not_passed",
        "feature_price_basis_not_raw", "feature_per_symbol_not_raw",
    }:
        reasons.append("upstream_feature_output_not_verified")
    feature_degraded = bool(
        feature_component_passed
        and feature_per_symbol
        and (
            daily_excluded
            or len(feature_per_symbol) < len(universe_symbols)
            or str(feature_gate.get("status") or "") == "partial"
        )
    )
    if str(feature_gate.get("status") or "") not in {"passed", "partial"} and not feature_degraded:
        reasons.append("upstream_features_gate_not_passed_without_symbol_evidence")
    if _iso_day(analysis_pool.get("target_date")) != target_date:
        reasons.append("upstream_universe_target_date_mismatch")
    if str(analysis_pool.get("name") or "") != "current_analysis_pool":
        reasons.append("upstream_universe_contract_name_mismatch")
    if not _is_sha256(universe_hash):
        reasons.append("upstream_universe_hash_invalid")
    if not universe_symbols_valid:
        reasons.append("upstream_universe_symbols_invalid")
    universe_symbol_count = _integer_or_none(analysis_pool.get("symbol_count"))
    if universe_symbol_count is None or universe_symbol_count < 0:
        reasons.append("upstream_universe_symbol_count_invalid")
    elif universe_symbol_count != len(universe_symbols):
        reasons.append("upstream_universe_symbol_count_mismatch")
    if len(declared_universe_symbols) != len(universe_symbols):
        reasons.append("upstream_universe_symbols_duplicate")
    if declared_universe_symbols != sorted(universe_symbols):
        reasons.append("upstream_universe_symbols_not_canonical")
    universe_contract_projection = {
        "name": "current_analysis_pool",
        "target_date": target_date,
        "symbols": sorted(universe_symbols),
    }
    if _is_sha256(universe_hash) and universe_hash != _stable_hash(
        universe_contract_projection
    ):
        reasons.append("upstream_universe_contract_digest_mismatch")
    if (
        set(daily_stale + daily_missing + daily_failed + daily_invalid_basis)
        - universe_set
    ):
        reasons.append("upstream_daily_symbol_evidence_outside_universe")
    for count_name in ("requested", "reported"):
        count_key = f"{count_name}_count"
        if count_key not in daily_coverage:
            continue
        parsed_count = _integer_or_none(daily_coverage.get(count_key))
        if parsed_count is None or parsed_count < 0:
            reasons.append(f"upstream_daily_{count_name}_count_invalid")
        elif parsed_count != len(universe_symbols):
            reasons.append(f"upstream_daily_{count_name}_count_mismatch")
    if _iso_day(features.get("latest_date")) != target_date:
        reasons.append("upstream_feature_target_date_mismatch")
    if str(features.get("accepted_price_basis") or "") != "raw":
        reasons.append("upstream_feature_price_basis_not_raw")
    if not _is_sha256(feature_hash):
        reasons.append("upstream_feature_hash_invalid")
    coverage_feature_hash = str(feature_gate.get("output_sha256") or "").strip().lower()
    if coverage_feature_hash and coverage_feature_hash != feature_hash:
        reasons.append("upstream_feature_hash_mismatch")
    unexpected_feature_symbols = sorted(set(feature_per_symbol) - universe_set)
    if unexpected_feature_symbols:
        reasons.append("upstream_feature_symbol_set_invalid")

    exclusions_by_symbol: dict[str, list[str]] = {}

    def exclude(symbol: str, reason: str) -> None:
        exclusions_by_symbol.setdefault(symbol, []).append(reason)

    for symbol in daily_stale:
        exclude(symbol, "daily_stale")
    for symbol in daily_missing:
        exclude(symbol, "daily_missing")
    for symbol in daily_failed:
        exclude(symbol, "daily_failed")
    for symbol in daily_legitimate:
        exclude(symbol, "daily_no_session")
    for symbol in daily_invalid_basis:
        exclude(symbol, "daily_price_basis_invalid")
    for symbol in session_unknown:
        if symbol in universe_set:
            exclude(symbol, "session_expectation_unknown")
    for symbol in universe_symbols:
        evidence = feature_per_symbol.get(symbol)
        if not evidence:
            exclude(symbol, "feature_evidence_missing")
            continue
        if symbol in verified_exclusion_symbols:
            exclude(symbol, str(evidence["reason_code"]))
            continue
        if (
            evidence.get("eligible") is False
            and str(evidence.get("reason_code") or "") == "missing_or_invalid_daily"
        ):
            exclude(symbol, "feature_evidence_missing")
            continue
        feature_basis = str(evidence.get("price_basis") or "")
        if feature_basis != "raw":
            reasons.append("upstream_feature_per_symbol_price_basis_not_raw")
        if evidence.get("eligible") is not True:
            exclude(symbol, "feature_not_eligible")
        if _iso_day(evidence.get("latest_date")) != target_date:
            exclude(symbol, "feature_not_current")

    excluded_symbols = [
        symbol for symbol in universe_symbols if symbol in exclusions_by_symbol
    ]
    eligible_symbols = [
        symbol for symbol in universe_symbols if symbol not in exclusions_by_symbol
    ]
    if not eligible_symbols:
        reasons.append("candidate_refresh_no_current_daily_symbols")
    reasons = list(dict.fromkeys(reasons))
    exclusions = [
        {
            "symbol": symbol,
            "status": "unknown",
            "tonight_unavailable": True,
            "reason_codes": list(dict.fromkeys(exclusions_by_symbol[symbol])),
            "session_evidence": dict(_mapping(session_per_symbol.get(symbol))),
        }
        for symbol in excluded_symbols
    ]
    return {
        "status": "passed" if not reasons else "blocked",
        "formal_eligible": not reasons,
        "degraded": bool(excluded_symbols),
        "reason_codes": reasons,
        "degradation_reason_codes": (
            ["candidate_source_symbols_excluded"] if excluded_symbols else []
        ),
        "target_date": target_date,
        "source_run_id": run_id,
        "source_universe_hash": universe_hash,
        "source_feature_hash": feature_hash,
        "data_chain_contract_version": contract_version,
        "upstream_manifest_status": str(manifest.get("status") or ""),
        "expected_count": len(universe_symbols),
        "eligible_count": len(eligible_symbols),
        "excluded_count": len(excluded_symbols),
        "universe_symbols": universe_symbols,
        "eligible_symbols": eligible_symbols,
        "excluded_symbols": excluded_symbols,
        "exclusions": exclusions,
        "daily_stale_symbols": daily_stale,
        "daily_missing_symbols": daily_missing,
        "daily_failed_symbols": daily_failed,
        "feature_per_symbol": feature_per_symbol,
        "universe_source": str(
            research_universe.get("source") or analysis_pool.get("source") or ""
        ),
        "universe_filter_used": bool(research_universe.get("filter_used", False)),
        "universe_filter_id": str(research_universe.get("filter_id") or ""),
        "universe_filter_count": _integer_or_none(
            research_universe.get("filter_symbol_count")
        ) or 0,
    }


def _manifest_gate(data_chain_manifest: Mapping[str, Any]) -> dict[str, Any]:
    return candidate_refresh_gate(data_chain_manifest)


def _write_candidate_eligible_universe(
    *,
    root: Path,
    run_id: str,
    eligible_symbols: Sequence[str],
    source_path: Path | None = None,
    require_included: bool = True,
) -> tuple[Path | None, list[str]]:
    source_path = source_path or (
        root / "data" / "universe" / "low_price_learning_symbols.csv"
    )
    if not source_path.is_file():
        return None, ["candidate_source_universe_file_missing"]
    try:
        with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error):
        return None, ["candidate_source_universe_file_unreadable"]
    if not fieldnames or not ({"symbol", "code", "ts_code", "代码", "证券代码"} & set(fieldnames)):
        return None, ["candidate_source_universe_schema_invalid"]

    rows_by_symbol: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for row in rows:
        symbol = normalize_symbol(
            row.get("symbol")
            or row.get("code")
            or row.get("ts_code")
            or row.get("证券代码")
            or row.get("代码")
        )
        if not symbol:
            continue
        if symbol in rows_by_symbol:
            duplicates.add(symbol)
        rows_by_symbol[symbol] = row
    expected = list(dict.fromkeys(normalize_symbol(value) for value in eligible_symbols))
    if duplicates & set(expected):
        return None, ["candidate_source_universe_duplicate_symbols"]
    missing = [symbol for symbol in expected if symbol not in rows_by_symbol]
    if missing:
        return None, ["candidate_source_universe_eligible_rows_missing"]
    selected = [rows_by_symbol[symbol] for symbol in expected]
    if require_included and any(
        str(row.get("included") or "").strip().lower()
        not in {"1", "true", "yes", "y"}
        for row in selected
    ):
        return None, ["candidate_source_universe_eligible_row_not_included"]

    if "included" not in fieldnames:
        fieldnames.append("included")
    if not require_included:
        for row in selected:
            row["included"] = "true"

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(selected)
    scoped_path = (
        root
        / "results"
        / "candidate_refresh"
        / "runs"
        / run_id
        / "candidate_eligible_universe.csv"
    )
    atomic_write_text(scoped_path, output.getvalue(), encoding="utf-8")
    return scoped_path, []


def _strategy_gate(
    strategy_payload: Mapping[str, Any], *, target_date: str
) -> list[str]:
    payload = _mapping(strategy_payload)
    summary = _mapping(payload.get("summary"))
    feature_status = _mapping(payload.get("daily_feature_status"))
    reasons: list[str] = []
    if payload.get("ok") is not True:
        reasons.append("candidate_strategy_payload_not_ok")
    if _iso_day(summary.get("latest_daily_date")) != target_date:
        reasons.append("candidate_strategy_target_date_mismatch")
    if _iso_day(feature_status.get("latest_date")) != target_date:
        reasons.append("candidate_feature_target_date_mismatch")
    return reasons


def _candidate_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    score = row.get("score")
    if score in (None, ""):
        score = _nested_value(row, "long_score")
    if score in (None, ""):
        score = _nested_value(row, "board_score")
    projected = {
        "symbol": normalize_symbol(row.get("symbol") or row.get("code")),
        "name": str(row.get("name") or row.get("stock_name") or "").strip(),
        "strategy_type": str(row.get("strategy_type") or "").strip(),
        "strategy_label": str(row.get("strategy_label") or "").strip(),
        "rank": row.get("rank"),
        "score": score,
        "reason": str(row.get("reason") or row.get("trigger_reason") or "").strip(),
        "candidate_source": str(
            row.get("candidate_source") or _nested_value(row, "source") or ""
        ).strip(),
        "data_latest_date": _iso_day(
            row.get("data_latest_date") or _nested_value(row, "latest_date")
        ),
        "is_target_date_covered": row.get("is_target_date_covered") is True,
        "daily_feature_available": _nested_value(row, "daily_feature_available") is True,
        "daily_feature_latest_date": _iso_day(
            _nested_value(row, "daily_feature_latest_date")
        ),
        "data_health_status": str(
            _nested_value(row, "data_health_status") or ""
        ).strip(),
        "liquidity_status": str(
            _nested_value(row, "liquidity_status") or ""
        ).strip(),
        "sector": str(row.get("sector") or "").strip(),
        "industry": str(row.get("industry") or "").strip(),
        "primary_theme": str(row.get("primary_theme") or "").strip(),
    }
    # Carry the reference price and turnover the upstream strategy row already
    # computed from canonical raw daily history. Without them the paper surfaces
    # receive a candidate with no price: _candidate_price returns None, the daily
    # buy path skips the candidate before any risk evaluation, and the liquidity
    # component has no amount to seal. These are pass-throughs of values that
    # already exist upstream, not newly asserted figures — omitted when absent so
    # a missing input stays visible instead of being defaulted.
    for field in ("close", "amount_ma20"):
        value = row.get(field)
        if value in (None, ""):
            value = _nested_value(row, field)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")) or number < 0:
            continue
        projected[field] = number
    return projected


def _candidate_reasons(
    candidate: Mapping[str, Any],
    *,
    target_date: str,
    seen_keys: set[tuple[str, str]],
) -> list[str]:
    symbol = str(candidate.get("symbol") or "")
    strategy_type = str(candidate.get("strategy_type") or "")
    candidate_key = (symbol, strategy_type)
    reasons: list[str] = []
    if not re.fullmatch(r"\d{6}", symbol):
        reasons.append("candidate_symbol_invalid")
    if not strategy_type:
        reasons.append("candidate_strategy_type_missing")
    if symbol and strategy_type and candidate_key in seen_keys:
        reasons.append("candidate_key_duplicate")
    if candidate.get("data_latest_date") != target_date:
        reasons.append("candidate_target_date_mismatch")
    if candidate.get("is_target_date_covered") is not True:
        reasons.append("candidate_target_date_not_covered")
    if candidate.get("daily_feature_available") is not True:
        reasons.append("candidate_feature_missing")
    if candidate.get("daily_feature_latest_date") != target_date:
        reasons.append("candidate_feature_date_mismatch")
    if candidate.get("data_health_status") != "ok":
        reasons.append("candidate_data_health_not_ok")
    if not str(candidate.get("candidate_source") or "").strip():
        reasons.append("candidate_source_missing")
    try:
        score = float(candidate.get("score"))
    except (TypeError, ValueError):
        score = math.nan
    if not math.isfinite(score):
        reasons.append("candidate_score_nonfinite")
    return reasons


def _candidate_hash_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _mapping(payload.get("provenance"))
    gate = _mapping(payload.get("qualification_gate"))
    return {
        "schema_version": payload.get("schema_version"),
        "target_date": payload.get("target_date"),
        "generated_batch": payload.get("generated_batch"),
        "status": payload.get("status"),
        "formal_eligible": payload.get("formal_eligible"),
        "candidate_count": payload.get("candidate_count"),
        "candidates": payload.get("candidates"),
        "rejected_candidate_count": payload.get("rejected_candidate_count"),
        "rejected_candidates": payload.get("rejected_candidates"),
        "reason_codes": payload.get("reason_codes"),
        "qualification_gate": gate,
        "source": provenance.get("source"),
        "captured_at": provenance.get("captured_at"),
        "source_run_id": provenance.get("source_run_id"),
        "source_universe_hash": provenance.get("source_universe_hash"),
        "source_feature_hash": provenance.get("source_feature_hash"),
        "boundaries": payload.get("boundaries"),
        "disclaimer": payload.get("disclaimer"),
    }


def build_formal_candidate_batch(
    *,
    data_chain_manifest: Mapping[str, Any],
    strategy_payload: Mapping[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    """Build one immutable candidate batch from already-local formal evidence."""

    manifest_gate = _manifest_gate(data_chain_manifest)
    target_date = str(manifest_gate.get("target_date") or "")
    normalized_captured_at = _iso_datetime(captured_at)
    reasons = list(manifest_gate.get("reason_codes") or [])
    if not normalized_captured_at:
        reasons.append("candidate_captured_at_invalid")
    if target_date and normalized_captured_at:
        captured_day = _iso_day(normalized_captured_at)
        if captured_day < target_date:
            reasons.append("candidate_captured_before_target_date")
    if not reasons:
        reasons.extend(_strategy_gate(strategy_payload, target_date=target_date))

    universe_symbols = {
        normalize_symbol(value)
        for value in (manifest_gate.get("universe_symbols") or [])
        if normalize_symbol(value)
    }
    gate_eligible_symbols = {
        normalize_symbol(value)
        for value in (manifest_gate.get("eligible_symbols") or [])
        if normalize_symbol(value)
    }
    daily_blocked_symbols = {
        normalize_symbol(value)
        for value in (manifest_gate.get("excluded_symbols") or [])
        if normalize_symbol(value)
    }
    feature_per_symbol = dict(manifest_gate.get("feature_per_symbol") or {})
    features_component = _mapping(
        _mapping(_mapping(data_chain_manifest).get("components")).get("features")
    )
    feature_latest_date_default = _iso_day(features_component.get("latest_date"))
    feature_price_basis_default = str(
        features_component.get("accepted_price_basis") or ""
    )

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    if not reasons:
        declared_standard = strategy_payload.get("standard_candidates")
        raw_candidates = (
            [row for row in declared_standard if isinstance(row, Mapping)]
            if isinstance(declared_standard, list)
            else candidate_rows_from_strategy_payload(dict(strategy_payload))
        )
        for raw in raw_candidates:
            candidate = _candidate_projection(_mapping(raw))
            row_reasons = _candidate_reasons(
                candidate,
                target_date=target_date,
                seen_keys=seen_keys,
            )
            symbol = str(candidate.get("symbol") or "")
            strategy_type = str(candidate.get("strategy_type") or "")
            if symbol and strategy_type:
                seen_keys.add((symbol, strategy_type))
            if symbol and symbol not in universe_symbols:
                row_reasons.append("candidate_not_in_source_universe")
            if symbol and symbol not in gate_eligible_symbols:
                row_reasons.append("candidate_not_in_gate_eligible_symbols")
            if symbol and symbol in daily_blocked_symbols:
                row_reasons.append("candidate_upstream_symbol_not_current")
            per_symbol = _mapping(feature_per_symbol.get(symbol))
            if symbol and _iso_day(per_symbol.get("latest_date")) != target_date:
                row_reasons.append("candidate_upstream_symbol_not_current")
            if row_reasons:
                rejected.append(
                    {
                        "symbol": symbol,
                        "strategy_type": strategy_type,
                        "reason_codes": list(dict.fromkeys(row_reasons)),
                    }
                )
            else:
                # Provenance of acceptance: the batch records, per candidate,
                # exactly which upstream evidence qualified it, so a later
                # reader can audit the qualification instead of trusting the
                # candidate's own claims.
                candidate["source_qualification"] = {
                    "universe_member": bool(symbol and symbol in universe_symbols),
                    "feature_eligible": per_symbol.get("eligible") is True,
                    "feature_latest_date": (
                        _iso_day(per_symbol.get("latest_date"))
                        or feature_latest_date_default
                        or ""
                    ),
                    "feature_price_basis": (
                        str(per_symbol.get("price_basis") or "")
                        or feature_price_basis_default
                        or ""
                    ),
                    "excluded_by_daily_gate": bool(symbol in daily_blocked_symbols),
                    "excluded_by_feature_gate": bool(
                        _iso_day(per_symbol.get("latest_date")) != target_date
                    ),
                }
                accepted.append(candidate)

    formal_eligible = not reasons
    status = "blocked" if reasons else ("passed" if accepted else "passed_with_warnings")
    visible_reasons = list(dict.fromkeys(reasons))
    if formal_eligible and not accepted:
        visible_reasons.append("no_eligible_candidates")
    gate_payload = dict(manifest_gate)
    gate_payload["candidate_level_filtering"] = bool(rejected)
    source_run_id = str(manifest_gate.get("source_run_id") or "")
    payload: dict[str, Any] = {
        "task_type": "formal_candidate_daily_batch",
        "schema_version": CANDIDATE_REFRESH_SCHEMA_VERSION,
        "status": status,
        "formal_eligible": formal_eligible,
        "target_date": target_date,
        "generated_batch": f"formal-candidate-{target_date}" if target_date else "",
        "candidate_count": len(accepted),
        "candidates": accepted,
        "rejected_candidate_count": len(rejected),
        "rejected_candidates": rejected,
        "reason_codes": visible_reasons,
        "qualification_gate": gate_payload,
        "universe_source": gate_payload.get("universe_source") or "",
        "universe_filter_used": bool(gate_payload.get("universe_filter_used")),
        "universe_filter_id": gate_payload.get("universe_filter_id") or "",
        "universe_filter_count": int(gate_payload.get("universe_filter_count") or 0),
        "provenance": {
            "source": f"daily_data_chain:{source_run_id}" if source_run_id else "",
            "captured_at": normalized_captured_at,
            "hash": "",
            "source_run_id": source_run_id,
            "source_universe_hash": manifest_gate.get("source_universe_hash") or "",
            "source_feature_hash": manifest_gate.get("source_feature_hash") or "",
        },
        "network_used": False,
        "upstream_network_authorized": data_chain_manifest.get("network") is True,
        "writes_legacy_v420_candidate_csv": False,
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
            "paper_ledger_mutated": False,
        },
        "disclaimer": DISCLAIMER,
    }
    payload["provenance"]["hash"] = _stable_hash(_candidate_hash_projection(payload))
    return payload


def verify_formal_candidate_batch(payload: Mapping[str, Any]) -> list[str]:
    """Return complete semantic and cryptographic rejection reasons."""

    batch = _mapping(payload)
    provenance = _mapping(batch.get("provenance"))
    gate = _mapping(batch.get("qualification_gate"))
    reasons: list[str] = []
    if batch.get("task_type") != "formal_candidate_daily_batch":
        reasons.append("candidate_batch_task_type_invalid")
    if batch.get("schema_version") != CANDIDATE_REFRESH_SCHEMA_VERSION:
        reasons.append("candidate_batch_schema_version_invalid")
    target_date = _iso_day(batch.get("target_date"))
    if not target_date or str(batch.get("target_date") or "") != target_date:
        reasons.append("candidate_batch_target_date_invalid")
    if batch.get("generated_batch") != f"formal-candidate-{target_date}":
        reasons.append("candidate_batch_id_invalid")
    if not isinstance(batch.get("reason_codes"), list):
        reasons.append("candidate_batch_reason_codes_invalid")
    visible_reasons = list(batch.get("reason_codes") or [])

    candidates = batch.get("candidates")
    rejected = batch.get("rejected_candidates")
    if not isinstance(candidates, list):
        reasons.append("candidate_batch_candidates_invalid")
        candidates = []
    if not isinstance(rejected, list):
        reasons.append("candidate_batch_rejections_invalid")
        rejected = []
    for name, value, expected in (
        ("candidate_count", batch.get("candidate_count"), len(candidates)),
        (
            "rejected_candidate_count",
            batch.get("rejected_candidate_count"),
            len(rejected),
        ),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            reasons.append(f"candidate_batch_{name}_mismatch")

    accepted_keys: list[tuple[str, str]] = []
    for row in candidates:
        candidate = _mapping(row)
        symbol = str(candidate.get("symbol") or "")
        strategy_type = str(candidate.get("strategy_type") or "")
        accepted_keys.append((symbol, strategy_type))
        if not re.fullmatch(r"\d{6}", symbol):
            reasons.append("candidate_batch_candidate_symbol_invalid")
        if not strategy_type:
            reasons.append("candidate_batch_candidate_strategy_type_missing")
        if candidate.get("data_latest_date") != target_date:
            reasons.append("candidate_batch_candidate_date_mismatch")
        if candidate.get("daily_feature_available") is not True:
            reasons.append("candidate_batch_candidate_feature_missing")
        if candidate.get("daily_feature_latest_date") != target_date:
            reasons.append("candidate_batch_candidate_feature_date_mismatch")
        if candidate.get("is_target_date_covered") is not True:
            reasons.append("candidate_batch_candidate_coverage_invalid")
        if candidate.get("data_health_status") != "ok":
            reasons.append("candidate_batch_candidate_data_health_invalid")
        qualification = _mapping(candidate.get("source_qualification"))
        if (
            not isinstance(candidate.get("source_qualification"), Mapping)
            or qualification.get("universe_member") is not True
            or qualification.get("feature_eligible") is not True
            or qualification.get("excluded_by_daily_gate") is not False
            or qualification.get("excluded_by_feature_gate") is not False
            or not _iso_day(qualification.get("feature_latest_date"))
            or qualification.get("feature_price_basis") != "raw"
        ):
            reasons.append("candidate_batch_candidate_source_qualification_invalid")
        if not str(candidate.get("candidate_source") or "").strip():
            reasons.append("candidate_batch_candidate_source_missing")
        try:
            score = float(candidate.get("score"))
        except (TypeError, ValueError):
            score = math.nan
        if not math.isfinite(score):
            reasons.append("candidate_batch_candidate_score_nonfinite")
    if len(accepted_keys) != len(set(accepted_keys)):
        reasons.append("candidate_batch_duplicate_keys")
    for row in rejected:
        rejection = _mapping(row)
        rejection_reasons = rejection.get("reason_codes")
        if not isinstance(rejection_reasons, list) or not rejection_reasons:
            reasons.append("candidate_batch_rejection_reason_missing")

    source_run_id = _safe_run_id(provenance.get("source_run_id"))
    if not source_run_id:
        reasons.append("candidate_batch_source_run_id_invalid")
    if provenance.get("source") != f"daily_data_chain:{source_run_id}":
        reasons.append("candidate_batch_source_invalid")
    captured_at = _iso_datetime(provenance.get("captured_at"))
    if not captured_at:
        reasons.append("candidate_batch_captured_at_invalid")
    elif target_date and _iso_day(captured_at) < target_date:
        reasons.append("candidate_batch_captured_before_target_date")
    universe_hash = str(provenance.get("source_universe_hash") or "").lower()
    feature_hash = str(provenance.get("source_feature_hash") or "").lower()
    if not _is_sha256(universe_hash):
        reasons.append("candidate_batch_source_universe_hash_invalid")
    if not _is_sha256(feature_hash):
        reasons.append("candidate_batch_source_feature_hash_invalid")

    if gate.get("status") != "passed" or list(gate.get("reason_codes") or []):
        reasons.append("candidate_batch_qualification_gate_invalid")
    if gate.get("target_date") != target_date:
        reasons.append("candidate_batch_gate_target_date_mismatch")
    if gate.get("source_run_id") != source_run_id:
        reasons.append("candidate_batch_gate_source_run_id_mismatch")
    if gate.get("source_universe_hash") != universe_hash:
        reasons.append("candidate_batch_gate_universe_hash_mismatch")
    if gate.get("source_feature_hash") != feature_hash:
        reasons.append("candidate_batch_gate_feature_hash_mismatch")
    if gate.get("data_chain_contract_version") != REQUIRED_DATA_CHAIN_CONTRACT_VERSION:
        reasons.append("candidate_batch_gate_contract_version_invalid")

    formal_eligible = batch.get("formal_eligible") is True
    if not formal_eligible:
        reasons.append("candidate_batch_not_formal_eligible")
    expected_status = "passed" if candidates else "passed_with_warnings"
    if batch.get("status") != expected_status:
        reasons.append("candidate_batch_status_invalid")
    if candidates and "no_eligible_candidates" in visible_reasons:
        reasons.append("candidate_batch_reason_status_mismatch")
    if not candidates and "no_eligible_candidates" not in visible_reasons:
        reasons.append("candidate_batch_empty_reason_missing")
    if batch.get("network_used") is not False:
        reasons.append("candidate_batch_network_boundary_invalid")
    if batch.get("upstream_network_authorized") is not True:
        reasons.append("candidate_batch_upstream_network_evidence_missing")
    if batch.get("writes_legacy_v420_candidate_csv") is not False:
        reasons.append("candidate_batch_legacy_owner_boundary_invalid")

    declared_hash = str(provenance.get("hash") or "").strip().lower()
    if not _is_sha256(declared_hash):
        reasons.append("candidate_batch_hash_invalid")
    elif declared_hash != _stable_hash(_candidate_hash_projection(batch)):
        reasons.append("candidate_batch_hash_mismatch")
    boundaries = _mapping(batch.get("boundaries"))
    if (
        boundaries.get("research_only") is not True
        or boundaries.get("paper_account_only") is not True
        or boundaries.get("broker_route") is not False
        or boundaries.get("live_execution") is not False
        or boundaries.get("paper_ledger_mutated") is not False
    ):
        reasons.append("candidate_batch_boundary_invalid")
    return list(dict.fromkeys(reasons))


def load_formal_candidate_batch(
    project_root: Path,
    *,
    target_date: str,
) -> dict[str, Any]:
    """Load one immutable, date-addressed formal candidate batch.

    The paper loop must never consume ``formal_candidate_batch_latest.json``:
    that mutable pointer can advance independently of the review date.  The
    dated snapshot is therefore the only accepted path and is revalidated on
    every read, including its embedded provenance hash and candidate schema.
    """

    root = Path(project_root).resolve()
    normalized_date = _iso_day(target_date)
    if not normalized_date or str(target_date or "")[:10] != normalized_date:
        return {
            "status": "blocked",
            "batch": None,
            "path": "",
            "reason_codes": ["formal_candidate_batch_target_date_invalid"],
        }
    path = root / "results" / "candidate_refresh" / "daily" / f"{normalized_date}.json"
    payload, read_error = _read_json_strict(path)
    path_text = str(path)
    if read_error == "missing":
        return {
            "status": "missing",
            "batch": None,
            "path": path_text,
            "reason_codes": ["formal_candidate_batch_missing"],
        }
    if read_error:
        return {
            "status": "blocked",
            "batch": None,
            "path": path_text,
            "reason_codes": [f"formal_candidate_batch_{read_error}"],
        }
    assert payload is not None
    reasons = verify_formal_candidate_batch(payload)
    if _iso_day(payload.get("target_date")) != normalized_date:
        reasons.append("formal_candidate_batch_date_path_mismatch")
    if reasons:
        return {
            "status": "blocked",
            "batch": None,
            "path": path_text,
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    return {
        "status": "passed",
        "batch": payload,
        "path": path_text,
        "reason_codes": [],
    }


def load_formal_candidate_intake(
    project_root: Path, *, target_date: str,
    batch: Mapping[str, Any] | None = None,
    batch_path: Path | str | None = None, status: str = "",
    data_chain_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve and revalidate the one formal source allowed for a paper date.

    An injected batch is a claim to compare with the actual immutable source,
    never a way to bypass its verification. Completed-chain conflict recovery
    retains the same run binding as the daily workflow.
    """
    root = Path(project_root).resolve()
    target = _iso_day(target_date)
    if status and status != "passed":
        loaded: dict[str, Any] = {
            "status": status if status in {"missing", "blocked"} else "blocked",
            "batch": None, "path": str(batch_path or ""),
            "reason_codes": [f"formal_candidate_batch_{status}"],
        }
    else:
        latest, _ = _read_json_strict(root / "results/data_chain/daily_data_chain_latest.json")
        latest = latest or {}
        current_chain = bool(
            latest.get("status") == "completed" and latest.get("authoritative") is True
            and latest.get("published_latest") is True and latest.get("target_date") == target
        )
        if data_chain_manifest is not None:
            loaded = resolve_chain_bound_formal_candidate_batch(root, data_chain_manifest=data_chain_manifest)
            if data_chain_manifest.get("target_date") != target:
                loaded = {**loaded, "status": "blocked", "batch": None,
                          "reason_codes": [*loaded.get("reason_codes", []), "formal_candidate_chain_target_mismatch"]}
        elif current_chain:
            run_id = _safe_run_id(latest.get("run_id"))
            terminal, error = _read_json_strict(root / "results/data_chain/runs" / run_id / "manifest.json") if run_id else (None, "run_id_invalid")
            if error or terminal != latest:
                loaded = {"status": "blocked", "batch": None, "path": "",
                          "source_mode": "completed_chain_unverified",
                          "reason_codes": [f"formal_candidate_data_chain_terminal_{error or 'mismatch'}"]}
            else:
                loaded = resolve_chain_bound_formal_candidate_batch(root, data_chain_manifest=terminal)
        else:
            loaded = {**load_formal_candidate_batch(root, target_date=target_date),
                      "source_mode": "dated_observation_snapshot"}
        reasons = list(loaded.get("reason_codes") or [])
        if batch is not None:
            reasons.extend(verify_formal_candidate_batch(batch))
            if dict(batch) != loaded.get("batch"):
                reasons.append("formal_candidate_injected_batch_mismatch")
        if batch_path:
            claimed = (root / str(batch_path)).resolve()
            actual = (root / str(loaded.get("path") or "")).resolve()
            if claimed != actual:
                reasons.append("formal_candidate_injected_path_mismatch")
        if reasons:
            loaded = {**loaded, "status": "blocked" if loaded.get("status") != "missing" else "missing",
                      "batch": None, "reason_codes": list(dict.fromkeys(reasons))}
    accepted = loaded.get("batch") if loaded.get("status") == "passed" else None
    accepted = accepted if isinstance(accepted, Mapping) else {}
    provenance = _mapping(accepted.get("provenance"))
    rows = [row for row in accepted.get("candidates", []) if isinstance(row, Mapping)]
    return {
        **loaded, "target_date": target, "candidate_count": len(rows),
        "symbol_count": len({normalize_symbol(row.get("symbol")) for row in rows}),
        "source": "formal_candidate_daily_batch", "as_of": target,
        "provenance_hash": str(provenance.get("hash") or ""),
        "source_run_id": str(provenance.get("source_run_id") or ""),
        "input_source": "formal_candidate_batch" if accepted else f"formal_candidate_batch_{loaded.get('status') or 'blocked'}",
        "legacy_fallback_used": False,
    }


def formal_candidate_strategy_payload(intake: Mapping[str, Any]) -> dict[str, Any]:
    """Project verified source rows, retaining their complete batch envelopes."""
    batch = intake.get("batch") if intake.get("status") == "passed" else None
    batch = dict(batch) if isinstance(batch, Mapping) else None
    rows: list[dict[str, Any]] = []
    if batch is not None:
        for raw in batch.get("candidates") or []:
            row = copy.deepcopy(dict(raw))
            row["candidate_envelope"] = build_candidate_envelope(batch, batch_path=str(intake.get("path") or ""), candidate=raw)
            rows.append(row)
    return {
        "ok": batch is not None, "generated_at": str(intake.get("target_date") or ""),
        "candidates": rows, "standard_candidates": rows,
        "long_candidates": [row for row in rows if row.get("strategy_type") == "long"],
        "board_candidates": [row for row in rows if row.get("strategy_type") == "board"],
        "formal_candidate_batch": batch, "formal_candidate_batch_path": str(intake.get("path") or ""),
        "formal_candidate_status": str(intake.get("status") or "blocked"),
        "candidate_intake": {key: value for key, value in intake.items() if key != "batch"},
    }


def _chain_candidate_binding(
    batch: Mapping[str, Any],
    *,
    data_chain_manifest: Mapping[str, Any],
) -> list[str]:
    manifest = _mapping(data_chain_manifest)
    universe = _mapping(_mapping(manifest.get("universe")).get("current_analysis_pool"))
    coverage = _mapping(manifest.get("coverage"))
    components = _mapping(manifest.get("components"))
    features = _mapping(components.get("features"))
    feature_gate = _mapping(coverage.get("features"))
    provenance = _mapping(batch.get("provenance"))
    reasons: list[str] = []
    if str(provenance.get("source_run_id") or "") != str(manifest.get("run_id") or ""):
        reasons.append("formal_candidate_batch_source_run_mismatch")
    if str(provenance.get("source_universe_hash") or "").lower() != str(
        universe.get("sha256") or ""
    ).lower():
        reasons.append("formal_candidate_batch_source_universe_mismatch")
    expected_feature_hash = str(
        features.get("output_sha256") or feature_gate.get("output_sha256") or ""
    ).lower()
    if str(provenance.get("source_feature_hash") or "").lower() != expected_feature_hash:
        reasons.append("formal_candidate_batch_source_feature_mismatch")
    return reasons


def resolve_chain_bound_formal_candidate_batch(
    project_root: Path,
    *,
    data_chain_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the exact formal batch bound to one completed authoritative chain.

    The immutable dated snapshot remains the normal source.  A run-scoped batch
    is accepted only for the narrow same-day retry case where a valid earlier
    dated snapshot caused exactly ``candidate_dated_snapshot_conflict``.
    """

    root = Path(project_root).resolve()
    manifest = _mapping(data_chain_manifest)
    target_date = _iso_day(manifest.get("target_date"))
    run_id = _safe_run_id(manifest.get("run_id"))
    coverage = _mapping(manifest.get("coverage"))
    universe = _mapping(_mapping(manifest.get("universe")).get("current_analysis_pool"))
    components = _mapping(manifest.get("components"))
    features = _mapping(components.get("features"))
    feature_gate = _mapping(coverage.get("features"))
    feature_hash = str(
        features.get("output_sha256") or feature_gate.get("output_sha256") or ""
    ).lower()
    upstream_reasons: list[str] = []
    if str(manifest.get("status") or "") != "completed":
        upstream_reasons.append("authoritative_data_chain_not_completed")
    if manifest.get("authoritative") is not True:
        upstream_reasons.append("authoritative_data_chain_required")
    if manifest.get("network") is not True:
        upstream_reasons.append("network_authorized_data_chain_required")
    if manifest.get("dry_run") is True:
        upstream_reasons.append("dry_run_data_chain_not_allowed")
    if not target_date:
        upstream_reasons.append("authoritative_data_chain_target_date_invalid")
    if not run_id:
        upstream_reasons.append("authoritative_data_chain_run_id_invalid")
    if manifest.get("protected_state_unchanged") is not True:
        upstream_reasons.append("authoritative_data_chain_protected_state_invalid")
    # Daily/feature/minute holes are carried as per-symbol exclusions.  They do
    # not invalidate the unaffected candidate batch; structural and price-basis
    # failures remain hard gates.
    symbol_hole_codes = {
        "daily_symbols_stale",
        "daily_symbols_missing",
        "daily_symbols_failed",
        "feature_target_date_mismatch",
        "feature_symbols_skipped",
        "feature_symbol_set_mismatch",
        "feature_requested_count_mismatch",
        "feature_count_mismatch",
        "feature_output_row_count_mismatch",
    }
    research_gate = candidate_refresh_gate(manifest)
    verified_research = research_gate.get("formal_eligible") is True
    if not verified_research:
        upstream_reasons.extend(research_gate.get("reason_codes") or ["candidate_refresh_qualification_not_passed"])
    for key in ("daily", "current_analysis_pool", "features"):
        gate = _mapping(coverage.get(key))
        gate_status = str(gate.get("status") or "")
        gate_reasons = {str(item) for item in (gate.get("reason_codes") or []) if item}
        degraded = bool(
            (gate_reasons and gate_reasons.issubset(symbol_hole_codes))
            or (key in {"daily", "features"} and verified_research)
        )
        minute_not_requested = (
            key == "current_analysis_pool"
            and gate_status == "not_requested"
            and gate_reasons == {"analysis_pool_minute_on_demand"}
            and gate.get("requested_count") == 0
            and gate.get("audited_count") == 0
        )
        if gate_status not in {"passed", "partial"} and not degraded and not minute_not_requested:
            upstream_reasons.append(f"authoritative_data_chain_{key}_gate_not_passed")
    if not _is_sha256(universe.get("sha256")):
        upstream_reasons.append("authoritative_data_chain_universe_hash_invalid")
    if not _is_sha256(feature_hash):
        upstream_reasons.append("authoritative_data_chain_feature_hash_invalid")
    try:
        contract_version = int(manifest.get("daily_data_chain_contract_version") or 0)
    except (TypeError, ValueError):
        contract_version = 0
    if contract_version != REQUIRED_DATA_CHAIN_CONTRACT_VERSION:
        upstream_reasons.append("authoritative_data_chain_contract_version_mismatch")
    if upstream_reasons:
        return {
            "status": "blocked",
            "batch": None,
            "path": "",
            "source_mode": "",
            "reason_codes": list(dict.fromkeys(upstream_reasons)),
        }

    dated = load_formal_candidate_batch(root, target_date=target_date)
    dated_batch = dated.get("batch")
    if dated.get("status") != "passed" or not isinstance(dated_batch, Mapping):
        return {
            **dated,
            "status": "blocked",
            "source_mode": "",
            "reason_codes": list(dated.get("reason_codes") or [])
            or ["formal_candidate_dated_snapshot_not_passed"],
        }
    dated_binding_reasons = _chain_candidate_binding(
        dated_batch,
        data_chain_manifest=manifest,
    )
    if not dated_binding_reasons:
        return {
            **dated,
            "source_mode": "dated_snapshot",
        }

    refresh_gate = _mapping(components.get("candidate_refresh_gate"))
    refresh_result = _mapping(components.get("candidate_refresh"))
    recovery_reasons: list[str] = []
    if (
        str(refresh_gate.get("status") or "") != "passed"
        or refresh_gate.get("formal_eligible") is not True
        or str(refresh_gate.get("source_run_id") or "") != run_id
        or str(refresh_gate.get("target_date") or "") != target_date
        or str(refresh_gate.get("source_universe_hash") or "").lower()
        != str(universe.get("sha256") or "").lower()
        or str(refresh_gate.get("source_feature_hash") or "").lower() != feature_hash
        or str(refresh_gate.get("upstream_manifest_status") or "") != "completed"
    ):
        recovery_reasons.append("formal_candidate_recovery_gate_invalid")
    if (
        str(refresh_result.get("status") or "") != "blocked"
        or refresh_result.get("applied") is not False
        or list(refresh_result.get("reason_codes") or [])
        != ["candidate_dated_snapshot_conflict"]
    ):
        recovery_reasons.append("formal_candidate_recovery_reason_invalid")

    expected_path = (
        root
        / "results"
        / "candidate_refresh"
        / "runs"
        / run_id
        / "formal_candidate_batch.json"
    ).resolve()
    staging_text = str(refresh_result.get("staging_path") or "").strip()
    staging_path = Path(staging_text) if staging_text else None
    if (
        staging_path is None
        or staging_path.is_absolute()
        or (root / staging_path).resolve() != expected_path
    ):
        recovery_reasons.append("formal_candidate_recovery_staging_path_mismatch")
    if recovery_reasons:
        return {
            "status": "blocked",
            "batch": None,
            "path": str(expected_path),
            "source_mode": "",
            "reason_codes": list(
                dict.fromkeys(dated_binding_reasons + recovery_reasons)
            ),
            "dated_binding_reason_codes": dated_binding_reasons,
        }

    run_batch, read_error = _read_json_strict(expected_path)
    if read_error or run_batch is None:
        return {
            "status": "blocked",
            "batch": None,
            "path": str(expected_path),
            "source_mode": "",
            "reason_codes": [
                f"formal_candidate_run_snapshot_{read_error or 'invalid'}"
            ],
            "dated_binding_reason_codes": dated_binding_reasons,
        }
    run_reasons = verify_formal_candidate_batch(run_batch)
    run_gate = _mapping(run_batch.get("qualification_gate"))
    if str(run_gate.get("upstream_manifest_status") or "") != "completed":
        run_reasons.append("formal_candidate_run_snapshot_upstream_not_completed")
    if run_batch.get("formal_eligible") is not True:
        run_reasons.append("candidate_batch_not_formal_eligible")
    if _iso_day(run_batch.get("target_date")) != target_date:
        run_reasons.append("formal_candidate_run_snapshot_target_date_mismatch")
    run_reasons.extend(
        _chain_candidate_binding(run_batch, data_chain_manifest=manifest)
    )
    if run_reasons:
        return {
            "status": "blocked",
            "batch": None,
            "path": str(expected_path),
            "source_mode": "",
            "reason_codes": list(dict.fromkeys(run_reasons)),
            "dated_binding_reason_codes": dated_binding_reasons,
        }
    return {
        "status": "passed",
        "batch": run_batch,
        "path": str(expected_path),
        "source_mode": "run_scoped_conflict_recovery",
        "reason_codes": [],
        "dated_path": str(dated.get("path") or ""),
        "dated_binding_reason_codes": dated_binding_reasons,
    }


def build_candidate_envelope(
    batch: Mapping[str, Any],
    *,
    batch_path: Path | str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one exact formal candidate to its immutable formal batch."""

    candidate_copy = copy.deepcopy(dict(candidate))
    source_run_id = _safe_run_id(_mapping(batch.get("provenance")).get("source_run_id"))
    return {
        "schema_version": CANDIDATE_ENVELOPE_SCHEMA_VERSION,
        "target_date": _iso_day(batch.get("target_date")),
        "batch_path": str(batch_path),
        "batch_provenance_hash": str(
            _mapping(batch.get("provenance")).get("hash") or ""
        ).lower(),
        "source_run_id": source_run_id,
        "candidate_sha256": _stable_hash(candidate_copy),
        "candidate": candidate_copy,
    }


def verify_candidate_envelope(envelope: Mapping[str, Any] | None) -> list[str]:
    """Validate envelope structure and the candidate-to-batch binding claims."""

    if not isinstance(envelope, Mapping):
        return ["candidate_envelope_missing"]
    payload = _mapping(envelope)
    reasons: list[str] = []
    if payload.get("schema_version") != CANDIDATE_ENVELOPE_SCHEMA_VERSION:
        reasons.append("candidate_envelope_schema_invalid")
    target_date = _iso_day(payload.get("target_date"))
    if not target_date or str(payload.get("target_date") or "") != target_date:
        reasons.append("candidate_envelope_target_date_invalid")
    batch_path_text = str(payload.get("batch_path") or "").strip()
    batch_path = Path(batch_path_text) if batch_path_text else None
    dated_path = bool(
        batch_path is not None
        and batch_path.suffix.lower() == ".json"
        and batch_path.stem == target_date
        and batch_path.parent.name == "daily"
        and batch_path.parent.parent.name == "candidate_refresh"
    )
    source_run_id = _safe_run_id(payload.get("source_run_id"))
    run_scoped_path = bool(
        batch_path is not None
        and batch_path.name == "formal_candidate_batch.json"
        and batch_path.parent.parent.name == "runs"
        and batch_path.parent.parent.parent.name == "candidate_refresh"
    )
    if not dated_path and not run_scoped_path:
        reasons.append("candidate_envelope_batch_path_invalid")
    elif run_scoped_path:
        if not source_run_id:
            reasons.append("candidate_envelope_source_run_id_invalid")
        elif batch_path is not None and batch_path.parent.name != source_run_id:
            reasons.append("candidate_envelope_source_run_id_mismatch")
    elif payload.get("source_run_id") not in (None, "") and not source_run_id:
        reasons.append("candidate_envelope_source_run_id_invalid")
    if not _is_sha256(payload.get("batch_provenance_hash")):
        reasons.append("candidate_envelope_batch_hash_invalid")
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping):
        reasons.append("candidate_envelope_candidate_invalid")
    else:
        declared_hash = str(payload.get("candidate_sha256") or "").lower()
        if not _is_sha256(declared_hash):
            reasons.append("candidate_envelope_candidate_hash_invalid")
        elif declared_hash != _stable_hash(candidate):
            reasons.append("candidate_envelope_candidate_hash_mismatch")
        if _iso_day(candidate.get("data_latest_date")) != target_date:
            reasons.append("candidate_envelope_candidate_date_mismatch")
        if _iso_day(candidate.get("daily_feature_latest_date")) != target_date:
            reasons.append("candidate_envelope_feature_date_mismatch")
    return list(dict.fromkeys(reasons))


def _same_decimal_value(left: Any, right: Any) -> bool:
    try:
        left_value = Decimal(str(left))
        right_value = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        left_value.is_finite()
        and right_value.is_finite()
        and left_value == right_value
    )


def _candidate_batch_path_key(project_root: Path, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text)
        if not path.is_absolute():
            path = Path(project_root).resolve() / path
        return os.path.normcase(str(path.resolve()))
    except (OSError, RuntimeError, ValueError):
        return ""


def _preserve_modern_due_plan_candidate_envelopes(
    project_root: Path,
    plan_copy: dict[str, Any],
    rows: list[Any],
    *,
    plan_date: str,
    trade_date: str,
) -> dict[str, Any]:
    """Keep already-bound modern plan envelopes; never rebind them by date."""

    batch_path = str(plan_copy.get("formal_candidate_batch_path") or "").strip()
    batch_path_key = _candidate_batch_path_key(project_root, batch_path)
    batch_hash = str(
        plan_copy.get("formal_candidate_batch_provenance_hash") or ""
    ).strip().lower()
    run_id = _safe_run_id(plan_copy.get("data_chain_run_id"))
    declared_source_mode = str(
        plan_copy.get("formal_candidate_batch_source_mode") or ""
    ).strip()
    dated_path_key = _candidate_batch_path_key(
        project_root,
        Path("results") / "candidate_refresh" / "daily" / f"{plan_date}.json",
    )
    run_scoped_path_key = (
        _candidate_batch_path_key(
            project_root,
            Path("results")
            / "candidate_refresh"
            / "runs"
            / run_id
            / "formal_candidate_batch.json",
        )
        if run_id
        else ""
    )
    source_mode = declared_source_mode
    if not source_mode:
        if batch_path_key == dated_path_key:
            source_mode = "dated_snapshot"
        elif run_scoped_path_key and batch_path_key == run_scoped_path_key:
            source_mode = "run_scoped_conflict_recovery"
    plan_reasons: list[str] = []
    if not batch_path_key:
        plan_reasons.append("modern_candidate_batch_path_invalid")
    if not _is_sha256(batch_hash):
        plan_reasons.append("modern_candidate_batch_hash_invalid")
    if not run_id:
        plan_reasons.append("modern_candidate_data_chain_run_invalid")
    if source_mode not in {"dated_snapshot", "run_scoped_conflict_recovery"}:
        plan_reasons.append("modern_candidate_batch_source_mode_invalid")
    elif (
        source_mode == "dated_snapshot" and batch_path_key != dated_path_key
    ) or (
        source_mode == "run_scoped_conflict_recovery"
        and batch_path_key != run_scoped_path_key
    ):
        plan_reasons.append("modern_candidate_batch_source_mode_path_mismatch")

    exact_bindable = 0
    existing_valid_envelope = 0
    unchanged_unmatched = 0
    rejected_mismatch = 0
    rejections: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            unchanged_unmatched += 1
            continue
        if str(row.get("position_state") or "") == "watch_exit":
            unchanged_unmatched += 1
            continue

        symbol = normalize_symbol(row.get("symbol"))
        strategy_type = str(row.get("strategy_type") or "").strip()
        row_reasons = list(plan_reasons)
        if (
            _iso_day(row.get("plan_date")) != plan_date
            or _iso_day(row.get("source_data_cutoff")) != plan_date
        ):
            row_reasons.append("modern_candidate_plan_date_mismatch")
        if _iso_day(row.get("trade_date")) != trade_date:
            row_reasons.append("modern_candidate_trade_date_mismatch")

        envelope = row.get("candidate_envelope")
        envelope_reasons = verify_candidate_envelope(
            envelope if isinstance(envelope, Mapping) else None
        )
        row_reasons.extend(envelope_reasons)
        envelope_map = _mapping(envelope)
        envelope_path_key = _candidate_batch_path_key(
            project_root,
            envelope_map.get("batch_path"),
        )
        if batch_path_key != envelope_path_key:
            row_reasons.append("modern_candidate_batch_path_mismatch")
        if batch_hash != str(
            envelope_map.get("batch_provenance_hash") or ""
        ).strip().lower():
            row_reasons.append("modern_candidate_batch_hash_mismatch")
        envelope_run_raw = envelope_map.get("source_run_id")
        envelope_run_id = _safe_run_id(envelope_run_raw)
        if source_mode == "run_scoped_conflict_recovery" and run_id != envelope_run_id:
            row_reasons.append("modern_candidate_data_chain_run_mismatch")
        elif (
            source_mode == "dated_snapshot"
            and envelope_run_raw not in (None, "")
            and run_id != envelope_run_id
        ):
            row_reasons.append("modern_candidate_data_chain_run_mismatch")

        candidate = envelope_map.get("candidate")
        candidate_map = _mapping(candidate)
        if normalize_symbol(candidate_map.get("symbol")) != symbol:
            row_reasons.append("modern_candidate_symbol_mismatch")
        if str(candidate_map.get("strategy_type") or "").strip() != strategy_type:
            row_reasons.append("modern_candidate_strategy_mismatch")
        if not _same_decimal_value(
            row.get("reference_close"),
            candidate_map.get("close"),
        ):
            row_reasons.append("modern_candidate_reference_close_mismatch")

        row_reasons = list(dict.fromkeys(row_reasons))
        if row_reasons:
            row.pop("candidate_envelope", None)
            rejected_mismatch += 1
            rejections.append(
                {
                    "symbol": symbol,
                    "strategy_type": strategy_type,
                    "reason_codes": row_reasons,
                }
            )
            continue
        exact_bindable += 1
        existing_valid_envelope += 1

    reason_codes = list(
        dict.fromkeys(
            reason
            for rejection in rejections
            for reason in (rejection.get("reason_codes") or [])
        )
    )
    return {
        "status": (
            "blocked"
            if plan_reasons
            else ("completed_with_rejections" if rejections else "completed")
        ),
        "plan": plan_copy,
        "target_date": plan_date,
        "formal_batch_path": batch_path,
        "exact_bindable": exact_bindable,
        "enriched": 0,
        "existing_valid_envelope": existing_valid_envelope,
        "rebound_existing_envelope": 0,
        "unchanged_unmatched": unchanged_unmatched,
        "rejected_mismatch": rejected_mismatch,
        "rejections": rejections,
        "warnings": [],
        "reason_codes": reason_codes,
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
            "archive_write": False,
            "state_write": False,
        },
    }


def enrich_legacy_due_plan_candidate_envelopes(
    project_root: Path,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind legacy due-plan rows to their original dated formal batch in memory.

    Append-only plan archives are never rewritten.  A row is enriched only
    when its plan date, source cutoff, exact ``(symbol, strategy_type)`` key,
    and reference close all agree with the verified formal candidate.  Any
    ambiguity leaves the row unchanged so downstream shadow evaluation remains
    fail-closed.
    """

    plan_copy = copy.deepcopy(dict(plan))
    rows = plan_copy.get("symbols")
    if not isinstance(rows, list):
        return {
            "status": "blocked",
            "plan": plan_copy,
            "target_date": "",
            "formal_batch_path": "",
            "exact_bindable": 0,
            "enriched": 0,
            "existing_valid_envelope": 0,
            "rebound_existing_envelope": 0,
            "unchanged_unmatched": 0,
            "rejected_mismatch": 0,
            "rejections": [],
            "warnings": [],
            "reason_codes": ["legacy_due_plan_symbols_invalid"],
        }

    plan_date = _iso_day(plan_copy.get("plan_date"))
    cutoff = _iso_day(plan_copy.get("source_data_cutoff"))
    trade_date = _iso_day(plan_copy.get("trade_date"))
    if not plan_date or cutoff != plan_date:
        return {
            "status": "blocked",
            "plan": plan_copy,
            "target_date": plan_date,
            "formal_batch_path": "",
            "exact_bindable": 0,
            "enriched": 0,
            "existing_valid_envelope": 0,
            "rebound_existing_envelope": 0,
            "unchanged_unmatched": len(rows),
            "rejected_mismatch": 0,
            "rejections": [],
            "warnings": [],
            "reason_codes": ["legacy_due_plan_date_mismatch"],
        }

    modern_candidate_binding_fields = (
        "formal_candidate_batch_path",
        "formal_candidate_batch_provenance_hash",
        "formal_candidate_batch_source_mode",
    )
    if any(field in plan_copy for field in modern_candidate_binding_fields):
        return _preserve_modern_due_plan_candidate_envelopes(
            Path(project_root).resolve(),
            plan_copy,
            rows,
            plan_date=plan_date,
            trade_date=trade_date,
        )

    loaded = load_formal_candidate_batch(
        Path(project_root).resolve(),
        target_date=plan_date,
    )
    batch_path = str(loaded.get("path") or "")
    batch = loaded.get("batch")
    if loaded.get("status") != "passed" or not isinstance(batch, Mapping):
        reasons = list(loaded.get("reason_codes") or [])
        if not reasons:
            reasons.append("legacy_formal_candidate_batch_unavailable")
        return {
            "status": "blocked",
            "plan": plan_copy,
            "target_date": plan_date,
            "formal_batch_path": batch_path,
            "exact_bindable": 0,
            "enriched": 0,
            "existing_valid_envelope": 0,
            "rebound_existing_envelope": 0,
            "unchanged_unmatched": len(rows),
            "rejected_mismatch": 0,
            "rejections": [],
            "warnings": [],
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    if _iso_day(batch.get("target_date")) != plan_date:
        return {
            "status": "blocked",
            "plan": plan_copy,
            "target_date": plan_date,
            "formal_batch_path": batch_path,
            "exact_bindable": 0,
            "enriched": 0,
            "existing_valid_envelope": 0,
            "rebound_existing_envelope": 0,
            "unchanged_unmatched": len(rows),
            "rejected_mismatch": 0,
            "rejections": [],
            "warnings": [],
            "reason_codes": ["legacy_formal_candidate_date_mismatch"],
        }

    candidates_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_strategies: dict[str, set[str]] = {}
    duplicate_keys: set[tuple[str, str]] = set()
    for raw in batch.get("candidates") or []:
        if not isinstance(raw, Mapping):
            continue
        candidate = copy.deepcopy(dict(raw))
        symbol = normalize_symbol(candidate.get("symbol"))
        strategy_type = str(candidate.get("strategy_type") or "").strip()
        key = (symbol, strategy_type)
        if not symbol or not strategy_type:
            continue
        if key in candidates_by_key:
            duplicate_keys.add(key)
        candidates_by_key[key] = candidate
        candidate_strategies.setdefault(symbol, set()).add(strategy_type)
    if duplicate_keys:
        return {
            "status": "blocked",
            "plan": plan_copy,
            "target_date": plan_date,
            "formal_batch_path": batch_path,
            "exact_bindable": 0,
            "enriched": 0,
            "existing_valid_envelope": 0,
            "rebound_existing_envelope": 0,
            "unchanged_unmatched": len(rows),
            "rejected_mismatch": 0,
            "rejections": [],
            "warnings": [],
            "reason_codes": ["legacy_formal_candidate_duplicate_key"],
        }

    exact_bindable = 0
    enriched = 0
    existing_valid_envelope = 0
    rebound_existing_envelope = 0
    unchanged_unmatched = 0
    rejected_mismatch = 0
    rejections: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            unchanged_unmatched += 1
            continue
        if str(row.get("position_state") or "") == "watch_exit":
            unchanged_unmatched += 1
            continue

        symbol = normalize_symbol(row.get("symbol"))
        strategy_type = str(row.get("strategy_type") or "").strip()
        row_reasons: list[str] = []
        if (
            _iso_day(row.get("plan_date")) != plan_date
            or _iso_day(row.get("source_data_cutoff")) != plan_date
        ):
            row_reasons.append("legacy_candidate_plan_date_mismatch")
        if _iso_day(row.get("trade_date")) != trade_date:
            row_reasons.append("legacy_candidate_trade_date_mismatch")
        candidate = candidates_by_key.get((symbol, strategy_type))
        if candidate is None:
            if symbol in candidate_strategies:
                row_reasons.append("legacy_candidate_strategy_mismatch")
            else:
                row_reasons.append("legacy_candidate_not_in_formal_batch")
        elif not _same_decimal_value(
            row.get("reference_close"), candidate.get("close")
        ):
            row_reasons.append("legacy_candidate_reference_close_mismatch")
        if row_reasons or candidate is None:
            rejected_mismatch += 1
            rejections.append(
                {
                    "symbol": symbol,
                    "strategy_type": strategy_type,
                    "reason_codes": list(dict.fromkeys(row_reasons)),
                }
            )
            continue

        envelope = build_candidate_envelope(
            batch,
            batch_path=batch_path,
            candidate=candidate,
        )
        envelope_reasons = verify_candidate_envelope(envelope)
        if envelope_reasons:
            rejected_mismatch += 1
            rejections.append(
                {
                    "symbol": symbol,
                    "strategy_type": strategy_type,
                    "reason_codes": envelope_reasons,
                }
            )
            continue
        exact_bindable += 1
        existing = row.get("candidate_envelope")
        if (
            isinstance(existing, Mapping)
            and not verify_candidate_envelope(existing)
            and dict(existing) == envelope
        ):
            existing_valid_envelope += 1
            continue
        if "candidate_envelope" in row:
            rebound_existing_envelope += 1
            warnings.append("legacy_candidate_existing_envelope_rebound")
        row["candidate_envelope"] = envelope
        enriched += 1

    return {
        "status": (
            "completed_with_rejections"
            if rejections
            else ("completed_with_warnings" if warnings else "completed")
        ),
        "plan": plan_copy,
        "target_date": plan_date,
        "formal_batch_path": batch_path,
        "exact_bindable": exact_bindable,
        "enriched": enriched,
        "existing_valid_envelope": existing_valid_envelope,
        "rebound_existing_envelope": rebound_existing_envelope,
        "unchanged_unmatched": unchanged_unmatched,
        "rejected_mismatch": rejected_mismatch,
        "rejections": rejections,
        "warnings": list(dict.fromkeys(warnings)),
        "reason_codes": list(
            dict.fromkeys(
                [
                    reason
                    for rejection in rejections
                    for reason in (rejection.get("reason_codes") or [])
                ]
                + warnings
            )
        ),
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
            "archive_write": False,
            "state_write": False,
        },
    }




def _exclusive_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Create one immutable JSON file, never replacing an existing path."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    completed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        completed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not completed:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass


def _write_run_candidate_batch_once(
    path: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal one run-scoped batch without ever replacing existing bytes."""

    target = Path(path)
    expected_hash = str(_mapping(payload.get("provenance")).get("hash") or "")
    existing, read_error = _read_json_strict(target)
    if read_error not in {"", "missing"}:
        return {
            "status": "blocked",
            "reason_codes": ["candidate_run_snapshot_unreadable"],
        }
    if existing is not None:
        existing_hash = str(
            _mapping(existing.get("provenance")).get("hash") or ""
        )
        if existing_hash == expected_hash and not verify_formal_candidate_batch(existing):
            return {"status": "idempotent", "reason_codes": []}
        return {
            "status": "blocked",
            "reason_codes": ["candidate_run_snapshot_conflict"],
        }
    try:
        _exclusive_write_json(target, payload)
    except FileExistsError:
        raced, raced_error = _read_json_strict(target)
        raced_hash = str(_mapping(_mapping(raced).get("provenance")).get("hash") or "")
        if (
            raced_error
            or raced is None
            or raced_hash != expected_hash
            or verify_formal_candidate_batch(raced)
        ):
            return {
                "status": "blocked",
                "reason_codes": ["candidate_run_snapshot_conflict"],
            }
        return {"status": "idempotent", "reason_codes": []}
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason_codes": ["candidate_run_snapshot_write_failed"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }
    return {"status": "created", "reason_codes": []}


def publish_formal_candidate_batch(
    project_root: Path,
    payload: Mapping[str, Any],
    *,
    replace_same_date: bool = False,
) -> dict[str, Any]:
    """Publish one verified batch to an append-only date and atomic latest.

    ``replace_same_date`` is Pipeline-only: a verified upstream-reuse run may
    take over the same target date that a previous writer already sealed.
    Scheduled same-day retries keep the default append-only conflict.
    """

    root = Path(project_root).resolve()
    batch = copy.deepcopy(dict(payload))
    reasons = verify_formal_candidate_batch(batch)
    if batch.get("formal_eligible") is not True:
        reasons.append("candidate_batch_not_formal_eligible")
    if reasons:
        return {
            "status": "blocked",
            "applied": False,
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    target_date = str(batch.get("target_date") or "")
    base = root / "results" / "candidate_refresh"
    dated = base / "daily" / f"{target_date}.json"
    latest = base / "formal_candidate_batch_latest.json"
    lock_path = base / ".formal_candidate_publish.lock"
    batch_hash = str(_mapping(batch.get("provenance")).get("hash") or "")
    dated_rel = str(dated.relative_to(root)).replace("\\", "/")
    latest_rel = str(latest.relative_to(root)).replace("\\", "/")
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=0):
            dated_existing, dated_error = _read_json_strict(dated)
            if dated_error not in {"", "missing"}:
                return {
                    "status": "blocked",
                    "applied": False,
                    "reason_codes": ["candidate_dated_snapshot_unreadable"],
                    "dated_path": dated_rel,
                    "existing_error": dated_error,
                }
            if dated_existing is not None:
                existing_hash = str(
                    _mapping(dated_existing.get("provenance")).get("hash") or ""
                )
                if (
                    existing_hash != batch_hash
                    or verify_formal_candidate_batch(dated_existing)
                ):
                    if not replace_same_date:
                        return {
                            "status": "blocked",
                            "applied": False,
                            "reason_codes": ["candidate_dated_snapshot_conflict"],
                            "dated_path": dated_rel,
                        }
                    try:
                        atomic_write_json(dated, batch)
                    except (OSError, RuntimeError, ValueError) as exc:
                        return {
                            "status": "blocked",
                            "applied": False,
                            "reason_codes": ["candidate_dated_snapshot_write_failed"],
                            "error": f"{type(exc).__name__}:{exc}"[:1000],
                            "dated_path": dated_rel,
                        }
                    dated_existing = None
            else:
                try:
                    _exclusive_write_json(dated, batch)
                except FileExistsError:
                    raced, raced_error = _read_json_strict(dated)
                    raced_hash = str(
                        _mapping(_mapping(raced).get("provenance")).get("hash")
                        or ""
                    )
                    if (
                        raced_error
                        or raced is None
                        or raced_hash != batch_hash
                        or verify_formal_candidate_batch(raced)
                    ):
                        if not replace_same_date:
                            return {
                                "status": "blocked",
                                "applied": False,
                                "reason_codes": ["candidate_dated_snapshot_conflict"],
                                "dated_path": dated_rel,
                            }
                        try:
                            atomic_write_json(dated, batch)
                        except (OSError, RuntimeError, ValueError) as exc:
                            return {
                                "status": "blocked",
                                "applied": False,
                                "reason_codes": [
                                    "candidate_dated_snapshot_write_failed"
                                ],
                                "error": f"{type(exc).__name__}:{exc}"[:1000],
                                "dated_path": dated_rel,
                            }
                        dated_existing = None
                    else:
                        dated_existing = raced
                except (OSError, RuntimeError, ValueError) as exc:
                    return {
                        "status": "blocked",
                        "applied": False,
                        "reason_codes": ["candidate_dated_snapshot_write_failed"],
                        "error": f"{type(exc).__name__}:{exc}"[:1000],
                    }

            latest_existing, latest_error = _read_json_strict(latest)
            if latest_error not in {"", "missing"}:
                return {
                    "status": "blocked",
                    "applied": False,
                    "reason_codes": ["candidate_latest_snapshot_unreadable"],
                    "existing_error": latest_error,
                }
            if latest_existing is not None:
                if verify_formal_candidate_batch(latest_existing):
                    return {
                        "status": "blocked",
                        "applied": False,
                        "reason_codes": ["candidate_latest_snapshot_invalid"],
                    }
                latest_date = _iso_day(latest_existing.get("target_date"))
                latest_hash = str(
                    _mapping(latest_existing.get("provenance")).get("hash") or ""
                )
                if latest_date > target_date:
                    return {
                        "status": "blocked",
                        "applied": False,
                        "reason_codes": ["candidate_latest_regression"],
                    }
                if latest_date == target_date and latest_hash != batch_hash:
                    if not replace_same_date:
                        return {
                            "status": "blocked",
                            "applied": False,
                            "reason_codes": ["candidate_latest_same_date_conflict"],
                        }

            latest_same = bool(
                latest_existing
                and _iso_day(latest_existing.get("target_date")) == target_date
                and str(
                    _mapping(latest_existing.get("provenance")).get("hash") or ""
                )
                == batch_hash
            )
            if not latest_same:
                try:
                    atomic_write_json(latest, batch)
                except RuntimeError as exc:
                    return {
                        "status": "blocked",
                        "applied": False,
                        "reason_codes": ["candidate_latest_publish_failed"],
                        "error": f"{type(exc).__name__}:{exc}"[:1000],
                    }
            return {
                "status": (
                    "idempotent" if dated_existing is not None and latest_same else "completed"
                ),
                "applied": True,
                "reason_codes": [],
                "dated_path": dated_rel,
                "latest_path": latest_rel,
                "candidate_count": len(batch.get("candidates") or []),
                "candidate_batch_hash": batch_hash,
            }
    except TimeoutError:
        return {
            "status": "blocked",
            "applied": False,
            "reason_codes": ["candidate_publish_lock_busy"],
        }


def run_formal_candidate_refresh(
    *,
    project_root: Path,
    data_chain_manifest: Mapping[str, Any],
    strategy_payload: Mapping[str, Any] | None = None,
    strategy_runner: StrategyRunner | None = None,
    captured_at: str,
    apply: bool = False,
    replace_same_date: bool = False,
    use_low_price_filter: bool = False,
) -> dict[str, Any]:
    """Stage a candidate batch, then optionally publish its dated/latest pair."""

    root = Path(project_root).resolve()
    manifest_gate = _manifest_gate(data_chain_manifest)
    run_id = str(manifest_gate.get("source_run_id") or "")
    if not run_id:
        return {
            "status": "blocked",
            "applied": False,
            "reason_codes": list(manifest_gate.get("reason_codes") or [])
            or ["upstream_run_id_invalid"],
        }
    staging_path = (
        root
        / "results"
        / "candidate_refresh"
        / "runs"
        / run_id
        / "formal_candidate_batch.json"
    )
    scoped_universe_path: Path | None = None

    resolved_strategy: Mapping[str, Any]
    if strategy_payload is not None:
        resolved_strategy = strategy_payload
    elif manifest_gate.get("status") != "passed":
        resolved_strategy = {}
    else:
        if strategy_runner is None:
            if root != PROJECT_ROOT.resolve():
                return {
                    "status": "blocked",
                    "applied": False,
                    "reason_codes": [
                        "candidate_strategy_runner_injection_required_for_custom_root"
                    ],
                }
            from quantdesk.auto_strategy_sandbox import run_auto_strategy_cycle

            strategy_runner = run_auto_strategy_cycle
        # The default strategy input is the all-A universe with per-symbol
        # exclusions already applied by the gate.  The legacy 914 file is only
        # selected when a caller explicitly opts into that named filter.
        source_path = (
            root / "data" / "universe" / "low_price_learning_symbols.csv"
            if use_low_price_filter
            else root / "data" / "universe" / "a_share_symbols.csv"
        )
        scoped_universe_path, scope_reasons = _write_candidate_eligible_universe(
            root=root,
            run_id=run_id,
            eligible_symbols=list(manifest_gate.get("eligible_symbols") or []),
            source_path=source_path,
            require_included=bool(use_low_price_filter),
        )
        if scope_reasons:
            return {
                "status": "blocked",
                "applied": False,
                "reason_codes": scope_reasons,
                "qualification_gate": manifest_gate,
            }
        try:
            resolved_strategy = strategy_runner(
                mode="live",
                strategy="all",
                initial_cash=10_000.0,
                dry_run=True,
                universe_path=scoped_universe_path,
                daily_dir=root / "data" / "daily",
                report_json=root / "results" / "low_price_universe_latest.json",
                expected_latest_date=str(data_chain_manifest.get("target_date") or "")[:10],
            )
        except Exception as exc:  # noqa: BLE001 - failure is staged visibly
            resolved_strategy = {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }

    batch = build_formal_candidate_batch(
        data_chain_manifest=data_chain_manifest,
        strategy_payload=resolved_strategy,
        captured_at=captured_at,
    )
    staging_rel = str(staging_path.relative_to(root)).replace("\\", "/")
    run_snapshot = _write_run_candidate_batch_once(staging_path, batch)
    if run_snapshot.get("status") == "blocked":
        return {
            "status": "blocked",
            "applied": False,
            "reason_codes": list(run_snapshot.get("reason_codes") or []),
            "staging_path": staging_rel,
            **(
                {"error": run_snapshot["error"]}
                if run_snapshot.get("error")
                else {}
            ),
        }
    if batch.get("formal_eligible") is not True:
        return {
            "status": "blocked",
            "applied": False,
            "reason_codes": list(batch.get("reason_codes") or []),
            "staging_path": staging_rel,
            "candidate_batch": batch,
            "scoped_universe_path": (
                str(scoped_universe_path.relative_to(root)).replace("\\", "/")
                if scoped_universe_path is not None
                else ""
            ),
        }
    if not apply:
        return {
            "status": "staged",
            "applied": False,
            "reason_codes": [],
            "staging_path": staging_rel,
            "candidate_count": batch.get("candidate_count", 0),
            "candidate_batch_hash": _mapping(batch.get("provenance")).get("hash"),
            "scoped_universe_path": (
                str(scoped_universe_path.relative_to(root)).replace("\\", "/")
                if scoped_universe_path is not None
                else ""
            ),
        }
    published = publish_formal_candidate_batch(
        root, batch, replace_same_date=replace_same_date
    )
    return {
        **published,
        "staging_path": staging_rel,
    }


def _plan_symbols(plan: Mapping[str, Any]) -> list[str]:
    symbols: list[str] = []
    for row in plan.get("symbols") or []:
        value = row.get("symbol") if isinstance(row, Mapping) else row
        symbol = normalize_symbol(value)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _formal_plan_reasons(
    plan: Mapping[str, Any], *, as_of: str, role: str
) -> list[str]:
    reasons: list[str] = []
    plan_id = str(plan.get("plan_id") or "").strip()
    plan_date = _iso_day(plan.get("plan_date"))
    trade_date = _iso_day(plan.get("trade_date"))
    cutoff = _iso_day(plan.get("source_data_cutoff"))
    if not plan_id:
        reasons.append(f"{role}_plan_id_missing")
    if plan.get("ok") is not True:
        reasons.append(f"{role}_plan_not_ok")
    if plan.get("formal_execution_eligible") is not True:
        reasons.append(f"{role}_plan_not_formal")
    if not _plan_symbols(plan):
        reasons.append(f"{role}_plan_symbols_missing")
    validation = _mapping(plan.get("price_data_validation"))
    if (
        validation.get("status") != "passed"
        or validation.get("formal_execution_eligible") is not True
        or list(validation.get("blocked_symbols") or [])
    ):
        reasons.append(f"{role}_plan_price_gate_not_passed")
    if role == "due":
        if trade_date != as_of:
            reasons.append("due_plan_trade_date_mismatch")
        if not plan_date or plan_date >= as_of:
            reasons.append("due_plan_date_invalid")
        if cutoff != plan_date:
            reasons.append("due_plan_cutoff_mismatch")
        if _iso_day(validation.get("target_date")) != plan_date:
            reasons.append("due_plan_price_target_date_mismatch")
    else:
        if plan_date != as_of:
            reasons.append("next_plan_date_mismatch")
        if not trade_date or trade_date <= as_of:
            reasons.append("next_plan_trade_date_invalid")
        if cutoff != as_of:
            reasons.append("next_plan_cutoff_mismatch")
        if _iso_day(validation.get("target_date")) != as_of:
            reasons.append("next_plan_price_target_date_mismatch")
    return reasons


def _conditional_plan_hash(plan: Mapping[str, Any]) -> str:
    return _stable_hash(dict(plan))


def _conditional_plan_archive_reasons(plan: Mapping[str, Any]) -> list[str]:
    plan_date = _iso_day(plan.get("plan_date"))
    if not plan_date:
        return ["conditional_plan_archive_plan_date_invalid"]
    return [
        reason.replace("next_plan_", "conditional_plan_archive_")
        for reason in _formal_plan_reasons(plan, as_of=plan_date, role="next")
    ]


def _archive_conditional_plan_locked(
    root: Path, plan: Mapping[str, Any], *, replace_same_date: bool = False
) -> dict[str, Any]:
    plan_payload = copy.deepcopy(dict(plan))
    reasons = _conditional_plan_archive_reasons(plan_payload)
    try:
        plan_hash = _conditional_plan_hash(plan_payload)
    except (TypeError, ValueError):
        reasons.append("conditional_plan_archive_hash_failed")
        plan_hash = ""
    trade_date = _iso_day(plan_payload.get("trade_date"))
    archive_path = (
        root / "results" / "conditional_trade_plans_daily" / f"{trade_date}.json"
    )
    archive_rel = str(archive_path.relative_to(root)).replace("\\", "/")
    if reasons:
        return {
            "status": "blocked",
            "archived": False,
            "reason_codes": list(dict.fromkeys(reasons)),
            "archive_path": archive_rel,
        }

    existing, existing_error = _read_json_strict(archive_path)
    if existing_error not in {"", "missing"}:
        return {
            "status": "blocked",
            "archived": False,
            "reason_codes": ["conditional_plan_archive_unreadable"],
            "existing_error": existing_error,
            "archive_path": archive_rel,
        }
    if existing is not None:
        existing_reasons = _conditional_plan_archive_reasons(existing)
        try:
            existing_hash = _conditional_plan_hash(existing)
        except (TypeError, ValueError):
            existing_hash = ""
            existing_reasons.append("conditional_plan_archive_hash_failed")
        if existing_reasons or existing_hash != plan_hash:
            if not replace_same_date:
                return {
                    "status": "blocked",
                    "archived": False,
                    "reason_codes": ["conditional_plan_archive_conflict"],
                    "archive_path": archive_rel,
                }
            try:
                atomic_write_json(archive_path, plan_payload)
            except (OSError, RuntimeError, ValueError) as exc:
                return {
                    "status": "blocked",
                    "archived": False,
                    "reason_codes": ["conditional_plan_archive_write_failed"],
                    "error": f"{type(exc).__name__}:{exc}"[:1000],
                    "archive_path": archive_rel,
                }
            return {
                "status": "replaced",
                "archived": True,
                "reason_codes": [],
                "archive_path": archive_rel,
                "plan_id": str(plan_payload.get("plan_id") or ""),
                "trade_date": trade_date,
                "plan_hash": plan_hash,
            }
        return {
            "status": "idempotent",
            "archived": True,
            "reason_codes": [],
            "archive_path": archive_rel,
            "plan_id": str(plan_payload.get("plan_id") or ""),
            "trade_date": trade_date,
            "plan_hash": plan_hash,
        }

    try:
        _exclusive_write_json(archive_path, plan_payload)
    except FileExistsError:
        raced, raced_error = _read_json_strict(archive_path)
        try:
            raced_hash = _conditional_plan_hash(_mapping(raced))
        except (TypeError, ValueError):
            raced_hash = ""
        if (
            raced_error
            or raced is None
            or _conditional_plan_archive_reasons(raced)
            or raced_hash != plan_hash
        ):
            return {
                "status": "blocked",
                "archived": False,
                "reason_codes": ["conditional_plan_archive_conflict"],
                "archive_path": archive_rel,
            }
        return {
            "status": "idempotent",
            "archived": True,
            "reason_codes": [],
            "archive_path": archive_rel,
            "plan_id": str(plan_payload.get("plan_id") or ""),
            "trade_date": trade_date,
            "plan_hash": plan_hash,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "blocked",
            "archived": False,
            "reason_codes": ["conditional_plan_archive_write_failed"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
            "archive_path": archive_rel,
        }
    return {
        "status": "completed",
        "archived": True,
        "reason_codes": [],
        "archive_path": archive_rel,
        "plan_id": str(plan_payload.get("plan_id") or ""),
        "trade_date": trade_date,
        "plan_hash": plan_hash,
    }


def archive_conditional_plan(
    project_root: Path, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Append one formal plan under its trade date using exclusive create."""

    root = Path(project_root).resolve()
    lock_path = root / "results" / "conditional_trade_plans_daily" / ".archive.lock"
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=0):
            return _archive_conditional_plan_locked(root, plan)
    except TimeoutError:
        return {
            "status": "blocked",
            "archived": False,
            "reason_codes": ["conditional_plan_archive_lock_busy"],
        }


def load_archived_conditional_plan(
    project_root: Path, *, trade_date: str
) -> dict[str, Any]:
    """Load one due plan from the append-only archive without mutation."""

    root = Path(project_root).resolve()
    normalized_trade_date = _iso_day(trade_date)
    if not normalized_trade_date or normalized_trade_date != str(trade_date):
        return {
            "status": "blocked",
            "exists": False,
            "reason_codes": ["conditional_plan_archive_trade_date_invalid"],
        }
    archive_path = (
        root
        / "results"
        / "conditional_trade_plans_daily"
        / f"{normalized_trade_date}.json"
    )
    archive_rel = str(archive_path.relative_to(root)).replace("\\", "/")
    plan, error = _read_json_strict(archive_path)
    if error == "missing":
        return {
            "status": "missing",
            "exists": False,
            "reason_codes": ["conditional_plan_archive_missing"],
            "archive_path": archive_rel,
        }
    if error or plan is None:
        return {
            "status": "blocked",
            "exists": True,
            "reason_codes": ["conditional_plan_archive_unreadable"],
            "existing_error": error,
            "archive_path": archive_rel,
        }
    reasons = _conditional_plan_archive_reasons(plan)
    if _iso_day(plan.get("trade_date")) != normalized_trade_date:
        reasons.append("conditional_plan_archive_filename_date_mismatch")
    plan_hash = _try_stable_hash(plan)
    if not plan_hash:
        reasons.append("conditional_plan_archive_hash_failed")
    if reasons:
        return {
            "status": "blocked",
            "exists": True,
            "reason_codes": list(dict.fromkeys(reasons)),
            "archive_path": archive_rel,
        }
    return {
        "status": "passed",
        "exists": True,
        "reason_codes": [],
        "archive_path": archive_rel,
        "plan_id": str(plan.get("plan_id") or ""),
        "trade_date": normalized_trade_date,
        "plan_hash": plan_hash,
        "plan": plan,
    }


def publish_archived_conditional_plan(
    project_root: Path,
    plan: Mapping[str, Any],
    *,
    replace_same_date: bool = False,
) -> dict[str, Any]:
    """Archive the previous latest and new plan before replacing latest."""

    root = Path(project_root).resolve()
    archive_root = root / "results" / "conditional_trade_plans_daily"
    lock_path = archive_root / ".archive.lock"
    latest_path = root / "results" / "conditional_trade_plans_latest.json"
    new_plan = copy.deepcopy(dict(plan))
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=0):
            latest, latest_error = _read_json_strict(latest_path)
            if latest_error not in {"", "missing"}:
                return {
                    "status": "blocked",
                    "published_latest": False,
                    "reason_codes": ["conditional_plan_latest_unreadable"],
                    "existing_error": latest_error,
                }

            previous_archived = False
            if latest is not None:
                previous = _archive_conditional_plan_locked(
                    root, latest, replace_same_date=replace_same_date
                )
                if previous.get("archived") is not True:
                    return {
                        "status": "blocked",
                        "published_latest": False,
                        "previous_latest_archived": False,
                        "reason_codes": list(previous.get("reason_codes") or []),
                    }
                previous_archived = True
                latest_trade_date = _iso_day(latest.get("trade_date"))
                new_trade_date = _iso_day(new_plan.get("trade_date"))
                if latest_trade_date > new_trade_date:
                    return {
                        "status": "blocked",
                        "published_latest": False,
                        "previous_latest_archived": True,
                        "reason_codes": ["conditional_plan_latest_regression"],
                    }
                if latest_trade_date == new_trade_date:
                    try:
                        same_latest = _conditional_plan_hash(
                            latest
                        ) == _conditional_plan_hash(new_plan)
                    except (TypeError, ValueError):
                        same_latest = False
                    if not same_latest and not replace_same_date:
                        return {
                            "status": "blocked",
                            "published_latest": False,
                            "previous_latest_archived": True,
                            "reason_codes": ["conditional_plan_latest_same_date_conflict"],
                        }

            archived = _archive_conditional_plan_locked(
                root, new_plan, replace_same_date=replace_same_date
            )
            if archived.get("archived") is not True:
                return {
                    "status": "blocked",
                    "published_latest": False,
                    "previous_latest_archived": previous_archived,
                    "reason_codes": list(archived.get("reason_codes") or []),
                }

            latest_same = False
            if latest is not None:
                try:
                    latest_same = _conditional_plan_hash(
                        latest
                    ) == _conditional_plan_hash(new_plan)
                except (TypeError, ValueError):
                    latest_same = False
            if not latest_same:
                try:
                    atomic_write_json(latest_path, new_plan)
                except RuntimeError as exc:
                    return {
                        "status": "blocked",
                        "published_latest": False,
                        "previous_latest_archived": previous_archived,
                        "reason_codes": ["conditional_plan_latest_publish_failed"],
                        "error": f"{type(exc).__name__}:{exc}"[:1000],
                    }
            return {
                "status": "idempotent" if latest_same else "completed",
                "published_latest": True,
                "previous_latest_archived": previous_archived,
                "reason_codes": [],
                "archive_path": archived.get("archive_path"),
                "latest_path": str(latest_path.relative_to(root)).replace("\\", "/"),
                "plan_id": str(new_plan.get("plan_id") or ""),
                "trade_date": _iso_day(new_plan.get("trade_date")),
                "plan_hash": archived.get("plan_hash"),
            }
    except TimeoutError:
        return {
            "status": "blocked",
            "published_latest": False,
            "reason_codes": ["conditional_plan_archive_lock_busy"],
        }


def _check_minute_evidence(
    evidence: Mapping[str, Any],
    *,
    expected_plan_id: str,
    expected_trade_date: str,
    expected_symbols: set[str],
    prefix: str = "due_minute",
) -> tuple[list[str], dict[str, Any]]:
    """Judge whether minute evidence is *trustworthy*, not whether it is complete.

    A missing minute file is a normal market-data outcome: the honest response is
    to replay the plan and record that symbol as an explicit ``no_fill``, not to
    abandon the due plan. Blocking on incompleteness meant one absent symbol
    stalled the whole rollover, so the plan was never replayed and the paper
    account never accumulated a single evaluation.

    What must still block is evidence that cannot be believed at all: counts that
    disagree with the symbol lists or with the item rows, symbols that are not the
    plan's, or a status outside the producer's closed vocabulary. In those cases
    we cannot tell which symbols really had minute data, so no honest replay is
    possible.

    The expectations come from the caller: the rollover path judges the evidence
    against the due plan (``prefix="due_minute"``), and the replay-receipt path
    against the executed plan (``prefix="replay_minute"``). Keeping one core
    means the two paths can never disagree about what counts as trustworthy.
    """

    reasons: list[str] = []
    summary: dict[str, Any] = {
        "status": "untrustworthy",
        "total_symbols": 0,
        "available_count": 0,
        "non_available_count": 0,
        "non_available_items": [],
    }
    if evidence.get("ok") is not True:
        reasons.append(f"{prefix}_evidence_not_ok")
    if str(evidence.get("plan_id") or "") != expected_plan_id:
        reasons.append(f"{prefix}_plan_id_mismatch")
    if _iso_day(evidence.get("target_trade_date")) != expected_trade_date:
        reasons.append(f"{prefix}_trade_date_mismatch")
    try:
        counts = {
            bucket: int(evidence.get(f"{bucket}_count"))
            for bucket in _MINUTE_BUCKETS
        }
        total = int(evidence.get("total_symbols"))
    except (TypeError, ValueError):
        reasons.append(f"{prefix}_counts_invalid")
        return reasons, summary

    rows = evidence.get("items")
    if not isinstance(rows, list) or any(
        not isinstance(row, Mapping) for row in rows or []
    ):
        reasons.append(f"{prefix}_evidence_untrustworthy")
        return reasons, summary
    items = [row for row in rows if isinstance(row, Mapping)]

    untrustworthy = False
    if total != len(expected_symbols) or total != len(items):
        untrustworthy = True
    if sum(counts.values()) != total:
        untrustworthy = True
    item_symbols = [normalize_symbol(row.get("symbol")) for row in items]
    if set(item_symbols) != expected_symbols or len(set(item_symbols)) != len(items):
        untrustworthy = True

    # Each declared count must agree with its own symbol list *and* with the
    # per-item statuses. Checking only one of the three lets a forged summary
    # disagree with the rows it claims to summarise.
    observed: dict[str, list[str]] = {bucket: [] for bucket in _MINUTE_BUCKETS}
    for row, symbol in zip(items, item_symbols):
        status = str(row.get("status") or "")
        if status == MINUTE_STATUS_AVAILABLE:
            if row.get("ok") is not True:
                untrustworthy = True
            observed["available"].append(symbol)
            continue
        bucket = MINUTE_STATUS_BUCKETS.get(status)
        if bucket is None:
            untrustworthy = True
            continue
        if row.get("ok") is not False:
            untrustworthy = True
        observed[bucket].append(symbol)
    for bucket in _MINUTE_BUCKETS:
        declared_list = evidence.get(f"{bucket}_symbols")
        if not isinstance(declared_list, list):
            untrustworthy = True
            continue
        declared = [normalize_symbol(value) for value in declared_list]
        if len(declared) != counts[bucket]:
            untrustworthy = True
        if sorted(declared) != sorted(observed[bucket]):
            untrustworthy = True

    if untrustworthy:
        reasons.append(f"{prefix}_evidence_untrustworthy")
        return reasons, summary

    non_available = [
        {
            "symbol": symbol,
            "status": str(row.get("status") or ""),
            "warnings": list(row.get("warnings") or []),
            "errors": list(row.get("errors") or []),
        }
        for row, symbol in zip(items, item_symbols)
        if str(row.get("status") or "") != MINUTE_STATUS_AVAILABLE
    ]
    summary = {
        "status": "complete" if not non_available else "partial",
        "total_symbols": total,
        "available_count": counts["available"],
        "non_available_count": len(non_available),
        "non_available_items": non_available,
    }
    return reasons, summary


def _minute_evidence_reasons(
    minute_evidence: Mapping[str, Any],
    *,
    due_plan: Mapping[str, Any],
    as_of: str,
) -> tuple[list[str], dict[str, Any]]:
    """Rollover-scoped wrapper: evidence must belong to the due plan for ``as_of``."""

    return _check_minute_evidence(
        minute_evidence,
        expected_plan_id=str(due_plan.get("plan_id") or ""),
        expected_trade_date=as_of,
        expected_symbols=set(_plan_symbols(due_plan)),
    )


def _try_stable_hash(value: Any) -> str:
    try:
        return _stable_hash(value)
    except (TypeError, ValueError):
        return ""


def _replay_receipt_hash_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _mapping(payload.get("provenance"))
    return {
        "schema_version": payload.get("schema_version"),
        "status": payload.get("status"),
        "formal_evidence_eligible": payload.get("formal_evidence_eligible"),
        "plan_id": payload.get("plan_id"),
        "trade_date": payload.get("trade_date"),
        "execution_id": payload.get("execution_id"),
        "execution_counts": payload.get("execution_counts"),
        "execution_payload_hash": payload.get("execution_payload_hash"),
        "apply_state_hash": payload.get("apply_state_hash"),
        "persisted_state_hash": payload.get("persisted_state_hash"),
        "application_action_count": payload.get("application_action_count"),
        "already_updated_today": payload.get("already_updated_today"),
        "state_evidence": payload.get("state_evidence"),
        "reason_codes": payload.get("reason_codes"),
        "minute_evidence_summary": payload.get("minute_evidence_summary"),
        "minute_reconciliation": payload.get("minute_reconciliation"),
        "source": provenance.get("source"),
        "captured_at": provenance.get("captured_at"),
        "boundaries": payload.get("boundaries"),
        "disclaimer": payload.get("disclaimer"),
    }


def build_conditional_replay_receipt(
    *,
    execution_payload: Mapping[str, Any],
    apply_result: Mapping[str, Any],
    persisted_state: Mapping[str, Any] | None,
    minute_evidence: Mapping[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    """Bind simulation, application, re-read persisted state, and minute evidence."""

    execution = _mapping(execution_payload)
    application = _mapping(apply_result)
    applied_state = _mapping(application.get("state"))
    persisted = _mapping(persisted_state)
    plan_id = str(execution.get("plan_id") or "").strip()
    trade_date = _iso_day(execution.get("trade_date"))
    execution_id = f"{plan_id}:{trade_date}" if plan_id and trade_date else ""
    events = execution.get("events")
    no_fill = execution.get("no_fill")
    blocked = execution.get("blocked")
    reasons: list[str] = []
    if execution.get("ok") is not True:
        reasons.append("replay_execution_payload_not_ok")
    if not plan_id:
        reasons.append("replay_plan_id_missing")
    if not trade_date:
        reasons.append("replay_trade_date_invalid")
    for name, rows in (
        ("events", events),
        ("no_fill", no_fill),
        ("blocked", blocked),
    ):
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) for row in (rows or [])
        ):
            reasons.append(f"replay_execution_{name}_invalid")
    events = events if isinstance(events, list) else []
    no_fill = no_fill if isinstance(no_fill, list) else []
    blocked = blocked if isinstance(blocked, list) else []
    counts = {
        "event_count": len(events),
        "no_fill_count": len(no_fill),
        "blocked_count": len(blocked),
    }

    # Minute evidence is bound into the receipt the same way the rollover binds
    # it: counts, symbol lists, and per-item statuses must agree, and the
    # evidence must name the executed plan. A non-available symbol the simulation
    # did not resolve (no ``no_fill``/``blocked`` row) makes the receipt unusable
    # as proof the due plan was honestly handled, so it blocks the receipt.
    evidence = _mapping(minute_evidence)
    evidence_rows = evidence.get("items")
    if not isinstance(evidence_rows, list):
        evidence_rows = []
    evidence_rows = [row for row in evidence_rows if isinstance(row, Mapping)]
    evidence_symbols = [normalize_symbol(row.get("symbol")) for row in evidence_rows]
    execution_symbols: list[str] = []
    resolution: dict[str, tuple[str, str]] = {}
    for bucket_name, bucket_rows in (
        ("event", events),
        ("no_fill", no_fill),
        ("blocked", blocked),
    ):
        for row in bucket_rows:
            symbol = normalize_symbol(row.get("symbol"))
            if not symbol:
                continue
            execution_symbols.append(symbol)
            resolution.setdefault(symbol, (bucket_name, str(row.get("reason") or "")))
    trust_reasons, minute_summary = _check_minute_evidence(
        evidence,
        expected_plan_id=plan_id,
        expected_trade_date=trade_date,
        expected_symbols=set(execution_symbols),
        prefix="replay_minute",
    )
    reasons.extend(trust_reasons)
    if set(execution_symbols) != set(evidence_symbols):
        reasons.append("replay_execution_symbol_set_mismatch")
    if any(
        str(row.get("status") or "") != MINUTE_STATUS_AVAILABLE
        and symbol not in resolution
        for row, symbol in zip(evidence_rows, evidence_symbols)
    ):
        reasons.append("replay_non_available_symbol_not_resolved")
    minute_reconciliation = [
        {
            "symbol": symbol,
            "minute_status": str(row.get("status") or ""),
            "execution_bucket": (
                resolution[symbol][0] if symbol in resolution else "unresolved"
            ),
            "reason": resolution[symbol][1] if symbol in resolution else "",
        }
        for row, symbol in zip(evidence_rows, evidence_symbols)
    ]

    summary = _mapping(execution.get("summary"))
    for name, expected in counts.items():
        if name in summary and summary.get(name) != expected:
            reasons.append(f"replay_execution_{name}_mismatch")
    if "plan_count" in summary and summary.get("plan_count") != sum(counts.values()):
        reasons.append("replay_execution_plan_count_mismatch")
    if application.get("ok") is not True:
        reasons.append("replay_apply_result_not_ok")
    if application.get("is_paper_simulation") is not True:
        reasons.append("replay_apply_not_paper_simulation")
    if not isinstance(application.get("actions"), list):
        reasons.append("replay_apply_actions_invalid")
    if application.get("no_broker") is not True:
        reasons.append("replay_apply_boundary_invalid")
    if execution.get("allow_daily_approx") is True:
        reasons.append("replay_daily_approx_not_allowed")
    if execution.get("is_real_order") is True:
        reasons.append("replay_execution_boundary_invalid")
    if not applied_state:
        reasons.append("replay_apply_state_missing")
    if not persisted:
        reasons.append("replay_persisted_state_missing")

    applied_ids_raw = applied_state.get("applied_execution_ids")
    persisted_ids_raw = persisted.get("applied_execution_ids")
    if not isinstance(applied_ids_raw, list):
        reasons.append("replay_apply_execution_ids_invalid")
    if not isinstance(persisted_ids_raw, list):
        reasons.append("replay_persisted_execution_ids_invalid")
    applied_ids = list(applied_ids_raw) if isinstance(applied_ids_raw, list) else []
    persisted_ids = list(persisted_ids_raw) if isinstance(persisted_ids_raw, list) else []
    if execution_id and execution_id not in applied_ids:
        reasons.append("replay_execution_id_not_in_apply_state")
    if execution_id and execution_id not in persisted_ids:
        reasons.append("replay_execution_id_not_persisted")
    for prefix, state in (("apply", applied_state), ("persisted", persisted)):
        if str(state.get("last_conditional_plan_id") or "") != plan_id:
            reasons.append(f"replay_{prefix}_last_plan_id_mismatch")
        if _iso_day(state.get("last_trade_date")) != trade_date:
            reasons.append(f"replay_{prefix}_last_trade_date_mismatch")

    execution_hash = _try_stable_hash(dict(execution))
    apply_state_hash = _try_stable_hash(dict(applied_state))
    persisted_state_hash = _try_stable_hash(dict(persisted))
    if not execution_hash:
        reasons.append("replay_execution_hash_failed")
    if not apply_state_hash:
        reasons.append("replay_apply_state_hash_failed")
    if not persisted_state_hash:
        reasons.append("replay_persisted_state_hash_failed")
    if apply_state_hash and persisted_state_hash and apply_state_hash != persisted_state_hash:
        reasons.append("replay_persisted_state_content_mismatch")
    normalized_captured_at = _iso_datetime(captured_at)
    if not normalized_captured_at:
        reasons.append("replay_receipt_captured_at_invalid")
    elif trade_date and _iso_day(normalized_captured_at) < trade_date:
        reasons.append("replay_receipt_captured_before_trade_date")

    formal = not reasons
    receipt: dict[str, Any] = {
        "task_type": "conditional_plan_replay_receipt",
        "schema_version": REPLAY_RECEIPT_SCHEMA_VERSION,
        "status": "passed" if formal else "blocked",
        "formal_evidence_eligible": formal,
        "plan_id": plan_id,
        "trade_date": trade_date,
        "execution_id": execution_id,
        "execution_counts": counts,
        "execution_payload_hash": execution_hash,
        "apply_state_hash": apply_state_hash,
        "persisted_state_hash": persisted_state_hash,
        "application_action_count": len(application.get("actions") or []),
        "already_updated_today": application.get("already_updated_today") is True,
        "state_evidence": {
            "execution_id_in_apply_state": execution_id in applied_ids,
            "execution_id_persisted": execution_id in persisted_ids,
            "last_conditional_plan_id": str(
                persisted.get("last_conditional_plan_id") or ""
            ),
            "last_trade_date": _iso_day(persisted.get("last_trade_date")),
        },
        "reason_codes": list(dict.fromkeys(reasons)),
        "minute_evidence_summary": minute_summary,
        "minute_reconciliation": minute_reconciliation,
        "provenance": {
            "source": "simulate_conditional_plan_file+apply_conditional_execution_to_state+persisted_state_reread",
            "captured_at": normalized_captured_at,
            "hash": "",
        },
        "risk_gate_enforced": False,
        "enforcement_applied": False,
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
        },
        "disclaimer": DISCLAIMER,
    }
    receipt["provenance"]["hash"] = _stable_hash(
        _replay_receipt_hash_projection(receipt)
    )
    return receipt


def verify_conditional_replay_receipt(payload: Mapping[str, Any]) -> list[str]:
    receipt = _mapping(payload)
    provenance = _mapping(receipt.get("provenance"))
    state_evidence = _mapping(receipt.get("state_evidence"))
    reasons: list[str] = []
    if receipt.get("task_type") != "conditional_plan_replay_receipt":
        reasons.append("replay_receipt_task_type_invalid")
    if receipt.get("schema_version") != REPLAY_RECEIPT_SCHEMA_VERSION:
        reasons.append("replay_receipt_schema_version_invalid")
    plan_id = str(receipt.get("plan_id") or "")
    trade_date = _iso_day(receipt.get("trade_date"))
    execution_id = f"{plan_id}:{trade_date}" if plan_id and trade_date else ""
    if not plan_id:
        reasons.append("replay_receipt_plan_id_missing")
    if not trade_date:
        reasons.append("replay_receipt_trade_date_invalid")
    if receipt.get("execution_id") != execution_id:
        reasons.append("replay_receipt_execution_id_mismatch")
    counts = _mapping(receipt.get("execution_counts"))
    for name in ("event_count", "no_fill_count", "blocked_count"):
        value = counts.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reasons.append("replay_receipt_counts_invalid")
            break
    action_count = receipt.get("application_action_count")
    if isinstance(action_count, bool) or not isinstance(action_count, int) or action_count < 0:
        reasons.append("replay_receipt_action_count_invalid")
    if receipt.get("already_updated_today") is not True and receipt.get(
        "already_updated_today"
    ) is not False:
        reasons.append("replay_receipt_already_updated_invalid")
    for name in (
        "execution_payload_hash",
        "apply_state_hash",
        "persisted_state_hash",
    ):
        if not _is_sha256(receipt.get(name)):
            reasons.append(f"replay_receipt_{name}_invalid")
    if receipt.get("apply_state_hash") != receipt.get("persisted_state_hash"):
        reasons.append("replay_receipt_state_hash_mismatch")
    if state_evidence.get("execution_id_in_apply_state") is not True:
        reasons.append("replay_receipt_apply_state_not_proven")
    if state_evidence.get("execution_id_persisted") is not True:
        reasons.append("replay_receipt_persisted_state_not_proven")
    if state_evidence.get("last_conditional_plan_id") != plan_id:
        reasons.append("replay_receipt_last_plan_id_mismatch")
    if _iso_day(state_evidence.get("last_trade_date")) != trade_date:
        reasons.append("replay_receipt_last_trade_date_mismatch")
    if receipt.get("status") != "passed":
        reasons.append("replay_receipt_status_invalid")
    if receipt.get("formal_evidence_eligible") is not True:
        reasons.append("replay_receipt_not_formal_evidence")
    if list(receipt.get("reason_codes") or []):
        reasons.append("replay_receipt_has_rejection_reasons")
    if provenance.get("source") != (
        "simulate_conditional_plan_file+apply_conditional_execution_to_state+persisted_state_reread"
    ):
        reasons.append("replay_receipt_source_invalid")
    captured_at = _iso_datetime(provenance.get("captured_at"))
    if not captured_at:
        reasons.append("replay_receipt_captured_at_invalid")
    elif trade_date and _iso_day(captured_at) < trade_date:
        reasons.append("replay_receipt_captured_before_trade_date")
    declared_hash = str(provenance.get("hash") or "")
    if not _is_sha256(declared_hash):
        reasons.append("replay_receipt_hash_invalid")
    elif declared_hash != _stable_hash(_replay_receipt_hash_projection(receipt)):
        reasons.append("replay_receipt_hash_mismatch")
    if receipt.get("risk_gate_enforced") is not False or receipt.get(
        "enforcement_applied"
    ) is not False:
        reasons.append("replay_receipt_risk_boundary_invalid")
    boundaries = _mapping(receipt.get("boundaries"))
    if (
        boundaries.get("research_only") is not True
        or boundaries.get("paper_account_only") is not True
        or boundaries.get("broker_route") is not False
        or boundaries.get("live_execution") is not False
    ):
        reasons.append("replay_receipt_boundary_invalid")
    reconciliation = receipt.get("minute_reconciliation")
    if not isinstance(reconciliation, list) or any(
        not isinstance(row, Mapping) for row in reconciliation or []
    ):
        reasons.append("replay_receipt_minute_reconciliation_invalid")
    else:
        for row in reconciliation or []:
            if str(row.get("execution_bucket") or "") not in {
                "event",
                "no_fill",
                "blocked",
                "unresolved",
            }:
                reasons.append("replay_receipt_minute_reconciliation_invalid")
                break
        if any(
            str(row.get("execution_bucket") or "") == "unresolved"
            for row in reconciliation or []
        ):
            reasons.append("replay_receipt_minute_evidence_unresolved")
    minute_summary = _mapping(receipt.get("minute_evidence_summary"))
    if str(minute_summary.get("status") or "") not in {"complete", "partial"}:
        reasons.append("replay_receipt_minute_evidence_invalid")
    return list(dict.fromkeys(reasons))


def coordinate_plan_rollover(
    *,
    as_of: str,
    due_plan: Mapping[str, Any] | None,
    next_plan: Mapping[str, Any] | None,
    minute_evidence: Mapping[str, Any] | None,
    replay_receipt: Mapping[str, Any] | None = None,
    due_plan_required: bool = True,
) -> dict[str, Any]:
    """Decide whether to replay a due plan or publish the next plan.

    Call once without ``replay_receipt``. A ``replay_due_first`` result permits
    only the due replay. Call again with a receipt built from simulation,
    application, and re-read persisted state evidence; only then can the next
    plan be published.
    """

    normalized_as_of = _iso_day(as_of)
    due = _mapping(due_plan)
    nxt = _mapping(next_plan)
    due_reasons: list[str] = []
    next_reasons: list[str] = []
    replay_reasons: list[str] = []
    bootstrap = False
    minute_summary: dict[str, Any] = {
        "status": "missing",
        "total_symbols": 0,
        "available_count": 0,
        "non_available_count": 0,
        "non_available_items": [],
    }

    if not normalized_as_of:
        due_reasons.append("rollover_as_of_invalid")
    if not nxt:
        next_reasons.append("next_plan_missing")
    elif normalized_as_of:
        next_reasons.extend(
            _formal_plan_reasons(nxt, as_of=normalized_as_of, role="next")
        )

    if not due:
        if due_plan_required:
            due_reasons.append("due_plan_missing")
        else:
            bootstrap = True
    elif normalized_as_of:
        due_reasons.extend(
            _formal_plan_reasons(due, as_of=normalized_as_of, role="due")
        )
        if minute_evidence is None:
            due_reasons.append("due_minute_evidence_missing")
        else:
            evidence_reasons, minute_summary = _minute_evidence_reasons(
                minute_evidence,
                due_plan=due,
                as_of=normalized_as_of,
            )
            due_reasons.extend(evidence_reasons)

    replay_due = False
    publish_next = False
    if due_reasons:
        status = "blocked"
    elif bootstrap:
        if next_reasons:
            status = "blocked"
        else:
            status = "ready_to_publish_next"
            publish_next = True
    elif replay_receipt is None:
        status = "replay_due_first"
        replay_due = True
    else:
        replay_reasons.extend(verify_conditional_replay_receipt(replay_receipt))
        receipt = _mapping(replay_receipt)
        if str(receipt.get("plan_id") or "") != str(due.get("plan_id") or ""):
            replay_reasons.append("due_replay_receipt_plan_id_mismatch")
        if _iso_day(receipt.get("trade_date")) != normalized_as_of:
            replay_reasons.append("due_replay_receipt_trade_date_mismatch")
        if replay_reasons or next_reasons:
            status = "blocked"
        else:
            status = "ready_to_publish_next"
            publish_next = True

    reasons = list(dict.fromkeys(due_reasons + replay_reasons + next_reasons))
    return {
        "task_type": "daily_plan_rollover_decision",
        "schema_version": PLAN_ROLLOVER_SCHEMA_VERSION,
        "status": status,
        "as_of": normalized_as_of,
        "due_plan_id": str(due.get("plan_id") or ""),
        "next_plan_id": str(nxt.get("plan_id") or ""),
        "replay_due_plan": replay_due,
        "publish_next_plan": publish_next,
        "preserve_existing_plan": not publish_next,
        "bootstrap_without_due_plan": bootstrap,
        "action_order": (
            ["replay_due_plan", "publish_next_plan_after_replay_success"]
            if replay_due
            else (["publish_next_plan"] if publish_next else [])
        ),
        "reason_codes": reasons,
        "minute_evidence_summary": minute_summary,
        "risk_gate_enforced": False,
        "enforcement_applied": False,
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
        },
        "disclaimer": DISCLAIMER,
    }


def _receipt_key(row: Any) -> tuple[str, str]:
    if not isinstance(row, Mapping):
        return ("", "")
    return (str(row.get("plan_id") or ""), _iso_day(row.get("trade_date")))


def scan_unreplayed_archived_plans(
    project_root: Path,
    *,
    as_of: str,
    applied_execution_ids: Sequence[str] = (),
    review_receipts: Sequence[Mapping[str, Any]] | None = None,
    replay_receipts: Sequence[Mapping[str, Any]] | None = None,
    include_as_of: bool = False,
) -> dict[str, Any]:
    """Report archived plans whose trade date passed without any recorded evidence.

    Why this exists: exit rules are only evaluated on days the evening routine
    actually runs, and the stop-loss band is recomputed from a *rolling* support
    each run. Between 2026-05-14 and 2026-08-05 the routine ran on 17 of 59
    trading days; every exit signal that fired on a skipped day was silently
    discarded, because by the next run the band had followed price downward and
    no longer matched. The result was 26 applied executions but a single closed
    trade.

    Three different facts are kept apart (B1):

    * ``applied_execution_ids`` - a fill/simulation was applied to the ledger;
    * ``replay_receipts`` - an intraday simulation pass ran for the plan;
    * ``review_receipts`` - a read-only expiry review was performed even when
      no simulation ran.

    Status text is not a receipt. Each v2 record is checked against the actual
    plan and input files; an old unbound v1 receipt remains diagnostic only.
    Applied IDs retain their existing ledger semantics, never a review claim.

    Read-only by construction: it opens no lock, writes nothing, and fabricates
    no replay. Recovering a missed day stays a separate, explicitly authorized
    action -- a scan that silently replayed history would be exactly the kind of
    invented evidence this project forbids.
    """

    root = Path(project_root).resolve()
    normalized_as_of = _iso_day(as_of)
    if not normalized_as_of:
        return {
            "task_type": "unreplayed_archived_plan_scan",
            "schema_version": PLAN_ROLLOVER_SCHEMA_VERSION,
            "status": "blocked",
            "reason_codes": ["scan_as_of_invalid"],
            "unreplayed": [],
            "unreplayed_count": 0,
        }
    from quantdesk.plan_review_receipts import load_plan_review_receipts, verify_plan_review_receipt

    stored = load_plan_review_receipts(root) if review_receipts is None or replay_receipts is None else {}
    review_rows = list(stored.get("review_receipts") or []) if review_receipts is None else list(review_receipts)
    replay_rows = list(stored.get("replay_receipts") or []) if replay_receipts is None else list(replay_receipts)
    store_reasons = list(stored.get("reason_codes") or [])
    by_plan: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for receipt in [*review_rows, *replay_rows]:
        key = _receipt_key(receipt)
        if not all(key):
            store_reasons.append("receipt_identity_missing")
            continue
        by_plan.setdefault(key, []).append(receipt)
    applied = {str(item) for item in applied_execution_ids if str(item)}
    archive_root = root / "results" / "conditional_trade_plans_daily"
    unreplayed: list[dict[str, Any]] = []
    unreadable: list[str] = []
    scanned = 0
    applied_count = 0
    simulated_count = 0
    reviewed_count = 0
    review_passed_count = 0
    all_items: list[dict[str, Any]] = []
    if archive_root.is_dir():
        for path in sorted(archive_root.glob("*.json")):
            plan, error = _read_json_strict(path)
            if error or plan is None:
                unreadable.append(path.name)
                continue
            scanned += 1
            trade_date = _iso_day(plan.get("trade_date"))
            plan_id = str(plan.get("plan_id") or "")
            # Only a *passed* trade date can be missed; today's and future plans
            # are simply not due yet.
            if not trade_date:
                unreadable.append(path.name)
                continue
            if trade_date > normalized_as_of or (trade_date == normalized_as_of and not include_as_of):
                all_items.append({"archive_file": path.name, "plan_id": plan_id, "trade_date": trade_date,
                                  "status": "not_due", "reason_codes": ["plan_not_due"],
                                  "evidence": {"applied_to_ledger": False, "intraday_simulation_executed": False, "expiry_reviewed": False}})
                continue
            execution_id = f"{plan_id}:{trade_date}" if plan_id and trade_date else ""
            key = (plan_id, trade_date)
            receipts = by_plan.get(key, [])
            valid: list[Mapping[str, Any]] = []
            rejected: list[str] = []
            for receipt in receipts:
                reasons = verify_plan_review_receipt(receipt, plan=plan, project_root=root, as_of=normalized_as_of)
                if reasons:
                    rejected.extend(reasons)
                else:
                    valid.append(receipt)
            evidence = {
                "applied_to_ledger": bool(execution_id and execution_id in applied),
                "intraday_simulation_executed": any(row.get("kind") == "intraday_replay" and row.get("simulation_executed") is True for row in valid),
                "expiry_reviewed": any(row.get("kind") == "expiry_review" and row.get("review_completed") is True for row in valid),
            }
            review_passed = any(row.get("kind") == "expiry_review" and row.get("review_passed") is True for row in valid)
            replay_passed = any(row.get("kind") == "intraday_replay" and row.get("simulation_executed") is True and row.get("review_passed") is True for row in valid)
            passed = review_passed or replay_passed
            applied_count += int(evidence["applied_to_ledger"])
            simulated_count += int(evidence["intraday_simulation_executed"])
            reviewed_count += int(evidence["expiry_reviewed"])
            review_passed_count += int(review_passed)
            if passed:
                status = "replayed" if evidence["intraday_simulation_executed"] else (
                    "simulation_disabled" if all(row.get("simulation_enabled") is False for row in valid) else "reviewed"
                )
            elif valid and evidence["expiry_reviewed"]:
                status = "evidence_insufficient"
            elif rejected:
                status = "legacy_unbound" if "legacy_unbound" in rejected else "receipt_invalid"
            elif evidence["applied_to_ledger"]:
                status = "applied_record_only"
            else:
                status = "unreviewed"
            resolved = passed or (evidence["applied_to_ledger"] and not receipts)
            missing: list[str] = []
            if not execution_id:
                missing.append("plan_id_or_trade_date_missing")
            else:
                if not evidence["applied_to_ledger"]:
                    missing.append("no_applied_execution")
                if not evidence["intraday_simulation_executed"]:
                    missing.append("no_intraday_simulation")
                if not evidence["expiry_reviewed"]:
                    missing.append("no_expiry_review")
            missing.extend(rejected)
            if not passed:
                missing.extend(str(reason) for row in valid for reason in (row.get("reason_codes") or []))
            item = {
                    "archive_file": path.name,
                    "plan_id": plan_id,
                    "trade_date": trade_date,
                    "execution_id": execution_id,
                    "symbol_count": len(_plan_symbols(plan)),
                    "status": status,
                    "evidence": evidence,
                    "review_passed": review_passed,
                    "replay_validation_passed": replay_passed,
                    "missing_evidence_reasons": list(dict.fromkeys(missing)),
                    "reason_codes": list(dict.fromkeys(rejected)),
                    "receipts": [{"kind": row.get("kind"), "checked_at": row.get("checked_at"),
                                  "status": row.get("status"), "receipt_hash": row.get("receipt_hash"),
                                  "plan_hash": row.get("plan_hash"), "reconstruction": row.get("reconstruction")}
                                 for row in valid],
                    "reason_code": (
                        "plan_id_or_trade_date_missing"
                        if not execution_id
                        else "trade_date_passed_without_recorded_replay"
                    ),
                }
            all_items.append(item)
            if not resolved:
                unreplayed.append(item)
    summary = {
        "status": "attention" if unreplayed or unreadable or store_reasons else "passed",
        "as_of": normalized_as_of, "book_id": "strategy_paper",
        "plan_count": len(all_items), "due_count": sum(row["status"] != "not_due" for row in all_items),
        "not_due_count": sum(row["status"] == "not_due" for row in all_items),
        "reviewed_count": reviewed_count, "review_passed_count": review_passed_count,
        "replayed_count": simulated_count, "applied_count": applied_count,
        "unresolved_count": len(unreplayed),
        "evidence_insufficient_count": sum(row["status"] == "evidence_insufficient" for row in all_items),
        "legacy_unbound_count": sum(row["status"] == "legacy_unbound" for row in all_items),
        "reason_codes": list(dict.fromkeys([*store_reasons, *(reason for row in all_items for reason in row.get("reason_codes", []))])),
    }
    return {
        "task_type": "unreplayed_archived_plan_scan",
        "schema_version": PLAN_ROLLOVER_SCHEMA_VERSION,
        "status": summary["status"],
        "as_of": normalized_as_of,
        "archive_scanned": scanned,
        "archive_unreadable": unreadable,
        "applied_execution_id_count": len(applied),
        "evidence_counts": {
            "applied_to_ledger": applied_count,
            "intraday_simulation_executed": simulated_count,
            "expiry_reviewed": reviewed_count,
        },
        "unreplayed": unreplayed,
        "unreplayed_count": len(unreplayed),
        "plan_review_summary": summary,
        "plan_review_items": all_items,
        "recovery_authorized": False,
        "reason_codes": (
            ["unreplayed_archived_plans_present"] if unreplayed else []
        )
        + (["archive_entries_unreadable"] if unreadable else []) + store_reasons,
        "note": (
            "只读扫描：不补放、不写盘。到期已复核、盘中已模拟、已应用到账本三者分开，"
            "状态字样或无计划绑定的旧收据不构成核验；已应用ID不冒充到期复核。"
        ),
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
            "state_write": False,
        },
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "CANDIDATE_ENVELOPE_SCHEMA_VERSION",
    "CANDIDATE_REFRESH_SCHEMA_VERSION",
    "PLAN_ROLLOVER_SCHEMA_VERSION",
    "REPLAY_RECEIPT_SCHEMA_VERSION",
    "archive_conditional_plan",
    "build_conditional_replay_receipt",
    "build_formal_candidate_batch",
    "candidate_refresh_gate",
    "coordinate_plan_rollover",
    "enrich_legacy_due_plan_candidate_envelopes",
    "load_formal_candidate_batch",
    "build_candidate_envelope",
    "verify_candidate_envelope",
    "load_archived_conditional_plan",
    "publish_archived_conditional_plan",
    "publish_formal_candidate_batch",
    "resolve_chain_bound_formal_candidate_batch",
    "run_formal_candidate_refresh",
    "scan_unreplayed_archived_plans",
    "verify_conditional_replay_receipt",
    "verify_formal_candidate_batch",
]
