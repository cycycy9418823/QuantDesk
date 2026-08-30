"""Weekly maintenance for canonical minute symbols outside the daily pool.

The runner is offline and no-apply by default. Network-enabled runs stage one
symbol at a time, perform the existing strict quality audit, and may reach the
canonical store only through the Stage 6 append-only importer.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch
from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols
from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch
from quantdesk.shared import now_text
from quantdesk.utils import atomic_write_json


WEEKLY_MINUTE_WIDE_POOL_CONTRACT_VERSION = 1
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "Not Investment Advice / Not Position Advice."
)
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
_SAFE_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_TRUTHY = {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class WeeklyMinutePaths:
    root: Path
    canonical_dir: Path
    formal_pool_path: Path
    results_root: Path
    daily_chain_lock: Path


def _validated_date(value: str) -> str:
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid_target_date:{value}") from exc
    return text


def _descendant(root: Path, path: Path, *, role: str) -> Path:
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path_outside_project_root:{role}:{resolved}") from exc
    return resolved


def _validated_paths(
    project_root: Path,
    *,
    canonical_dir: Path | None = None,
    formal_pool_path: Path | None = None,
    results_root: Path | None = None,
) -> WeeklyMinutePaths:
    root = Path(project_root).resolve(strict=True)
    canonical = _descendant(
        root,
        Path(canonical_dir or (root / "data" / "intraday" / "1m")),
        role="canonical_dir",
    )
    formal = _descendant(
        root,
        Path(
            formal_pool_path
            or (root / "data" / "universe" / "low_price_learning_symbols.csv")
        ),
        role="formal_pool_path",
    )
    output = _descendant(
        root,
        Path(
            results_root
            or (root / "results" / "data_chain" / "weekly_minute_wide_pool")
        ),
        role="results_root",
    )
    lock = _descendant(
        root,
        root / "state" / "daily_data_chain.lock",
        role="daily_chain_lock",
    )
    for forbidden_name in ("data", "state"):
        forbidden = (root / forbidden_name).resolve(strict=False)
        try:
            output.relative_to(forbidden)
        except ValueError:
            continue
        raise ValueError(f"weekly_results_forbidden_path:{output}")
    if not canonical.is_dir():
        raise FileNotFoundError(f"canonical_intraday_dir_missing:{canonical}")
    if not formal.is_file():
        raise FileNotFoundError(f"formal_pool_missing:{formal}")
    return WeeklyMinutePaths(
        root=root,
        canonical_dir=canonical,
        formal_pool_path=formal,
        results_root=output,
        daily_chain_lock=lock,
    )


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdigit() and len(text) <= 6:
        text = text.zfill(6)
    return text if _SYMBOL_PATTERN.fullmatch(text) else ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_symbols(symbols: Sequence[str]) -> str:
    payload = "\n".join(sorted(set(symbols))).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _csv_symbols(path: Path) -> set[str]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if "symbol" not in set(reader.fieldnames or []):
            raise ValueError(f"evidence_symbol_column_missing:{path}")
        return {
            symbol
            for row in reader
            if (symbol := _normalize_symbol(row.get("symbol")))
        }


def load_weekly_minute_exclusion_policy(
    project_root: Path,
    *,
    policy_path: Path,
    target_date: str,
) -> dict[str, Any]:
    """Validate every explicit exclusion against current on-disk evidence."""

    root = Path(project_root).resolve(strict=True)
    target = _validated_date(target_date)
    resolved_policy = _descendant(root, Path(policy_path), role="exclusion_policy")
    if not resolved_policy.is_file():
        raise ValueError(f"exclusion_policy_missing:{resolved_policy}")
    payload = json.loads(resolved_policy.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("exclusion_policy_not_object")
    if (
        payload.get("schema_version") != "weekly_minute_wide_pool_exclusions_v1"
        or payload.get("task_type") != "weekly_minute_wide_pool_exclusions"
    ):
        raise ValueError("exclusion_policy_contract_mismatch")
    effective_from = _validated_date(str(payload.get("effective_from") or ""))
    if effective_from > target:
        raise ValueError("exclusion_policy_not_yet_effective")
    sources = payload.get("evidence_sources")
    if not isinstance(sources, Mapping):
        raise ValueError("exclusion_policy_evidence_sources_missing")
    delist_path = _descendant(
        root,
        Path(str(sources.get("delist_history") or "")),
        role="delist_history",
    )
    universe_path = _descendant(
        root,
        Path(str(sources.get("current_a_share_universe") or "")),
        role="current_a_share_universe",
    )
    if not delist_path.is_file() or not universe_path.is_file():
        raise ValueError("exclusion_policy_evidence_source_missing")
    with delist_path.open("r", newline="", encoding="utf-8-sig") as handle:
        delist_rows = [dict(row) for row in csv.DictReader(handle)]
    current_symbols = _csv_symbols(universe_path)
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("exclusion_policy_entries_missing")

    seen: set[str] = set()
    audited: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("exclusion_policy_entry_not_object")
        symbol = _normalize_symbol(raw_entry.get("symbol"))
        if not symbol or symbol in seen:
            raise ValueError(f"exclusion_policy_symbol_invalid_or_duplicate:{symbol}")
        seen.add(symbol)
        reason = str(raw_entry.get("reason_code") or "").strip()
        evidence: dict[str, Any]
        if reason == "delisted":
            expected_name = str(raw_entry.get("expected_name") or "").strip()
            expected_date = _validated_date(
                str(raw_entry.get("expected_delist_date") or "")
            )
            matches = [
                row
                for row in delist_rows
                if _normalize_symbol(row.get("symbol")) == symbol
                and str(row.get("name") or "").strip() == expected_name
                and str(row.get("delist_date") or "")[:10] == expected_date
            ]
            if len(matches) != 1 or expected_date > target:
                raise ValueError(f"delist_evidence_missing:{symbol}")
            evidence = {
                "path": str(delist_path),
                "name": expected_name,
                "delist_date": expected_date,
                "matched_rows": 1,
            }
        elif reason == "outside_current_universe_orphan_stub":
            if symbol in current_symbols:
                raise ValueError(f"orphan_symbol_still_in_current_universe:{symbol}")
            canonical_path = _descendant(
                root,
                Path(str(raw_entry.get("canonical_path") or "")),
                role=f"orphan_canonical:{symbol}",
            )
            canonical_root = (root / "data" / "intraday" / "1m").resolve(
                strict=False
            )
            try:
                canonical_path.relative_to(canonical_root)
            except ValueError as exc:
                raise ValueError(f"orphan_path_outside_canonical:{symbol}") from exc
            expected_bytes = int(raw_entry.get("expected_bytes") or -1)
            expected_sha = str(raw_entry.get("expected_sha256") or "").lower()
            if (
                not canonical_path.is_file()
                or canonical_path.stat().st_size != expected_bytes
                or _sha256_file(canonical_path) != expected_sha
            ):
                raise ValueError(f"orphan_stub_evidence_mismatch:{symbol}")
            evidence = {
                "universe_path": str(universe_path),
                "current_universe_membership": False,
                "canonical_path": str(canonical_path),
                "bytes": expected_bytes,
                "sha256": expected_sha,
            }
        else:
            raise ValueError(f"exclusion_reason_unsupported:{symbol}:{reason}")
        audited.append(
            {
                "symbol": symbol,
                "reason_code": reason,
                "evidence": evidence,
                "verified": True,
            }
        )

    symbols = sorted(seen)
    return {
        "schema_version": "weekly_minute_wide_pool_exclusion_audit_v1",
        "status": "passed",
        "target_date": target,
        "policy_path": str(resolved_policy),
        "policy_sha256": _sha256_file(resolved_policy),
        "symbols": symbols,
        "counts": {"requested": len(entries), "verified": len(audited)},
        "items": sorted(audited, key=lambda item: item["symbol"]),
    }


def _canonical_symbols(canonical_dir: Path) -> list[str]:
    symbols: list[str] = []
    for path in sorted(canonical_dir.glob("*.csv"), key=lambda item: item.name):
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(canonical_dir)
        except ValueError as exc:
            raise ValueError(f"path_outside_project_root:canonical_file:{resolved}") from exc
        symbol = _normalize_symbol(path.stem)
        if symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def _formal_symbols(formal_pool_path: Path) -> list[str]:
    with formal_pool_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        if not {"symbol", "included"}.issubset(fields):
            raise ValueError("formal_pool_columns_missing:symbol,included")
        symbols = {
            symbol
            for row in reader
            if str(row.get("included") or "").strip().lower() in _TRUTHY
            if (symbol := _normalize_symbol(row.get("symbol")))
        }
    return sorted(symbols)


def build_weekly_minute_wide_pool_contract(
    project_root: Path,
    *,
    canonical_dir: Path | None = None,
    formal_pool_path: Path | None = None,
    explicit_exclusions: Sequence[str] = (),
    target_date: str,
) -> dict[str, Any]:
    """Build a deterministic, read-only residual-pool contract."""

    target = _validated_date(target_date)
    paths = _validated_paths(
        project_root,
        canonical_dir=canonical_dir,
        formal_pool_path=formal_pool_path,
    )
    canonical = _canonical_symbols(paths.canonical_dir)
    formal = _formal_symbols(paths.formal_pool_path)
    excluded = sorted(
        {
            symbol
            for item in explicit_exclusions
            if (symbol := _normalize_symbol(item))
        }
    )
    canonical_set = set(canonical)
    formal_set = set(formal)
    intersection = sorted(canonical_set & formal_set)
    pre_exclusion = sorted(canonical_set - formal_set)
    applied_exclusions = sorted(set(pre_exclusion) & set(excluded))
    weekly = sorted(set(pre_exclusion) - set(applied_exclusions))
    return {
        "schema_version": "weekly_minute_wide_pool_contract_v1",
        "contract_version": WEEKLY_MINUTE_WIDE_POOL_CONTRACT_VERSION,
        "task_type": "weekly_minute_wide_pool",
        "generated_at": now_text(),
        "target_date": target,
        "definition": (
            "existing_canonical_1m_symbols_minus_formal_daily_analysis_pool_"
            "minus_explicit_exclusions"
        ),
        "symbols": weekly,
        "formal_symbols": formal,
        "explicit_exclusions": excluded,
        "counts": {
            "canonical": len(canonical),
            "formal": len(formal),
            "intersection": len(intersection),
            "pre_exclusion_weekly_wide": len(pre_exclusion),
            "weekly_wide": len(weekly),
            "explicit_exclusions": len(excluded),
            "applied_explicit_exclusions": len(applied_exclusions),
        },
        "paths": {
            "project_root": str(paths.root),
            "canonical_dir": str(paths.canonical_dir),
            "formal_pool_path": str(paths.formal_pool_path),
        },
        "hashes": {
            "canonical_symbol_set_sha256": _sha256_symbols(canonical),
            "formal_pool_file_sha256": _sha256_file(paths.formal_pool_path),
            "formal_symbol_set_sha256": _sha256_symbols(formal),
            "intersection_symbol_set_sha256": _sha256_symbols(intersection),
            "weekly_symbol_set_sha256": _sha256_symbols(weekly),
            "explicit_exclusion_set_sha256": _sha256_symbols(excluded),
            "applied_explicit_exclusion_set_sha256": _sha256_symbols(
                applied_exclusions
            ),
        },
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "no_broker_route": True,
            "live_execution_locked": True,
            "not_investment_advice": True,
            "not_position_advice": True,
            "full_a_scan": False,
            "existing_canonical_symbols_only": True,
            "canonical_write": False,
            "apply_executed": False,
        },
        "disclaimer": DISCLAIMER,
    }


def _run_id(value: str | None) -> str:
    if value:
        text = str(value).strip()
        if not _SAFE_RUN_ID_PATTERN.fullmatch(text):
            raise ValueError(f"invalid_run_id:{value}")
        return text
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"weekly-minute-wide-pool-{stamp}-{uuid4().hex[:10]}"


def _legacy_price_basis_preflight(
    canonical_dir: Path,
    symbols: Sequence[str],
) -> dict[str, Any]:
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    for symbol in sorted(set(symbols)):
        path = canonical_dir / f"{symbol}.csv"
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                header = next(csv.reader(handle, strict=True), [])
        except (OSError, UnicodeError, csv.Error) as exc:
            errors.append(
                {
                    "symbol": symbol,
                    "reason_code": "canonical_intraday_csv_unreadable_or_malformed",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        if "price_basis" not in {str(field).strip() for field in header}:
            missing.append(symbol)
    error_symbols = [item["symbol"] for item in errors]
    return {
        "status": "blocked" if missing or errors else "passed",
        "checked_symbol_count": len(set(symbols)),
        "missing_price_basis_count": len(missing),
        "missing_price_basis_symbols": missing,
        "unreadable_or_malformed_count": len(errors),
        "unreadable_or_malformed_symbols": error_symbols,
        "errors": errors,
    }


def _audit_passed(symbols: Sequence[str], payload: Mapping[str, Any]) -> bool:
    wanted = set(symbols)
    items = {
        _normalize_symbol(item.get("symbol")): item
        for item in list(payload.get("items") or [])
        if isinstance(item, Mapping) and _normalize_symbol(item.get("symbol"))
    }
    return set(items) == wanted and all(
        item.get("complete_session") is True
        and item.get("formal_execution_eligible") is True
        and str(item.get("price_basis") or "").strip().lower() == "raw"
        for item in items.values()
    )


def _base_run_payload(
    contract: Mapping[str, Any],
    *,
    network: bool,
    apply: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": "weekly_minute_wide_pool_run_v1",
        "contract_version": WEEKLY_MINUTE_WIDE_POOL_CONTRACT_VERSION,
        "task_type": "weekly_minute_wide_pool",
        "generated_at": now_text(),
        "target_date": contract.get("target_date"),
        "network": bool(network),
        "apply": bool(apply),
        "pool_contract": dict(contract),
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "no_broker_route": True,
            "live_execution_locked": True,
            "full_a_scan": False,
            "daily_chain_latest_write": False,
            "state_metadata_write": False,
        },
        "disclaimer": DISCLAIMER,
    }


def run_weekly_minute_wide_pool(
    project_root: Path,
    *,
    target_date: str,
    network: bool = False,
    apply: bool = False,
    batch_size: int = 1,
    lock_timeout_seconds: float = 0,
    explicit_exclusions: Sequence[str] = (),
    exclusion_policy_path: Path | None = None,
    canonical_dir: Path | None = None,
    formal_pool_path: Path | None = None,
    results_root: Path | None = None,
    run_id: str | None = None,
    provider_updater: Callable[..., Mapping[str, Any]] | None = None,
    quality_auditor: Callable[..., Mapping[str, Any]] | None = None,
    append_importer: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the weekly residual-pool maintenance with explicit side-effect gates."""

    if int(batch_size) < 1:
        raise ValueError("batch_size_must_be_positive")
    lock_timeout = float(lock_timeout_seconds)
    if lock_timeout < 0 or lock_timeout > 21600:
        raise ValueError("lock_timeout_seconds_outside_bounded_range:0..21600")
    target = _validated_date(target_date)
    paths = _validated_paths(
        project_root,
        canonical_dir=canonical_dir,
        formal_pool_path=formal_pool_path,
        results_root=results_root,
    )
    if exclusion_policy_path is not None and explicit_exclusions:
        return {
            "ok": False,
            "schema_version": "weekly_minute_wide_pool_run_v1",
            "task_type": "weekly_minute_wide_pool",
            "target_date": target,
            "network": bool(network),
            "apply": bool(apply),
            "status": "blocked",
            "reason_codes": ["exclusion_policy_invalid"],
            "error": "explicit_exclusions_conflict_with_policy",
            "disclaimer": DISCLAIMER,
        }
    def resolve_exclusions() -> tuple[list[str], dict[str, Any] | None]:
        if exclusion_policy_path is None:
            return list(explicit_exclusions), None
        audit = load_weekly_minute_exclusion_policy(
                paths.root,
                policy_path=exclusion_policy_path,
                target_date=target,
            )
        return list(audit["symbols"]), audit

    def invalid_exclusion_policy(exc: BaseException) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "weekly_minute_wide_pool_run_v1",
            "task_type": "weekly_minute_wide_pool",
            "target_date": target,
            "network": bool(network),
            "apply": bool(apply),
            "status": "blocked",
            "reason_codes": ["exclusion_policy_invalid"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
            "disclaimer": DISCLAIMER,
        }

    def current_contract(
        configured_exclusions: Sequence[str],
        exclusion_audit: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload = build_weekly_minute_wide_pool_contract(
            paths.root,
            canonical_dir=paths.canonical_dir,
            formal_pool_path=paths.formal_pool_path,
            explicit_exclusions=configured_exclusions,
            target_date=target,
        )
        if exclusion_audit is not None:
            payload["exclusion_audit"] = exclusion_audit
        return payload

    if not network:
        try:
            configured_exclusions, exclusion_audit = resolve_exclusions()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return invalid_exclusion_policy(exc)
        contract = current_contract(configured_exclusions, exclusion_audit)
        base = _base_run_payload(contract, network=network, apply=apply)
        if apply:
            return {
                **base,
                "ok": False,
                "status": "blocked",
                "reason_codes": ["apply_requires_network"],
            }
        return {
            **base,
            "status": "planned_offline",
            "reason_codes": ["network_not_authorized"],
        }

    try:
        lock_context = interprocess_file_lock(
            paths.daily_chain_lock,
            timeout_seconds=lock_timeout,
        )
        with lock_context:
            try:
                configured_exclusions, exclusion_audit = resolve_exclusions()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return invalid_exclusion_policy(exc)
            contract = current_contract(configured_exclusions, exclusion_audit)
            base = _base_run_payload(contract, network=network, apply=apply)
            price_basis_preflight = _legacy_price_basis_preflight(
                paths.canonical_dir,
                list(contract.get("symbols") or []),
            )
            if price_basis_preflight["status"] != "passed":
                if price_basis_preflight["unreadable_or_malformed_count"]:
                    status = "blocked_canonical_price_basis_preflight_failed"
                    reason_codes = [
                        "canonical_intraday_csv_unreadable_or_malformed"
                    ]
                else:
                    status = "blocked_legacy_price_basis_migration_required"
                    reason_codes = ["legacy_price_basis_migration_required"]
                return {
                    **base,
                    "ok": False,
                    "status": status,
                    "reason_codes": reason_codes,
                    "legacy_price_basis_preflight": price_basis_preflight,
                }

            current_run_id = _run_id(run_id)
            run_dir = paths.results_root / "runs" / current_run_id
            provider = provider_updater or update_intraday_1m_for_symbols
            auditor = quality_auditor or audit_intraday_batch
            importer = append_importer or apply_audited_intraday_batch
            run_dir.mkdir(parents=True, exist_ok=False)
            batches_dir = run_dir / "batches"
            staging_root = run_dir / "intraday_staging"
            batches_dir.mkdir()
            staging_root.mkdir()
            batch_results: list[dict[str, Any]] = []
            symbols = list(contract.get("symbols") or [])
            for offset in range(0, len(symbols), int(batch_size)):
                batch = symbols[offset : offset + int(batch_size)]
                index = offset // int(batch_size) + 1
                batch_name = f"batch_{index:04d}"
                staging_dir = staging_root / batch_name
                staging_dir.mkdir()
                batch_payload: dict[str, Any] = {
                    "batch_index": index,
                    "symbols": batch,
                    "status": "failed",
                    "provider": {},
                    "quality": {},
                    "canonical_import": {"status": "not_applicable", "applied": False},
                }
                try:
                    provider_payload = dict(
                        provider(
                            batch,
                            str(contract["target_date"]),
                            provider_order=list(DEFAULT_INTRADAY_PROVIDER_ORDER),
                            source="symbols",
                            overwrite=False,
                            append=True,
                            dry_run=False,
                            network=True,
                            execute_after_update=False,
                            update_portfolio_after_execution=False,
                            intraday_dir=staging_dir,
                            existing_intraday_dir=paths.canonical_dir,
                            materialize_local_existing=True,
                            output_path=batches_dir / f"{batch_name}_provider.json",
                            run_id=current_run_id,
                            require_raw_price_basis=True,
                            require_complete_session=True,
                        )
                    )
                    batch_payload["provider"] = provider_payload
                    quality_payload = dict(
                        auditor(
                            batch,
                            str(contract["target_date"]),
                            intraday_dir=staging_dir,
                            output_path=batches_dir / f"{batch_name}_quality.json",
                            write=True,
                            run_id=current_run_id,
                        )
                    )
                    batch_payload["quality"] = quality_payload
                    if not _audit_passed(batch, quality_payload):
                        batch_payload["status"] = "quality_blocked"
                        batch_payload["reason_codes"] = ["batch_quality_audit_failed"]
                    elif apply:
                        import_payload = dict(
                            importer(
                                staging_dir=staging_dir,
                                canonical_dir=paths.canonical_dir,
                                symbols=batch,
                                target_date=str(contract["target_date"]),
                                run_id=current_run_id,
                                quality_audit=quality_payload,
                                output_path=batches_dir / f"{batch_name}_append.json",
                                apply=True,
                            )
                        )
                        batch_payload["canonical_import"] = import_payload
                        import_completed = (
                            str(import_payload.get("status") or "") == "completed"
                            and import_payload.get("applied") is True
                        )
                        batch_payload["status"] = (
                            "completed" if import_completed else "import_blocked"
                        )
                    else:
                        batch_payload["status"] = "staged_and_audited"
                except Exception as exc:  # noqa: BLE001 - isolate one-symbol batches
                    batch_payload["status"] = "failed"
                    batch_payload["error"] = f"{type(exc).__name__}:{exc}"[:1000]
                atomic_write_json(batches_dir / f"{batch_name}.json", batch_payload)
                batch_results.append(batch_payload)

            failed_batches = sum(item["status"] == "failed" for item in batch_results)
            quality_failed = sum(
                item["status"] == "quality_blocked" for item in batch_results
            )
            import_failed = sum(
                item["status"] == "import_blocked" for item in batch_results
            )
            complete_status = "completed" if apply else "completed_staging_only"
            status = (
                complete_status
                if not (failed_batches or quality_failed or import_failed)
                else "partial"
            )
            payload = {
                **base,
                "ok": status == complete_status,
                "run_id": current_run_id,
                "run_dir": str(run_dir),
                "status": status,
                "completed_at": now_text(),
                "summary": {
                    "symbol_count": len(symbols),
                    "batch_count": len(batch_results),
                    "failed_batches": failed_batches,
                    "quality_failed_batches": quality_failed,
                    "import_failed_batches": import_failed,
                },
                "batches": batch_results,
            }
            atomic_write_json(run_dir / "manifest.json", payload)
            return payload
    except TimeoutError:
        return {
            "ok": False,
            "schema_version": "weekly_minute_wide_pool_run_v1",
            "contract_version": WEEKLY_MINUTE_WIDE_POOL_CONTRACT_VERSION,
            "task_type": "weekly_minute_wide_pool",
            "generated_at": now_text(),
            "target_date": target,
            "network": bool(network),
            "apply": bool(apply),
            "status": "busy",
            "reason_codes": ["daily_data_chain_lock_busy"],
            "disclaimer": DISCLAIMER,
        }


__all__ = [
    "WEEKLY_MINUTE_WIDE_POOL_CONTRACT_VERSION",
    "build_weekly_minute_wide_pool_contract",
    "load_weekly_minute_exclusion_policy",
    "run_weekly_minute_wide_pool",
]
