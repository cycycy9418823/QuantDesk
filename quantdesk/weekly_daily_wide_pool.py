"""Audited weekly maintenance for daily bars outside the formal analysis pool."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from quantdesk.daily_price_providers import (
    STANDARD_DAILY_COLUMNS,
    daily_provider_supports_symbol,
    fetch_daily,
)
from quantdesk.daily_session_evidence import validate_baostock_session_evidence
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday.weekly_minute_wide_pool import run_weekly_minute_wide_pool
from quantdesk.market_clock import is_cn_a_share_trading_day, load_cn_trading_calendar
from quantdesk.process_deadline import run_process_tasks_until_deadline
from quantdesk.price_basis import (
    PRICE_BASIS_RAW,
    classify_frame_price_basis,
    validate_price_frame_set,
)
from quantdesk.shared import now_text
from quantdesk.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC_PATH = PROJECT_ROOT / "config" / "weekly_daily_wide_pool_contract.json"
WEEKLY_DAILY_WIDE_POOL_CONTRACT_VERSION = 1
WEEKLY_DAILY_WIDE_POOL_DEFINITION = (
    "current_a_share_symbols_intersect_existing_canonical_daily_symbols_"
    "minus_formal_daily_analysis_pool_minus_explicit_exclusions"
)
DEFAULT_DAILY_PROVIDER_ORDER = (
    "mootdx_daily",
    "baostock_daily",
    "akshare_daily",
    "sina_daily",
    "tencent_daily",
)
# A provider call can block indefinitely inside a socket read, and a Python
# thread stuck there cannot be interrupted -- the 2026-08-03 staging run proved
# it: eight worker threads were consumed one by one and the whole run produced
# nothing for 28 minutes while holding the shared chain lock. The mature
# ``daily_price_update_engine`` path already solved this by running each
# provider slice in killable subprocesses under a hard deadline, so reuse the
# same budget shape here instead of inventing a second one.
DAILY_PROVIDER_GLOBAL_TIMEOUT_SECONDS = 6600.0
DAILY_PROVIDER_TIMEOUT_OVERRIDES: dict[str, float] = {
    "mootdx_daily": 6000.0,
    "baostock_daily": 180.0,
    "tencent_daily": 300.0,
    "akshare_daily": 300.0,
    "sina_daily": 300.0,
}
# Per-symbol ceiling for one staging task. The global budget bounds the whole
# run; this bounds a single hung symbol so one bad instrument cannot eat the
# entire window on its own.
DAILY_SYMBOL_STAGE_TIMEOUT_SECONDS = 180.0
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "Not Investment Advice / Not Position Advice."
)
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
_SAFE_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TRUTHY = {"1", "true", "yes", "y", "on", "included"}
_DIGEST_KEYS = (
    "schema_version",
    "contract_version",
    "task_type",
    "target_date",
    "definition",
    "paths",
    "rules",
    "required_flow",
    "boundaries",
    "counts",
    "hashes",
    "market_universe_audit",
    "canonical_daily_audit",
    "formal_symbols",
    "explicit_exclusions",
    "symbols",
    "formal_eligible",
    "reason_codes",
)
_DAILY_COMPARE_COLUMNS = (
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adjust",
)


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdigit() and len(text) <= 6:
        text = text.zfill(6)
    return text if _SYMBOL_PATTERN.fullmatch(text) else ""


def _target_date(value: str) -> str:
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid_target_date:{value}") from exc
    return text


def _integer(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _descendant(root: Path, value: Path, *, role: str) -> Path:
    candidate = value if value.is_absolute() else root / value
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path_outside_project_root:{role}:{resolved}") from exc
    return resolved


def _relative(root: Path, path: Path) -> str:
    return str(path.resolve(strict=False).relative_to(root)).replace("\\", "/")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_symbols(symbols: Sequence[str]) -> str:
    payload = "\n".join(symbols).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _sha256_payload(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _read_spec(root: Path, spec_path: Path | None) -> tuple[Path, dict[str, Any]]:
    path = _descendant(
        root,
        Path(spec_path or root / "config" / "weekly_daily_wide_pool_contract.json"),
        role="contract_spec_path",
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"contract_spec_unreadable:{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("contract_spec_not_object")
    if (
        payload.get("schema_version") != "weekly_daily_wide_pool_spec_v1"
        or _integer(payload.get("contract_version"))
        != WEEKLY_DAILY_WIDE_POOL_CONTRACT_VERSION
        or payload.get("task_type") != "weekly_daily_wide_pool"
        or payload.get("definition") != WEEKLY_DAILY_WIDE_POOL_DEFINITION
    ):
        raise ValueError("contract_spec_identity_mismatch")
    return path, payload


def load_weekly_daily_exclusion_policy(
    project_root: Path,
    *,
    policy_path: Path,
    target_date: str,
) -> dict[str, Any]:
    """Validate every daily-pool exclusion against current on-disk evidence.

    The whole-pool gate is all-or-nothing: one永久 unfetchable symbol keeps 4,599
    good ones out of canonical. Excluding those symbols is therefore the only way
    to unblock the pool without weakening the gate itself -- but an exclusion must
    never be a free pass. Every entry re-proves itself here against the current
    universe/delist/ST files, and any drift fails the whole task closed rather
    than silently dropping a symbol that has resumed trading.
    """

    root = Path(project_root).resolve(strict=True)
    target = _target_date(target_date)
    resolved_policy = _descendant(root, Path(policy_path), role="exclusion_policy")
    if not resolved_policy.is_file():
        raise ValueError(f"exclusion_policy_missing:{resolved_policy}")
    payload = json.loads(resolved_policy.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("exclusion_policy_not_object")
    if (
        payload.get("schema_version") != "weekly_daily_wide_pool_exclusions_v1"
        or payload.get("task_type") != "weekly_daily_wide_pool_exclusions"
    ):
        raise ValueError("exclusion_policy_contract_mismatch")
    effective_from = _target_date(str(payload.get("effective_from") or ""))
    if effective_from > target:
        raise ValueError("exclusion_policy_not_yet_effective")
    sources = payload.get("evidence_sources")
    if not isinstance(sources, Mapping):
        raise ValueError("exclusion_policy_evidence_sources_missing")
    universe_path = _descendant(
        root,
        Path(str(sources.get("current_a_share_universe") or "")),
        role="current_a_share_universe",
    )
    st_path = _descendant(
        root, Path(str(sources.get("st_history") or "")), role="st_history"
    )
    if not universe_path.is_file() or not st_path.is_file():
        raise ValueError("exclusion_policy_evidence_source_missing")
    with universe_path.open("r", newline="", encoding="utf-8-sig") as handle:
        universe_by_symbol = {
            _symbol(row.get("symbol")): str(row.get("name") or "").strip()
            for row in csv.DictReader(handle)
            if _symbol(row.get("symbol"))
        }
    with st_path.open("r", newline="", encoding="utf-8-sig") as handle:
        st_symbols = {
            _symbol(row.get("symbol"))
            for row in csv.DictReader(handle)
            if _symbol(row.get("symbol"))
        }
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("exclusion_policy_entries_missing")

    canonical_root = _descendant(root, root / "data" / "daily", role="canonical_daily")
    seen: set[str] = set()
    moot: set[str] = set()
    audited: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("exclusion_policy_entry_not_object")
        symbol = _symbol(raw_entry.get("symbol"))
        if not symbol or symbol in seen:
            raise ValueError(f"exclusion_policy_symbol_invalid_or_duplicate:{symbol}")
        seen.add(symbol)
        reason = str(raw_entry.get("reason_code") or "").strip()
        expected_name = str(raw_entry.get("expected_name") or "").strip()
        if not expected_name:
            raise ValueError(f"universe_name_evidence_mismatch:{symbol}")
        observed = universe_by_symbol.get(symbol)
        absent_from_universe = observed is None
        observed_name = observed or ""
        # 缺席不等于证据漂移。本守卫的目的是"别让恢复交易的票继续被排除"；
        # 一只已从上市名单整体移除的票恰恰是"不可能恢复交易"的最强形式，也
        # 不可能再进池子（池子 = 全集 ∩ canonical − 正式池）。因此该排除项
        # 自动失效（moot）：跳过名称等值检查，其余证据照查，并显式记账。
        if not absent_from_universe and observed_name != expected_name:
            raise ValueError(f"universe_name_evidence_mismatch:{symbol}")
        if absent_from_universe:
            moot.add(symbol)
        expected_last = _target_date(
            str(raw_entry.get("expected_last_trade_date") or "")
        )
        canonical_path = canonical_root / f"{symbol}.csv"
        if not canonical_path.is_file():
            raise ValueError(f"exclusion_canonical_missing:{symbol}")
        with canonical_path.open("r", newline="", encoding="utf-8-sig") as handle:
            observed_last = ""
            for row in csv.DictReader(handle):
                day = str(row.get("date") or "")[:10]
                if day:
                    observed_last = day
        if observed_last != expected_last or expected_last > target:
            raise ValueError(f"exclusion_last_trade_date_mismatch:{symbol}")
        if reason == "delisted_by_universe_name":
            # 交易所把退市标记直接写进名称后缀；这是权威事实，即使
            # delist_history.csv 尚未补登也成立。
            if not expected_name.endswith("退"):
                raise ValueError(f"delist_name_suffix_missing:{symbol}")
        elif reason == "long_suspended_st":
            if "ST" not in expected_name.upper():
                raise ValueError(f"st_name_marker_missing:{symbol}")
            if symbol not in st_symbols:
                raise ValueError(f"st_history_evidence_missing:{symbol}")
        else:
            raise ValueError(f"exclusion_reason_code_unsupported:{reason or '<empty>'}")
        audited.append(
            {
                "symbol": symbol,
                "reason_code": reason,
                "observed_name": observed_name,
                "observed_last_trade_date": observed_last,
                "universe_evidence": (
                    "absent_from_universe" if absent_from_universe else "name_matched"
                ),
                "exclusion_effective": not absent_from_universe,
                "evidence_paths": {
                    "universe": _relative(root, universe_path),
                    "st_history": _relative(root, st_path),
                    "canonical_daily": _relative(root, canonical_path),
                },
            }
        )
    return {
        "schema_version": str(payload.get("schema_version")),
        "policy_path": _relative(root, resolved_policy),
        "policy_sha256": _sha256_file(resolved_policy),
        "effective_from": effective_from,
        "target_date": target,
        "symbols": sorted(seen - moot),
        "declared_symbols": sorted(seen),
        "absent_from_universe": sorted(moot),
        "entries": audited,
        "verified_against_disk": True,
    }


def _market_symbols(
    path: Path,
    *,
    target: str,
    rules: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    required = {"symbol", "source", "updated_at"}
    approved_sources = {
        str(item).strip() for item in rules.get("approved_market_universe_sources") or []
    }
    max_age_days = _integer(rules.get("market_universe_max_age_days"))
    symbols: list[str] = []
    invalid_rows: list[int] = []
    source_values: set[str] = set()
    timestamps: list[datetime] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        if not required.issubset(set(fields)):
            raise ValueError(
                "market_universe_columns_missing:" + ",".join(sorted(required - set(fields)))
            )
        for row_number, row in enumerate(reader, start=2):
            normalized = _symbol(row.get("symbol"))
            source = str(row.get("source") or "").strip()
            captured = _parse_timestamp(row.get("updated_at"))
            if not normalized or not source or captured is None:
                invalid_rows.append(row_number)
                continue
            symbols.append(normalized)
            source_values.add(source)
            timestamps.append(captured)
    unique = sorted(set(symbols))
    target_day = date.fromisoformat(target)
    observed_dates = [item.date() for item in timestamps]
    oldest = min(observed_dates) if observed_dates else None
    newest = max(observed_dates) if observed_dates else None
    age_days = (target_day - oldest).days if oldest is not None else None
    reasons: list[str] = []
    if invalid_rows:
        reasons.append("market_universe_rows_invalid")
    if len(unique) != len(symbols):
        reasons.append("market_universe_duplicate_symbols")
    if not approved_sources or not source_values or not source_values <= approved_sources:
        reasons.append("market_universe_source_not_approved")
    if max_age_days < 0 or age_days is None or age_days < 0 or age_days > max_age_days:
        reasons.append("market_universe_stale")
    return unique, {
        "status": "passed" if not reasons else "blocked",
        "reason_codes": reasons,
        "row_count": len(symbols),
        "unique_symbol_count": len(unique),
        "invalid_row_numbers": invalid_rows,
        "sources": sorted(source_values),
        "oldest_updated_at_date": oldest.isoformat() if oldest else "",
        "newest_updated_at_date": newest.isoformat() if newest else "",
        "age_days_at_target": age_days,
        "max_age_days": max_age_days,
    }


def _formal_symbols(path: Path) -> list[str]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        if not {"symbol", "included"}.issubset(fields):
            raise ValueError("formal_pool_columns_missing:symbol,included")
        return sorted(
            {
                normalized
                for row in reader
                if str(row.get("included") or "").strip().lower() in _TRUTHY
                if (normalized := _symbol(row.get("symbol")))
            }
        )


def _canonical_paths(path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for item in sorted(path.glob("*.csv"), key=lambda candidate: candidate.name):
        normalized = _symbol(item.stem)
        if normalized:
            result[normalized] = item
    return result


def _canonical_file_manifest(
    root: Path,
    paths: Mapping[str, Path],
    symbols: Sequence[str],
    *,
    required_columns: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required = set(required_columns)
    items: list[dict[str, Any]] = []
    invalid_symbols: list[str] = []
    for symbol in symbols:
        path = paths[symbol]
        reasons: list[str] = []
        header: list[str] = []
        first_row: dict[str, str] | None = None
        if path.is_symlink() or not path.is_file():
            reasons.append("canonical_daily_not_regular_file")
        else:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(path.parent.resolve(strict=True))
                with path.open("r", newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    header = list(reader.fieldnames or [])
                    first_row = next(reader, None)
            except (OSError, UnicodeError, csv.Error, ValueError):
                reasons.append("canonical_daily_unreadable")
        if required - set(header):
            reasons.append("canonical_daily_header_invalid")
        if first_row is None:
            reasons.append("canonical_daily_empty")
        elif _symbol(first_row.get("symbol")) != symbol:
            reasons.append("canonical_daily_symbol_mismatch")
        if reasons:
            invalid_symbols.append(symbol)
        items.append(
            {
                "symbol": symbol,
                "path": _relative(root, path),
                "bytes": path.stat().st_size if path.is_file() and not path.is_symlink() else 0,
                "sha256": _sha256_file(path) if path.is_file() and not path.is_symlink() else "",
                "header": header,
                "nonempty": first_row is not None,
                "status": "passed" if not reasons else "blocked",
                "reason_codes": list(dict.fromkeys(reasons)),
            }
        )
    manifest_sha = _sha256_payload({"items": items})
    return items, {
        "status": "passed" if not invalid_symbols else "blocked",
        "reason_codes": ["canonical_daily_files_invalid"] if invalid_symbols else [],
        "checked_file_count": len(items),
        "passed_file_count": len(items) - len(invalid_symbols),
        "invalid_symbols": invalid_symbols,
        "required_columns": list(required_columns),
        "file_manifest_sha256": manifest_sha,
        "files": items,
    }


def _contract_digest_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in _DIGEST_KEYS}


def build_weekly_daily_wide_pool_contract(
    project_root: Path,
    *,
    target_date: str,
    market_universe_path: Path | None = None,
    canonical_daily_dir: Path | None = None,
    formal_pool_path: Path | None = None,
    spec_path: Path | None = None,
    explicit_exclusions: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a complete, replayable contract from real local files."""

    root = Path(project_root).resolve(strict=True)
    target = _target_date(target_date)
    resolved_spec_path, spec = _read_spec(root, spec_path)
    inputs = dict(spec.get("inputs") or {})
    market_path = _descendant(
        root,
        Path(market_universe_path or root / str(inputs.get("market_universe_path"))),
        role="market_universe_path",
    )
    daily_dir = _descendant(
        root,
        Path(canonical_daily_dir or root / str(inputs.get("canonical_daily_dir"))),
        role="canonical_daily_dir",
    )
    formal_path = _descendant(
        root,
        Path(formal_pool_path or root / str(inputs.get("formal_pool_path"))),
        role="formal_pool_path",
    )
    if not market_path.is_file():
        raise FileNotFoundError(f"market_universe_missing:{market_path}")
    if not daily_dir.is_dir():
        raise FileNotFoundError(f"canonical_daily_dir_missing:{daily_dir}")
    if not formal_path.is_file():
        raise FileNotFoundError(f"formal_pool_missing:{formal_path}")

    rules = dict(spec.get("rules") or {})
    boundaries = dict(spec.get("boundaries") or {})
    required_flow = list(spec.get("required_flow") or [])
    if required_flow != [
        "stage_network_fetch",
        "audit_staging",
        "append_only_canonical_import",
    ]:
        raise ValueError("contract_spec_required_flow_mismatch")
    exclusions: list[str] = []
    for value in explicit_exclusions:
        normalized = _symbol(value)
        if not normalized:
            raise ValueError(f"invalid_explicit_exclusion:{value}")
        exclusions.append(normalized)
    exclusions = sorted(set(exclusions))

    market, market_audit = _market_symbols(market_path, target=target, rules=rules)
    canonical_by_symbol = _canonical_paths(daily_dir)
    canonical = sorted(canonical_by_symbol)
    formal = _formal_symbols(formal_path)
    population = sorted(set(market) & set(canonical))
    weekly = sorted(set(population) - set(formal) - set(exclusions))
    required_columns = list(rules.get("canonical_daily_required_columns") or [])
    _file_items, canonical_audit = _canonical_file_manifest(
        root,
        canonical_by_symbol,
        population,
        required_columns=required_columns,
    )
    reason_codes = list(
        dict.fromkeys(
            list(market_audit.get("reason_codes") or [])
            + list(canonical_audit.get("reason_codes") or [])
        )
    )
    hashes = {
        "config_spec_sha256": _sha256_file(resolved_spec_path),
        "market_universe_file_sha256": _sha256_file(market_path),
        "market_symbol_set_sha256": _sha256_symbols(market),
        "canonical_daily_symbol_set_sha256": _sha256_symbols(canonical),
        "market_with_canonical_symbol_set_sha256": _sha256_symbols(population),
        "canonical_file_manifest_sha256": canonical_audit["file_manifest_sha256"],
        "formal_pool_file_sha256": _sha256_file(formal_path),
        "formal_symbol_set_sha256": _sha256_symbols(formal),
        "explicit_exclusion_set_sha256": _sha256_symbols(exclusions),
        "weekly_daily_wide_symbol_set_sha256": _sha256_symbols(weekly),
    }
    payload: dict[str, Any] = {
        "schema_version": "weekly_daily_wide_pool_contract_v2",
        "contract_version": WEEKLY_DAILY_WIDE_POOL_CONTRACT_VERSION,
        "task_type": "weekly_daily_wide_pool",
        "target_date": target,
        "definition": WEEKLY_DAILY_WIDE_POOL_DEFINITION,
        "paths": {
            "project_root": ".",
            "contract_spec_path": _relative(root, resolved_spec_path),
            "market_universe_path": _relative(root, market_path),
            "canonical_daily_dir": _relative(root, daily_dir),
            "formal_pool_path": _relative(root, formal_path),
        },
        "rules": rules,
        "required_flow": required_flow,
        "boundaries": boundaries,
        "counts": {
            "market_universe": len(market),
            "canonical_daily": len(canonical),
            "market_with_canonical_daily": len(population),
            "market_missing_canonical_daily": len(set(market) - set(canonical)),
            "canonical_daily_outside_market": len(set(canonical) - set(market)),
            "formal_daily_analysis": len(formal),
            "formal_in_market_with_canonical": len(set(formal) & set(population)),
            "explicit_exclusions": len(exclusions),
            "weekly_daily_wide": len(weekly),
        },
        "hashes": hashes,
        "market_universe_audit": market_audit,
        "canonical_daily_audit": canonical_audit,
        "formal_symbols": formal,
        "explicit_exclusions": exclusions,
        "symbols": weekly,
        "formal_eligible": not reason_codes and bool(weekly),
        "reason_codes": reason_codes + ([] if weekly else ["weekly_daily_wide_pool_empty"]),
    }
    payload["contract_sha256"] = _sha256_payload(_contract_digest_view(payload))
    payload["generated_at"] = now_text()
    payload["disclaimer"] = DISCLAIMER
    return payload


