"""One authoritative post-close data chain for QuantDesk.

The chain is local, research-only and paper-only. It coordinates market-data
capture and quality evidence; it never connects to a broker or mutates a paper
portfolio.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_candidate_refresh import (
    candidate_refresh_gate,
    publish_archived_conditional_plan,
    run_formal_candidate_refresh,
)
from quantdesk.daily_feature_engine import build_daily_features
from quantdesk.daily_gap_recovery import recover_target_day_gaps, merge_recovered_daily_status
from quantdesk.daily_plan_freeze import (
    REQUIRED_DATA_CHAIN_CONTRACT_VERSION,
    freeze_t_plus_one_plan,
)
from quantdesk.daily_price_update_engine import (
    DEFAULT_PROVIDERS,
    infer_target_date,
    local_daily_status_for_symbols,
    resolve_symbols,
    update_daily_prices,
)
from quantdesk.price_basis import classify_frame_price_basis
from quantdesk.data_close_upstream_contract import validate_upstream_reuse_manifest
from quantdesk.data_truth.benchmark_incremental_update import update_benchmark_files
from quantdesk.data_truth.sector_history_archive import (
    archive_forward_sector_snapshot,
    capture_current_sector_membership,
    verify_snapshot_self_hash,
)
from quantdesk.intraday.intraday_gap_checker import collect_intraday_required_symbols
from quantdesk.intraday.intraday_providers import DEFAULT_INTRADAY_PROVIDER_ORDER
from quantdesk.intraday.intraday_quality_audit import audit_intraday_batch
from quantdesk.intraday.intraday_update_engine import update_intraday_1m_for_symbols
from quantdesk.intraday.stage6_append_only_import import apply_audited_intraday_batch
from quantdesk.intraday.stage6_conflict_resolution import (
    resolve_stage6_intraday_conflicts,
)
from quantdesk.session_expectation import build_session_expectation
from quantdesk.shared import normalize_symbol, now_text, read_json
from quantdesk.trading_calendar import load_a_share_trading_calendar, trade_days_between
from quantdesk.universe_refresh import (
    build_default_fetchers,
    refresh_universe_snapshot,
)
from quantdesk.utils import atomic_write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

_CANONICAL_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = _CANONICAL_PROJECT_ROOT
DAILY_PROVIDER_ORDER = tuple(DEFAULT_PROVIDERS)
# Hard provider budget for one formal daily acquisition stage. The global budget
# bounds the whole fallback chain; each per-provider slice retires only that
# provider so the remaining fallback chain still runs. Values are seconds.
DAILY_PROVIDER_GLOBAL_TIMEOUT_SECONDS = 6600.0
DAILY_PROVIDER_TIMEOUT_OVERRIDES: dict[str, float] = {
    "mootdx_daily": 6000.0,
    "baostock_daily": 180.0,
    "tencent_daily": 300.0,
    "akshare_daily": 300.0,
    "sina_daily": 300.0,
}
# Network acquisition is deliberately conservative at the scheduler boundary.
# Analysis batches may remain parallel, but provider requests must be serial and
# spaced so a transient host/provider failure cannot become a request storm.
DAILY_NETWORK_WORKERS = 1
DAILY_NETWORK_SLEEP_SECONDS = 1.0
INTRADAY_PROVIDER_ORDER = tuple(DEFAULT_INTRADAY_PROVIDER_ORDER)
# CODEX_TASK_quantdesk_ai_fund_manager_v1.md section 14.3 requires an entire
# quality-failing import batch to remain in staging. A one-symbol default keeps
# that fail-closed boundary without quarantining unrelated complete symbols;
# callers may still choose a larger explicitly reviewed transaction batch.
DEFAULT_BATCH_SIZE = 1
SECTION15_CONFLICT_DATE = "2026-07-14"
SECTION15_CONFLICT_SYMBOLS = ("002115", "300323", "600744", "600958", "601975")
DAILY_DATA_CHAIN_CONTRACT_VERSION = REQUIRED_DATA_CHAIN_CONTRACT_VERSION
RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION = "recovery_source_selection.v1"
RECOVERABLE_SOURCE_STATUSES = frozenset({"blocked"})
DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / Live Execution Locked / "
    "QMT Order Route Locked / Not Investment Advice / Not Position Advice."
)
_THREAD_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _daily_update_rate_contract(
    *,
    network: bool,
    dry_run: bool,
    requested_workers: int,
) -> tuple[int, float]:
    """Return the effective daily-provider worker count and inter-symbol delay."""

    bounded_workers = max(1, min(int(requested_workers or 1), 8))
    if bool(network) and not bool(dry_run):
        return DAILY_NETWORK_WORKERS, DAILY_NETWORK_SLEEP_SECONDS
    return bounded_workers, 0.0


def _reused_daily_payload(
    root: Path, *, target_date: str, symbols: Sequence[str], source_run_id: str
) -> dict[str, Any]:
    rows = [{"symbol": symbol} for symbol in _normalized_symbols(symbols)]
    status = local_daily_status_for_symbols(
        rows,
        target_date=target_date,
        daily_dir=root / "data/daily",
        metadata_dir=root / "data/metadata",
    )
    fresh = list(status.get("fresh_symbols") or [])
    stale = list(status.get("stale_symbols") or [])
    missing = list(status.get("missing_symbols") or [])
    latest = dict(status.get("latest_by_symbol") or {})
    invalid: dict[str, str] = {}
    raw_positive: list[str] = []
    raw_zero: list[str] = []
    # The freshness reader normalizes dates; classify original rows separately
    # before passing bounded session evidence to the downstream fast reader.
    for row in rows:
        symbol = row["symbol"]
        path = root / "data/daily" / f"{symbol}.csv"
        if not path.is_file():
            continue
        frame = read_csv_frame(path, dtype=str)
        basis, reason = classify_frame_price_basis(frame)
        if frame.attrs.get("warnings"):
            invalid[symbol] = "original_csv_read_failed"
            continue
        if reason or basis != "raw":
            invalid[symbol] = reason or "consumer_price_basis_not_allowed"
            continue
        if "date" not in frame.columns or "volume" not in frame.columns:
            continue
        dates = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        target_rows = frame.loc[dates.eq(target_date)]
        if target_rows.empty:
            continue
        if "symbol" in target_rows.columns and not target_rows["symbol"].map(normalize_symbol).eq(symbol).all():
            invalid[symbol] = "target_symbol_mismatch"
            continue
        volumes = pd.to_numeric(target_rows["volume"], errors="coerce")
        if volumes.isna().any() or not volumes.ge(0).all() or not volumes.lt(float("inf")).all():
            continue
        if volumes.gt(0).any():
            raw_positive.append(symbol)
        else:
            raw_zero.append(symbol)
    fresh = sorted(set(fresh) - set(invalid))
    stale = sorted(set(stale) | set(invalid))
    return {
        "ok": not stale and not missing,
        "status": "partial" if stale or missing else "completed",
        "source_status": "reused_verified_upstream",
        "operation": "local_input_audit",
        "upstream_reuse": True,
        "source_run_id": source_run_id,
        "provider_calls": 0,
        "network": False,
        "dry_run": False,
        "target_date": target_date,
        "total_symbols": len(rows),
        "symbols_checked": len(rows),
        "processed_symbols": [row["symbol"] for row in rows],
        "fresh_symbol_list_after": fresh,
        "stale_symbol_list_after": stale,
        "missing_symbol_list_after": missing,
        "require_raw_price_basis": True,
        "failed_symbol_list": sorted(invalid),
        "failed_symbols": len(invalid),
        "invalid_price_basis_symbols": sorted(invalid),
        "invalid_price_basis_reasons": invalid,
        "stale_count_after": len(stale),
        "missing_count_after": len(missing),
        "latest_by_symbol_after": latest,
        "raw_positive_volume_target_symbols": len(raw_positive),
        "raw_positive_volume_target_symbol_list": raw_positive,
        "raw_zero_volume_target_symbols": len(raw_zero),
        "raw_zero_volume_target_symbol_list": raw_zero,
        "warnings": list(status.get("warnings") or []),
        "errors": [],
    }


def resolve_upstream_reuse_steps(
    *,
    project_root: str | Path,
    target_date: str,
    upstream_reuse: Mapping[str, Any] | None,
    daily_symbols: Sequence[str],
    universe_writer: Callable[[], Mapping[str, Any]],
    daily_writer: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    if upstream_reuse is None:
        return {
            "reused": False,
            "universe": dict(universe_writer()),
            "daily": dict(daily_writer()),
            "reason_codes": [],
        }
    ok, reasons, normalized = validate_upstream_reuse_manifest(
        upstream_reuse, target_date=target_date, project_root=root
    )
    if not ok:
        return {
            "reused": False,
            "blocked": True,
            "universe": {},
            "daily": {},
            "reason_codes": list(reasons),
        }
    return {
        "reused": True,
        "universe": {
            "ok": True,
            "status": "skipped",
            "formal_eligible": True,
            "reason": "verified_upstream_reuse",
            "target_date": target_date,
            "source_run_id": normalized["run_id"],
        },
        "daily": _reused_daily_payload(
            root,
            target_date=target_date,
            symbols=daily_symbols,
            source_run_id=normalized["run_id"],
        ),
        "reason_codes": [],
        "normalized": normalized,
    }


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"daily-data-chain-{stamp}-{uuid.uuid4().hex[:10]}"


def _normalized_symbols(values: Sequence[Any]) -> list[str]:
    return sorted(
        {
            symbol
            for value in values
            if (symbol := normalize_symbol(value.get("symbol") if isinstance(value, Mapping) else value))
        }
    )


def build_universe_contract(
    rows: Sequence[Any],
    *,
    target_date: str,
    name: str,
) -> dict[str, Any]:
    symbols = _normalized_symbols(rows)
    canonical = json.dumps(
        {"name": name, "target_date": str(target_date)[:10], "symbols": symbols},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "name": name,
        "target_date": str(target_date)[:10],
        "symbol_count": len(symbols),
        "symbols": symbols,
        "sha256": digest,
        "version": f"{name}:{str(target_date)[:10]}:{digest[:12]}",
    }


def _probe_modules(executable: Path, modules: tuple[str, ...]) -> dict[str, bool]:
    try:
        if executable.resolve() == Path(sys.executable).resolve():
            return {name: importlib.util.find_spec(name) is not None for name in modules}
    except OSError:
        pass
    code = (
        "import importlib.util,json;"
        f"m={modules!r};"
        "print(json.dumps({x:importlib.util.find_spec(x) is not None for x in m}))"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        payload = {}
    return {name: bool(payload.get(name)) for name in modules}


def build_runtime_preflight(
    project_root: Path,
    *,
    current_executable: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    module_probe: Callable[[Path, tuple[str, ...]], Mapping[str, bool]] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    environment = dict(os.environ if env is None else env)
    current = Path(current_executable or sys.executable)
    candidates: list[tuple[str, Path]] = []
    configured = str(environment.get("QUANTDESK_PYTHON") or "").strip()
    if configured:
        candidates.append(("environment", Path(configured)))
    candidates.append(("project_venv", root / ".venv" / "Scripts" / "python.exe"))
    candidates.append(("current_interpreter", current))
    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for source, path in candidates:
        key = str(path).lower()
        if key not in seen and path.exists():
            seen.add(key)
            unique.append((source, path))

    probe = module_probe or _probe_modules
    required = ("pandas", "mootdx")
    optional = ("akshare", "baostock")
    checked: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    for source, executable in unique:
        modules = dict(probe(executable, required + optional))
        row = {
            "runtime_source": source,
            "executable": str(executable),
            "modules": {name: bool(modules.get(name)) for name in required + optional},
        }
        row["ready"] = all(row["modules"].get(name) for name in required)
        checked.append(row)
        if selected is None and row["ready"]:
            selected = row

    reason_codes: list[str] = []
    if selected is None:
        reason_codes.append("required_module_missing:mootdx")
        if not unique:
            reason_codes.append("python_runtime_not_found")
    current_ready = next(
        (
            bool(row["ready"])
            for row in checked
            if Path(row["executable"]).resolve() == current.resolve()
        ),
        False,
    )
    result = {
        "status": "passed" if selected else "blocked",
        "ready": selected is not None,
        "selected_executable": str(selected["executable"]) if selected else "",
        "runtime_source": str(selected["runtime_source"]) if selected else "",
        "modules": dict(selected["modules"]) if selected else {},
        "active_executable": str(current),
        "active_runtime_ready": current_ready,
        "checked_runtimes": checked,
        "reason_codes": reason_codes,
    }
    if selected and not current_ready:
        result["reason_codes"] = ["active_runtime_missing_required_modules"]
        result["next_action"] = (
            f'请使用 "{selected["executable"]}" 运行 scripts/qd.py daily-data-chain。'
        )
    return result


def _package_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("mootdx", "akshare", "baostock", "pandas"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = ""
    return result


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)




def _lock_is_held(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            return False
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            return False
        except OSError:
            return True
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _daily_chain_lock(
    project_root: Path,
    run_id: str,
    *,
    target_date: str = "",
) -> Iterator[dict[str, Any]]:
    root_key = str(Path(project_root).resolve()).lower()
    with _THREAD_LOCK_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(root_key, threading.Lock())
    thread_acquired = thread_lock.acquire(blocking=False)
    state_dir = Path(project_root) / "state"
    lock_path = state_dir / "daily_data_chain.lock"
    metadata_path = state_dir / "daily_data_chain.lock.json"
    if not thread_acquired:
        active = read_json(metadata_path, {})
        yield {
            "acquired": False,
            "active_run_id": str(active.get("run_id") or ""),
            "lock_path": str(lock_path),
        }
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        thread_lock.release()
        raise
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            active = read_json(metadata_path, {})
            yield {
                "acquired": False,
                "active_run_id": str(active.get("run_id") or ""),
                "lock_path": str(lock_path),
            }
            return

        atomic_write_json(
            metadata_path,
            {
                "run_id": run_id,
                "pid": os.getpid(),
                "status": "running",
                "target_date": str(target_date or "")[:10],
                "acquired_at": now_text(),
            },
        )
        yield {"acquired": True, "active_run_id": run_id, "lock_path": str(lock_path)}
    finally:
        if acquired:
            try:
                atomic_write_json(
                    metadata_path,
                    {
                        "run_id": run_id,
                        "pid": os.getpid(),
                        "status": "released",
                        "released_at": now_text(),
                    },
                )
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()
        if thread_acquired:
            thread_lock.release()




def _low_price_filter_inventory(root: Path) -> dict[str, Any]:
    """914 is a leftover filter output, not the universe, not a chain prerequisite."""

    path = root / "data" / "universe" / "low_price_learning_symbols.csv"
    fingerprint = _file_fingerprint(path)
    symbols = _csv_symbol_rows(path, included_only=True)
    return {
        "id": "low_price_learning",
        "role": "filter_output_not_universe",
        "used": False,
        "reason": "optional_filter_not_selected",
        "path": _display_path(path, root),
        "exists": bool(fingerprint.get("exists")),
        "symbol_count": len(symbols),
        "sha256": str(fingerprint.get("sha256") or ""),
        "size": int(fingerprint.get("size") or 0),
    }


def _csv_symbol_rows(path: Path, *, included_only: bool = False) -> list[str]:
    """Read canonical symbol rows without consulting the low-price resolver.

    The resolver's historical default is the 914-row learning filter.  The
    research chain must be able to read the all-A baseline independently, so
    this small parser is deliberately local to the chain and accepts the
    common symbol/code aliases used by the baseline exports.
    """

    if not path.exists() or not path.is_file():
        return []
    symbols: list[str] = []
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not isinstance(row, Mapping):
                    continue
                if included_only and str(row.get("included") or "").strip().lower() not in {
                    "1", "true", "yes", "y", "on", "included",
                }:
                    continue
                value = next(
                    (
                        row.get(key)
                        for key in ("symbol", "code", "ts_code", "证券代码", "代码")
                        if row.get(key) not in (None, "")
                    ),
                    "",
                )
                symbol = normalize_symbol(value)
                if len(symbol) == 6 and symbol.isdigit() and symbol not in seen:
                    symbols.append(symbol)
                    seen.add(symbol)
    except (OSError, csv.Error, UnicodeError):
        return []
    return sorted(symbols)


def _full_a_research_universe(root: Path) -> dict[str, Any]:
    """Return the default full-A research input and its source evidence.

    ``a_share_symbols.csv`` is the authoritative current-universe source.  A
    missing baseline is visible and may fall back to symbols represented by
    local daily bars; the 914 low-price file is never used as this fallback.
    """

    baseline = root / "data" / "universe" / "a_share_symbols.csv"
    fingerprint = _file_fingerprint(baseline)
    symbols = _csv_symbol_rows(baseline)
    source = "a_share_symbols.csv"
    source_path = baseline
    reason_codes: list[str] = []
    if not symbols:
        daily_dir = root / "data" / "daily"
        daily_symbols = sorted(
            {
                normalize_symbol(path.stem)
                for path in daily_dir.glob("*.csv")
                if len(normalize_symbol(path.stem)) == 6
                and normalize_symbol(path.stem).isdigit()
            }
        ) if daily_dir.exists() else []
        if daily_symbols:
            symbols = daily_symbols
            source = "local_daily_cache_fallback"
            source_path = daily_dir
            reason_codes.append("all_a_baseline_missing_or_empty")
        else:
            source = "all_a_baseline_missing"
            reason_codes.append("all_a_universe_source_missing")
    return {
        "source": source,
        "path": _display_path(source_path, root),
        "source_sha256": str(fingerprint.get("sha256") or ""),
        "source_size": int(fingerprint.get("size") or 0),
        "symbols": symbols,
        "symbol_count": len(symbols),
        "reason_codes": reason_codes,
    }


def _file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"path": str(path), "exists": False, "size": 0, "sha256": ""}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path),
        "exists": True,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _protected_fingerprints(root: Path) -> dict[str, dict[str, Any]]:
    return {
        "paper_ledger": _file_fingerprint(root / "state" / "auto_paper_portfolio.json"),
        "adjustment_factors": _file_fingerprint(root / "data" / "metadata" / "adjustment_factors.csv"),
    }


def _coverage_from_audit(
    audit: Mapping[str, Any],
    *,
    requested_count: int,
    tier: str,
    legitimate_no_data_symbols: Sequence[str] = (),
) -> dict[str, Any]:
    summary = dict(audit.get("summary") or {})
    complete = int(summary.get("complete_session") or 0)
    partial = int(summary.get("partial_session") or 0)
    invalid = int(summary.get("invalid_session") or summary.get("invalid") or 0)
    missing = int(summary.get("missing") or 0)
    legitimate = set(_normalized_symbols(legitimate_no_data_symbols))
    audited_count = max(0, requested_count - len(legitimate))
    effective_complete = min(requested_count, complete + len(legitimate))
    ratio = (
        round(effective_complete / requested_count, 6)
        if requested_count
        else None
    )
    if requested_count == 0:
        status = "not_applicable"
    elif effective_complete == requested_count:
        status = "passed"
    elif tier == "critical":
        status = "blocked"
    elif tier == "current_analysis_pool":
        status = "partial"
    else:
        status = "blocked"
    failed_symbols = [
        normalize_symbol(item.get("symbol"))
        for item in list(audit.get("items") or [])
        if (
            not bool(item.get("complete_session"))
            and normalize_symbol(item.get("symbol")) not in legitimate
        )
    ]
    return {
        "tier": tier,
        "requested_count": requested_count,
        "eligible_count": audited_count,
        "audited_count": audited_count,
        "complete_session_count": complete,
        "effective_complete_count": effective_complete,
        "partial_session_count": partial,
        "invalid_count": invalid,
        "missing_count": missing,
        "legitimate_no_data_count": len(legitimate),
        "legitimate_no_data_symbols": sorted(legitimate),
        "failed_count": max(0, requested_count - effective_complete),
        "coverage_ratio": ratio,
        "status": status,
        "failed_symbols": [item for item in failed_symbols if item],
    }


def _daily_coverage(
    payload: Mapping[str, Any],
    expected_count: int,
    legitimate_no_data_symbols: Sequence[str] = (),
) -> dict[str, Any]:
    expected = max(0, int(expected_count or 0))
    reported_total = int(
        payload.get("total_symbols")
        or payload.get("symbols_checked")
        or expected
    )
    legitimate = set(_normalized_symbols(legitimate_no_data_symbols))
    nested = dict(payload.get("after") or {})
    stale_lists_available = (
        "stale_symbol_list_after" in payload
        or "stale_symbols" in nested
    )
    missing_lists_available = (
        "missing_symbol_list_after" in payload
        or "missing_symbols" in nested
    )
    stale_symbol_list = _normalized_symbols(
        list(
            payload.get(
                "stale_symbol_list_after",
                nested.get("stale_symbols") or [],
            )
            or []
        )
    )
    missing_symbol_list = _normalized_symbols(
        list(
            payload.get(
                "missing_symbol_list_after",
                nested.get("missing_symbols") or [],
            )
            or []
        )
    )
    stale_count_raw = int(
        payload.get(
            "stale_count_after",
            nested.get("stale_count", reported_total),
        )
        or 0
    )
    missing_count_raw = int(
        payload.get(
            "missing_count_after",
            nested.get("missing_count", 0),
        )
        or 0
    )
    effective_stale_symbols = sorted(set(stale_symbol_list) - legitimate)
    effective_missing_symbols = sorted(set(missing_symbol_list) - legitimate)
    stale = (
        len(effective_stale_symbols)
        if stale_lists_available
        else stale_count_raw
    )
    missing = (
        len(effective_missing_symbols)
        if missing_lists_available
        else missing_count_raw
    )
    fresh = max(0, reported_total - stale)
    ratio = round(fresh / expected, 6) if expected else None
    failed_lists_available = "failed_symbol_list" in payload
    failed_symbol_list = _normalized_symbols(
        list(payload.get("failed_symbol_list") or [])
    )
    effective_failed_symbols = sorted(set(failed_symbol_list) - legitimate)
    failed_count_raw = int(
        payload.get("failed_symbols") or len(failed_symbol_list) or 0
    )
    failed_count = (
        len(effective_failed_symbols)
        if failed_lists_available
        else failed_count_raw
    )
    invalid_basis_symbols = _normalized_symbols(
        list(payload.get("invalid_price_basis_symbols") or [])
    )
    component_status = str(payload.get("status") or "")
    # Pipeline Stage 3 reuses Stage 2 bars as reused_verified_upstream.
    # That is a successful daily component, not an incomplete writer.
    component_acceptable = component_status in {
        "completed",
        "passed",
        "reused_verified_upstream",
        "partial",
        "skipped_network_false",
        "skipped",
    } or (
        component_status in {"partial", "failed"}
        and bool(legitimate)
        and not stale
        and not missing
        and not failed_count
        and not invalid_basis_symbols
    )
    reason_codes: list[str] = []
    if expected == 0:
        return {
            "requested_count": 0,
            "reported_count": reported_total,
            "fresh_count": 0,
            "stale_count": 0,
            "stale_symbols": [],
            "missing_count": 0,
            "missing_symbols": [],
            "failed_count": 0,
            "failed_symbols": [],
            "invalid_price_basis_count": 0,
            "invalid_price_basis_symbols": [],
            "legitimate_no_data_count": len(legitimate),
            "legitimate_no_data_symbols": sorted(legitimate),
            "coverage_ratio": None,
            "component_status": component_status,
            "reason_codes": [],
            "status": "passed",
            "note": "daily_update_not_required_universe_is_all_a",
        }
    if reported_total != expected:
        reason_codes.append("daily_universe_count_mismatch")
    if legitimate and not (
        stale_lists_available
        and missing_lists_available
        and failed_lists_available
    ):
        reason_codes.append("daily_symbol_lists_missing_for_no_session_evidence")
    if not component_acceptable:
        reason_codes.append("daily_component_not_completed")
    if stale:
        reason_codes.append("daily_symbols_stale")
    if missing:
        reason_codes.append("daily_symbols_missing")
    if failed_count or effective_failed_symbols:
        reason_codes.append("daily_symbols_failed")
    if invalid_basis_symbols:
        reason_codes.append("daily_price_basis_not_raw")
    return {
        "requested_count": expected,
        "reported_count": reported_total,
        "fresh_count": fresh,
        "stale_count": stale,
        "stale_symbols": effective_stale_symbols,
        "missing_count": missing,
        "missing_symbols": effective_missing_symbols,
        "failed_count": failed_count,
        "failed_symbols": effective_failed_symbols,
        "invalid_price_basis_count": len(invalid_basis_symbols),
        "invalid_price_basis_symbols": invalid_basis_symbols,
        "legitimate_no_data_count": len(legitimate),
        "legitimate_no_data_symbols": sorted(legitimate),
        "coverage_ratio": ratio,
        "component_status": component_status,
        "reason_codes": reason_codes,
        "status": "passed" if not reason_codes else "blocked",
    }


def _directory_fingerprint(path: Path) -> dict[str, Any]:
    directory = Path(path)
    if not directory.exists() or not directory.is_dir():
        return {"path": str(directory), "exists": False, "files": 0, "bytes": 0}
    file_count = 0
    total_bytes = 0
    for item in directory.rglob("*"):
        if not item.is_file():
            continue
        file_count += 1
        try:
            total_bytes += int(item.stat().st_size)
        except OSError:
            continue
    return {
        "path": str(directory),
        "exists": True,
        "files": file_count,
        "bytes": total_bytes,
    }


def _stage6_data_fingerprints(root: Path) -> dict[str, dict[str, Any]]:
    paths = {
        "daily": root / "data" / "daily",
        "intraday": root / "data" / "intraday",
        "metadata": root / "data" / "metadata",
        "benchmark": root / "data" / "benchmark",
        "calendar": root / "data" / "calendar",
        "cache": root / "data" / "cache",
        "universe": root / "data" / "universe",
        "features": root / "data" / "features",
        "sector_history": root / "data" / "sector_history",
        "state": root / "state",
    }
    return {name: _directory_fingerprint(path) for name, path in paths.items()}


def _free_window_gap_status(
    target_date: str,
    failed_symbols: Sequence[str],
    *,
    calendar_path: Path,
    current_date: date | None = None,
) -> dict[str, Any]:
    symbols = _normalized_symbols(failed_symbols)
    today = current_date or datetime.now().date()
    sessions = trade_days_between(
        str(target_date)[:10],
        today.isoformat(),
        calendar_path=Path(calendar_path),
    )
    expired = bool(symbols and len(sessions) > 5)
    return {
        "status": (
            "permanently_missing_free_window"
            if expired
            else "within_free_window"
            if symbols
            else "not_missing"
        ),
        "target_date": str(target_date)[:10],
        "checked_through": today.isoformat(),
        "free_window_trading_sessions": 5,
        "observed_session_count": len(sessions),
        "symbols": symbols,
        "symbol_count": len(symbols),
    }


def _isolated_critical_append_conflict(
    critical_update: Mapping[str, Any],
) -> dict[str, Any]:
    """Limit a Stage 6 append conflict red gate to its critical import batch.

    This is the decoupling ordered by task-book section 15.2. Quality failures,
    provider failures, and any non-conflict import failure remain globally
    blocking; only an already-quarantined canonical symbol/date conflict may let
    unrelated time-sensitive fetches continue.
    """

    canonical_import = critical_update.get("canonical_import")
    if not isinstance(canonical_import, Mapping):
        canonical_import = {}
    reasons = [str(item) for item in list(canonical_import.get("reason_codes") or [])]
    is_conflict = (
        critical_update.get("stage6_append_only") is True
        and str(canonical_import.get("status") or "") == "blocked"
        and canonical_import.get("quarantined") is True
        and reasons == ["batch_contains_canonical_conflict"]
    )
    return {
        "status": "isolated" if is_conflict else "not_applicable",
        "global_fetch_blocked": not is_conflict,
        "reason_codes": reasons,
        "conflict_symbols": _normalized_symbols(
            list(canonical_import.get("conflict_symbols") or [])
        ),
        "contract_reference": (
            "CODEX_TASK_quantdesk_ai_fund_manager_v1.md section 15.2"
        ),
    }


def _refresh_sector_snapshot(
    root: Path,
    *,
    network: bool,
    board_refresher: Any | None = None,
    member_fetcher: Any | None = None,
) -> dict[str, Any]:
    """Refresh the board-heat cache and capture today's membership cross-section.

    ``board_refresher`` and ``member_fetcher`` are injection seams used only by
    isolated-root tests to exercise the full T0-birth path deterministically and
    offline.  In production both stay ``None`` and the real network-authorized
    providers are used, so runtime behaviour is unchanged.
    """

    if not network:
        return {"ok": True, "status": "skipped", "network": False}
    # ``PROJECT_ROOT`` is an ordinary dependency seam and many isolated-chain
    # tests replace it so all run artifacts land below ``tmp_path``.  It must
    # therefore never define which root is allowed to call the default market
    # helper: that helper owns a process-global production cache destination.
    # Compare against the immutable module location instead.  Custom/staging
    # roots may exercise this branch only with an explicitly injected refresher.
    if (
        board_refresher is None
        and Path(root).resolve() != _CANONICAL_PROJECT_ROOT.resolve()
    ):
        return {
            "ok": False,
            "status": "blocked",
            "network": True,
            "reason_codes": ["sector_refresh_custom_root_not_supported"],
        }
    try:
        if board_refresher is None:
            from scripts.server.helpers.market import refresh_industry_board_cache

            payload = dict(refresh_industry_board_cache() or {})
        else:
            payload = dict(board_refresher() or {})
    except Exception as exc:  # noqa: BLE001 - visible degraded provider result
        return {
            "ok": False,
            "status": "blocked",
            "network": True,
            "reason_codes": ["sector_refresh_exception"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }
    payload["network"] = True
    payload["status"] = "completed" if payload.get("ok") is True else "blocked"
    if payload.get("ok") is True:
        captured_at = now_text()
        raw_cache_path = Path(
            str(payload.get("cache_path") or "data/cache/industry_board_latest.csv")
        )
        cache_path = raw_cache_path if raw_cache_path.is_absolute() else Path(root) / raw_cache_path
        cache_fingerprint = _file_fingerprint(cache_path)
        payload["captured_at"] = captured_at
        payload["cache_path"] = str(cache_path.resolve())
        payload["cache_sha256"] = (
            f"sha256:{cache_fingerprint.get('sha256')}"
            if cache_fingerprint.get("sha256")
            else ""
        )
        try:
            payload["membership_observation"] = capture_current_sector_membership(
                Path(root),
                board_cache_path=cache_path,
                provider=str(payload.get("source") or ""),
                network=True,
                captured_at=captured_at,
                member_fetcher=member_fetcher,
            )
        except Exception as exc:  # noqa: BLE001 - archive path degrades visibly
            payload["membership_observation"] = {
                "ok": False,
                "status": "degraded",
                "network": True,
                "source": str(payload.get("source") or ""),
                "captured_at": captured_at,
                "errors": [
                    f"membership_capture_exception:{type(exc).__name__}:{exc}"[:1000]
                ],
                "canonical_write": False,
                "data_universe_written": False,
            }
    return payload


def _archive_forward_sector_snapshot(
    root: Path,
    *,
    target_date: str,
    sector_refresh_payload: Mapping[str, Any] | None = None,
    network: bool = True,
    dry_run: bool = False,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Archive today's observed sector evidence without blocking the data chain.

    Round 6 Part C is deliberately independent from historical ``--apply``:
    only the new append-only ``data/sector_history`` tree is eligible here.
    A failed/absent observation is recorded as degraded in the run manifest and
    never promoted to a formal historical source.
    """

    if dry_run or not network:
        return {
            "ok": True,
            "status": "skipped",
            "evidence_eligible": False,
            "written": False,
            "reason_codes": ["network_not_authorized" if not network else "dry_run"],
            "canonical_write": False,
            "append_only": True,
        }
    refresh = dict(sector_refresh_payload or {})
    if str(refresh.get("status") or "") != "completed" or refresh.get("ok") is not True:
        return {
            "ok": True,
            "status": "degraded",
            "evidence_eligible": False,
            "written": False,
            "reason_codes": ["sector_refresh_not_completed"],
            "canonical_write": False,
            "append_only": True,
        }
    try:
        membership_observation = refresh.get("membership_observation")
        membership_path = (
            Path(str(membership_observation.get("path")))
            if isinstance(membership_observation, Mapping)
            and str(membership_observation.get("path") or "").strip()
            else None
        )
        refresh_provenance = {
            key: value
            for key, value in refresh.items()
            if key != "membership_observation"
        }
        payload = archive_forward_sector_snapshot(
            Path(root),
            target_date=target_date,
            captured_at=captured_at,
            theme_map_path=membership_path,
            source_override={
                "sector_refresh": refresh_provenance,
                "membership_observation": membership_observation,
            },
            write=True,
        )
        return payload
    except ValueError as exc:
        # Same-day divergent bytes are a local fail-closed conflict.  Do not
        # let one archive day block unrelated daily/minute acquisition.
        return {
            "ok": True,
            "status": "degraded",
            "evidence_eligible": False,
            "written": False,
            "reason_codes": [str(exc)[:500]],
            "canonical_write": False,
            "append_only": True,
        }
    except Exception as exc:  # noqa: BLE001 - archive is non-blocking by contract
        return {
            "ok": True,
            "status": "degraded",
            "evidence_eligible": False,
            "written": False,
            "reason_codes": [f"sector_history_archive_exception:{type(exc).__name__}"],
            "error": str(exc)[:1000],
            "canonical_write": False,
            "append_only": True,
        }