def validate_weekly_daily_contract(
    contract: Mapping[str, Any],
    *,
    project_root: Path | None = None,
) -> list[str]:
    """Recompute every contract binding needed before staging or apply."""

    reasons: list[str] = []
    if (
        contract.get("schema_version") != "weekly_daily_wide_pool_contract_v2"
        or _integer(contract.get("contract_version"))
        != WEEKLY_DAILY_WIDE_POOL_CONTRACT_VERSION
        or contract.get("task_type") != "weekly_daily_wide_pool"
        or contract.get("definition") != WEEKLY_DAILY_WIDE_POOL_DEFINITION
    ):
        reasons.append("contract_schema_or_definition_mismatch")
    raw_symbols = list(contract.get("symbols") or [])
    symbols = [_symbol(item) for item in raw_symbols]
    if not symbols:
        reasons.append("empty_contract")
    elif any(not item for item in symbols) or symbols != sorted(set(symbols)):
        reasons.append("contract_symbols_invalid_or_unsorted")
    hashes = dict(contract.get("hashes") or {})
    if str(hashes.get("weekly_daily_wide_symbol_set_sha256") or "").lower() != _sha256_symbols(symbols):
        reasons.append("contract_symbol_hash_mismatch")
    if _integer(dict(contract.get("counts") or {}).get("weekly_daily_wide")) != len(symbols):
        reasons.append("contract_symbol_count_mismatch")
    expected_digest = _sha256_payload(_contract_digest_view(contract))
    if str(contract.get("contract_sha256") or "").lower() != expected_digest:
        reasons.append("contract_digest_mismatch")
    if contract.get("formal_eligible") is not True or contract.get("reason_codes"):
        reasons.append("contract_not_formal_eligible")
    if project_root is not None:
        root = Path(project_root).resolve(strict=True)
        paths = dict(contract.get("paths") or {})
        try:
            spec_path = _descendant(root, Path(str(paths.get("contract_spec_path") or "")), role="contract_spec_path")
        except ValueError:
            reasons.append("contract_spec_path_invalid")
        else:
            actual_spec_hash = _sha256_file(spec_path) if spec_path.is_file() else ""
            if actual_spec_hash != str(hashes.get("config_spec_sha256") or ""):
                reasons.append("contract_spec_hash_mismatch")
    return list(dict.fromkeys(reasons))