def _is_valid_recovery_source_run_id(value: str) -> bool:
    source_id = str(value or "")
    return (
        bool(source_id)
        and source_id not in {".", ".."}
        and "/" not in source_id
        and "\\" not in source_id
        and Path(source_id).name == source_id
    )


def _declared_sector_snapshot_hash(manifest: Mapping[str, Any]) -> str:
    components = manifest.get("components")
    components = components if isinstance(components, Mapping) else {}
    archive = components.get("sector_history_archive")
    archive = archive if isinstance(archive, Mapping) else {}
    return str(archive.get("snapshot_hash") or "")


def _same_day_recovery_source_rejections(
    runs_dir: Path,
    *,
    run_dir_name: str,
    manifest: Mapping[str, Any],
) -> list[str]:
    """Reject a same-day predecessor unless every lineage claim it makes holds."""

    reasons: list[str] = []
    if str(manifest.get("run_id") or "") != run_dir_name:
        reasons.append("source_run_id_identity_mismatch")
    if str(manifest.get("status") or "") not in RECOVERABLE_SOURCE_STATUSES:
        reasons.append("source_status_not_recoverable")
    if manifest.get("authoritative") is not True:
        reasons.append("source_not_authoritative")
    if manifest.get("dry_run") is True:
        reasons.append("source_dry_run_not_recoverable")
    if manifest.get("network") is not True:
        reasons.append("source_network_not_authorized")
    try:
        contract_version = int(manifest.get("daily_data_chain_contract_version") or 0)
    except (TypeError, ValueError):
        contract_version = 0
    if contract_version != DAILY_DATA_CHAIN_CONTRACT_VERSION:
        reasons.append("source_contract_version_mismatch")
    declared_hash = _declared_sector_snapshot_hash(manifest)
    attempt = read_json(runs_dir / run_dir_name / "sector_history_archive_attempt.json", {})
    attempt_hash = (
        str(attempt.get("snapshot_hash") or "") if isinstance(attempt, Mapping) else ""
    )
    if declared_hash != attempt_hash:
        # The manifest and its own sidecar disagree about what was sealed, so
        # neither statement can be trusted as a recovery source.
        reasons.append("source_hash_mismatch")
    elif not declared_hash:
        # Nothing was sealed, so there is no verified artifact to continue from.
        reasons.append("source_sector_snapshot_unavailable")
    return sorted(set(reasons))


def _resolve_same_day_recovery_source(
    root: Path,
    *,
    target_date: str,
    current_run_id: str,
    abandoned_lock_run_id: str = "",
    retry_from_latest: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Choose at most one same-day recovery lineage, or none at all.

    A stale ``running`` lock keeps priority: that run died mid-flight, so its
    sealed same-day artifacts are the only ones no later run can republish.
    Failing that, the scheduled path may continue from the *latest* same-target-
    date ``blocked`` run, which is the case a retry after a single-symbol
    provider failure hits. Recovery is refused outright when any same-day run
    already reached ``completed`` (the day is closed), when no predecessor
    carries this run's target date, or when a predecessor's declared sector
    snapshot hash disagrees with its own on-disk attempt artifact. Nothing here
    relaxes an append-only gate: every consumer still revalidates the artifact
    it reuses before trusting it.
    """

    target = str(target_date or "")[:10]
    selection: dict[str, Any] = {
        "schema_version": RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION,
        "target_date": target,
        "current_run_id": str(current_run_id or ""),
        "source_run_id": "",
        "recovery_kind": "none",
        "retry_from_latest": bool(retry_from_latest),
        "scanned_run_count": 0,
        "same_day_run_count": 0,
        "eligible_run_ids": [],
        "rejected": [],
        "reason_codes": [],
    }
    lock_run_id = str(abandoned_lock_run_id or "")
    if lock_run_id:
        selection["source_run_id"] = lock_run_id
        selection["recovery_kind"] = "abandoned_lock"
        return lock_run_id, selection
    if not retry_from_latest:
        selection["reason_codes"] = ["retry_from_latest_disabled"]
        return "", selection
    if not target:
        selection["reason_codes"] = ["recovery_target_date_invalid"]
        return "", selection

    runs_dir = Path(root) / "results" / "data_chain" / "runs"
    if not runs_dir.is_dir():
        selection["reason_codes"] = ["no_same_day_recovery_source"]
        return "", selection

    same_day: list[tuple[str, str, Mapping[str, Any]]] = []
    completed_run_ids: list[str] = []
    scanned = 0
    # The scan stays exhaustive rather than guessing the target day from the
    # run-id timestamp: a naming heuristic that silently skipped the real
    # predecessor would turn a fail-closed gate into a fail-open one.  Cost is
    # bounded by how many run directories are kept hot, not by this function.
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir() or entry.name == str(current_run_id or ""):
            continue
        if not _is_valid_recovery_source_run_id(entry.name):
            continue
        manifest = read_json(entry / "manifest.json", {})
        if not isinstance(manifest, Mapping) or not manifest:
            continue
        scanned += 1
        if str(manifest.get("target_date") or "")[:10] != target:
            continue
        same_day.append((str(manifest.get("started_at") or ""), entry.name, manifest))
        if str(manifest.get("status") or "") == "completed":
            completed_run_ids.append(entry.name)

    selection["scanned_run_count"] = scanned
    selection["same_day_run_count"] = len(same_day)
    if completed_run_ids:
        selection["completed_run_ids"] = sorted(completed_run_ids)
        selection["reason_codes"] = ["same_day_run_already_completed"]
        return "", selection
    if not same_day:
        selection["reason_codes"] = ["no_same_day_recovery_source"]
        return "", selection

    eligible: list[tuple[str, str]] = []
    for started_at, run_dir_name, manifest in same_day:
        rejections = _same_day_recovery_source_rejections(
            runs_dir,
            run_dir_name=run_dir_name,
            manifest=manifest,
        )
        if rejections:
            selection["rejected"].append(
                {"run_id": run_dir_name, "reason_codes": rejections}
            )
            continue
        eligible.append((started_at, run_dir_name))

    eligible.sort()
    selection["eligible_run_ids"] = [run_dir_name for _, run_dir_name in eligible]
    if not eligible:
        selection["reason_codes"] = ["no_eligible_same_day_recovery_source"]
        return "", selection
    chosen = eligible[-1][1]
    selection["source_run_id"] = chosen
    selection["recovery_kind"] = "same_day_blocked_run"
    return chosen, selection


def preflight_daily_data_chain(
    project_root: Path = PROJECT_ROOT,
    *,
    target_date: str | None = None,
    current_run_id: str | None = None,
    retry_from_latest: bool = True,
) -> dict[str, Any]:
    """Decide whether a scheduled chain should launch at all.

    This is deliberately read-only. A target date with an authoritative
    ``completed`` run is closed and must be skipped before providers, locks,
    feature rebuilds, or append-only publishers are started. All other
    outcomes remain runnable and retain the resolver's recovery selection.
    """

    root = Path(project_root).resolve()
    resolved_target, target_note = infer_target_date(target_date)
    run_id = str(current_run_id or new_run_id())
    source_run_id, selection = _resolve_same_day_recovery_source(
        root,
        target_date=resolved_target,
        current_run_id=run_id,
        retry_from_latest=bool(retry_from_latest),
    )
    reason_codes = [
        str(code) for code in (selection.get("reason_codes") or []) if str(code)
    ]
    completed_run_ids = [
        str(item)
        for item in (selection.get("completed_run_ids") or [])
        if str(item)
    ]
    if "same_day_run_already_completed" in reason_codes:
        return {
            "schema_version": RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION,
            "action": "skip",
            "status": "skipped_already_completed",
            "target_date": resolved_target,
            "target_date_note": target_note,
            "current_run_id": run_id,
            "reason_codes": ["same_day_run_already_completed"],
            "completed_run_ids": completed_run_ids,
            "recovery_source_run_id": "",
            "recovery_selection": selection,
            "read_only": True,
        }
    return {
        "schema_version": RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION,
        "action": "run",
        "status": "ready_to_run",
        "target_date": resolved_target,
        "target_date_note": target_note,
        "current_run_id": run_id,
        "reason_codes": reason_codes,
        "completed_run_ids": completed_run_ids,
        "recovery_source_run_id": source_run_id,
        "recovery_selection": selection,
        "read_only": True,
    }


def _reuse_verified_sector_snapshot(
    root: Path,
    *,
    target_date: str,
    source_run_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Reuse an abandoned run's already-sealed same-day sector snapshot.

    An abandoned run may have completed the forward sector capture before it
    died. Re-capturing the same calendar day either produces byte-identical
    output or a fail-closed conflict, so the only safe recovery is to verify the
    existing sealed evidence and reuse it read-only. Every check below must pass:
    self-hash, target date, eligibility flags, absence of a revocation sidecar,
    and identity agreement with the abandoned run's own attempt artifact.
    Returns ``(payload_or_None, diagnostics)``; the caller falls back to the
    normal refresh/archive path whenever the payload is ``None``.
    """

    target = str(target_date or "")[:10]
    diagnostics: dict[str, Any] = {
        "status": "not_available",
        "attempted": True,
        "target_date": target,
        "source_run_id": str(source_run_id or ""),
        "reuse": False,
        "reason_codes": [],
        "snapshot_path": "",
        "verified_snapshot_hash": "",
    }
    source_id = str(source_run_id or "")
    if not source_id:
        diagnostics["reason_codes"] = ["recovery_source_run_id_missing"]
        return None, diagnostics
    runs_dir = (Path(root) / "results" / "data_chain" / "runs").resolve()
    if not _is_valid_recovery_source_run_id(source_id):
        diagnostics["status"] = "rejected"
        diagnostics["reason_codes"] = ["recovery_source_run_id_invalid"]
        return None, diagnostics
    attempt_path = (
        runs_dir / source_id / "sector_history_archive_attempt.json"
    ).resolve()
    if attempt_path.parent.parent != runs_dir:
        diagnostics["status"] = "rejected"
        diagnostics["reason_codes"] = ["recovery_source_run_id_invalid"]
        return None, diagnostics

    history_day = Path(root) / "data" / "sector_history" / target
    snapshot_path = history_day / "snapshot.json"
    revocation_path = history_day / "revocation.json"
    diagnostics["snapshot_path"] = str(snapshot_path)
    if revocation_path.exists():
        diagnostics["status"] = "rejected"
        diagnostics["reason_codes"] = ["sector_snapshot_revoked"]
        return None, diagnostics
    if not snapshot_path.is_file():
        diagnostics["reason_codes"] = ["sector_snapshot_missing"]
        return None, diagnostics
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        diagnostics["status"] = "rejected"
        diagnostics["reason_codes"] = [
            f"sector_snapshot_unreadable:{type(exc).__name__}"
        ]
        return None, diagnostics
    if not isinstance(snapshot, Mapping):
        diagnostics["status"] = "rejected"
        diagnostics["reason_codes"] = ["sector_snapshot_shape_invalid"]
        return None, diagnostics

    reason_codes: list[str] = []
    if str(snapshot.get("target_date") or "")[:10] != target:
        reason_codes.append("sector_snapshot_target_date_mismatch")
    if str(snapshot.get("status") or "") != "passed":
        reason_codes.append("sector_snapshot_status_not_passed")
    if snapshot.get("evidence_eligible") is not True:
        reason_codes.append("sector_snapshot_not_evidence_eligible")
    if snapshot.get("pit_eligible") is not True:
        reason_codes.append("sector_snapshot_not_pit_eligible")
    if not verify_snapshot_self_hash(snapshot):
        reason_codes.append("sector_snapshot_self_hash_mismatch")

    attempt = read_json(attempt_path, {})
    if not isinstance(attempt, Mapping) or not attempt:
        reason_codes.append("abandoned_sector_attempt_missing")
    else:
        if attempt.get("written") is not True:
            reason_codes.append("abandoned_sector_attempt_not_written")
        if (
            attempt.get("status") != "passed"
            or attempt.get("evidence_eligible") is not True
            or attempt.get("pit_eligible") is not True
        ):
            reason_codes.append(
                "abandoned_sector_attempt_not_evidence_eligible"
            )
        if str(attempt.get("snapshot_hash") or "") != str(
            snapshot.get("snapshot_hash") or ""
        ):
            reason_codes.append("abandoned_sector_attempt_snapshot_hash_mismatch")
        if str(attempt.get("content_identity") or "") != str(
            snapshot.get("content_identity") or ""
        ):
            reason_codes.append("abandoned_sector_attempt_content_identity_mismatch")
        if str(attempt.get("target_date") or "")[:10] != target:
            reason_codes.append("abandoned_sector_attempt_target_date_mismatch")
        attempt_content = dict(attempt)
        attempt_content.pop("written", None)
        snapshot_content = dict(snapshot)
        snapshot_content.pop("written", None)
        if attempt_content != snapshot_content:
            reason_codes.append("abandoned_sector_attempt_content_mismatch")

    if reason_codes:
        diagnostics["status"] = "rejected"
        diagnostics["reason_codes"] = sorted(set(reason_codes))
        return None, diagnostics

    payload = dict(snapshot)
    payload["written"] = False
    payload["canonical_write"] = False
    payload["recovery_reuse"] = True
    payload["recovery_source_run_id"] = source_id
    payload["original_snapshot_hash"] = str(snapshot.get("snapshot_hash") or "")
    payload["snapshot_path"] = str(snapshot_path.resolve())
    diagnostics.update(
        {
            "status": "reused",
            "reuse": True,
            "reason_codes": [],
            "snapshot_path": str(snapshot_path.resolve()),
            "verified_snapshot_hash": str(snapshot.get("snapshot_hash") or ""),
        }
    )
    return payload, diagnostics


def _update_current_state(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Persist machine-readable runtime status without editing governance docs.

    The legacy function name remains for monkeypatch and import compatibility.
    Scheduled runs must never rewrite ``docs/CURRENT_STATE.md``; that document
    is updated only during an explicit human handoff checkpoint.
    """

    path = (
        Path(root)
        / "results"
        / "data_chain"
        / "daily_data_chain_runtime_status_latest.json"
    )
    runtime_status = {
        "schema_version": "daily_data_chain_runtime_status_v1",
        "task_type": "daily_data_chain_runtime_status",
        "generated_at": now_text(),
        "run_id": str(manifest.get("run_id") or ""),
        "target_date": str(manifest.get("target_date") or "")[:10],
        "source_status": str(manifest.get("status") or "unknown"),
        "source_ok": manifest.get("ok") is True,
        "coverage": dict(manifest.get("coverage") or {}),
        "manifest_path": str(manifest.get("manifest_path") or ""),
        "published_latest": manifest.get("published_latest") is True,
        "warnings": list(manifest.get("warnings") or []),
        "errors": list(manifest.get("errors") or []),
        "boundaries": dict(manifest.get("boundaries") or {}),
        "governance_docs_mutated": False,
    }
    try:
        atomic_write_json(path, runtime_status)
    except Exception as exc:  # noqa: BLE001 - this derived status must not suppress the run manifest
        return {
            "ok": False, "status": "failed", "path": str(path),
            "reason_codes": ["runtime_status_write_failed"],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
            "governance_docs_mutated": False,
        }
    return {
        "ok": True,
        "status": "updated",
        "path": str(path),
        "run_id": runtime_status["run_id"],
        "schema_version": runtime_status["schema_version"],
        "governance_docs_mutated": False,
    }


def _subset_daily_update_payload(
    payload: Mapping[str, Any],
    symbols: Sequence[Any],
) -> dict[str, Any]:
    """Keep one daily-update result scoped to an explicit symbol subset."""

    requested = _normalized_symbols(symbols)
    requested_set = set(requested)

    def scoped_list(key: str) -> list[str]:
        return [
            symbol
            for symbol in _normalized_symbols(list(payload.get(key) or []))
            if symbol in requested_set
        ]

    result = dict(payload)
    fresh = scoped_list("fresh_symbol_list_after")
    stale = scoped_list("stale_symbol_list_after")
    missing = scoped_list("missing_symbol_list_after")
    failed = scoped_list("failed_symbol_list")
    invalid = scoped_list("invalid_price_basis_symbols")
    raw_positive = scoped_list("raw_positive_volume_target_symbol_list")
    raw_zero = scoped_list("raw_zero_volume_target_symbol_list")
    latest_by_symbol = payload.get("latest_by_symbol_after")
    result.update(
        {
            "total_symbols": len(requested),
            "symbols_checked": len(requested),
            "processed_symbols": requested,
            "fresh_symbol_list_after": fresh,
            "stale_symbol_list_after": stale,
            "missing_symbol_list_after": missing,
            "failed_symbol_list": failed,
            "invalid_price_basis_symbols": invalid,
            "raw_positive_volume_target_symbols": len(raw_positive),
            "raw_positive_volume_target_symbol_list": raw_positive,
            "raw_zero_volume_target_symbols": len(raw_zero),
            "raw_zero_volume_target_symbol_list": raw_zero,
            "stale_count_after": len(stale),
            "missing_count_after": len(missing),
            "failed_symbols": len(failed),
            "latest_by_symbol_after": {
                normalize_symbol(symbol): value
                for symbol, value in dict(latest_by_symbol or {}).items()
                if normalize_symbol(symbol) in requested_set
            },
        }
    )
    return result


def _run_candidate_refresh_step(
    *,
    root: Path,
    manifest: dict[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    """Run candidate observation independently without changing chain truth."""

    components = manifest.setdefault("components", {})
    try:
        gate = candidate_refresh_gate(manifest)
    except Exception as exc:  # noqa: BLE001 - observation is failure-isolated
        gate = {
            "status": "blocked",
            "applied": False,
            "reason_codes": [
                f"candidate_refresh_gate_exception:{type(exc).__name__}"
            ],
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }
        components["candidate_refresh_gate"] = gate
        components["candidate_refresh"] = gate
        return gate
    components["candidate_refresh_gate"] = gate
    if gate.get("formal_eligible") is not True:
        result = {
            "status": "blocked",
            "applied": False,
            "reason_codes": list(gate.get("reason_codes") or []),
        }
    else:
        try:
            reuse = components.get("upstream_reuse")
            reuse = reuse if isinstance(reuse, Mapping) else {}
            result = run_formal_candidate_refresh(
                project_root=Path(root).resolve(),
                data_chain_manifest=manifest,
                captured_at=captured_at,
                apply=True,
                replace_same_date=bool(reuse.get("reused")),
                use_low_price_filter=any(
                    isinstance(item, Mapping) and item.get("used") is True
                    for item in (
                        (manifest.get("scope") or {}).get("filters", [])
                        if isinstance(manifest.get("scope"), Mapping)
                        else []
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001 - observation is failure-isolated
            result = {
                "status": "blocked",
                "applied": False,
                "reason_codes": [
                    f"candidate_refresh_exception:{type(exc).__name__}"
                ],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
    components["candidate_refresh"] = result
    if result.get("status") not in {"completed", "idempotent", "blocked"}:
        manifest.setdefault("warnings", []).append(
            f"candidate_refresh_unexpected_status:{result.get('status')}"
        )
    return result


def _feature_coverage(
    payload: Mapping[str, Any],
    expected_symbols: Sequence[str],
    target_date: str,
    legitimate_no_data_symbols: Sequence[str] = (),
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    expected = _normalized_symbols(expected_symbols)
    legitimate = set(_normalized_symbols(legitimate_no_data_symbols))
    expected_set = set(expected)
    requested_count = int(payload.get("requested_count") or 0)
    feature_count = int(payload.get("feature_count") or 0)
    skipped_count = int(payload.get("skipped_count") or 0)
    component_status = str(payload.get("status") or "")
    accepted_basis = str(payload.get("accepted_price_basis") or "")
    snapshot_stable = payload.get("feature_input_snapshot_stable") is True
    policy = dict(payload.get("price_policy_validation") or {})
    per_symbol = {
        normalize_symbol(symbol): dict(evidence or {})
        for symbol, evidence in dict(payload.get("per_symbol") or {}).items()
        if normalize_symbol(symbol)
    }
    observed_set = set(per_symbol)
    declared_exclusions = {
        symbol for symbol in expected_set & observed_set
        if per_symbol[symbol].get("eligible") is False
        and str(per_symbol[symbol].get("reason_code") or "")
    }
    excluded_symbols: set[str] = set()
    output_integrity_failed = False
    # Exclusion is safe only when the actual published CSV contains precisely
    # the accepted symbols. A summary alone cannot prove contaminated rows left.
    if declared_exclusions and output_path is not None:
        output_integrity_failed = True
        try:
            raw_output = output_path.read_bytes()
            output_rows = list(csv.DictReader(raw_output.decode("utf-8-sig").splitlines()))
            output_symbols = [normalize_symbol(row.get("symbol")) for row in output_rows]
            accepted_symbols = {
                symbol for symbol in expected_set & observed_set
                if per_symbol[symbol].get("eligible") is True
            }
            if (
                hashlib.sha256(raw_output).hexdigest() == payload.get("output_sha256")
                and set(output_symbols) == accepted_symbols
                and len(output_symbols) == len(accepted_symbols) == feature_count
                and int(payload.get("output_row_count") or 0) == feature_count
            ):
                excluded_symbols = declared_exclusions
                output_integrity_failed = False
        except (OSError, UnicodeError, csv.Error):
            pass
    missing_data_symbols = {
        symbol
        for symbol in expected_set & observed_set
        if per_symbol[symbol].get("eligible") is False
        and str(per_symbol[symbol].get("reason_code") or "") == "missing_or_invalid_daily"
    }
    missing_symbols = sorted((expected_set - observed_set) | missing_data_symbols)
    unexpected_symbols = sorted(observed_set - expected_set)
    invalid_symbols = sorted(
        symbol
        for symbol in expected_set & observed_set
        if (
            symbol not in missing_data_symbols and symbol not in excluded_symbols
            and (
            per_symbol[symbol].get("eligible") is not True
            or str(per_symbol[symbol].get("price_basis") or "") != "raw"
            )
        )
    )
    target = str(target_date or "")[:10]
    stale_symbols = sorted(
        symbol
        for symbol in expected_set & observed_set
        if (
            str(per_symbol[symbol].get("latest_date") or "")[:10] != target
            and symbol not in legitimate
            and symbol not in missing_data_symbols
        )
    )
    legitimate_stale_symbols = sorted(
        symbol
        for symbol in expected_set & observed_set
        if (
            str(per_symbol[symbol].get("latest_date") or "")[:10] != target
            and symbol in legitimate
        )
    )
    reason_codes: list[str] = []
    if output_integrity_failed:
        reason_codes.append("feature_output_integrity_invalid")
    if payload.get("ok") is not True or component_status not in {"passed", "partial"}:
        reason_codes.append("feature_component_not_passed")
    if requested_count != len(expected):
        reason_codes.append("feature_requested_count_mismatch")
    if feature_count != len(expected):
        reason_codes.append("feature_count_mismatch")
    output_sha256 = str(payload.get("output_sha256") or "")
    output_row_count = int(payload.get("output_row_count") or 0)
    if len(output_sha256) != 64:
        reason_codes.append("feature_output_digest_missing")
    if output_row_count != feature_count:
        reason_codes.append("feature_output_row_count_mismatch")
    if skipped_count:
        reason_codes.append("feature_symbols_skipped")
    if accepted_basis != "raw":
        reason_codes.append("feature_price_basis_not_raw")
    if not snapshot_stable:
        reason_codes.append("feature_input_snapshot_unstable")
    policy_partial_ok = (
        component_status == "partial"
        and str(policy.get("status") or "") == "passed"
        and str(policy.get("run_status") or "") in {"passed", "partial"}
        and policy.get("eligible") is True
        and str(policy.get("accepted_price_basis") or "") == "raw"
        and not list(policy.get("blocked_symbols") or [])
    )
    if (
        not policy_partial_ok
        and (
            str(policy.get("status") or "") != "passed"
            or str(policy.get("run_status") or "") not in {"passed", "partial"}
            or policy.get("eligible") is not True
            or str(policy.get("accepted_price_basis") or "") != "raw"
            or list(policy.get("blocked_symbols") or [])
            or list(policy.get("reason_codes") or [])
        )
    ):
        reason_codes.append("feature_price_policy_not_passed")
    if missing_symbols or unexpected_symbols:
        reason_codes.append("feature_symbol_set_mismatch")
    if invalid_symbols:
        reason_codes.append("feature_per_symbol_not_raw")
    if excluded_symbols:
        reason_codes.append("feature_symbols_excluded")
    if stale_symbols:
        reason_codes.append("feature_target_date_mismatch")
    symbol_hole_codes = {
        "feature_symbols_skipped",
        "feature_symbol_set_mismatch",
        "feature_requested_count_mismatch",
        "feature_count_mismatch",
        "feature_output_row_count_mismatch",
        "feature_target_date_mismatch",
        "feature_symbols_excluded",
    }
    status = (
        "partial"
        if reason_codes and set(reason_codes).issubset(symbol_hole_codes)
        else "passed"
        if not reason_codes
        else "blocked"
    )
    return {
        "requested_count": len(expected),
        "reported_requested_count": requested_count,
        "feature_count": feature_count,
        "skipped_count": skipped_count,
        "component_status": component_status,
        "accepted_price_basis": accepted_basis,
        "input_snapshot_stable": snapshot_stable,
        "missing_symbols": missing_symbols,
        "unexpected_symbols": unexpected_symbols,
        "invalid_symbols": invalid_symbols,
        "excluded_symbols": sorted(excluded_symbols),
        "excluded_symbol_evidence": {
            symbol: per_symbol[symbol] for symbol in sorted(excluded_symbols)
        },
        "exclusion_output_sha256": payload.get("output_sha256") if excluded_symbols else "",
        "stale_symbols": stale_symbols,
        "legitimate_stale_symbols": legitimate_stale_symbols,
        "legitimate_no_data_symbols": sorted(legitimate),
        "reason_codes": reason_codes,
        "status": status,
    }


def _chunks(values: Sequence[str], size: int) -> Iterator[list[str]]:
    batch_size = max(1, int(size))
    for offset in range(0, len(values), batch_size):
        yield list(values[offset : offset + batch_size])






def _symbols_sha256(values: Sequence[str]) -> str:
    canonical = json.dumps(
        _normalized_symbols(values),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _checkpoint_path(project_root: Path, run_id: str) -> Path:
    return (
        Path(project_root)
        / "results"
        / "data_chain"
        / "runs"
        / str(run_id)
        / "checkpoint.json"
    )




def _formal_batch_quality(
    batch: Sequence[str],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    expected = _normalized_symbols(batch)
    items_by_symbol = {
        normalize_symbol(item.get("symbol")): dict(item)
        for item in list(audit.get("items") or [])
        if isinstance(item, Mapping) and normalize_symbol(item.get("symbol"))
    }
    accepted: list[str] = []
    rejected: list[str] = []
    reasons: dict[str, list[str]] = {}
    item_evidence: list[dict[str, Any]] = []
    for symbol in expected:
        item = items_by_symbol.get(symbol)
        symbol_reasons: list[str] = []
        if item is None:
            symbol_reasons.append("formal_quality_evidence_missing")
        else:
            if item.get("complete_session") is not True:
                symbol_reasons.append("complete_session_not_passed")
            if item.get("formal_execution_eligible") is not True:
                symbol_reasons.append("formal_execution_not_eligible")
            if str(item.get("price_basis") or "").lower() != "raw":
                symbol_reasons.append("price_basis_not_raw")
            symbol_reasons.extend(
                str(reason)
                for reason in list(item.get("session_reason_codes") or [])
                if str(reason)
            )
        symbol_reasons = list(dict.fromkeys(symbol_reasons))
        eligible = not symbol_reasons
        if eligible:
            accepted.append(symbol)
        else:
            rejected.append(symbol)
            reasons[symbol] = symbol_reasons
        item_evidence.append(
            {
                "symbol": symbol,
                "complete_session": bool(
                    item and item.get("complete_session") is True
                ),
                "formal_execution_eligible": bool(
                    item and item.get("formal_execution_eligible") is True
                ),
                "price_basis": str((item or {}).get("price_basis") or "unknown"),
                "reason_codes": symbol_reasons,
            }
        )
    return {
        "status": "passed" if not rejected else "partial",
        "formal_accepted_symbols": accepted,
        "formal_rejected_symbols": rejected,
        "formal_quality_reasons": reasons,
        "items": item_evidence,
    }


def _checkpoint_batch_artifact(
    *,
    root: Path,
    run_dir: Path,
    batch_index: int,
    batch: Sequence[str],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    output_path = run_dir / "batches" / f"batch_{int(batch_index):04d}.json"
    provider_accepted = set(
        _normalized_symbols(
            list(result.get("updated_symbol_list") or [])
            + list(result.get("unchanged_symbol_list") or [])
        )
    )
    provider_failed = set(
        _normalized_symbols(list(result.get("failed_symbol_list") or []))
    )
    batch_symbols = set(_normalized_symbols(batch))
    provider_accepted &= batch_symbols
    provider_failed &= batch_symbols
    provider_accepted -= provider_failed
    status = str(result.get("status") or "failed").lower()
    formal_accepted = set(
        _normalized_symbols(list(result.get("formal_accepted_symbols") or []))
    )
    formal_rejected = set(
        _normalized_symbols(list(result.get("formal_rejected_symbols") or []))
    )
    if status not in {"ok", "partial", "completed"}:
        formal_accepted.clear()
        formal_rejected.update(_normalized_symbols(batch))
    formal_accepted &= provider_accepted
    formal_rejected.update(set(_normalized_symbols(batch)) - formal_accepted)
    raw_formal_reasons = result.get("formal_quality_reasons")
    if not isinstance(raw_formal_reasons, Mapping):
        raw_formal_reasons = {}
    formal_reasons = {
        normalize_symbol(symbol): [
            str(reason)
            for reason in list(reasons or [])
            if str(reason)
        ]
        for symbol, reasons in raw_formal_reasons.items()
        if normalize_symbol(symbol)
    }
    for symbol in sorted(formal_rejected):
        formal_reasons.setdefault(
            symbol,
            ["formal_quality_not_accepted"],
        )
    canonical_import = (
        dict(result.get("canonical_import") or {})
        if isinstance(result.get("canonical_import"), Mapping)
        else {}
    )
    fingerprint = _file_fingerprint(output_path)
    return {
        "batch": int(batch_index),
        "path": _display_path(output_path, root),
        "sha256": str(fingerprint.get("sha256") or ""),
        "size": int(fingerprint.get("size") or 0),
        "status": status,
        "processed_symbols": _normalized_symbols(batch),
        "provider_accepted_symbols": sorted(provider_accepted),
        "provider_failed_symbols": sorted(provider_failed),
        "accepted_symbols": sorted(formal_accepted),
        "failed_symbols": sorted(formal_rejected),
        "formal_accepted_symbols": sorted(formal_accepted),
        "formal_rejected_symbols": sorted(formal_rejected),
        "formal_quality_reasons": formal_reasons,
        "formal_quality_items": list(result.get("formal_quality_items") or []),
        "stage6_append_only": result.get("stage6_append_only") is True,
        "canonical_import_status": str(canonical_import.get("status") or ""),
        "canonical_import_applied": canonical_import.get("applied") is True,
        "canonical_import_output_path": str(
            canonical_import.get("output_path") or ""
        ),
        "reason_codes": list(result.get("reason_codes") or []),
        "error": str(result.get("error") or "")[:500],
    }


def _write_batch_checkpoint(
    *,
    path: Path,
    phase: str,
    run_id: str,
    target_date: str,
    universe_sha256: str,
    workset_sha256: str,
    workset_symbols: Sequence[str],
    provider_order: Sequence[str],
    batch_size: int,
    total_batch_count: int,
    completed_batch_count: int,
    processed_symbol_count: int,
    total_symbol_count: int,
    last_batch: int | Mapping[str, Any],
    accepted_symbols: Sequence[str],
    failed_symbols: Sequence[str],
    batch_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    workset = _normalized_symbols(workset_symbols)
    total = max(0, int(total_symbol_count))
    if total != len(workset):
        raise ValueError("checkpoint_total_symbol_count_must_equal_workset")
    processed = max(0, min(total, int(processed_symbol_count)))
    progress = round((processed / total) * 100.0, 4) if total else 100.0
    formal_accepted = _normalized_symbols(accepted_symbols)
    formal_rejected = _normalized_symbols(failed_symbols)
    last_batch_evidence = (
        dict(last_batch)
        if isinstance(last_batch, Mapping)
        else {"batch": int(last_batch)}
    )
    try:
        last_batch_index = int(last_batch_evidence.get("batch") or 0)
    except (TypeError, ValueError):
        last_batch_index = 0
    processed_evidence_symbols = _normalized_symbols(
        [
            symbol
            for evidence in batch_evidence
            for symbol in list(evidence.get("processed_symbols") or [])
        ]
    )
    formal_reasons: dict[str, list[str]] = {}
    for evidence in batch_evidence:
        for symbol, reasons in dict(
            evidence.get("formal_quality_reasons") or {}
        ).items():
            normalized = normalize_symbol(symbol)
            if normalized:
                formal_reasons[normalized] = [
                    str(reason)
                    for reason in list(reasons or [])
                    if str(reason)
                ]
    checkpoint = {
        "phase": str(phase),
        "contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "run_id": str(run_id),
        "target_date": str(target_date)[:10],
        "universe_sha256": str(universe_sha256),
        "workset_sha256": str(workset_sha256),
        "workset_symbols": workset,
        "provider_order": [str(item) for item in provider_order],
        "batch_size": max(1, int(batch_size)),
        "total_batch_count": max(0, int(total_batch_count)),
        "batch_count": max(0, int(total_batch_count)),
        "completed_batch_count": max(0, int(completed_batch_count)),
        "processed_symbol_count": processed,
        "completed_symbols": processed_evidence_symbols,
        "total_symbol_count": total,
        "total_symbol_scope": "analysis_intraday_update_workset",
        "progress_percent": progress,
        "last_batch": last_batch_index,
        "last_batch_evidence": last_batch_evidence,
        "accepted_symbols": formal_accepted,
        "failed_symbols": formal_rejected,
        "formal_accepted_symbols": formal_accepted,
        "formal_rejected_symbols": formal_rejected,
        "formal_quality_reasons": formal_reasons,
        "batch_evidence": [dict(item) for item in batch_evidence],
        "updated_at": now_text(),
    }
    atomic_write_json(path, checkpoint)
    return checkpoint


def _recovery_contract_mismatches(
    checkpoint: Mapping[str, Any],
    *,
    target_date: str,
    universe_sha256: str,
    workset_sha256: str,
    provider_order: Sequence[str],
    batch_size: int,
) -> list[str]:
    mismatches: list[str] = []
    if str(checkpoint.get("phase") or "") != "analysis_intraday_batches":
        mismatches.append("phase")
    try:
        checkpoint_contract_version = int(checkpoint.get("contract_version") or 0)
    except (TypeError, ValueError):
        checkpoint_contract_version = 0
    if checkpoint_contract_version != DAILY_DATA_CHAIN_CONTRACT_VERSION:
        mismatches.append("contract_version")
    if str(checkpoint.get("target_date") or "")[:10] != str(target_date)[:10]:
        mismatches.append("target_date")
    if str(checkpoint.get("universe_sha256") or "") != str(universe_sha256):
        mismatches.append("universe_sha256")
    if str(checkpoint.get("workset_sha256") or "") != str(workset_sha256):
        mismatches.append("workset_sha256")
    if list(checkpoint.get("provider_order") or []) != list(provider_order):
        mismatches.append("provider_order")
    try:
        checkpoint_batch_size = int(checkpoint.get("batch_size") or 0)
    except (TypeError, ValueError):
        checkpoint_batch_size = 0
    if checkpoint_batch_size != max(1, int(batch_size)):
        mismatches.append("batch_size")
    return mismatches


def _recover_abandoned_batch_artifacts(
    *,
    project_root: Path,
    source_run_id: str,
    target_date: str,
    universe_sha256: str,
    provider_order: Sequence[str],
    batch_size: int,
    expected_symbols: Sequence[str],
    workset_sha256: str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    expected_list = _normalized_symbols(expected_symbols)
    expected = set(expected_list)
    expected_workset_sha256 = _symbols_sha256(expected_list)
    normalized_batch_size = max(1, int(batch_size))
    expected_batch_count = (
        (len(expected_list) + normalized_batch_size - 1) // normalized_batch_size
        if expected_list
        else 0
    )
    result: dict[str, Any] = {
        "status": "not_available",
        "source_run_id": str(source_run_id),
        "workset_sha256": expected_workset_sha256,
        "accepted_symbols": [],
        "retry_symbols": sorted(expected),
        "batch_evidence": [],
        "reason_codes": [],
    }
    if (
        workset_sha256 is not None
        and str(workset_sha256) != expected_workset_sha256
    ):
        result["status"] = "rejected"
        result["reason_codes"] = ["requested_workset_sha256_mismatch"]
        return result
    if not source_run_id:
        result["reason_codes"] = ["abandoned_run_id_missing"]
        return result
    source_dir = root / "results" / "data_chain" / "runs" / str(source_run_id)
    checkpoint_path = source_dir / "checkpoint.json"
    checkpoint = read_json(checkpoint_path, {})
    if not isinstance(checkpoint, dict) or not checkpoint:
        result["status"] = "rejected"
        result["reason_codes"] = ["checkpoint_missing_or_corrupt"]
        return result
    mismatches = _recovery_contract_mismatches(
        checkpoint,
        target_date=target_date,
        universe_sha256=universe_sha256,
        workset_sha256=expected_workset_sha256,
        provider_order=provider_order,
        batch_size=normalized_batch_size,
    )
    if str(checkpoint.get("run_id") or "") != str(source_run_id):
        mismatches.append("run_id")
    checkpoint_workset = _normalized_symbols(
        list(checkpoint.get("workset_symbols") or [])
    )
    if checkpoint_workset != expected_list:
        mismatches.append("workset_symbols")
    if mismatches:
        result["status"] = "rejected"
        result["reason_codes"] = [
            f"checkpoint_contract_mismatch:{field}"
            for field in sorted(set(mismatches))
        ]
        return result

    evidence_rows = checkpoint.get("batch_evidence")
    if not isinstance(evidence_rows, list) or not evidence_rows:
        result["status"] = "rejected"
        result["reason_codes"] = ["checkpoint_batch_evidence_missing"]
        return result

    plan_valid = True
    try:
        checkpoint_total = int(checkpoint.get("total_symbol_count"))
        checkpoint_batch_count = int(checkpoint.get("total_batch_count"))
        completed_batch_count = int(checkpoint.get("completed_batch_count"))
        processed_symbol_count = int(checkpoint.get("processed_symbol_count"))
    except (TypeError, ValueError):
        plan_valid = False
        checkpoint_total = checkpoint_batch_count = completed_batch_count = -1
        processed_symbol_count = -1
    batch_indexes: list[int] = []
    evidence_processed_symbols: set[str] = set()
    evidence_shape_invalid = False
    for row in evidence_rows:
        try:
            batch_indexes.append(int(row.get("batch")) if isinstance(row, Mapping) else -1)
        except (TypeError, ValueError):
            batch_indexes.append(-1)
        if isinstance(row, Mapping) and isinstance(
            row.get("processed_symbols"), list
        ):
            evidence_processed_symbols.update(
                _normalized_symbols(row.get("processed_symbols") or [])
            )
        else:
            evidence_shape_invalid = True
    try:
        last_batch_index = int(checkpoint.get("last_batch") or 0)
    except (TypeError, ValueError):
        last_batch_index = 0
    if (
        checkpoint_total != len(expected_list)
        or checkpoint_batch_count != expected_batch_count
        or completed_batch_count != len(evidence_rows)
        or not 0 <= processed_symbol_count <= len(expected_list)
        or evidence_shape_invalid
        or len(evidence_processed_symbols) != processed_symbol_count
        or len(set(batch_indexes)) != len(batch_indexes)
        or any(index < 1 or index > expected_batch_count for index in batch_indexes)
        or (evidence_rows and last_batch_index not in set(batch_indexes))
    ):
        plan_valid = False
    if not plan_valid:
        result["status"] = "rejected"
        result["reason_codes"] = ["checkpoint_batch_plan_mismatch"]
        return result

    accepted: set[str] = set()
    evidence_out: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    for evidence in evidence_rows:
        if not isinstance(evidence, Mapping):
            reason_codes.append("batch_evidence_corrupt")
            continue
        raw_path = str(evidence.get("path") or "")
        artifact_path = Path(raw_path)
        if not artifact_path.is_absolute():
            artifact_path = root / artifact_path
        try:
            artifact_path = artifact_path.resolve()
            artifact_path.relative_to(source_dir.resolve())
        except (OSError, ValueError):
            reason_codes.append("batch_artifact_path_outside_run")
            continue
        if not artifact_path.exists() or not artifact_path.is_file():
            reason_codes.append("batch_artifact_missing")
            continue
        expected_sha = str(evidence.get("sha256") or "").lower()
        if (
            len(expected_sha) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha)
        ):
            reason_codes.append("batch_artifact_sha256_missing_or_invalid")
            continue
        actual_sha = str(_file_fingerprint(artifact_path).get("sha256") or "")
        if expected_sha != actual_sha:
            reason_codes.append("batch_artifact_hash_mismatch")
            continue
        artifact = read_json(artifact_path, {})
        if not isinstance(artifact, Mapping) or not artifact:
            reason_codes.append("batch_artifact_corrupt")
            continue
        row_status = str(artifact.get("status") or "").lower()
        if row_status in {"failed", "degraded", "blocked", "error"}:
            reason_codes.append(f"artifact_status_not_reusable:{row_status}")
            continue
        if row_status not in {"ok", "partial", "completed"}:
            reason_codes.append(
                f"artifact_status_not_reusable:{row_status or 'missing'}"
            )
            continue
        if str(artifact.get("phase") or "") != "analysis_intraday_batches":
            reason_codes.append("artifact_phase_mismatch")
            continue
        if str(artifact.get("run_id") or "") != str(source_run_id):
            reason_codes.append("artifact_run_id_mismatch")
            continue
        if str(artifact.get("target_date") or "")[:10] != str(target_date)[:10]:
            reason_codes.append("artifact_target_date_mismatch")
            continue
        try:
            artifact_contract_version = int(
                artifact.get("contract_version") or 0
            )
        except (TypeError, ValueError):
            artifact_contract_version = 0
        if artifact_contract_version != DAILY_DATA_CHAIN_CONTRACT_VERSION:
            reason_codes.append("artifact_contract_version_mismatch")
            continue
        if str(artifact.get("universe_sha256") or "") != str(universe_sha256):
            reason_codes.append("artifact_universe_sha256_mismatch")
            continue
        if str(artifact.get("workset_sha256") or "") != expected_workset_sha256:
            reason_codes.append("artifact_workset_sha256_mismatch")
            continue
        try:
            artifact_batch_size = int(artifact.get("batch_size") or 0)
            artifact_batch_index = int(artifact.get("batch_index") or 0)
        except (TypeError, ValueError):
            artifact_batch_size = artifact_batch_index = 0
        if artifact_batch_size != normalized_batch_size:
            reason_codes.append("artifact_batch_size_mismatch")
            continue
        if artifact_batch_index != int(evidence.get("batch") or 0):
            reason_codes.append("artifact_batch_index_mismatch")
            continue
        if list(artifact.get("provider_order") or []) != list(provider_order):
            reason_codes.append("artifact_provider_order_mismatch")
            continue
        if artifact.get("require_raw_price_basis") is not True:
            reason_codes.append("artifact_price_basis_not_raw")
            continue
        canonical_import = artifact.get("canonical_import")
        # CODEX_TASK_quantdesk_ai_fund_manager_v1.md section 14.3: an
        # interrupted batch is reusable only after the audited append-only
        # canonical transaction completed. Provider/formal-quality acceptance
        # alone is not proof that canonical minute evidence exists.
        if (
            artifact.get("stage6_append_only") is not True
            or not isinstance(canonical_import, Mapping)
            or str(canonical_import.get("status") or "") != "completed"
            or canonical_import.get("applied") is not True
        ):
            reason_codes.append("artifact_stage6_append_import_missing")
            continue
        processed = set(
            _normalized_symbols(list(artifact.get("processed_symbols") or []))
        )
        provider_accepted = set(
            _normalized_symbols(
                list(artifact.get("updated_symbol_list") or [])
                + list(artifact.get("unchanged_symbol_list") or [])
            )
        )
        provider_failed = set(
            _normalized_symbols(list(artifact.get("failed_symbol_list") or []))
        )
        if (
            not processed
            or len(processed) > normalized_batch_size
            or not processed.issubset(expected)
            or not provider_accepted.issubset(processed)
            or not provider_failed.issubset(processed)
        ):
            reason_codes.append("artifact_symbol_set_invalid")
            continue
        if (
            "formal_accepted_symbols" not in artifact
            or "formal_rejected_symbols" not in artifact
            or not isinstance(artifact.get("formal_accepted_symbols"), list)
            or not isinstance(artifact.get("formal_rejected_symbols"), list)
            or not isinstance(artifact.get("formal_quality_reasons"), Mapping)
        ):
            reason_codes.append("artifact_formal_quality_missing")
            continue
        formal_accepted = set(
            _normalized_symbols(
                list(artifact.get("formal_accepted_symbols") or [])
            )
        )
        formal_rejected = set(
            _normalized_symbols(
                list(artifact.get("formal_rejected_symbols") or [])
            )
        )
        if (
            formal_accepted & formal_rejected
            or not formal_accepted.issubset(provider_accepted - provider_failed)
            or formal_accepted | formal_rejected != processed
        ):
            reason_codes.append("artifact_formal_quality_invalid")
            continue
        if formal_rejected:
            reason_codes.append("artifact_formal_quality_rejected")
        accepted.update(formal_accepted & expected)
        evidence_out.append(
            {
                **dict(evidence),
                "path": _display_path(artifact_path, root),
                "actual_sha256": actual_sha,
                "status": row_status,
                "formal_accepted_symbols": sorted(formal_accepted & expected),
                "formal_rejected_symbols": sorted(formal_rejected & expected),
                "formal_quality_reasons": dict(
                    artifact.get("formal_quality_reasons") or {}
                ),
            }
        )

    result["accepted_symbols"] = sorted(accepted)
    result["retry_symbols"] = sorted(expected - accepted)
    result["batch_evidence"] = evidence_out
    result["reason_codes"] = sorted(set(reason_codes))
    result["status"] = "recovered" if accepted else "rejected"
    return result


def _empty_audit(target_date: str, run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "target_date": target_date,
        "summary": {
            "complete_session": 0,
            "formal_execution_eligible": 0,
            "partial_session": 0,
            "invalid_session": 0,
            "missing": 0,
        },
        "items": [],
    }


def _refresh_data_health_snapshot(
    root: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild the persisted health snapshot from final canonical data."""

    target = root / "results" / "data_health" / "data_health_dashboard_latest.json"
    try:
        from quantdesk.data_health_dashboard import build_data_health_dashboard

        payload = build_data_health_dashboard(
            daily_data_chain_payload=manifest,
            output_path=target,
            write=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "failed",
            "path": _display_path(target, root),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }

    ok = bool(payload.get("ok", True))
    result = {
        "ok": ok,
        "status": "refreshed" if ok else "failed",
        "path": _display_path(target, root),
        "generated_at": str(payload.get("generated_at") or ""),
        "health_level": str(payload.get("health_level") or "unknown"),
        "full_scan": True,
    }
    if not ok:
        result["error"] = str(
            payload.get("message_for_ui")
            or payload.get("message")
            or "data_health_snapshot_not_ok"
        )
    return result


def _refresh_forward_pit_readiness(root: Path) -> dict[str, Any]:
    """Rebuild the forward-PIT replay readiness artifact (Round 7 Part B).

    Scans the append-only ``data/sector_history`` tree, anchors T0 at the
    earliest eligible non-revoked snapshot, and projects trading-day milestones.
    Read-only against ``data/sector_history``; writes only the readiness JSON
    under the ignored ``results/`` root.  Never blocks the chain.
    """

    try:
        from quantdesk.data_truth.forward_pit_readiness import (
            write_forward_pit_readiness,
        )

        payload = write_forward_pit_readiness(root)
    except Exception as exc:  # noqa: BLE001 - readiness must never break the chain
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }
    return {
        "status": "refreshed",
        "t0": payload.get("t0"),
        "current_eligible_forward_days": int(
            payload.get("current_eligible_forward_days") or 0
        ),
        "path": _display_path(Path(payload.get("readiness_path") or ""), root),
    }


def _refresh_t0_birth_evidence(root: Path) -> dict[str, Any]:
    """Rebuild the T0 birth evidence pack (Round 8 Part C).

    Derived, read-only evidence that cross-checks the earliest eligible snapshot
    against the same day's chain manifest and the readiness anchor.  When no
    eligible day exists yet it reports ``born=false`` with reason
    ``t0_not_yet_born`` and writes nothing that could fabricate a T0.  Writes
    only the evidence-pack JSON under the ignored ``results/`` root; never blocks
    the chain.
    """

    try:
        from quantdesk.data_truth.t0_birth_evidence import write_t0_birth_evidence

        payload = write_t0_birth_evidence(root)
    except Exception as exc:  # noqa: BLE001 - evidence must never break the chain
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}:{exc}"[:1000],
        }
    return {
        "status": "refreshed",
        "born": bool(payload.get("born")),
        "t0": payload.get("t0"),
        "reason_codes": list(payload.get("reason_codes") or []),
        "path": _display_path(Path(payload.get("evidence_pack_path") or ""), root),
    }