def write_weekly_daily_wide_pool_contract(
    contract: Mapping[str, Any],
    output_path: Path,
    *,
    allowed_root: Path,
) -> Path:
    output = _descendant(Path(allowed_root).resolve(strict=True), Path(output_path), role="contract_output_path")
    atomic_write_json(output, dict(contract))
    try:
        written = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OSError("contract_write_verification_failed") from exc
    if written.get("contract_sha256") != contract.get("contract_sha256"):
        raise OSError("contract_write_verification_failed")
    return output


def freeze_weekly_daily_wide_pool_contract(
    project_root: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append-only freeze one measured contract under data/universe."""

    root = Path(project_root).resolve(strict=True)
    target = _target_date(str(contract.get("target_date") or ""))
    output = _descendant(
        root,
        root
        / "data"
        / "universe"
        / f"weekly_daily_wide_pool_contract_{target}.json",
        role="frozen_contract_path",
    )
    expected_digest = str(contract.get("contract_sha256") or "")
    if not _SHA256_PATTERN.fullmatch(expected_digest):
        raise ValueError("frozen_contract_digest_invalid")
    if output.exists():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("frozen_contract_existing_unreadable") from exc
        existing_digest = str(existing.get("contract_sha256") or "")
        if existing_digest != expected_digest:
            return dict(contract), {
                "status": "conflict",
                "reason_codes": ["same_day_frozen_contract_hash_conflict"],
                "path": str(output),
                "existing_contract_sha256": existing_digest,
                "candidate_contract_sha256": expected_digest,
                "bytes": output.stat().st_size,
                "sha256": _sha256_file(output),
                "written": False,
            }
        frozen = dict(existing)
        return frozen, {
            "status": "reused_identical",
            "reason_codes": [],
            "path": str(output),
            "contract_sha256": existing_digest,
            "bytes": output.stat().st_size,
            "sha256": _sha256_file(output),
            "written": False,
        }
    atomic_write_json(output, dict(contract))
    frozen, evidence = freeze_weekly_daily_wide_pool_contract(root, contract)
    evidence["status"] = "written"
    evidence["written"] = True
    return frozen, evidence


def _items_by_symbol(value: Any) -> tuple[dict[str, Mapping[str, Any]], bool]:
    raw_items = list(value or [])
    items: dict[str, Mapping[str, Any]] = {}
    invalid = False
    for item in raw_items:
        if not isinstance(item, Mapping):
            invalid = True
            continue
        normalized = _symbol(item.get("symbol"))
        if not normalized or normalized in items:
            invalid = True
            continue
        items[normalized] = item
    return items, invalid or len(items) != len(raw_items)


def coordinate_weekly_daily_update(
    contract: Mapping[str, Any],
    *,
    stage_result: Mapping[str, Any] | None = None,
    quality_result: Mapping[str, Any] | None = None,
    apply_requested: bool = False,
) -> dict[str, Any]:
    """Pure compatibility gate; the end-to-end runner additionally rereads files."""

    required_order = [
        "stage_network_fetch",
        "audit_staging",
        "append_only_canonical_import",
    ]
    expected = list(contract.get("symbols") or [])
    target = str(contract.get("target_date") or "")[:10]
    contract_hash = str(contract.get("contract_sha256") or "").lower()
    base = {
        "schema_version": "weekly_daily_update_coordination_v2",
        "task_type": "weekly_daily_wide_pool_coordination",
        "target_date": target,
        "contract_sha256": contract_hash,
        "symbol_count": len(expected),
        "apply_requested": bool(apply_requested),
        "required_order": required_order,
        "canonical_import_allowed": False,
        "side_effects_executed": False,
        "disclaimer": DISCLAIMER,
    }
    contract_reasons = validate_weekly_daily_contract(contract)
    if contract_reasons:
        return {**base, "status": "blocked", "reason_codes": contract_reasons}
    if not stage_result:
        return {**base, "status": "staging_required", "reason_codes": ["staging_evidence_missing"]}
    if not quality_result:
        return {**base, "status": "blocked", "reason_codes": ["quality_evidence_missing"]}
    stage = dict(stage_result)
    quality = dict(quality_result)
    reasons: list[str] = []
    for payload, prefix, required_status in (
        (stage, "stage", "completed"),
        (quality, "quality", "passed"),
    ):
        if payload.get("status") != required_status:
            reasons.append(f"{prefix}_status_not_{required_status}")
        if str(payload.get("target_date") or "")[:10] != target:
            reasons.append(f"{prefix}_target_date_mismatch")
        if str(payload.get("contract_sha256") or "").lower() != contract_hash:
            reasons.append(f"{prefix}_contract_hash_mismatch")
    stage_items, stage_invalid = _items_by_symbol(stage.get("items"))
    quality_items, quality_invalid = _items_by_symbol(quality.get("items"))
    if stage_invalid or sorted(stage_items) != expected:
        reasons.append("stage_symbol_set_mismatch")
    if quality_invalid or sorted(quality_items) != expected:
        reasons.append("quality_symbol_set_mismatch")
    for symbol in expected:
        stage_item = stage_items.get(symbol)
        quality_item = quality_items.get(symbol)
        if not stage_item or not quality_item:
            continue
        stage_hash = str(stage_item.get("staging_sha256") or "").lower()
        if (
            stage_item.get("status") != "passed"
            or not str(stage_item.get("source") or "").strip()
            or not _parse_timestamp(stage_item.get("captured_at"))
            or (stage_hash and not _SHA256_PATTERN.fullmatch(stage_hash))
            or stage_item.get("price_basis") != "raw"
        ):
            reasons.append("stage_provenance_incomplete")
        if (
            quality_item.get("status") != "passed"
            or quality_item.get("schema_valid") is not True
            or quality_item.get("target_date_complete") is not True
            or quality_item.get("price_basis") != "raw"
        ):
            reasons.append("quality_item_not_eligible")
        if str(quality_item.get("staging_sha256") or "").lower() != stage_hash:
            reasons.append("quality_staging_hash_mismatch")
    unique_reasons = list(dict.fromkeys(reasons))
    if unique_reasons:
        return {**base, "status": "blocked", "reason_codes": unique_reasons}
    if not apply_requested:
        return {
            **base,
            "status": "staged_and_audited",
            "reason_codes": ["apply_not_requested"],
            "canonical_import_eligible": True,
        }
    return {
        **base,
        "status": "ready_for_canonical_import",
        "reason_codes": [],
        "canonical_import_eligible": True,
        "canonical_import_allowed": True,
    }


def _raw_daily_frame(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"symbol": str})


def _price_basis_gate(symbol: str, frame: pd.DataFrame) -> tuple[bool, str]:
    basis, reason = classify_frame_price_basis(frame)
    gate = validate_price_frame_set({symbol: frame}, consumer="execution")
    eligible = (
        basis == PRICE_BASIS_RAW
        and not reason
        and gate.get("eligible") is True
        and gate.get("accepted_price_basis") == PRICE_BASIS_RAW
    )
    return eligible, reason or ",".join(gate.get("reason_codes") or [])


def _frame_dates(frame: pd.DataFrame) -> list[str]:
    if "date" not in frame.columns:
        return []
    parsed = pd.to_datetime(frame["date"], errors="coerce")
    return sorted(set(parsed.dropna().dt.strftime("%Y-%m-%d")))


def _trading_dates(start_exclusive: str, target: str, calendar: Mapping[str, Any]) -> list[str]:
    current = date.fromisoformat(start_exclusive) + timedelta(days=1)
    end = date.fromisoformat(target)
    dates: list[str] = []
    while current <= end:
        if is_cn_a_share_trading_day(current, dict(calendar)):
            dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def _frame_provenance(
    frame: pd.DataFrame,
    *,
    approved_sources: set[str],
) -> tuple[bool, list[str], str]:
    sources = sorted(
        {str(item or "").strip() for item in frame.get("source", pd.Series(dtype=str)) if str(item or "").strip()}
    )
    captured = sorted(
        {str(item or "").strip() for item in frame.get("updated_at", pd.Series(dtype=str)) if str(item or "").strip()}
    )
    valid = (
        bool(sources)
        and set(sources) <= approved_sources
        and bool(captured)
        and all(_parse_timestamp(item) is not None for item in captured)
    )
    return valid, sources, captured[-1] if captured else ""


def _write_stage_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            frame[list(STANDARD_DAILY_COLUMNS)].to_csv(handle, index=False, lineterminator="\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _verified_suspension_dates(
    provider: str,
    frame: pd.DataFrame,
    *,
    symbol: str,
    start_date: str,
    target_date: str,
) -> set[str]:
    if provider != "baostock_daily":
        return set()
    evidence = frame.attrs.get("suspension_evidence")
    if not evidence:
        return set()
    validated = validate_baostock_session_evidence(
        evidence,
        expected_symbol=symbol,
        expected_start_date=start_date,
        expected_end_date=target_date,
    )
    return set(validated.get("suspended_dates") or []) if validated.get("valid") else set()


def _stage_one_symbol(
    symbol: str,
    *,
    target: str,
    canonical_path: Path,
    staging_path: Path,
    calendar: Mapping[str, Any],
    approved_sources: set[str],
    provider_order: Sequence[str],
    provider_fetcher: Callable[..., tuple[pd.DataFrame, list[str]]],
) -> dict[str, Any]:
    before_sha = _sha256_file(canonical_path)
    original = _raw_daily_frame(canonical_path)
    basis_ok, basis_reason = _price_basis_gate(symbol, original)
    dates = _frame_dates(original)
    reasons: list[str] = []
    if not basis_ok:
        reasons.append("canonical_daily_price_basis_not_raw")
    if not dates:
        reasons.append("canonical_daily_dates_invalid")
    latest = dates[-1] if dates else ""
    existing_target = original.loc[original["date"].astype(str).str[:10] == target] if "date" in original.columns else pd.DataFrame()
    if latest >= target and len(existing_target) == 1:
        provenance_ok, sources, captured_at = _frame_provenance(
            existing_target, approved_sources=approved_sources
        )
        if not provenance_ok:
            reasons.append("canonical_target_provenance_invalid")
        return {
            "symbol": symbol,
            "target_date": target,
            "status": "passed" if not reasons else "blocked",
            "reason_codes": reasons,
            "operation": "already_current",
            "canonical_path": str(canonical_path),
            "canonical_sha256_before": before_sha,
            "canonical_row_count_before": int(len(original)),
            "staging_path": "",
            "staging_sha256": "",
            "staging_row_count": 0,
            "source": ",".join(sources),
            "captured_at": captured_at,
            "price_basis": "raw" if basis_ok else "mixed_or_unknown",
            "schema_valid": not reasons,
            "target_date_complete": not reasons,
            "verified_no_session_dates": [],
        }
    if not latest:
        return {
            "symbol": symbol,
            "target_date": target,
            "status": "blocked",
            "reason_codes": list(dict.fromkeys(reasons or ["canonical_daily_dates_invalid"])),
            "operation": "blocked",
            "canonical_path": str(canonical_path),
            "canonical_sha256_before": before_sha,
            "canonical_row_count_before": int(len(original)),
            "staging_path": "",
            "staging_sha256": "",
            "staging_row_count": 0,
            "source": "",
            "captured_at": "",
            "price_basis": "mixed_or_unknown",
            "schema_valid": False,
            "target_date_complete": False,
            "verified_no_session_dates": [],
        }
    expected = _trading_dates(latest, target, calendar)
    attempts: list[dict[str, Any]] = []
    for provider in provider_order:
        # 能力先问：sina/baostock 不映射北交所 920xxx，硬派过去只会拿到
        # provider_unsupported_symbol，把"该源不支持该板"伪装成"抓取失败"。
        if not daily_provider_supports_symbol(provider, symbol):
            attempts.append(
                {
                    "provider": provider,
                    "status": "unsupported_symbol",
                    "reason": f"provider_does_not_support_symbol:{provider}:{symbol}",
                }
            )
            continue
        try:
            fresh, warnings = provider_fetcher(
                provider,
                symbol,
                expected[0] if expected else target,
                target,
                network=True,
            )
        except Exception as exc:  # noqa: BLE001 - provider isolation
            attempts.append({"provider": provider, "status": "failed", "error": f"{type(exc).__name__}:{exc}"[:500]})
            continue
        if not isinstance(fresh, pd.DataFrame):
            attempts.append({"provider": provider, "status": "failed", "warnings": list(warnings or [])})
            continue
        suspended = _verified_suspension_dates(
            provider,
            fresh,
            symbol=symbol,
            start_date=expected[0] if expected else target,
            target_date=target,
        )
        if fresh.empty:
            verified = sorted(suspended & set(expected))
            missing = sorted(set(expected) - suspended)
            attempt = {
                "provider": provider,
                "status": "passed" if verified and not missing else "failed",
                "source": [provider] if verified and not missing else [],
                "captured_at": (
                    str(dict(fresh.attrs.get("suspension_evidence") or {}).get("captured_at") or "")
                    if verified and not missing
                    else ""
                ),
                "observed_dates": [],
                "verified_no_session_dates": verified,
                "missing_dates": missing,
                "warnings": list(warnings or []),
            }
            attempts.append(attempt)
            if attempt["status"] != "passed":
                continue
            return {
                "symbol": symbol,
                "target_date": target,
                "status": "passed",
                "reason_codes": [],
                "operation": "verified_no_session_only",
                "canonical_path": str(canonical_path),
                "canonical_sha256_before": before_sha,
                "canonical_row_count_before": int(len(original)),
                "staging_path": "",
                "staging_sha256": "",
                "staging_row_count": 0,
                "source": provider,
                "captured_at": attempt["captured_at"],
                "price_basis": "raw",
                "schema_valid": True,
                "target_date_complete": True,
                "verified_no_session_dates": verified,
                "provider_attempts": attempts,
            }
        fresh_basis_ok, fresh_basis_reason = _price_basis_gate(symbol, fresh)
        provenance_ok, sources, captured_at = _frame_provenance(
            fresh, approved_sources=approved_sources
        )
        work = fresh.copy()
        work_dates = pd.to_datetime(work.get("date"), errors="coerce")
        work = work.loc[work_dates.notna()].copy()
        work["date"] = work_dates.loc[work_dates.notna()].dt.strftime("%Y-%m-%d")
        work = work.loc[work["date"].isin(expected)].copy()
        observed = set(work["date"].astype(str))
        missing = sorted(set(expected) - observed - suspended)
        attempt = {
            "provider": provider,
            "status": "passed" if fresh_basis_ok and provenance_ok and not missing else "blocked",
            "price_basis_reason": fresh_basis_reason,
            "source": sources,
            "captured_at": captured_at,
            "observed_dates": sorted(observed),
            "verified_no_session_dates": sorted(suspended & set(expected)),
            "missing_dates": missing,
            "warnings": list(warnings or []),
        }
        attempts.append(attempt)
        if attempt["status"] != "passed":
            continue
        if not work.empty:
            work = work.sort_values("date").drop_duplicates("date", keep="last")
            _write_stage_csv(staging_path, work)
            reread = _raw_daily_frame(staging_path)
            stage_basis_ok, _stage_basis_reason = _price_basis_gate(symbol, reread)
            stage_provenance_ok, stage_sources, stage_captured = _frame_provenance(
                reread, approved_sources=approved_sources
            )
            stage_schema = set(STANDARD_DAILY_COLUMNS).issubset(reread.columns)
            stage_dates = _frame_dates(reread)
            if not (
                stage_basis_ok
                and stage_provenance_ok
                and stage_schema
                and stage_dates == sorted(observed)
            ):
                return {
                    "symbol": symbol,
                    "target_date": target,
                    "status": "blocked",
                    "reason_codes": ["staging_file_reaudit_failed"],
                    "operation": "blocked",
                    "canonical_path": str(canonical_path),
                    "canonical_sha256_before": before_sha,
                    "canonical_row_count_before": int(len(original)),
                    "staging_path": str(staging_path),
                    "staging_sha256": _sha256_file(staging_path),
                    "staging_row_count": int(len(reread)),
                    "source": ",".join(stage_sources),
                    "captured_at": stage_captured,
                    "price_basis": "mixed_or_unknown",
                    "schema_valid": False,
                    "target_date_complete": False,
                    "verified_no_session_dates": sorted(suspended & set(expected)),
                    "provider_attempts": attempts,
                }
        return {
            "symbol": symbol,
            "target_date": target,
            "status": "passed",
            "reason_codes": [],
            "operation": "stage_append" if not work.empty else "verified_no_session_only",
            "canonical_path": str(canonical_path),
            "canonical_sha256_before": before_sha,
            "canonical_row_count_before": int(len(original)),
            "staging_path": str(staging_path) if not work.empty else "",
            "staging_sha256": _sha256_file(staging_path) if not work.empty else "",
            "staging_row_count": int(len(work)),
            "source": ",".join(sources),
            "captured_at": captured_at,
            "price_basis": "raw",
            "schema_valid": True,
            "target_date_complete": True,
            "verified_no_session_dates": sorted(suspended & set(expected)),
            "provider_attempts": attempts,
        }
    return {
        "symbol": symbol,
        "target_date": target,
        "status": "blocked",
        "reason_codes": list(dict.fromkeys(reasons + ["all_daily_providers_failed_quality_gate"])),
        "operation": "blocked",
        "canonical_path": str(canonical_path),
        "canonical_sha256_before": before_sha,
        "canonical_row_count_before": int(len(original)),
        "staging_path": "",
        "staging_sha256": "",
        "staging_row_count": 0,
        "source": "",
        "captured_at": "",
        "price_basis": "mixed_or_unknown",
        "schema_valid": False,
        "target_date_complete": False,
        "verified_no_session_dates": [],
        "provider_attempts": attempts,
    }


def _stage_process_worker(symbol: str, context: dict[str, Any]) -> dict[str, Any]:
    """Stage one symbol inside a killable worker process.

    A provider that hangs on a socket cannot be interrupted from the parent
    thread, so the staging fan-out must run in processes the deadline helper is
    able to terminate. Only the default ``fetch_daily`` is reachable here: the
    worker is spawned, so an injected fetcher would not survive pickling and the
    caller stays in-process in that case.
    """

    return _stage_one_symbol(
        symbol,
        target=str(context["target"]),
        canonical_path=Path(str(context["canonical_dir"])) / f"{symbol}.csv",
        staging_path=Path(str(context["staging_dir"])) / f"{symbol}.csv",
        calendar=dict(context["calendar"]),
        approved_sources=set(context["approved_sources"]),
        provider_order=tuple(context["provider_order"]),
        provider_fetcher=fetch_daily,
    )


_STAGE_PROCESS_WORKER = _stage_process_worker


def _timed_out_stage_item(
    symbol: str,
    *,
    target: str,
    canonical_path: Path,
    reason: str,
) -> dict[str, Any]:
    """Record an unfinished symbol as blocked without inventing any prices."""

    return {
        "symbol": symbol,
        "target_date": target,
        "status": "blocked",
        "reason_codes": [reason],
        "operation": "blocked",
        "canonical_path": str(canonical_path),
        "canonical_sha256_before": _sha256_file(canonical_path),
        "canonical_row_count_before": int(len(_raw_daily_frame(canonical_path))),
        "staging_path": "",
        "staging_sha256": "",
        "staging_row_count": 0,
        "source": "",
        "captured_at": "",
        "price_basis": "mixed_or_unknown",
        "schema_valid": False,
        "target_date_complete": False,
        "verified_no_session_dates": [],
        "provider_attempts": [],
    }


def _comparable_daily_rows(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    numeric_columns = {"open", "high", "low", "close", "volume", "amount"}

    def value(row: Mapping[str, Any], column: str) -> str:
        text = str(row.get(column) or "").strip()
        if column not in numeric_columns:
            return text.lower() if column == "adjust" else text
        try:
            return format(Decimal(text).normalize(), "f")
        except InvalidOperation:
            return text

    return sorted(
        tuple(value(row, column) for column in _DAILY_COMPARE_COLUMNS)
        for row in rows
    )


def _candidate_bytes(original: bytes, header: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(header), extrasaction="ignore", lineterminator="\n")
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in header})
    separator = b"" if not original or original.endswith((b"\n", b"\r")) else b"\n"
    return original + separator + buffer.getvalue().encode("utf-8")


def _header_is_unique_and_complete(
    header: Sequence[str], required_columns: Sequence[str]
) -> bool:
    columns = list(header)
    return len(columns) == len(set(columns)) and set(required_columns) <= set(columns)


def apply_audited_weekly_daily_batch(
    *,
    project_root: Path,
    contract: Mapping[str, Any],
    stage_audit: Mapping[str, Any],
    run_dir: Path,
    apply: bool,
    replace_func: Callable[[Path, Path], Any] = os.replace,
) -> dict[str, Any]:
    """Reaudit disk evidence, then append atomically with whole-pool rollback."""

    root = Path(project_root).resolve(strict=True)
    run_root = _descendant(root, Path(run_dir), role="run_dir")
    canonical_dir = _descendant(
        root,
        root / str(dict(contract.get("paths") or {}).get("canonical_daily_dir") or ""),
        role="canonical_daily_dir",
    )
    reasons = validate_weekly_daily_contract(contract, project_root=root)
    expected = list(contract.get("symbols") or [])
    stage_items, stage_invalid = _items_by_symbol(stage_audit.get("items"))
    if stage_invalid or sorted(stage_items) != expected:
        reasons.append("stage_symbol_set_mismatch")
    if stage_audit.get("status") != "passed":
        reasons.append("stage_full_pool_quality_not_passed")
    rules = dict(contract.get("rules") or {})
    approved_sources = {str(item) for item in rules.get("approved_daily_sources") or []}
    required_columns = list(
        rules.get("canonical_daily_required_columns") or STANDARD_DAILY_COLUMNS
    )
    plans: list[dict[str, Any]] = []
    candidates_dir = run_root / "canonical_candidates"
    backups_dir = run_root / "canonical_backups"
    for symbol in expected:
        item_reasons: list[str] = []
        item = stage_items.get(symbol)
        if not item or item.get("status") != "passed":
            item_reasons.append("stage_item_not_passed")
            reasons.extend(item_reasons)
            continue
        canonical_path = canonical_dir / f"{symbol}.csv"
        actual_before_sha = _sha256_file(canonical_path) if canonical_path.is_file() else ""
        if actual_before_sha != str(item.get("canonical_sha256_before") or ""):
            item_reasons.append("canonical_changed_after_stage")
            reasons.extend(item_reasons)
            continue
        canonical = _raw_daily_frame(canonical_path)
        basis_ok, _basis_reason = _price_basis_gate(symbol, canonical)
        if not basis_ok:
            item_reasons.append("canonical_daily_price_basis_not_raw")
            reasons.extend(item_reasons)
            continue
        operation = str(item.get("operation") or "")
        if operation in {"already_current", "verified_no_session_only"}:
            plans.append(
                {
                    "symbol": symbol,
                    "operation": operation,
                    "canonical_path": str(canonical_path),
                    "canonical_sha256_before": actual_before_sha,
                    "canonical_sha256_after": actual_before_sha,
                    "canonical_row_count_before": int(len(canonical)),
                    "canonical_row_count_after": int(len(canonical)),
                }
            )
            continue
        staging_path = Path(str(item.get("staging_path") or ""))
        try:
            staging_path = _descendant(run_root, staging_path, role="staging_path")
        except ValueError:
            item_reasons.append("staging_path_invalid")
            reasons.extend(item_reasons)
            continue
        if not staging_path.is_file() or staging_path.is_symlink():
            item_reasons.append("staging_file_missing_or_not_regular")
            reasons.extend(item_reasons)
            continue
        actual_stage_sha = _sha256_file(staging_path)
        if actual_stage_sha != str(item.get("staging_sha256") or ""):
            item_reasons.append("staging_hash_mismatch")
            reasons.extend(item_reasons)
            continue
        staged = _raw_daily_frame(staging_path)
        stage_basis_ok, _stage_basis_reason = _price_basis_gate(symbol, staged)
        provenance_ok, _sources, _captured = _frame_provenance(
            staged, approved_sources=approved_sources
        )
        if (
            not stage_basis_ok
            or not provenance_ok
            or not _header_is_unique_and_complete(list(staged.columns), required_columns)
        ):
            item_reasons.append("staging_file_reaudit_failed")
            reasons.extend(item_reasons)
            continue
        with canonical_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            canonical_rows = list(reader)
        with staging_path.open("r", newline="", encoding="utf-8-sig") as handle:
            stage_reader = csv.DictReader(handle)
            stage_header = list(stage_reader.fieldnames or [])
            staged_rows = list(stage_reader)
        canonical_header_valid = _header_is_unique_and_complete(header, required_columns)
        staging_header_valid = _header_is_unique_and_complete(stage_header, required_columns)
        if not canonical_header_valid:
            item_reasons.append("canonical_header_not_append_compatible")
            reasons.extend(item_reasons)
            continue
        if not staging_header_valid:
            item_reasons.append("staging_header_not_append_compatible")
            reasons.extend(item_reasons)
            continue
        if set(header) != set(stage_header):
            item_reasons.append("canonical_staging_header_mismatch")
            reasons.extend(item_reasons)
            continue
        existing_by_date: dict[str, list[dict[str, Any]]] = {}
        for row in canonical_rows:
            existing_by_date.setdefault(str(row.get("date") or "")[:10], []).append(row)
        append_rows: list[dict[str, Any]] = []
        for row in staged_rows:
            row_date = str(row.get("date") or "")[:10]
            existing = existing_by_date.get(row_date) or []
            if existing:
                if _comparable_daily_rows(existing) != _comparable_daily_rows([row]):
                    item_reasons.append("canonical_daily_date_conflict")
                continue
            append_rows.append(row)
        if item_reasons:
            reasons.extend(item_reasons)
            continue
        original = canonical_path.read_bytes()
        candidate = candidates_dir / f"{symbol}.csv"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(_candidate_bytes(original, header, append_rows))
        plans.append(
            {
                "symbol": symbol,
                "operation": "append_dates" if append_rows else "unchanged_identical_dates",
                "canonical_path": str(canonical_path),
                "candidate_path": str(candidate),
                "canonical_sha256_before": actual_before_sha,
                "canonical_row_count_before": int(len(canonical_rows)),
                "planned_append_row_count": len(append_rows),
                "staging_path": str(staging_path),
                "staging_sha256": actual_stage_sha,
            }
        )
    reasons = list(dict.fromkeys(reasons))
    base = {
        "schema_version": "weekly_daily_append_import_v1",
        "task_type": "weekly_daily_wide_pool_canonical_import",
        "contract_sha256": contract.get("contract_sha256"),
        "target_date": contract.get("target_date"),
        "requested_symbol_count": len(expected),
        "items": plans,
        "applied": False,
        "rollback_performed": False,
        "reason_codes": reasons,
    }
    if reasons or len(plans) != len(expected):
        return {**base, "status": "blocked", "quarantined": True}
    if not apply:
        return {**base, "status": "ready", "quarantined": False}
    changed = [item for item in plans if item.get("operation") == "append_dates"]
    backups_dir.mkdir(parents=True, exist_ok=True)
    replaced: list[dict[str, Any]] = []
    try:
        for item in changed:
            target = Path(str(item["canonical_path"]))
            candidate = Path(str(item["candidate_path"]))
            backup = backups_dir / target.name
            shutil.copy2(target, backup)
            item["backup_path"] = str(backup)
            replace_func(candidate, target)
            replaced.append(item)
        for item in plans:
            target = Path(str(item["canonical_path"]))
            item["canonical_sha256_after"] = _sha256_file(target)
            item["canonical_row_count_after"] = int(len(_raw_daily_frame(target)))
            if item.get("operation") == "append_dates":
                expected_rows = int(item["canonical_row_count_before"]) + int(item["planned_append_row_count"])
                if item["canonical_row_count_after"] != expected_rows:
                    raise ValueError(f"post_import_row_count_mismatch:{item['symbol']}")
    except Exception as exc:  # noqa: BLE001 - rollback is the safety boundary
        rollback_errors: list[str] = []
        for item in reversed(replaced):
            target = Path(str(item["canonical_path"]))
            backup = Path(str(item["backup_path"]))
            try:
                restore = backup.with_suffix(".restore")
                shutil.copy2(backup, restore)
                os.replace(restore, target)
                if _sha256_file(target) != str(item["canonical_sha256_before"]):
                    raise ValueError("rollback_hash_mismatch")
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(f"{item['symbol']}:{type(rollback_exc).__name__}:{rollback_exc}")
        return {
            **base,
            "status": "failed",
            "quarantined": True,
            "reason_codes": ["canonical_append_transaction_failed"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
            "rollback_performed": not rollback_errors,
            "rollback_verified": not rollback_errors,
            "rollback_errors": rollback_errors,
            "items": plans,
        }
    return {
        **base,
        "status": "completed",
        "quarantined": False,
        "applied": True,
        "completed_at": now_text(),
        "reason_codes": [],
        "items": plans,
    }


def _run_id(value: str | None) -> str:
    if value:
        text = str(value).strip()
        if not _SAFE_RUN_ID_PATTERN.fullmatch(text) or text in {".", ".."}:
            raise ValueError(f"invalid_run_id:{value}")
        return text
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"weekly-daily-wide-pool-{stamp}-{uuid4().hex[:10]}"


def _execute_weekly_daily_locked(
    *,
    root: Path,
    target: str,
    contract: Mapping[str, Any],
    frozen_contract_evidence: Mapping[str, Any],
    base: Mapping[str, Any],
    current_run_id: str,
    worker_count: int,
    provider_order: Sequence[str],
    provider_fetcher: Callable[..., tuple[pd.DataFrame, list[str]]],
    replace_func: Callable[[Path, Path], Any],
    apply: bool,
) -> dict[str, Any]:
    results_root = _descendant(
        root,
        root / "results" / "data_chain" / "weekly_daily_wide_pool" / "runs",
        role="results_root",
    )
    run_dir = results_root / current_run_id
    canonical_dir = root / str(dict(contract.get("paths") or {}).get("canonical_daily_dir"))
    approved_sources = {
        str(item) for item in dict(contract.get("rules") or {}).get("approved_daily_sources") or []
    }
    run_dir.mkdir(parents=True, exist_ok=False)
    contract_path = write_weekly_daily_wide_pool_contract(
        contract, run_dir / "pool_contract.json", allowed_root=run_dir
    )
    calendar = load_cn_trading_calendar()
    symbols = list(contract.get("symbols") or [])

    def stage_symbol(symbol: str) -> dict[str, Any]:
        return _stage_one_symbol(
            symbol,
            target=target,
            canonical_path=canonical_dir / f"{symbol}.csv",
            staging_path=run_dir / "staging" / f"{symbol}.csv",
            calendar=calendar,
            approved_sources=approved_sources,
            provider_order=provider_order,
            provider_fetcher=provider_fetcher,
        )

    timeout_events: list[dict[str, Any]] = []
    if worker_count == 1 or len(symbols) <= 1:
        items = [stage_symbol(symbol) for symbol in symbols]
    elif provider_fetcher is not fetch_daily:
        # An injected fetcher (tests) is not picklable and does no network I/O,
        # so it cannot hang on a socket. Run it in-process.
        with ThreadPoolExecutor(
            max_workers=min(worker_count, len(symbols)),
            thread_name_prefix="weekly-daily-wide",
        ) as executor:
            items = list(executor.map(stage_symbol, symbols))
    else:
        # A provider that stops responding mid-socket cannot be interrupted in a
        # thread: the 2026-08-03 run produced 70 of 4603 symbols and then sat
        # idle for 28 minutes holding the shared lock, because every worker
        # thread was parked inside a provider call that never returned. Run the
        # network stage in killable subprocesses under a hard deadline instead,
        # the same contract daily_price_update_engine already uses.
        stage_result = run_process_tasks_until_deadline(
            _STAGE_PROCESS_WORKER,
            symbols,
            context={
                "target": target,
                "canonical_dir": str(canonical_dir),
                "staging_dir": str(run_dir / "staging"),
                "calendar": dict(calendar),
                "approved_sources": sorted(approved_sources),
                "provider_order": list(provider_order),
            },
            max_workers=min(worker_count, len(symbols)),
            timeout_seconds=DAILY_PROVIDER_GLOBAL_TIMEOUT_SECONDS,
        )
        items = []
        for index, symbol in enumerate(symbols):
            if index in stage_result.results:
                items.append(stage_result.results[index])
                continue
            # An unfinished or crashed symbol is reported as blocked with its own
            # reason code. It is never silently dropped: the whole-pool gate must
            # see it, so a timeout can only fail the batch, never shrink it.
            reason = (
                f"weekly_daily_stage_worker_failed:{stage_result.failures[index]}"
                if index in stage_result.failures
                else "weekly_daily_stage_timeout"
                if stage_result.timed_out
                else "weekly_daily_stage_worker_exit"
            )
            items.append(
                _timed_out_stage_item(
                    symbol,
                    target=target,
                    canonical_path=canonical_dir / f"{symbol}.csv",
                    reason=reason,
                )
            )
        if stage_result.timed_out:
            timeout_events.append(
                {
                    "scope": "global_budget",
                    "timeout_seconds": DAILY_PROVIDER_GLOBAL_TIMEOUT_SECONDS,
                    "unfinished_symbol_count": len(stage_result.unfinished_indices),
                    "unfinished_symbols": [
                        symbols[index]
                        for index in stage_result.unfinished_indices[:50]
                    ],
                    "reason": "weekly_daily_stage_timeout",
                }
            )
    blocked = sum(item.get("status") != "passed" for item in items)
    stage_audit = {
        "schema_version": "weekly_daily_stage_audit_v1",
        "task_type": "weekly_daily_wide_pool_stage_audit",
        "run_id": current_run_id,
        "target_date": target,
        "contract_sha256": contract.get("contract_sha256"),
        "status": "passed" if not blocked else "blocked",
        "summary": {
            "requested": len(items),
            "passed": len(items) - blocked,
            "blocked": blocked,
        },
        "items": items,
    }
    atomic_write_json(run_dir / "stage_audit.json", stage_audit)
    import_payload = apply_audited_weekly_daily_batch(
        project_root=root,
        contract=contract,
        stage_audit=stage_audit,
        run_dir=run_dir,
        apply=apply,
        replace_func=replace_func,
    )
    status = (
        "completed"
        if import_payload.get("status") == "completed"
        else "completed_staging_only"
        if import_payload.get("status") == "ready" and not apply
        else "blocked"
    )
    payload = {
        **base,
        "run_id": current_run_id,
        "run_dir": str(run_dir),
        "contract_path": str(contract_path),
        "frozen_contract": dict(frozen_contract_evidence),
        "status": status,
        "workers": worker_count,
        "reason_codes": list(import_payload.get("reason_codes") or []),
        "stage_audit": stage_audit,
        "canonical_import": import_payload,
        "canonical_write": import_payload.get("applied") is True,
        "completed_at": now_text(),
    }
    atomic_write_json(run_dir / "manifest.json", payload)
    return payload


def run_weekly_daily_wide_pool(
    project_root: Path,
    *,
    target_date: str,
    network: bool = False,
    apply: bool = False,
    run_id: str | None = None,
    explicit_exclusions: Sequence[str] = (),
    exclusion_policy_path: Path | None = None,
    workers: int = 8,
    lock_timeout_seconds: float = 0,
    provider_order: Sequence[str] = DEFAULT_DAILY_PROVIDER_ORDER,
    provider_fetcher: Callable[..., tuple[pd.DataFrame, list[str]]] = fetch_daily,
    replace_func: Callable[[Path, Path], Any] = os.replace,
) -> dict[str, Any]:
    """Run contract -> staging -> disk audit -> append-only import under one lock."""

    root = Path(project_root).resolve(strict=True)
    target = _target_date(target_date)
    worker_count = int(workers)
    if worker_count < 1 or worker_count > 8:
        raise ValueError("workers_outside_bounded_range:1..8")
    lock_timeout = float(lock_timeout_seconds)
    if lock_timeout < 0 or lock_timeout > 21600:
        raise ValueError("lock_timeout_seconds_outside_bounded_range:0..21600")
    preliminary = {
        "schema_version": "weekly_daily_wide_pool_run_v1",
        "task_type": "weekly_daily_wide_pool",
        "target_date": target,
        "network": bool(network),
        "apply": bool(apply),
        "contract_sha256": "",
        "canonical_write": False,
        "disclaimer": DISCLAIMER,
    }
    if apply and not network:
        return {**preliminary, "status": "blocked", "reason_codes": ["apply_requires_network"]}
    if exclusion_policy_path is not None and explicit_exclusions:
        # 两条排除入口同时给出无法判定谁是权威，直接拒绝而不是隐式挑一个。
        return {
            **preliminary,
            "status": "blocked",
            "reason_codes": ["exclusion_policy_invalid"],
            "error": "explicit_exclusions_conflict_with_policy",
        }
    exclusion_audit: dict[str, Any] | None = None
    configured_exclusions = list(explicit_exclusions)
    if exclusion_policy_path is not None:
        try:
            exclusion_audit = load_weekly_daily_exclusion_policy(
                root, policy_path=exclusion_policy_path, target_date=target
            )
            configured_exclusions = list(exclusion_audit["symbols"])
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                **preliminary,
                "status": "blocked",
                "reason_codes": ["exclusion_policy_invalid"],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
    lock_path = _descendant(root, root / "state" / "daily_data_chain.lock", role="daily_chain_lock")
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=lock_timeout):
            contract = build_weekly_daily_wide_pool_contract(
                root,
                target_date=target,
                explicit_exclusions=configured_exclusions,
            )
            if exclusion_audit is not None:
                contract["exclusion_audit"] = exclusion_audit
            frozen_contract: dict[str, Any] = {}
            if network and contract.get("formal_eligible") is True:
                contract, frozen_contract = freeze_weekly_daily_wide_pool_contract(
                    root, contract
                )
            base = {
                **preliminary,
                "contract_sha256": contract.get("contract_sha256"),
                "frozen_contract": frozen_contract,
            }
            if not network:
                return {
                    **base,
                    "status": "planned_offline",
                    "reason_codes": ["network_disabled"],
                    "contract": contract,
                }
            if contract.get("formal_eligible") is not True:
                return {
                    **base,
                    "status": "blocked",
                    "reason_codes": ["pool_contract_not_formal_eligible"],
                    "contract": contract,
                }
            if frozen_contract.get("status") == "conflict":
                return {
                    **base,
                    "status": "blocked",
                    "reason_codes": list(frozen_contract.get("reason_codes") or []),
                    "contract": contract,
                }
            return _execute_weekly_daily_locked(
                root=root,
                target=target,
                contract=contract,
                frozen_contract_evidence=frozen_contract,
                base=base,
                current_run_id=_run_id(run_id),
                worker_count=worker_count,
                provider_order=provider_order,
                provider_fetcher=provider_fetcher,
                replace_func=replace_func,
                apply=apply,
            )
    except TimeoutError:
        return {**preliminary, "status": "busy", "reason_codes": ["daily_data_chain_lock_busy"]}


def run_weekly_wide_pool_maintenance(
    project_root: Path,
    *,
    target_date: str,
    network: bool = False,
    apply: bool = False,
    run_id: str | None = None,
    daily_workers: int = 8,
    minute_batch_size: int = 1,
    daily_provider_fetcher: Callable[..., tuple[pd.DataFrame, list[str]]] = fetch_daily,
    minute_runner: Callable[..., Mapping[str, Any]] = run_weekly_minute_wide_pool,
) -> dict[str, Any]:
    """Run daily-wide and 555-minute maintenance as separately reported legs."""

    root = Path(project_root).resolve(strict=True)
    target = _target_date(target_date)
    current_run_id = _run_id(run_id).replace("weekly-daily-wide-pool", "weekly-wide-pool")
    daily_policy = root / "config" / "weekly_daily_wide_pool_exclusions.json"
    daily_payload = run_weekly_daily_wide_pool(
        root,
        target_date=target,
        network=network,
        apply=apply,
        run_id=f"{current_run_id}-daily",
        workers=daily_workers,
        provider_fetcher=daily_provider_fetcher,
        exclusion_policy_path=daily_policy if daily_policy.is_file() else None,
    )
    minute_payload = dict(
        minute_runner(
            root,
            target_date=target,
            network=network,
            apply=apply,
            batch_size=minute_batch_size,
            run_id=f"{current_run_id}-minute",
            exclusion_policy_path=(
                root / "config" / "weekly_minute_wide_pool_exclusions.json"
                if (root / "config" / "weekly_minute_wide_pool_exclusions.json").is_file()
                else None
            ),
        )
    )
    daily_status = str(daily_payload.get("status") or "")
    minute_status = str(minute_payload.get("status") or "")
    expected_status = "completed" if apply else "completed_staging_only" if network else "planned_offline"
    status = (
        expected_status
        if daily_status == expected_status and minute_status == expected_status
        else "partial"
    )
    payload = {
        "schema_version": "weekly_wide_pool_maintenance_v1",
        "task_type": "weekly_wide_pool_maintenance",
        "run_id": current_run_id,
        "target_date": target,
        "network": bool(network),
        "apply": bool(apply),
        "status": status,
        "daily_wide_pool": daily_payload,
        "minute_residual_pool": minute_payload,
        "counts": {
            "daily_wide": int(
                dict(daily_payload.get("contract") or {}).get("counts", {}).get(
                    "weekly_daily_wide", 0
                )
                if isinstance(daily_payload.get("contract"), Mapping)
                else int(
                    dict(daily_payload.get("stage_audit") or {})
                    .get("summary", {})
                    .get("requested", 0)
                )
            ),
            "minute_residual": int(
                dict(minute_payload.get("pool_contract") or {})
                .get("counts", {})
                .get("weekly_wide", 0)
            ),
        },
        "count_scopes_must_not_be_combined": True,
        "disclaimer": DISCLAIMER,
    }
    if network:
        combined_root = root / "results" / "data_chain" / "weekly_wide_pool" / "runs" / current_run_id
        combined_root.mkdir(parents=True, exist_ok=False)
        atomic_write_json(combined_root / "manifest.json", payload)
        payload["manifest_path"] = str(combined_root / "manifest.json")
    return payload


__all__ = [
    "WEEKLY_DAILY_WIDE_POOL_CONTRACT_VERSION",
    "WEEKLY_DAILY_WIDE_POOL_DEFINITION",
    "build_weekly_daily_wide_pool_contract",
    "validate_weekly_daily_contract",
    "write_weekly_daily_wide_pool_contract",
    "coordinate_weekly_daily_update",
    "apply_audited_weekly_daily_batch",
    "run_weekly_daily_wide_pool",
    "run_weekly_wide_pool_maintenance",
]