def _publish_manifest(
    manifest: dict[str, Any],
    *,
    root: Path,
    manifest_path: Path,
    latest_path: Path,
    publish_latest: bool,
) -> dict[str, Any]:
    manifest["published_latest"] = bool(publish_latest)
    manifest["manifest_path"] = _display_path(manifest_path, root)
    atomic_write_json(manifest_path, manifest)
    if publish_latest:
        atomic_write_json(latest_path, manifest)
    return manifest


def _run_daily_data_chain_impl(
    project_root: Path = PROJECT_ROOT,
    *,
    target_date: str | None = None,
    symbols: Sequence[str] | None = None,
    network: bool = False,
    dry_run: bool = False,
    run_id: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_workers: int = 4,
    rebuild_features: bool = True,
    retry_from_latest: bool = False,
    skip_analysis_pool_minute: bool = True,
    include_critical_minute: bool = False,
    refresh_universe: bool = True,
    use_low_price_filter: bool = False,
    trigger: str = "manual",
    upstream_reuse: Mapping[str, Any] | None = None,
    pipeline_run_id: str = "",
    _failure_context: dict[str, Any] | None = None,
    _lock_already_held: bool = False,
    _force_busy: bool = False,
    _prelock_metadata: Mapping[str, Any] | None = None,
    _prelock_terminal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    resolved_target, target_note = infer_target_date(target_date)
    current_run_id = str(run_id or new_run_id())
    early_lock_path = root / "state" / "daily_data_chain.lock"
    early_lock_held = (
        False if _lock_already_held else _lock_is_held(early_lock_path)
    )
    early_lock_metadata = (
        dict(_prelock_metadata)
        if _prelock_metadata is not None
        else read_json(root / "state" / "daily_data_chain.lock.json", {})
    )
    early_terminal = (
        dict(_prelock_terminal)
        if _prelock_terminal is not None
        else read_json(
            root / "results" / "data_chain" / "daily_data_chain_latest.json",
            {},
        )
    )
    if not isinstance(early_lock_metadata, Mapping):
        early_lock_metadata = {}
    if not isinstance(early_terminal, Mapping):
        early_terminal = {}
    early_lock_run_id = str(early_lock_metadata.get("run_id") or "")
    early_terminal_run_id = str(early_terminal.get("run_id") or "")
    abandoned_lock_run_id = ""
    if (
        not early_lock_held
        and str(early_lock_metadata.get("status") or "") == "running"
        and early_lock_run_id
        and early_lock_run_id != current_run_id
        and early_lock_run_id != early_terminal_run_id
    ):
        abandoned_lock_run_id = early_lock_run_id
    if early_lock_held or _force_busy:
        lock_metadata = early_lock_metadata
        busy_at = now_text()
        return {
            "ok": False,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "package_version": PACKAGE_VERSION,
            "task_type": "daily_data_chain",
            "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
            "authoritative": False,
            "run_id": current_run_id,
            "trigger": str(trigger or "manual"),
            "status": "busy",
            "started_at": busy_at,
            "generated_at": busy_at,
            "target_date": resolved_target,
            "target_date_note": target_note,
            "network": bool(network),
            "dry_run": bool(dry_run),
            "refresh_universe": bool(refresh_universe),
            "active_run_id": str(lock_metadata.get("run_id") or ""),
            "active_pid": int(lock_metadata.get("pid") or 0),
            "published_latest": False,
            "path": _display_path(
                root / "results" / "data_chain" / "daily_data_chain_latest.json",
                root,
            ),

            "next_action": "Another daily data chain is active; wait for its terminal manifest.",
            "disclaimer": DISCLAIMER,
        }
    run_dir = root / "results" / "data_chain" / "runs" / current_run_id
    manifest_path = run_dir / "manifest.json"
    latest_path = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    intraday_dir = root / "data" / "intraday" / "1m"
    formal_stage6_apply = bool(network and not dry_run)
    formal_authorized = bool(network and not dry_run and symbols is None)
    started_at = now_text()

    # Recovery lineage for this run.  A stale ``running`` lock still wins; when
    # the predecessor instead ended cleanly as ``blocked`` (a scheduled retry
    # after a single-symbol provider failure), the scheduler path may continue
    # from the latest same-target-date blocked run.  ``recovery_source_run_id``
    # only unlocks *verified reuse* of already-sealed same-day artifacts;
    # ``abandoned_source_run_id`` stays restricted to the abandoned-lock case
    # because mid-flight batch checkpoints exist only there.
    # Pipeline upstream-reuse override: when the Pipeline passes a verified
    # upstream manifest, it manages its own lifecycle. The legacy
    # "same-day already completed" skip must not preempt it — the Pipeline
    # Stage 3 needs to actually run its universe/daily steps (which will
    # reuse upstream artifacts instead of writing fresh ones).
    if upstream_reuse is not None:
        recovery_source_run_id, recovery_source_selection = "", {
            "schema_version": RECOVERY_SOURCE_SELECTION_SCHEMA_VERSION,
            "action": "run",
            "status": "upstream_reuse_provided",
            "target_date": resolved_target,
            "current_run_id": current_run_id,
            "reason_codes": ["upstream_reuse_provided"],
            "completed_run_ids": [],
            "recovery_source_run_id": "",
            "read_only": True,
        }
    else:
        recovery_source_run_id, recovery_source_selection = _resolve_same_day_recovery_source(
            root,
            target_date=resolved_target,
            current_run_id=current_run_id,
            abandoned_lock_run_id=abandoned_lock_run_id,
            retry_from_latest=bool(retry_from_latest),
        )
    abandoned_source_run_id = (
        recovery_source_run_id
        if str(recovery_source_selection.get("recovery_kind") or "") == "abandoned_lock"
        else ""
    )

    upstream_reuse_normalized: dict[str, Any] | None = None
    if upstream_reuse is not None:
        reuse_ok, reuse_reasons, reuse_normalized = validate_upstream_reuse_manifest(
            upstream_reuse,
            target_date=resolved_target,
            project_root=root,
        )
        if not reuse_ok:
            run_dir.mkdir(parents=True, exist_ok=True)
            blocked_at = now_text()
            return _publish_manifest(
                {
                    "ok": False,
                    "schema_version": API_SCHEMA_VERSION,
                    "app_version": APP_VERSION,
                    "package_version": PACKAGE_VERSION,
                    "task_type": "daily_data_chain",
                    "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
                    "authoritative": False,
                    "run_id": current_run_id,
                    "trigger": str(trigger or "manual"),
                    "status": "blocked",
                    "started_at": started_at,
                    "completed_at": blocked_at,
                    "generated_at": blocked_at,
                    "target_date": resolved_target,
                    "target_date_note": target_note,
                    "network": bool(network),
                    "dry_run": bool(dry_run),
                    "refresh_universe": bool(refresh_universe),
                    "universe": {},
                    "components": {
                        "upstream_reuse": {
                            "reused": False,
                            "blocked": True,
                            "reason_codes": list(reuse_reasons),
                        }
                    },
                    "coverage": {},
                    "warnings": [],
                    "errors": list(reuse_reasons),
                    "reason_codes": list(reuse_reasons),
                    "retry_symbols": [],
                    "published_latest": False,
                    "next_action": (
                        "upstream reuse contract rejected; "
                        "universe and daily writers were not started."
                    ),
                    "boundaries": {
                        "research_only": True,
                        "paper_account_only": True,
                        "broker_route": False,
                        "live_execution": False,
                        "paper_ledger_mutated": False,
                    },
                    "disclaimer": DISCLAIMER,
                },
                root=root,
                manifest_path=manifest_path,
                latest_path=latest_path,
                publish_latest=False,
            )
        upstream_reuse_normalized = reuse_normalized

    # Refresh the derivative low-price pool only for a full, explicitly
    # network-authorized run.  Offline/dry-run/subset paths must not construct
    # providers or touch canonical bytes; they continue with the previous pool.
    universe_refresh_payload: dict[str, Any]
    universe_refresh_blocked = False
    if upstream_reuse_normalized is not None:
        universe_refresh_payload = {
            "ok": True,
            "status": "skipped",
            "formal_eligible": True,
            "reason": "verified_upstream_reuse",
            "network_authorized": bool(network),
            "target_date": resolved_target,
            "preserved_previous": True,
            "source_run_id": str(upstream_reuse_normalized.get("run_id") or ""),
            "quote_date": resolved_target,
            "provider": "verified_upstream_reuse",
        }
    elif symbols is not None:
        universe_refresh_payload = {
            "ok": True,
            "status": "skipped",
            "formal_eligible": False,
            "reason": "explicit_symbol_subset",
            "network_authorized": bool(network),
            "target_date": resolved_target,
            "preserved_previous": True,
        }
    elif not refresh_universe:
        universe_refresh_payload = {
            "ok": True,
            "status": "skipped",
            "formal_eligible": False,
            "reason": "disabled_by_config",
            "network_authorized": bool(network),
            "target_date": resolved_target,
            "preserved_previous": True,
        }
    elif dry_run:
        universe_refresh_payload = {
            "ok": True,
            "status": "skipped",
            "formal_eligible": False,
            "reason": "dry_run",
            "network_authorized": bool(network),
            "target_date": resolved_target,
            "preserved_previous": True,
        }
    elif not network:
        universe_refresh_payload = {
            "ok": True,
            "status": "skipped",
            "formal_eligible": False,
            "reason": "network_not_authorized",
            "network_authorized": False,
            "target_date": resolved_target,
            "preserved_previous": True,
        }
    else:
        try:
            universe_fetchers = build_default_fetchers()
            universe_refresh_payload = refresh_universe_snapshot(
                root,
                resolved_target,
                network=True,
                fetchers=universe_fetchers,
                baseline_path=root / "data" / "universe" / "a_share_symbols.csv",
            )
        except Exception as exc:  # noqa: BLE001 - preserve old pool and block
            universe_refresh_payload = {
                "ok": False,
                "status": "blocked",
                "formal_eligible": False,
                "stale": True,
                "preserved_previous": True,
                "network_authorized": True,
                "target_date": resolved_target,
                "reason_codes": ["universe_refresh_exception"],
                "error": f"{type(exc).__name__}:{exc}"[:1000],
            }
        universe_refresh_blocked = not (
            str(universe_refresh_payload.get("status") or "") == "completed"
            and universe_refresh_payload.get("ok") is True
            and universe_refresh_payload.get("formal_eligible") is True
            and str(universe_refresh_payload.get("quote_date") or "")[:10]
            == resolved_target
            and bool(str(universe_refresh_payload.get("provider") or "").strip())
        )

    formal_pool_path = root / "data" / "universe" / "low_price_learning_symbols.csv"
    formal_pool_fingerprint = _file_fingerprint(formal_pool_path)
    universe_refresh_payload.update(
        {
            "formal_pool_path": _display_path(formal_pool_path, root),
            "formal_pool_sha256": str(formal_pool_fingerprint.get("sha256") or ""),
            "formal_pool_size": int(formal_pool_fingerprint.get("size") or 0),
            "formal_pool_preserved": True,
            "discovery_pool_path": str(
                (universe_refresh_payload.get("output_paths") or {}).get(
                    "low_price_universe",
                    "data/universe/low_price_discovery_symbols.csv",
                )
            ),
            "expansion_status": "pending_universe_expansion_gate",
        }
    )
    sector_refresh_requested = bool(
        network
        and not dry_run
        and refresh_universe
        and symbols is None
    )
    # A same-target predecessor may already hold a sealed forward sector
    # snapshot. Re-capturing that day can only return identical bytes or raise a
    # fail-closed conflict, so verify and reuse the existing evidence instead of
    # refetching or overwriting it.
    sector_reuse_payload: dict[str, Any] | None = None
    sector_reuse_diagnostics: dict[str, Any] = {
        "status": "not_attempted",
        "attempted": False,
        "reuse": False,
        "reason_codes": [],
    }
    if sector_refresh_requested and retry_from_latest and recovery_source_run_id:
        sector_reuse_payload, sector_reuse_diagnostics = (
            _reuse_verified_sector_snapshot(
                root,
                target_date=resolved_target,
                source_run_id=recovery_source_run_id,
            )
        )
    if sector_reuse_payload is not None:
        sector_refresh_payload = {
            "ok": True,
            "status": "skipped",
            "network": True,
            "recovery_reuse": True,
            "recovery_source_run_id": recovery_source_run_id,
            "reason_codes": ["sector_snapshot_recovery_reuse"],
        }
    else:
        sector_refresh_payload = _refresh_sector_snapshot(
            root,
            network=sector_refresh_requested,
        )
    sector_refresh_blocked = bool(
        sector_refresh_requested
        and sector_reuse_payload is None
        and str(sector_refresh_payload.get("status") or "") != "completed"
    )

    low_price_filter = _low_price_filter_inventory(root)
    low_price_filter["used"] = bool(use_low_price_filter and symbols is None)
    if low_price_filter["used"]:
        low_price_filter["reason"] = "explicit_filter_selected"
    full_a_universe = _full_a_research_universe(root)
    analysis_rows = (
        [{"symbol": item} for item in _normalized_symbols(list(symbols))]
        if symbols is not None
        else (
            [
                {"symbol": item}
                for item in _csv_symbol_rows(
                    root / "data" / "universe" / "low_price_learning_symbols.csv",
                    included_only=True,
                )
            ]
            if use_low_price_filter
            else [{"symbol": item} for item in full_a_universe["symbols"]]
        )
    )
    analysis_contract = build_universe_contract(
        analysis_rows,
        target_date=resolved_target,
        name="current_analysis_pool",
    )
    analysis_contract.update(
        {
            "source": (
                "explicit_symbols"
                if symbols is not None
                else "low_price_learning_filter"
                if use_low_price_filter
                else full_a_universe["source"]
            ),
            "source_path": (
                "explicit"
                if symbols is not None
                else low_price_filter.get("path")
                if use_low_price_filter
                else full_a_universe["path"]
            ),
            "source_sha256": (
                ""
                if symbols is not None
                else low_price_filter.get("sha256")
                if use_low_price_filter
                else full_a_universe["source_sha256"]
            ),
            "filter_used": bool(use_low_price_filter and symbols is None),
            "filter_id": low_price_filter.get("id"),
            "filter_symbol_count": int(low_price_filter.get("symbol_count") or 0),
        }
    )
    analysis_symbols = list(analysis_contract["symbols"])
    if universe_refresh_blocked and analysis_symbols:
        universe_refresh_payload["research_fallback"] = {
            "status": "degraded", "source": analysis_contract.get("source"),
            "source_path": analysis_contract.get("source_path"),
            "source_sha256": analysis_contract.get("source_sha256"),
            "symbol_count": len(analysis_symbols),
            "reason_codes": ["universe_refresh_failed_using_previous_roster"],
        }
        universe_refresh_blocked = False
    critical_payload = (
        collect_intraday_required_symbols(
            source="preplan", target_trade_date=resolved_target, symbols=[],
        )
        if include_critical_minute
        else {"items": [], "source_manifest": {
            "status": "not_requested", "formal_eligible": False,
            "failed_sources": [], "reason_codes": ["minute_processing_not_requested"],
        }}
    )
    critical_source_manifest = dict(critical_payload.get("source_manifest") or {})
    critical_source_blocked = bool(
        include_critical_minute and critical_source_manifest
        and not bool(critical_source_manifest.get("formal_eligible"))
    )
    critical_symbols = _normalized_symbols(list(critical_payload.get("items") or []))
    if critical_source_blocked:
        critical_symbols = []
    critical_contract = build_universe_contract(
        critical_symbols,
        target_date=resolved_target,
        name="critical",
    )
    critical_daily_symbols = _normalized_symbols(
        sorted(set(critical_symbols) - set(analysis_symbols))
    )
    daily_update_symbols = _normalized_symbols(
        list(analysis_symbols) + list(critical_daily_symbols)
    )
    protected_before = _protected_fingerprints(root)
    data_directory_fingerprints_before = _stage6_data_fingerprints(root)
    if _failure_context is not None:
        _failure_context["protected_fingerprints_before"] = protected_before
        _failure_context["data_directory_fingerprints_before"] = (
            data_directory_fingerprints_before
        )
    base_manifest: dict[str, Any] = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "task_type": "daily_data_chain",
        "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
        "authoritative": formal_authorized,
        "run_id": current_run_id,
        "pipeline_run_id": str(pipeline_run_id),
        "trigger": str(trigger or "manual"),
        "status": "running",
        "started_at": started_at,
        "generated_at": started_at,
        "target_date": resolved_target,
        "target_date_note": target_note,
        "network": bool(network),
        "dry_run": bool(dry_run),
        "refresh_universe": bool(refresh_universe),
        "batch_size": max(1, int(batch_size)),
        "batch_workers": max(1, min(int(batch_workers or 1), 8)),
        "recovery_source_run_id": recovery_source_run_id,
        "recovery_kind": str(recovery_source_selection.get("recovery_kind") or "none"),
        "provider_order": {
            "daily": list(DAILY_PROVIDER_ORDER),
            "intraday": list(INTRADAY_PROVIDER_ORDER),
        },
        "provider_timeout_budget": {
            "daily_global_seconds": DAILY_PROVIDER_GLOBAL_TIMEOUT_SECONDS,
            "daily_provider_seconds": dict(DAILY_PROVIDER_TIMEOUT_OVERRIDES),
        },
        "provider_versions": _package_versions(),
        # Data coverage and research coverage are deliberately different scopes.
        # The daily-bar layer and default research input are full A; the named
        # 914 low-price file can be selected explicitly as a filter. Declaring
        # both scopes stops a filter output from being mistaken for the universe.
        "scope": {
            "market_data_scope": "all_a",
            "research_scope": "explicit_symbols" if symbols is not None else "all_a",
            "research_scope_selector": (
                "explicit_symbols" if symbols is not None else "universe_all_a"
            ),
            "full_a_research_scan": False,
            "full_a_local_research": symbols is None and not use_low_price_filter,
            "formal_authoritative": formal_authorized,
            "research_universe_source": (
                "explicit_symbols"
                if symbols is not None
                else "low_price_learning_filter"
                if use_low_price_filter
                else full_a_universe["source"]
            ),
            "research_universe_source_path": (
                "explicit"
                if symbols is not None
                else low_price_filter.get("path")
                if use_low_price_filter
                else full_a_universe["path"]
            ),
            "research_universe_source_sha256": (
                ""
                if symbols is not None
                else low_price_filter.get("sha256")
                if use_low_price_filter
                else full_a_universe["source_sha256"]
            ),
            "research_universe_count": len(analysis_symbols),
            "filters": [low_price_filter],
            "note": (
                "宇宙=全A。914低价名单是过滤器输出，不是宇宙，不是下游前提。"
                "本链默认不跑该过滤器。"
            ),
        },
        "universe": {
            "critical": critical_contract,
            "current_analysis_pool": analysis_contract,
            "research_universe": {
                "source": (
                "explicit_symbols"
                if symbols is not None
                else "low_price_learning_filter"
                if use_low_price_filter
                else full_a_universe["source"]
                ),
                "path": (
                    "explicit"
                    if symbols is not None
                    else low_price_filter.get("path")
                    if use_low_price_filter
                    else full_a_universe["path"]
                ),
                "source_sha256": (
                    ""
                    if symbols is not None
                    else low_price_filter.get("sha256")
                    if use_low_price_filter
                    else full_a_universe["source_sha256"]
                ),
                "symbol_count": len(analysis_symbols),
                "symbols": list(analysis_symbols),
                "filter_used": bool(use_low_price_filter),
                "filter_id": low_price_filter.get("id"),
                "filter_symbol_count": int(low_price_filter.get("symbol_count") or 0),
                "reason_codes": list(full_a_universe.get("reason_codes") or [])
                if symbols is None and not use_low_price_filter
                else [],
            },
            "critical_daily_outside_analysis_pool": {
                "symbol_count": len(critical_daily_symbols),
                "symbols": critical_daily_symbols,
            },
        },
        "components": {
            "critical_sources": critical_source_manifest,
            "universe_refresh": universe_refresh_payload,
            "sector_refresh": sector_refresh_payload,
            "upstream_reuse": (
                {
                    "reused": True,
                    "blocked": False,
                    "run_id": str(upstream_reuse_normalized.get("run_id") or ""),
                    "reason_codes": [],
                }
                if upstream_reuse_normalized is not None
                else {"reused": False, "blocked": False, "reason_codes": []}
            ),
        },
        "coverage": {},
        "warnings": [],
        "errors": [],
        "retry_symbols": [],
        "protected_fingerprints_before": protected_before,
        "data_directory_fingerprints_before": data_directory_fingerprints_before,
        "boundaries": {
            "research_only": True,
            "paper_account_only": True,
            "broker_route": False,
            "live_execution": False,
            "paper_ledger_mutated": False,
        },
        "disclaimer": DISCLAIMER,
    }
    sector_history_payload = (
        sector_reuse_payload
        if sector_reuse_payload is not None
        else _archive_forward_sector_snapshot(
            root,
            target_date=resolved_target,
            sector_refresh_payload=sector_refresh_payload,
            network=sector_refresh_requested,
            dry_run=dry_run,
        )
    )
    base_manifest["components"]["sector_history_archive"] = sector_history_payload
    base_manifest["components"]["sector_snapshot_recovery"] = sector_reuse_diagnostics
    base_manifest["components"]["recovery_source_selection"] = recovery_source_selection
    if str(sector_history_payload.get("status") or "") == "degraded":
        base_manifest["warnings"].append("sector_history_archive_degraded")

    lock_context = (
        nullcontext(
            {
                "acquired": True,
                "active_run_id": current_run_id,
                "lock_path": str(early_lock_path),
            }
        )
        if _lock_already_held
        else _daily_chain_lock(
            root,
            current_run_id,
            target_date=resolved_target,
        )
    )
    with lock_context as lock:
        if not lock["acquired"]:
            return {
                **base_manifest,
                "ok": False,
                "status": "busy",
                "active_run_id": lock.get("active_run_id") or "",
                "published_latest": False,
                "next_action": "已有每日数据链在运行；等待该 run_id 结束后查看清单。",
            }

        run_dir.mkdir(parents=True, exist_ok=True)
        # A② dual-landing (Round 10): mirror the sector-archive detail into an
        # independent run-dir artifact, byte-source-identical to the manifest
        # node written above at ``components.sector_history_archive``.  This is
        # a pure additive read-only evidence file: if a future readiness/T0
        # integration ever truncates or overwrites the manifest section, this
        # sibling still preserves the full degraded/written detail.  Placed
        # after the unconditional ``run_dir.mkdir`` (and inside the acquired
        # lock) so it never leaves a stray file on the ``busy`` early return and
        # never hits the pre-mkdir ordering trap.
        atomic_write_json(
            run_dir / "sector_history_archive_attempt.json",
            sector_history_payload,
        )
        runtime = build_runtime_preflight(root)
        base_manifest["runtime"] = runtime
        if network and not dry_run and (
            not runtime.get("ready") or not runtime.get("active_runtime_ready", True)
        ):
            base_manifest.update(
                {
                    "ok": False,
                    "status": "blocked",
                    "completed_at": now_text(),
                    "generated_at": now_text(),
                    "errors": list(runtime.get("reason_codes") or []),
                    "next_action": runtime.get("next_action")
                    or "安装并使用项目 .venv 的 mootdx 运行每日数据链。",
                }
            )
            return _publish_manifest(
                base_manifest,
                root=root,
                manifest_path=manifest_path,
                latest_path=latest_path,
                publish_latest=formal_authorized,
            )

        daily_network_effective = bool(
            network and (symbols is not None or use_low_price_filter)
        )
        daily_workers, daily_sleep_seconds = _daily_update_rate_contract(
            network=daily_network_effective,
            dry_run=dry_run,
            requested_workers=batch_workers,
        )
        if upstream_reuse_normalized is not None:
            daily_payload = _reused_daily_payload(
                root,
                target_date=resolved_target,
                symbols=daily_update_symbols,
                source_run_id=str(upstream_reuse_normalized.get("run_id") or ""),
            )
            daily_payload["scheduler_rate_limit"] = {
                "workers": 0,
                "sleep_seconds": 0.0,
                "network_enforced": False,
            }
        else:
            daily_payload = update_daily_prices(
                only_low_price=False,
                target_date=resolved_target,
                providers=list(DAILY_PROVIDER_ORDER),
                workers=daily_workers,
                sleep_seconds=daily_sleep_seconds,
                network=daily_network_effective,
                dry_run=bool(dry_run),
                rebuild_features=False,
                symbols=daily_update_symbols,
                output_path=run_dir / "daily_update.json",
                write_result=not dry_run,
                run_id=current_run_id,
                require_raw_price_basis=True,
                project_root=root,
                daily_dir=root / "data" / "daily",
                metadata_dir=root / "data" / "metadata",
                provider_timeout_seconds=DAILY_PROVIDER_GLOBAL_TIMEOUT_SECONDS,
                provider_timeout_overrides=dict(DAILY_PROVIDER_TIMEOUT_OVERRIDES),
            )
            daily_payload["scheduler_rate_limit"] = {
                "workers": daily_workers,
                "sleep_seconds": daily_sleep_seconds,
                "network_enforced": bool(daily_network_effective and not dry_run),
            }
        daily_payload["network_requested"] = bool(network)
        daily_payload["network_effective"] = bool(
            daily_network_effective and upstream_reuse_normalized is None
        )
        daily_payload["network_scope"] = (
            "verified_upstream_reuse"
            if upstream_reuse_normalized is not None
            else "explicit_symbol_or_filter_history"
            if daily_network_effective
            else "local_full_a_research"
        )
        if network and not daily_network_effective and upstream_reuse_normalized is None:
            base_manifest["warnings"].append(
                "full_a_per_symbol_history_network_not_enabled"
            )
        session_symbols = _normalized_symbols(
            list(analysis_symbols) + list(critical_symbols)
        )
        target_gap_payload: dict[str, Any] = {"status": "skipped", "reason": "not_verified_full_a_upstream", "session_evidence": {}}
        if (
            network and not dry_run and formal_authorized and root == PROJECT_ROOT
            and symbols is None and not use_low_price_filter
            and upstream_reuse_normalized is not None
        ):
            calendar_path = root / "data/calendar/a_share_trading_calendar.csv"
            ledger_calendar = load_a_share_trading_calendar(calendar_path)
            ledger_warning = str(ledger_calendar.get("coverage_warning") or "")
            ledger_days: list[str] = []
            if not ledger_calendar.get("weekend_only") and not ledger_warning:
                lookup = ledger_calendar.get("is_open_by_date") or {}
                if resolved_target in lookup:
                    ledger_days = sorted(day for day, is_open in lookup.items()
                                         if is_open and day <= resolved_target)[-250:]
                else:
                    ledger_warning = "historical_ledger_target_outside_calendar"
            # Describe the bounded observed-calendar scope. Missing calendar
            # evidence remains unavailable; never invent weekday sessions.
            ledger_scope = {
                "path": str(calendar_path), "target_date": resolved_target,
                "max_trading_days": 250, "start_date": ledger_days[0] if ledger_days else "",
                "end_date": ledger_days[-1] if ledger_days else "",
                "status": "available" if ledger_days else "unavailable",
                "warning": ledger_warning,
                "listing_status": "not_assessed; pending dates require symbol/session evidence",
            }
            target_gap_payload = recover_target_day_gaps(
                project_root=root, target_date=resolved_target,
                daily_payload=daily_payload, output_dir=run_dir / "target_day_gap_recovery",
                network=True,
                ledger_symbols=session_symbols, ledger_trading_days=ledger_days,
                ledger_calendar_scope=ledger_scope,
            )
            touched = list(target_gap_payload.get("appended_symbols") or [])
            refreshed = _reused_daily_payload(
                root, target_date=resolved_target, symbols=touched,
                source_run_id=str(upstream_reuse_normalized.get("run_id") or ""),
            ) if touched else {}
            daily_payload = merge_recovered_daily_status(
                daily_payload, refreshed, touched, target_gap_payload,
            )
            atomic_write_json(run_dir / "daily_update.json", daily_payload)
        # Bind the run-scoped no-session evidence to the run that produced it, so
        # the total reconciliation consumes the SAME evidence instead of falling
        # back to the legacy metadata directory and calling a verified
        # no-session day a gap.
        if (
            isinstance(target_gap_payload, dict)
            and target_gap_payload.get("schema_version") == "target_day_gap_recovery.v1"
        ):
            target_gap_payload.setdefault(
                "evidence_source",
                {
                    "run_id": current_run_id,
                    "target_date": resolved_target,
                    "source": "target_day_gap_recovery",
                    "evidence_dir": (run_dir / "target_day_gap_recovery").as_posix(),
                    "schema_version": str(target_gap_payload.get("schema_version") or ""),
                },
            )
        base_manifest["components"]["target_day_gap_recovery"] = target_gap_payload
        session_output_path = (
            run_dir / "session_expectation.json" if not dry_run else None
        )
        session_network = bool(
            network and not dry_run and root == PROJECT_ROOT
        )
        session_payload = build_session_expectation(
            session_symbols,
            resolved_target,
            project_root=root,
            daily_status=daily_payload,
            fast_daily_status=True,
            network=session_network,
            output_path=session_output_path,
            target_gap_session_evidence=target_gap_payload.get("session_evidence") or {},
        )
        session_legitimate = set(
            _normalized_symbols(
                list(session_payload.get("legitimate_no_session_symbols") or [])
            )
        )
        analysis_legitimate = sorted(
            session_legitimate & set(analysis_symbols)
        )
        critical_legitimate = sorted(
            session_legitimate & set(critical_symbols)
        )
        session_unknown = sorted(
            set(
                _normalized_symbols(
                    list(session_payload.get("unknown_symbols") or [])
                )
            )
            & set(session_symbols)
        )
        session_payload["status"] = (
            "passed" if not session_unknown else "blocked"
        )
        session_payload["formal_eligible"] = not bool(session_unknown)
        base_manifest["components"]["session_expectation"] = session_payload
        daily_gate = _daily_coverage(
            _subset_daily_update_payload(daily_payload, analysis_symbols),
            len(analysis_symbols),
            analysis_legitimate,
        )
        critical_daily_gate = (
            _daily_coverage(
                _subset_daily_update_payload(
                    daily_payload,
                    critical_daily_symbols,
                ),
                len(critical_daily_symbols),
                sorted(set(critical_legitimate) & set(critical_daily_symbols)),
            )
            if critical_daily_symbols
            else {
                "requested_count": 0,
                "reported_count": 0,
                "fresh_count": 0,
                "stale_count": 0,
                "missing_count": 0,
                "failed_count": 0,
                "status": "passed",
                "reason_codes": [],
                "outside_analysis_pool": True,
            }
        )
        base_manifest["components"]["daily_update"] = daily_payload
        base_manifest["components"]["critical_daily_update"] = _subset_daily_update_payload(
            daily_payload,
            critical_daily_symbols,
        )
        base_manifest["coverage"]["daily"] = daily_gate
        base_manifest["coverage"]["critical_daily"] = critical_daily_gate
        base_manifest["coverage"]["session_expectation"] = {
            "status": session_payload["status"],
            "unknown_count": len(session_unknown),
            "unknown_symbols": session_unknown,
            "legitimate_no_data_count": len(analysis_legitimate),
            "legitimate_no_data_symbols": analysis_legitimate,
        }

        # The benchmark index series had no writer anywhere in the repository:
        # the chain only ever *read* it (via latest_benchmark_date) to report
        # benchmark_staleness_days, so the staleness was measured faithfully
        # every day and never acted on. Appending here is what keeps the
        # reference series aligned with the daily bars it is compared against.
        benchmark_network = bool(formal_authorized and root == PROJECT_ROOT)
        if benchmark_network:
            try:
                benchmark_payload = update_benchmark_files(root, target_date=resolved_target)
            except Exception as exc:  # noqa: BLE001 - reason must reach the manifest
                benchmark_payload = {
                    "task_id": "benchmark_incremental_update",
                    "status": "failed",
                    "reason_codes": [f"{type(exc).__name__}:{exc}"],
                }
        else:
            benchmark_payload = {
                "task_id": "benchmark_incremental_update",
                "status": "skipped",
                "reason_codes": [
                    reason
                    for blocked, reason in (
                        (not network, "network_disabled"),
                        (dry_run, "dry_run"),
                        (symbols is not None, "explicit_symbol_subset"),
                        (root != PROJECT_ROOT, "not_canonical_project_root"),
                    )
                    if blocked
                ],
            }
        base_manifest["components"]["benchmark_update"] = benchmark_payload
        benchmark_reasons = list(benchmark_payload.get("reason_codes") or [])
        benchmark_reasons.extend(
            str(reason) for item in benchmark_payload.get("results") or []
            for reason in item.get("reason_codes") or []
        )
        if benchmark_network and benchmark_payload.get("target_covered") is not True:
            benchmark_reasons.append("benchmark_target_not_covered")
            base_manifest["warnings"].append("benchmark_target_not_covered")
        base_manifest["coverage"]["benchmark"] = {
            "target_date": benchmark_payload.get("target_date", resolved_target),
            "target_covered": benchmark_payload.get("target_covered"),
            "freshness_status": benchmark_payload.get("freshness_status", "not_assessed"),
            "status": benchmark_payload.get("status"),
            "reason_codes": sorted(set(benchmark_reasons)),
            "indices": list(benchmark_payload.get("results") or []),
        }

        retry_filter: set[str] | None = None
        previous = read_json(latest_path, {}) if retry_from_latest else {}
        if (
            retry_from_latest
            and str(previous.get("target_date") or "") == resolved_target
            and str(previous.get("status") or "") in {"partial", "blocked", "failed", "abandoned"}
        ):
            previous_retry = {
                normalize_symbol(item)
                for item in list(previous.get("retry_symbols") or [])
                if normalize_symbol(item)
            }
            if previous_retry:
                retry_filter = previous_retry

        critical_audit_symbols = [
            item for item in critical_symbols if item not in set(critical_legitimate)
        ]
        analysis_audit_symbols = [
            item for item in analysis_symbols if item not in set(analysis_legitimate)
        ]
        critical_to_update = list(critical_audit_symbols)
        analysis_to_update = [
            item for item in analysis_audit_symbols
            if item not in set(critical_audit_symbols)
        ]
        minute_not_assessed = list(analysis_to_update) if skip_analysis_pool_minute else []
        if skip_analysis_pool_minute:
            analysis_to_update = []

        recovery_payload: dict[str, Any] = {
            "status": "not_requested" if skip_analysis_pool_minute else "not_available",
            "source_run_id": abandoned_source_run_id,
            "accepted_symbols": [],
            "retry_symbols": sorted(analysis_to_update),
            "batch_evidence": [],
            "reason_codes": [],
        }
        if not skip_analysis_pool_minute and retry_from_latest and abandoned_source_run_id:
            recovery_payload = _recover_abandoned_batch_artifacts(
                project_root=root,
                source_run_id=abandoned_source_run_id,
                target_date=resolved_target,
                universe_sha256=str(analysis_contract.get("sha256") or ""),
                provider_order=list(INTRADAY_PROVIDER_ORDER),
                batch_size=batch_size,
                expected_symbols=analysis_to_update,
            )
            analysis_to_update = [
                item
                for item in analysis_to_update
                if item in set(recovery_payload.get("retry_symbols") or [])
            ]
        elif retry_filter is not None:
            analysis_to_update = [
                item for item in analysis_to_update if item in retry_filter
            ]
        base_manifest["components"]["analysis_intraday_recovery"] = recovery_payload
        analysis_workset_symbols = _normalized_symbols(analysis_to_update)
        analysis_workset_sha256 = _symbols_sha256(analysis_workset_symbols)
        base_manifest["universe"]["analysis_intraday_workset"] = {
            "symbol_count": len(analysis_workset_symbols),
            "symbols": analysis_workset_symbols,
            "sha256": analysis_workset_sha256,
        }

        if critical_to_update:
            critical_staging_dir = run_dir / "intraday_staging" / "critical"
            critical_work_dir = critical_staging_dir if formal_stage6_apply else intraday_dir
            critical_update = update_intraday_1m_for_symbols(
                critical_to_update,
                resolved_target,
                provider_order=list(INTRADAY_PROVIDER_ORDER),
                source="symbols",
                dry_run=dry_run,
                network=network,
                execute_after_update=False,
                update_portfolio_after_execution=False,
                intraday_dir=critical_work_dir,
                existing_intraday_dir=intraday_dir,
                materialize_local_existing=formal_stage6_apply,
                output_path=run_dir / "critical_intraday_update.json",
                run_id=current_run_id,
                require_raw_price_basis=True,
                require_complete_session=True,
            )
            if formal_stage6_apply:
                critical_stage_audit = audit_intraday_batch(
                    critical_to_update,
                    resolved_target,
                    intraday_dir=critical_staging_dir,
                    output_path=run_dir / "critical_intraday_staging_quality.json",
                    write=True,
                    run_id=current_run_id,
                )
                critical_import = apply_audited_intraday_batch(
                    staging_dir=critical_staging_dir,
                    canonical_dir=intraday_dir,
                    symbols=critical_to_update,
                    target_date=resolved_target,
                    run_id=current_run_id,
                    quality_audit=critical_stage_audit,
                    output_path=run_dir / "critical_intraday_append_import.json",
                    apply=True,
                )
                critical_update["staging_quality"] = critical_stage_audit
                critical_update["canonical_import"] = critical_import
                critical_update["stage6_append_only"] = True
                critical_isolation = _isolated_critical_append_conflict(
                    critical_update
                )
                critical_update["conflict_isolation"] = critical_isolation
                conflict_symbols = _normalized_symbols(
                    list(critical_import.get("conflict_symbols") or [])
                )
                if (
                    critical_isolation["status"] == "isolated"
                    and resolved_target == SECTION15_CONFLICT_DATE
                    and set(conflict_symbols) == set(SECTION15_CONFLICT_SYMBOLS)
                ):
                    # Task-book section 15.1 authorizes these exceptional audit
                    # branches only for the reviewed five-symbol 2026-07-14
                    # conflict. Any observed evidence outside those branches is
                    # retained as zero-write quarantine evidence.
                    critical_update["section15_conflict_resolution"] = (
                        resolve_stage6_intraday_conflicts(
                            canonical_dir=intraday_dir,
                            staging_dir=critical_staging_dir,
                            symbols=conflict_symbols,
                            target_date=resolved_target,
                            run_id=current_run_id,
                            backup_dir=run_dir / "section15_backups",
                            output_path=(
                                run_dir / "section15_conflict_resolution.json"
                            ),
                            apply=True,
                        )
                    )
                if critical_import.get("status") != "completed":
                    critical_update["status"] = (
                        "isolated_conflict"
                        if critical_isolation["status"] == "isolated"
                        else "failed"
                    )
                    critical_update["failed_symbol_list"] = list(critical_to_update)
                    critical_update["failed_symbols"] = len(critical_to_update)
        else:
            critical_update = {"status": "skipped", "processed_symbols": []}
        critical_conflict_isolation = _isolated_critical_append_conflict(
            critical_update
        )
        critical_update.setdefault(
            "conflict_isolation", critical_conflict_isolation
        )
        base_manifest["components"]["critical_intraday_update"] = critical_update
        base_manifest["components"]["critical_conflict_isolation"] = (
            critical_conflict_isolation
        )


        batch_results: list[dict[str, Any]] = []
        completed_symbols: list[str] = []
        accepted_symbols: set[str] = set()
        failed_symbols: set[str] = set()
        batch_evidence: list[dict[str, Any]] = []
        if not skip_analysis_pool_minute:
            batches_dir = run_dir / "batches"
            batch_specs = list(
                enumerate(_chunks(analysis_to_update, batch_size), start=1)
            )
            batch_count = len(batch_specs)

            def run_batch(spec: tuple[int, list[str]]) -> tuple[int, list[str], dict[str, Any]]:
                index, batch = spec
                batch_staging_dir = (
                    run_dir / "intraday_staging" / "analysis_batches" / f"batch_{index:04d}"
                )
                batch_work_dir = batch_staging_dir if formal_stage6_apply else intraday_dir
                try:
                    result = update_intraday_1m_for_symbols(
                        batch,
                        resolved_target,
                        provider_order=list(INTRADAY_PROVIDER_ORDER),
                        source="symbols",
                        dry_run=dry_run,
                        network=daily_network_effective,
                        execute_after_update=False,
                        update_portfolio_after_execution=False,
                        intraday_dir=batch_work_dir,
                        existing_intraday_dir=intraday_dir,
                        materialize_local_existing=formal_stage6_apply,
                        output_path=batches_dir / f"batch_{index:04d}.json",
                        run_id=current_run_id,
                        require_raw_price_basis=True,
                        require_complete_session=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "ok": False,
                        "status": "failed",
                        "run_id": current_run_id,
                        "target_date": resolved_target,
                        "require_raw_price_basis": True,
                        "provider_order": list(INTRADAY_PROVIDER_ORDER),
                        "processed_symbols": list(batch),
                        "updated_symbols": 0,
                        "unchanged_symbols": 0,
                        "failed_symbols": len(batch),
                        "updated_symbol_list": [],
                        "unchanged_symbol_list": [],
                        "failed_symbol_list": list(batch),
                        "error": str(exc)[:500],
                    }
                result = dict(result)
                try:
                    formal_audit = audit_intraday_batch(
                        batch,
                        resolved_target,
                        intraday_dir=batch_work_dir,
                        write=False,
                        run_id=current_run_id,
                    )
                except Exception as audit_exc:  # noqa: BLE001
                    formal_audit = {
                        "items": [],
                        "error": str(audit_exc)[:500],
                    }
                if not isinstance(formal_audit, Mapping):
                    formal_audit = {
                        "items": [],
                        "error": "formal_quality_payload_invalid",
                    }
                formal_quality = _formal_batch_quality(batch, formal_audit)
                canonical_import: dict[str, Any] = {
                    "status": "not_applicable",
                    "applied": False,
                }
                if formal_stage6_apply:
                    canonical_import = apply_audited_intraday_batch(
                        staging_dir=batch_staging_dir,
                        canonical_dir=intraday_dir,
                        symbols=batch,
                        target_date=resolved_target,
                        run_id=current_run_id,
                        quality_audit=formal_audit,
                        output_path=(
                            run_dir / "intraday_append_imports" / f"batch_{index:04d}.json"
                        ),
                        apply=True,
                    )
                    if canonical_import.get("status") != "completed":
                        import_reasons = list(canonical_import.get("reason_codes") or [])
                        formal_quality = {
                            "status": "blocked",
                            "formal_accepted_symbols": [],
                            "formal_rejected_symbols": _normalized_symbols(batch),
                            "formal_quality_reasons": {
                                symbol: import_reasons or ["stage6_append_import_blocked"]
                                for symbol in _normalized_symbols(batch)
                            },
                            "items": list(formal_quality.get("items") or []),
                        }
                        result["status"] = "failed"
                        result["failed_symbol_list"] = _normalized_symbols(batch)
                        result["failed_symbols"] = len(_normalized_symbols(batch))
                result.update(
                    {
                        "phase": "analysis_intraday_batches",
                        "contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
                        "run_id": current_run_id,
                        "target_date": resolved_target,
                        "universe_sha256": str(analysis_contract.get("sha256") or ""),
                        "workset_sha256": analysis_workset_sha256,
                        "provider_order": list(INTRADAY_PROVIDER_ORDER),
                        "batch_size": max(1, int(batch_size)),
                        "batch_index": int(index),
                        "require_raw_price_basis": True,
                        "processed_symbols": _normalized_symbols(batch),
                        "formal_accepted_symbols": list(
                            formal_quality["formal_accepted_symbols"]
                        ),
                        "formal_rejected_symbols": list(
                            formal_quality["formal_rejected_symbols"]
                        ),
                        "formal_quality_reasons": dict(
                            formal_quality["formal_quality_reasons"]
                        ),
                        "formal_quality_items": list(formal_quality["items"]),
                        "stage6_append_only": bool(formal_stage6_apply),
                        "canonical_import": canonical_import,
                    }
                )
                output_path = batches_dir / f"batch_{index:04d}.json"
                if not dry_run:
                    atomic_write_json(output_path, result)
                return index, batch, result

            def record_batch(
                index: int,
                batch: list[str],
                result: Mapping[str, Any],
            ) -> None:
                nonlocal completed_symbols
                evidence = _checkpoint_batch_artifact(
                    root=root,
                    run_dir=run_dir,
                    batch_index=index,
                    batch=batch,
                    result=result,
                )
                accepted_symbols.update(evidence["accepted_symbols"])
                failed_symbols.update(evidence["failed_symbols"])
                completed_symbols = _normalized_symbols(
                    list(completed_symbols) + list(batch)
                )
                batch_results.append(
                    {
                        "batch": index,
                        "status": result.get("status"),
                        "processed_count": len(batch),
                        "updated_symbols": int(result.get("updated_symbols") or 0),
                        "unchanged_symbols": int(result.get("unchanged_symbols") or 0),
                        "failed_symbols": int(result.get("failed_symbols") or 0),
                        "failed_symbol_list": list(result.get("failed_symbol_list") or []),
                        "stage6_append_only": result.get("stage6_append_only") is True,
                        "canonical_import_status": str(
                            dict(result.get("canonical_import") or {}).get("status")
                            if isinstance(result.get("canonical_import"), Mapping)
                            else ""
                        ),
                        "canonical_import_applied": bool(
                            dict(result.get("canonical_import") or {}).get("applied")
                            if isinstance(result.get("canonical_import"), Mapping)
                            else False
                        ),
                    }
                )
                batch_evidence.append(evidence)
                if not dry_run:
                    _write_batch_checkpoint(
                        path=run_dir / "checkpoint.json",
                        phase="analysis_intraday_batches",
                        run_id=current_run_id,
                        target_date=resolved_target,
                        universe_sha256=str(analysis_contract.get("sha256") or ""),
                        workset_sha256=analysis_workset_sha256,
                        workset_symbols=analysis_workset_symbols,
                        provider_order=list(INTRADAY_PROVIDER_ORDER),
                        batch_size=batch_size,
                        total_batch_count=batch_count,
                        completed_batch_count=len(batch_evidence),
                        processed_symbol_count=len(completed_symbols),
                        total_symbol_count=len(analysis_to_update),
                        last_batch={
                            "batch": index,
                            "status": str(result.get("status") or "failed"),
                            "processed_count": len(batch),
                            "accepted_symbols": list(evidence["accepted_symbols"]),
                            "failed_symbols": list(evidence["failed_symbols"]),
                        },
                        accepted_symbols=sorted(accepted_symbols),
                        failed_symbols=sorted(failed_symbols),
                        batch_evidence=batch_evidence,
                    )

            worker_count = max(1, min(int(batch_workers or 1), 8))
            if worker_count == 1 or len(batch_specs) <= 1:
                for spec in batch_specs:
                    record_batch(*run_batch(spec))
            else:
                with ThreadPoolExecutor(
                    max_workers=min(worker_count, len(batch_specs)),
                    thread_name_prefix="intraday-batch",
                ) as executor:
                    future_map = {
                        executor.submit(run_batch, spec): spec for spec in batch_specs
                    }
                    for future in as_completed(future_map):
                        record_batch(*future.result())
        base_manifest["components"]["analysis_intraday_batches"] = batch_results

        critical_provenance_symbols: set[str] = set()
        critical_import_payload = critical_update.get("canonical_import")
        if (
            isinstance(critical_import_payload, Mapping)
            and critical_import_payload.get("status") == "completed"
        ):
            critical_provenance_symbols.update(_normalized_symbols(critical_to_update))
        analysis_provenance_symbols = set(accepted_symbols)
        analysis_provenance_symbols.update(
            _normalized_symbols(recovery_payload.get("accepted_symbols") or [])
        )

        critical_audit = (
            audit_intraday_batch(
                critical_audit_symbols,
                resolved_target,
                intraday_dir=intraday_dir,
                output_path=run_dir / "critical_intraday_quality.json",
                write=not dry_run,
                run_id=current_run_id,
                price_basis_by_symbol={
                    symbol: "raw" for symbol in critical_provenance_symbols
                },
            )
            if critical_symbols
            else _empty_audit(resolved_target, current_run_id)
        )
        analysis_audit = (
            _empty_audit(resolved_target, current_run_id)
            if skip_analysis_pool_minute
            else audit_intraday_batch(
                analysis_audit_symbols,
                resolved_target,
                intraday_dir=intraday_dir,
                output_path=run_dir / "analysis_pool_intraday_quality.json",
                write=not dry_run,
                run_id=current_run_id,
                price_basis_by_symbol={
                    symbol: "raw" for symbol in analysis_provenance_symbols
                },
            )
        )
        critical_coverage = _coverage_from_audit(
            critical_audit,
            requested_count=len(critical_symbols),
            tier="critical",
            legitimate_no_data_symbols=critical_legitimate,
        )
        if not include_critical_minute:
            critical_coverage.update({
                "status": "not_requested", "reason_codes": ["minute_processing_not_requested"],
            })
        analysis_coverage = _coverage_from_audit(
            analysis_audit,
            requested_count=0 if skip_analysis_pool_minute else len(analysis_symbols),
            tier="current_analysis_pool",
            legitimate_no_data_symbols=() if skip_analysis_pool_minute else analysis_legitimate,
        )
        if skip_analysis_pool_minute:
            analysis_coverage.update({
                "status": "not_requested",
                "universe_count": len(analysis_symbols),
                "not_assessed_count": len(minute_not_assessed),
                "not_assessed_symbols": minute_not_assessed,
                "reason_codes": ["analysis_pool_minute_on_demand"],
            })
            base_manifest["warnings"].append("analysis_pool_minute_not_requested")
        base_manifest["components"]["minute_quality"] = {
            "critical": {
                "status": critical_coverage["status"],
                "summary": critical_audit.get("summary") or {},
                "path": critical_audit.get("path") or "",
            },
            "current_analysis_pool": {
                "status": analysis_coverage["status"],
                "summary": analysis_audit.get("summary") or {},
                "path": analysis_audit.get("path") or "",
            },
        }
        base_manifest["coverage"]["critical"] = critical_coverage
        base_manifest["coverage"]["current_analysis_pool"] = analysis_coverage

        if rebuild_features and not dry_run and analysis_symbols:
            feature_payload = build_daily_features(
                only_low_price=bool(low_price_filter.get("used")),
                symbols=analysis_symbols,
                daily_dir=root / "data" / "daily",
                low_price_universe_path=(
                    root / "data" / "universe" / "low_price_learning_symbols.csv"
                ),
                output_path=(
                    root / "data" / "features" / "daily_features_latest.csv"
                    if symbols is None
                    else run_dir / "daily_features.csv"
                ),
                summary_path=(
                    root / "data" / "features" / "daily_features_summary.json"
                    if symbols is None
                    else run_dir / "daily_features_summary.json"
                ),
            )
        else:
            feature_payload = {
                "ok": True,
                "status": "skipped",
                "reason": (
                    "dry_run"
                    if dry_run
                    else "disabled_by_config"
                    if not rebuild_features
                    else "research_pool_empty_filter_not_selected"
                ),
            }
        base_manifest["components"]["features"] = feature_payload
        if dry_run:
            feature_gate = {
                "status": "not_evaluated",
                "reason_codes": ["dry_run"],
            }
        elif not rebuild_features or not analysis_symbols:
            feature_gate = {
                "status": "skipped",
                "reason_codes": [
                    "disabled_by_config"
                    if not rebuild_features
                    else "research_pool_empty_filter_not_selected"
                ],
                "missing_symbols": [],
                "invalid_symbols": [],
                "stale_symbols": [],
            }
        else:
            feature_gate = _feature_coverage(
                feature_payload,
                analysis_symbols,
                resolved_target,
                analysis_legitimate,
                output_path=(
                    root / "data/features/daily_features_latest.csv"
                    if symbols is None else run_dir / "daily_features.csv"
                ),
            )
        base_manifest["coverage"]["features"] = feature_gate


        protected_after = _protected_fingerprints(root)
        base_manifest["protected_fingerprints_after"] = protected_after
        base_manifest["protected_state_unchanged"] = (
            base_manifest["protected_fingerprints_before"] == protected_after
        )
        retry_symbols = sorted(
            set(critical_coverage["failed_symbols"])
            | set(analysis_coverage["failed_symbols"])
            | set(daily_payload.get("failed_symbol_list") or [])
            | set(feature_gate.get("missing_symbols") or [])
            | set(feature_gate.get("invalid_symbols") or [])
            | set(feature_gate.get("excluded_symbols") or [])
            | set(feature_gate.get("stale_symbols") or [])
            | set(session_unknown)
        )
        base_manifest["retry_symbols"] = retry_symbols
        minute_failed_symbols = sorted(
            set(critical_coverage.get("failed_symbols") or [])
            | set(analysis_coverage.get("failed_symbols") or [])
        )
        minute_free_window = _free_window_gap_status(
            resolved_target,
            minute_failed_symbols,
            calendar_path=(
                root / "data" / "calendar" / "a_share_trading_calendar.csv"
            ),
        )
        base_manifest["components"]["minute_free_window"] = minute_free_window
        if minute_free_window.get("status") == "permanently_missing_free_window":
            base_manifest["warnings"].append("permanently_missing_free_window")
        if critical_source_blocked:
            base_manifest["errors"].append("critical_source_manifest_blocked")
            base_manifest["warnings"].extend(
                str(item) for item in (critical_source_manifest.get("failed_sources") or [])
            )
        if universe_refresh_blocked:
            base_manifest["errors"].append("universe_refresh_blocked")
        elif universe_refresh_payload.get("research_fallback"):
            base_manifest["warnings"].append("universe_refresh_failed_using_previous_roster")
        if sector_refresh_blocked:
            base_manifest["warnings"].append("sector_refresh_blocked")
        if critical_conflict_isolation["status"] == "isolated":
            base_manifest["warnings"].append(
                "critical_append_conflict_isolated_from_global_fetch"
            )
        daily_hole_codes = {
            "daily_symbols_stale",
            "daily_symbols_missing",
            "daily_symbols_failed",
        }
        feature_hole_codes = {
            "feature_component_not_passed",
            "feature_requested_count_mismatch",
            "feature_count_mismatch",
            "feature_output_row_count_mismatch",
            "feature_symbol_set_mismatch",
            "feature_target_date_mismatch",
            "feature_symbols_skipped",
            "feature_symbols_excluded",
        }
        daily_hard_codes = [
            code
            for code in (daily_gate.get("reason_codes") or [])
            if code not in daily_hole_codes
        ]
        isolated_daily_basis = set(daily_gate.get("invalid_price_basis_symbols") or [])
        if isolated_daily_basis and isolated_daily_basis.issubset(set(feature_gate.get("excluded_symbols") or [])):
            daily_hard_codes = [code for code in daily_hard_codes if code != "daily_price_basis_not_raw"]
        feature_hard_codes = [
            code
            for code in (feature_gate.get("reason_codes") or [])
            if code not in feature_hole_codes
        ]
        labeled_daily_holes = _normalized_symbols(
            list(session_unknown)
            + list(daily_gate.get("stale_symbols") or [])
            + list(daily_gate.get("missing_symbols") or [])
            + list(feature_gate.get("stale_symbols") or [])
            + list(feature_gate.get("missing_symbols") or [])
            + list(feature_gate.get("excluded_symbols") or [])
        )
        if session_unknown:
            base_manifest["warnings"].append("session_unknown_labeled_not_blocking")
        if labeled_daily_holes:
            base_manifest["warnings"].append("daily_holes_labeled_not_fillable_tonight")

        if dry_run:
            status = "dry_run"
        elif universe_refresh_blocked:
            status = "blocked"
        elif critical_source_blocked:
            status = "blocked"
        elif daily_hard_codes:
            status = "blocked"
        elif critical_daily_gate["status"] != "passed":
            status = "blocked"
        elif rebuild_features and analysis_symbols and feature_hard_codes:
            status = "blocked"
        elif not base_manifest.get("protected_state_unchanged"):
            status = "blocked"
        else:
            status = "completed"

        not_fillable_tonight = _normalized_symbols(
            list(analysis_coverage.get("failed_symbols") or [])
            + list(critical_coverage.get("failed_symbols") or [])
            + labeled_daily_holes
            + minute_not_assessed
        )
        base_manifest["fill_labels"] = {
            "not_fillable_tonight": not_fillable_tonight,
            "reason": "missing_incomplete_or_unassessed_price_evidence",
            "blocks_chain": False,
            "daily_holes": labeled_daily_holes,
            "minute_not_assessed": minute_not_assessed,
        }
        if minute_failed_symbols:
            base_manifest["warnings"].append("minute_gaps_labeled_not_fillable_tonight")

        if status == "completed" and sector_refresh_blocked:
            base_manifest["warnings"].append("sector_refresh_blocked")
        if status == "completed" and critical_conflict_isolation["status"] == "isolated":
            base_manifest["warnings"].append(
                "critical_append_conflict_isolated_from_global_fetch"
            )

        if status == "completed":
            if not_fillable_tonight:
                next_action = (
                    "日线与特征已处理；缺失或尚未核验成交证据的股票不可成交，不拦研究链。"
                )
            else:
                next_action = "数据链已通过；下游只能消费本 run_id 和 target_date 的产物。"
        elif status == "partial":
            if critical_conflict_isolation["status"] == "isolated":
                next_action = (
                    "critical 冲突批已隔离且未覆盖 canonical；其余抓取继续，"
                    "冲突证据需按 section 15.1 单独复核。"
                )
            else:
                next_action = "关键分钟已通过，但分析池不足 100%；按失败清单重试，历史学习保持降级。"
        elif status == "dry_run":
            next_action = "dry-run 未发布 authoritative latest；可显式启用网络执行正式链。"
        else:
            next_action = "按 retry_symbols 重试；未通过前，下游结论保持 blocked/degraded。"

        base_manifest.update(
            {
                "ok": status in {"completed", "dry_run"},
                "status": status,
                "completed_at": now_text(),
                "generated_at": now_text(),
                "next_action": next_action,
            }
        )

        if formal_authorized:
            _run_candidate_refresh_step(
                root=root,
                manifest=base_manifest,
                captured_at=now_text(),
            )

        features_ran = str(feature_payload.get("status") or "") not in {"skipped", "not_evaluated", ""}
        if formal_authorized and rebuild_features and features_ran:
            try:
                plan_freeze = freeze_t_plus_one_plan(
                    project_root=root, data_chain_manifest=base_manifest, write=False,
                )
            except Exception as exc:  # noqa: BLE001 - retain data results and the prior plan
                plan_freeze = {
                    "ok": False, "status": "blocked", "written": False,
                    "reason_codes": ["conditional_plan_prepare_exception"],
                    "error": f"{type(exc).__name__}:{exc}"[:1000],
                }
        else:
            plan_freeze = {
                "ok": True,
                "status": "not_evaluated",
                "written": False,
                "reason_codes": [
                    "dry_run"
                    if dry_run
                    else "feature_rebuild_disabled"
                    if formal_authorized and not rebuild_features
                    else "research_pool_empty_filter_not_selected"
                    if formal_authorized and rebuild_features and not features_ran
                    else "authoritative_network_run_required"
                ],
                "data_chain_run_id": current_run_id,
                "plan_date": resolved_target,
                "boundaries": {
                    "research_only": True,
                    "paper_account_only": True,
                    "broker_route": False,
                    "live_execution": False,
                    "paper_ledger_mutated": False,
                },
                "disclaimer": DISCLAIMER,
            }
        prepared_plan = (
            dict(plan_freeze.get("prepared_plan") or {})
            if isinstance(plan_freeze.get("prepared_plan"), Mapping)
            else {}
        )
        plan_freeze_summary = {
            key: value
            for key, value in plan_freeze.items()
            if key != "prepared_plan"
        }
        base_manifest["components"]["conditional_plan_freeze"] = plan_freeze_summary
        plan_freeze_status = str(plan_freeze.get("status") or "")
        if (
            status == "completed"
            and rebuild_features
            and features_ran
            and plan_freeze_status not in {"prepared", "completed", "not_evaluated"}
        ):
            base_manifest["warnings"].append("conditional_plan_freeze_blocked")
            base_manifest["warnings"].extend(
                str(item) for item in (plan_freeze.get("reason_codes") or [])
            )
            next_action = str(plan_freeze.get("next_action") or "").strip() or (
                "数据质量已通过，但 T+1 条件计划冻结门未通过；保留旧计划且不补造计划。"
            )
            base_manifest.update(
                {
                    "ok": True,
                    "status": status,
                    "generated_at": now_text(),
                    "next_action": next_action,
                }
            )

        publish = formal_authorized
        base_manifest["published_latest"] = publish
        base_manifest["manifest_path"] = _display_path(manifest_path, root)
        if publish:
            health_snapshot = _refresh_data_health_snapshot(root, base_manifest)
        else:
            health_snapshot = {
                "ok": True,
                "status": "skipped",
                "reason": (
                    "dry_run"
                    if dry_run
                    else "explicit_symbol_subset"
                    if symbols is not None
                    else "network_not_authorized"
                ),
                "path": _display_path(
                    root / "results" / "data_health" / "data_health_dashboard_latest.json",
                    root,
                ),
            }
        base_manifest["components"]["data_health_snapshot"] = health_snapshot
        base_manifest["ui_snapshot_ready"] = bool(health_snapshot.get("ok"))
        base_manifest["components"]["forward_pit_readiness"] = _refresh_forward_pit_readiness(
            root
        )
        base_manifest["components"]["t0_birth_evidence"] = _refresh_t0_birth_evidence(
            root
        )
        if publish and not health_snapshot.get("ok"):
            base_manifest["warnings"].append(
                "data_health_snapshot_refresh_failed"
            )
            next_action = (
                "Daily bars and features were processed, but the "
                "data-health snapshot refresh failed. Keep the UI degraded."
            )
            base_manifest.update(
                {
                    "ok": status in {"completed", "dry_run"},
                    "status": status,
                    "generated_at": now_text(),
                    "next_action": next_action,
                }
            )

        protected_after_plan = _protected_fingerprints(root)
        base_manifest["protected_fingerprints_after"] = protected_after_plan
        base_manifest["protected_state_unchanged"] = (
            base_manifest["protected_fingerprints_before"] == protected_after_plan
        )
        if not base_manifest["protected_state_unchanged"]:
            status = "blocked"
            base_manifest["ok"] = False
            base_manifest["status"] = status
            base_manifest["errors"].append(
                "protected_state_mutated_during_plan_freeze"
            )
            base_manifest["next_action"] = (
                "保护性账本或复权因子在计划准备阶段发生变化；不发布新条件计划，"
                "请先审计变更。"
            )

        if status == "completed" and plan_freeze_status == "prepared":
            if not prepared_plan:
                base_manifest["components"]["conditional_plan_freeze"].update({
                    "status": "blocked", "prepared": False, "written": False,
                    "reason_codes": ["conditional_plan_prepared_payload_missing"],
                })
                base_manifest["warnings"].append(
                    "conditional_plan_prepared_payload_missing"
                )
            else:
                # The canonical writer is the archive-aware publisher, never a
                # bare write over latest: it archives the previous latest and the
                # new plan under its trade date, then replaces latest. Any
                # failure leaves the prior latest untouched, matching the
                # "retain prior plan on failure" contract below.
                try:
                    reuse = base_manifest.get("components", {}).get("upstream_reuse")
                    reuse = reuse if isinstance(reuse, Mapping) else {}
                    published_plan = publish_archived_conditional_plan(
                        root,
                        prepared_plan,
                        replace_same_date=bool(reuse.get("reused")),
                    )
                except Exception as exc:  # noqa: BLE001 - retain prior plan on failure
                    published_plan = {
                        "status": "blocked",
                        "published_latest": False,
                        "reason_codes": [
                            f"conditional_plan_publish_exception:{type(exc).__name__}"
                        ],
                        "error": f"{type(exc).__name__}:{exc}"[:1000],
                    }
                plan_component = base_manifest["components"]["conditional_plan_freeze"]
                if published_plan.get("status") in {"completed", "idempotent"}:
                    plan_component.update(
                        {
                            "status": "completed",
                            "prepared": False,
                            "written": True,
                            "path": _display_path(
                                root / "results" / "conditional_trade_plans_latest.json",
                                root,
                            ),
                            "publish_status": published_plan.get("status"),
                            "archive_path": published_plan.get("archive_path", ""),
                        }
                    )
                else:
                    plan_component.update(
                        {
                            "status": "blocked",
                            "prepared": False,
                            "written": False,
                            "reason_codes": list(
                                published_plan.get("reason_codes") or []
                            ),
                        }
                    )
                    base_manifest["warnings"].append(
                        "conditional_plan_publish_failed:"
                        f"{published_plan.get('status')}"
                    )
                    base_manifest["next_action"] = (
                        "计划已在内存完成校验，但归档发布失败；旧计划保持不变。"
                    )

        if formal_authorized:
            current_state_update = _update_current_state(root, base_manifest)
        else:
            current_state_update = {
                "ok": True,
                "status": "skipped",
                "reason": "dry_run" if dry_run else "network_not_authorized",
            }
        base_manifest["components"]["runtime_state_update"] = current_state_update
        if formal_authorized and current_state_update.get("ok") is not True:
            base_manifest["warnings"].append("runtime_state_update_failed")

        data_directory_fingerprints_after = _stage6_data_fingerprints(root)
        base_manifest["data_directory_fingerprints_after"] = (
            data_directory_fingerprints_after
        )
        base_manifest.update(
            {
                "ok": status in {"completed", "dry_run"},
                "status": status,
                "generated_at": now_text(),
            }
        )
        return _publish_manifest(
            base_manifest,
            root=root,
            manifest_path=manifest_path,
            latest_path=latest_path,
            publish_latest=publish,
        )


def run_daily_data_chain(
    project_root: Path = PROJECT_ROOT,
    *,
    target_date: str | None = None,
    symbols: Sequence[str] | None = None,
    network: bool = False,
    dry_run: bool = False,
    run_id: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_workers: int = 4,
    rebuild_features: bool = True,
    retry_from_latest: bool = False,
    skip_analysis_pool_minute: bool = True,
    include_critical_minute: bool = False,
    refresh_universe: bool = True,
    use_low_price_filter: bool = False,
    trigger: str = "manual",
    upstream_reuse: Mapping[str, Any] | None = None,
    pipeline_run_id: str = "",
) -> dict[str, Any]:
    """Run the chain and always leave an auditable terminal result."""

    root = Path(project_root).resolve()
    current_run_id = str(run_id or new_run_id())
    formal_authorized = bool(network and not dry_run and symbols is None)
    # Scheduled 18:30 retry must no-op when the day already has an
    # authoritative completed chain. Pipeline Stage 3 passes upstream_reuse
    # and must reach _run_daily_data_chain_impl, which already refuses to
    # let that skip preempt verified reuse.
    if (
        retry_from_latest
        and symbols is None
        and not dry_run
        and upstream_reuse is None
    ):
        preflight = preflight_daily_data_chain(
            root,
            target_date=target_date,
            current_run_id=current_run_id,
            retry_from_latest=True,
        )
        if preflight.get("action") == "skip":
            return {
                "ok": True,
                "schema_version": API_SCHEMA_VERSION,
                "task_type": "daily_data_chain",
                "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
                "status": "skipped_already_completed",
                "authoritative": True,
                "published_latest": False,
                "run_id": current_run_id,
                "target_date": preflight.get("target_date"),
                "target_date_note": preflight.get("target_date_note"),
                "recovery_source_run_id": "",
                "recovery_kind": "none",
                "completed_run_ids": preflight.get("completed_run_ids") or [],
                "recovery_selection": preflight.get("recovery_selection") or {},
                "reason_codes": preflight.get("reason_codes") or [],
                "next_action": (
                    "同一目标日已有 authoritative completed 运行；"
                    "未启动 providers、锁、特征重建或 append-only 发布。"
                ),
                "network": bool(network),
                "dry_run": bool(dry_run),
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
            }

    failure_context: dict[str, Any] = {}
    prelock_metadata = read_json(
        root / "state" / "daily_data_chain.lock.json",
        {},
    )
    prelock_terminal = read_json(
        root / "results" / "data_chain" / "daily_data_chain_latest.json",
        {},
    )
    if not isinstance(prelock_metadata, Mapping):
        prelock_metadata = {}
    if not isinstance(prelock_terminal, Mapping):
        prelock_terminal = {}
    try:
        try:
            lock_target_date, _ = infer_target_date(target_date)
        except Exception:  # noqa: BLE001 - impl reports an explicit target error
            lock_target_date = str(target_date or "")[:10]
        with _daily_chain_lock(
            root,
            current_run_id,
            target_date=lock_target_date,
        ) as outer_lock:
            return _run_daily_data_chain_impl(
                project_root=root,
                target_date=target_date,
                symbols=symbols,
                network=network,
                dry_run=dry_run,
                run_id=current_run_id,
                batch_size=batch_size,
                batch_workers=batch_workers,
                _failure_context=failure_context,
                rebuild_features=rebuild_features,
                retry_from_latest=retry_from_latest,
                skip_analysis_pool_minute=skip_analysis_pool_minute,
                include_critical_minute=include_critical_minute,
                refresh_universe=refresh_universe,
                use_low_price_filter=use_low_price_filter,
                trigger=trigger,
                upstream_reuse=upstream_reuse,
                pipeline_run_id=pipeline_run_id,
                _lock_already_held=bool(outer_lock.get("acquired")),
                _force_busy=not bool(outer_lock.get("acquired")),
                _prelock_metadata=prelock_metadata,
                _prelock_terminal=prelock_terminal,
            )
    except Exception as exc:  # noqa: BLE001
        try:
            resolved_target, target_note = infer_target_date(target_date)
        except Exception:  # noqa: BLE001
            resolved_target = str(target_date or "")[:10]
            target_note = "target_date_resolution_failed"
        completed_at = now_text()
        error_text = str(exc).replace("\r", " ").replace("\n", " ").strip()
        if not error_text:
            error_text = type(exc).__name__
        error_text = error_text[:1000]
        manifest_path = (
            root
            / "results"
            / "data_chain"
            / "runs"
            / current_run_id
            / "manifest.json"
        )
        latest_path = (
            root
            / "results"
            / "data_chain"
            / "daily_data_chain_latest.json"
        )
        active_lock = _lock_is_held(root / "state" / "daily_data_chain.lock")
        publish = bool(formal_authorized and not active_lock)
        protected_before = dict(
            failure_context.get("protected_fingerprints_before") or {}
        )
        data_directory_fingerprints_before = dict(
            failure_context.get("data_directory_fingerprints_before") or {}
        )
        try:
            data_directory_fingerprints_after = _stage6_data_fingerprints(root)
        except Exception:  # noqa: BLE001 - failure manifest remains publishable
            data_directory_fingerprints_after = {}
        protected_after: dict[str, dict[str, Any]] = {}
        protected_error = ""
        try:
            protected_after = _protected_fingerprints(root)
        except Exception as fingerprint_exc:  # noqa: BLE001
            protected_error = (
                f"{type(fingerprint_exc).__name__}:{fingerprint_exc}"
            )[:1000]
        expected_protected_keys = {"paper_ledger", "adjustment_factors"}
        protected_verified = (
            set(protected_before) == expected_protected_keys
            and set(protected_after) == expected_protected_keys
        )
        protected_unchanged = (
            protected_before == protected_after if protected_verified else None
        )
        paper_ledger_mutated = (
            protected_before.get("paper_ledger")
            != protected_after.get("paper_ledger")
            if protected_verified
            else None
        )
        failed_manifest: dict[str, Any] = {
            "ok": False,
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "package_version": PACKAGE_VERSION,
            "task_type": "daily_data_chain",
            "daily_data_chain_contract_version": DAILY_DATA_CHAIN_CONTRACT_VERSION,
            "authoritative": publish,
            "run_id": current_run_id,
            "pipeline_run_id": str(pipeline_run_id),
            "trigger": str(trigger or "manual"),
            "status": "failed",
            "started_at": completed_at,
            "completed_at": completed_at,
            "generated_at": completed_at,
            "target_date": resolved_target,
            "target_date_note": target_note,
            "network": bool(network),
            "dry_run": bool(dry_run),
            "refresh_universe": bool(refresh_universe),
            "batch_size": max(1, int(batch_size)),
            "batch_workers": max(1, min(int(batch_workers or 1), 8)),
            "provider_order": {
                "daily": list(DAILY_PROVIDER_ORDER),
                "intraday": list(INTRADAY_PROVIDER_ORDER),
            },
            "components": {},
            "coverage": {},
            "warnings": [],
            "errors": ["unexpected_component_failure"],
            "retry_symbols": [],
            "protected_fingerprints_before": protected_before,
            "protected_fingerprints_after": protected_after,
            "data_directory_fingerprints_before": (
                data_directory_fingerprints_before
            ),
            "data_directory_fingerprints_after": (
                data_directory_fingerprints_after
            ),
            "protected_state_verified": protected_verified,
            "protected_state_unchanged": protected_unchanged,
            "protected_fingerprint_error": protected_error,
            "error_type": type(exc).__name__,
            "error": error_text,
            "published_latest": publish,
            "manifest_path": _display_path(manifest_path, root),
            "next_action": (
                "The daily data chain stopped unexpectedly. Keep downstream "
                "outputs blocked and inspect this run manifest before retrying."
            ),
            "boundaries": {
                "research_only": True,
                "paper_account_only": True,
                "broker_route": False,
                "live_execution": False,
                "paper_ledger_mutated": paper_ledger_mutated,
            },
            "disclaimer": DISCLAIMER,
        }
        if publish:
            current_state_update = _update_current_state(root, failed_manifest)
        else:
            current_state_update = {
                "ok": True,
                "status": "skipped",
                "reason": (
                    "dry_run"
                    if dry_run
                    else "explicit_symbol_subset"
                    if symbols is not None
                    else "network_not_authorized"
                ),
            }
        failed_manifest["components"]["runtime_state_update"] = current_state_update
        if publish and current_state_update.get("ok") is not True:
            failed_manifest["warnings"].append("runtime_state_update_failed")
        if not protected_verified:
            failed_manifest["warnings"].append(
                "protected_state_unverified_on_failure"
            )
        elif not protected_unchanged:
            failed_manifest["errors"].append(
                "protected_state_mutated_on_failure"
            )
        if active_lock:
            health_snapshot = {
                "ok": False,
                "status": "skipped",
                "reason": "another_daily_chain_lock_is_active",
            }
            failed_manifest["warnings"].append(
                "failed_manifest_not_published_while_another_run_is_active"
            )
        elif publish:
            health_snapshot = _refresh_data_health_snapshot(root, failed_manifest)
        else:
            health_snapshot = {
                "ok": True,
                "status": "skipped",
                "reason": "dry_run" if dry_run else "network_not_authorized",
            }
        failed_manifest["components"]["data_health_snapshot"] = health_snapshot
        failed_manifest["ui_snapshot_ready"] = bool(health_snapshot.get("ok"))
        if publish and not health_snapshot.get("ok"):
            failed_manifest["warnings"].append(
                "data_health_snapshot_refresh_failed"
            )
        try:
            return _publish_manifest(
                failed_manifest,
                root=root,
                manifest_path=manifest_path,
                latest_path=latest_path,
                publish_latest=publish,
            )
        except Exception as publish_exc:  # noqa: BLE001
            failed_manifest["published_latest"] = False
            failed_manifest["authoritative"] = False
            failed_manifest["publish_error_type"] = type(publish_exc).__name__
            failed_manifest["publish_error"] = str(publish_exc)[:1000]
            return failed_manifest


def latest_daily_data_chain(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    root = Path(project_root).resolve()
    path = root / "results" / "data_chain" / "daily_data_chain_latest.json"
    lock_path = root / "state" / "daily_data_chain.lock"
    lock_metadata = read_json(
        root / "state" / "daily_data_chain.lock.json",
        {},
    )
    if not isinstance(lock_metadata, Mapping):
        lock_metadata = {}
    lock_held = _lock_is_held(lock_path)
    if lock_held and isinstance(lock_metadata, dict):
        previous_payload = read_json(path, {})
        previous_terminal = {}
        if isinstance(previous_payload, dict) and previous_payload:
            previous_terminal = {
                "run_id": str(previous_payload.get("run_id") or ""),
                "status": str(previous_payload.get("status") or ""),
                "target_date": str(previous_payload.get("target_date") or "")[:10],
                "generated_at": str(previous_payload.get("generated_at") or ""),
            }
        active_run_id = str(lock_metadata.get("run_id") or "")
        checkpoint = read_json(_checkpoint_path(root, active_run_id), {})
        running_payload: dict[str, Any] = {
            "ok": True,
            "exists": True,
            "status": "running",
            "run_id": active_run_id,
            "target_date": str(lock_metadata.get("target_date") or "")[:10],
            "pid": int(lock_metadata.get("pid") or 0),
            "started_at": str(lock_metadata.get("acquired_at") or ""),
            "generated_at": now_text(),
            "authoritative": False,
            "published_latest": False,
            "previous_terminal": previous_terminal,
            "path": _display_path(path, root),
            "message": "The daily data chain is running; terminal evidence is not published yet.",
            "disclaimer": DISCLAIMER,
        }
        active_target_date = str(lock_metadata.get("target_date") or "")[:10]
        if isinstance(checkpoint, Mapping):
            try:
                checkpoint_contract_version = int(
                    checkpoint.get("contract_version") or 0
                )
            except (TypeError, ValueError):
                checkpoint_contract_version = 0
        else:
            checkpoint_contract_version = 0
        if (
            isinstance(checkpoint, dict)
            and active_target_date
            and str(checkpoint.get("run_id") or "") == active_run_id
            and str(checkpoint.get("target_date") or "")[:10] == active_target_date
            and str(checkpoint.get("phase") or "")
            == "analysis_intraday_batches"
            and checkpoint_contract_version == DAILY_DATA_CHAIN_CONTRACT_VERSION
        ):
            running_payload["checkpoint"] = checkpoint
            for key in (
                "phase",
                "contract_version",
                "target_date",
                "universe_sha256",
                "workset_sha256",
                "workset_symbols",
                "provider_order",
                "batch_size",
                "total_batch_count",
                "batch_count",
                "completed_batch_count",
                "processed_symbol_count",
                "completed_symbols",
                "total_symbol_count",
                "progress_percent",
                "last_batch",
                "last_batch_evidence",
                "accepted_symbols",
                "failed_symbols",
                "formal_accepted_symbols",
                "formal_rejected_symbols",
                "formal_quality_reasons",
                "batch_evidence",
                "updated_at",
            ):
                if key in checkpoint:
                    running_payload[key] = checkpoint[key]
        return running_payload
    payload = read_json(path, {})
    lock_run_id = str(lock_metadata.get("run_id") or "")
    metadata_status = str(lock_metadata.get("status") or "")
    terminal_run_id = (
        str(payload.get("run_id") or "")
        if isinstance(payload, dict)
        else ""
    )
    if (
        not lock_held
        and metadata_status == "running"
        and lock_run_id
        and terminal_run_id != lock_run_id
    ):
        previous_terminal = {}
        if isinstance(payload, dict) and payload:
            previous_terminal = {
                "run_id": terminal_run_id,
                "status": str(payload.get("status") or ""),
                "target_date": str(payload.get("target_date") or "")[:10],
                "generated_at": str(payload.get("generated_at") or ""),
            }
        return {
            "ok": False,
            "exists": True,
            "status": "abandoned",
            "authoritative": False,
            "published_latest": False,
            "run_id": lock_run_id,
            "target_date": str(lock_metadata.get("target_date") or "")[:10],
            "pid": int(lock_metadata.get("pid") or 0),
            "started_at": str(lock_metadata.get("acquired_at") or ""),
            "generated_at": now_text(),
            "previous_terminal": previous_terminal,
            "path": _display_path(path, root),
            "message": "The previous process released its OS lock without a matching terminal manifest.",
            "next_action": "Treat the run as failed/abandoned and execute a full idempotent retry.",
            "disclaimer": DISCLAIMER,
        }
    if not isinstance(payload, dict) or not payload:
        lock_path = root / "state" / "daily_data_chain.lock"
        lock_metadata = read_json(
            root / "state" / "daily_data_chain.lock.json",
            {},
        )
        if _lock_is_held(lock_path) and isinstance(lock_metadata, dict):
            return {
                "ok": True,
                "exists": True,
                "status": "running",
                "run_id": str(lock_metadata.get("run_id") or ""),
                "target_date": str(lock_metadata.get("target_date") or "")[:10],
                "pid": int(lock_metadata.get("pid") or 0),
                "started_at": str(lock_metadata.get("acquired_at") or ""),
                "generated_at": now_text(),
                "published_latest": False,
                "path": _display_path(path, root),
                "message": "每日数据链正在运行；终态清单尚未发布。",
                "disclaimer": DISCLAIMER,
            }
        return {
            "ok": True,
            "exists": False,
            "status": "not_run",
            "path": _display_path(path, root),
            "message": "尚未生成每日数据链运行清单。",
            "disclaimer": DISCLAIMER,
        }
    if int(payload.get("daily_data_chain_contract_version") or 0) != DAILY_DATA_CHAIN_CONTRACT_VERSION:
        return {
            **payload,
            "exists": True,
            "status": "superseded_contract",
            "authoritative": False,
            "published_latest": False,
            "path": _display_path(path, root),
            "message": (
                "该清单由旧数据链合同生成，不能作为 authoritative/fresh 证据；"
                "请等待当前锁释放后用新数据链重跑。"
            ),
            "disclaimer": DISCLAIMER,
        }
    return {
        **payload,
        "exists": True,
        "authoritative": True,
        "path": _display_path(path, root),
        "disclaimer": DISCLAIMER,
    }
