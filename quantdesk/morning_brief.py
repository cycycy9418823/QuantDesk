from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantdesk import owner_view, weekly_review
from quantdesk.candidate_contract import strategy_label
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.notifications.redaction import redact_payload
from quantdesk.notifications.server_chan import deliver_server_chan
from quantdesk.utils import atomic_write_json

DISCLAIMER = (
    "Research Only / Paper Account Only / No Broker Route / "
    "Live Execution Locked / Not Investment Advice / Not Position Advice."
)
TERMINAL_CHAIN_STATUSES = frozenset(
    {
        "passed",
        "completed",
        "partial",
        "warning",
        "failed",
        "released",
        "blocked",
        "abandoned",
    }
)
PROHIBITED_MESSAGE_TERMS = ("买入", "卖出", "仓位", "目标价", "止损价")
SHANGHAI = ZoneInfo("Asia/Shanghai")
_RECEIPT_PREFIX = "serverchan_morning_brief_"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


@dataclass(frozen=True)
class MorningBriefPaths:
    project_root: Path
    chain_lock: Path
    chain_runs: Path
    chain_latest: Path
    daily_latest: Path
    data_health_latest: Path
    market_intelligence_latest: Path
    risk_alerts_latest: Path
    readiness_latest: Path
    stage4_maintenance_latest: Path

    @classmethod
    def from_root(cls, project_root: str | Path) -> "MorningBriefPaths":
        root = Path(project_root).resolve()
        return cls(
            project_root=root,
            chain_lock=root / "state/daily_data_chain.lock.json",
            chain_runs=root / "results/data_chain/runs",
            chain_latest=root / "results/data_chain/daily_data_chain_latest.json",
            daily_latest=root / "results/daily_workflow_latest.json",
            data_health_latest=root
            / "results/data_health/data_health_dashboard_latest.json",
            market_intelligence_latest=root
            / "results/market_intelligence/market_intelligence_latest.json",
            risk_alerts_latest=root / "results/risk_alerts_latest.json",
            readiness_latest=root
            / "results/data_health/round7_forward_pit_readiness_latest.json",
            stage4_maintenance_latest=root
            / "results/data_health/stage4_maintenance_evidence_latest.json",
        )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _load_mapping(path: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, {}, "missing"
    except OSError:
        return {}, {}, "unreadable"
    provenance = {
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, provenance, "invalid"
    if not isinstance(payload, dict):
        return {}, provenance, "invalid"
    return payload, provenance, ""


def _default_lock_reader(path: Path) -> Mapping[str, Any]:
    payload, _, error = _load_mapping(path)
    return payload if not error else {}


def _lock_identity(lock: Mapping[str, Any]) -> tuple[str, int | None, str, str]:
    pid: int | None
    try:
        pid = int(lock.get("pid"))
    except (TypeError, ValueError):
        pid = None
    return (
        str(lock.get("run_id") or ""),
        pid,
        str(lock.get("status") or ""),
        str(lock.get("released_at") or ""),
    )


def _valid_chain_manifest(payload: Mapping[str, Any], *, run_dir: Path | None = None) -> bool:
    run_id = str(payload.get("run_id") or "")
    target_date = str(payload.get("target_date") or "")
    status = str(payload.get("status") or "")
    if (
        not _RUN_ID_RE.fullmatch(run_id)
        or not _DATE_RE.fullmatch(target_date)
        or status not in TERMINAL_CHAIN_STATUSES
        or payload.get("dry_run") is True
        or payload.get("authoritative") is False
    ):
        return False
    return run_dir is None or run_dir.name == run_id


def _manifest_sort_key(payload: Mapping[str, Any], path: Path) -> tuple[str, str, str]:
    return (
        str(payload.get("completed_at") or payload.get("generated_at") or ""),
        str(payload.get("target_date") or ""),
        path.parent.name,
    )


def _latest_terminal_archive(
    paths: MorningBriefPaths,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates: list[tuple[tuple[str, str, str], dict[str, Any], dict[str, Any]]] = []
    if not paths.chain_runs.is_dir():
        return {}, {}
    for run_dir in paths.chain_runs.iterdir():
        if not run_dir.is_dir() or not _RUN_ID_RE.fullmatch(run_dir.name):
            continue
        manifest_path = run_dir / "manifest.json"
        payload, provenance, error = _load_mapping(manifest_path)
        if error or not _valid_chain_manifest(payload, run_dir=run_dir):
            continue
        provenance = dict(provenance)
        provenance["path"] = _relative(manifest_path, paths.project_root)
        candidates.append(
            (_manifest_sort_key(payload, manifest_path), payload, provenance)
        )
    if not candidates:
        return {}, {}
    _, payload, provenance = max(candidates, key=lambda item: item[0])
    return payload, provenance


def _blocked_snapshot(reason_code: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "source_mode": "none",
        "reason_codes": [reason_code],
        "chain": {},
        "workflow": {},
        "data_health": {},
        "freshness_lag": {
            "core_daily": None,
            "core_minute": None,
            "sector_snapshot": None,
            "wide_daily": None,
            "wide_minute": None,
        },
        "market_intelligence": {},
        "risk_alerts": {},
        "readiness": {},
        "stage4_maintenance": {},
        "source_provenance": {},
    }


def load_morning_brief_snapshot(
    project_root: str | Path,
    *,
    lock_reader: Callable[[Path], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    paths = MorningBriefPaths.from_root(project_root)
    read_lock = lock_reader or _default_lock_reader
    try:
        lock_before = dict(read_lock(paths.chain_lock) or {})
    except Exception:
        return _blocked_snapshot("chain_lock_invalid")

    lock_status = str(lock_before.get("status") or "")
    lock_snapshot = {
        key: lock_before.get(key)
        for key in ("run_id", "pid", "status", "acquired_at", "released_at", "abandoned_reason")
        if key in lock_before
    }
    if lock_status != "released":
        chain, chain_provenance = _latest_terminal_archive(paths)
        if not chain:
            return {**_blocked_snapshot("terminal_chain_archive_missing"), "lock_snapshot": lock_snapshot}
        reasons = [
            "chain_running" if lock_status == "running" else
            f"chain_lock_{lock_status}" if lock_status else "chain_lock_missing",
            "mutable_latest_not_read",
        ]
        return {
            "status": "degraded",
            "source_mode": "archived_terminal_run",
            "lock_snapshot": lock_snapshot,
            "reason_codes": reasons,
            "chain": chain,
            "workflow": {},
            "data_health": {},
            "market_intelligence": {},
            "risk_alerts": {},
            "readiness": {},
            "stage4_maintenance": {},
            "source_provenance": {"chain": chain_provenance},
        }

    source_paths = {
        "chain": paths.chain_latest,
        "workflow": paths.daily_latest,
        "data_health": paths.data_health_latest,
        "market_intelligence": paths.market_intelligence_latest,
        "risk_alerts": paths.risk_alerts_latest,
        "readiness": paths.readiness_latest,
        "stage4_maintenance": paths.stage4_maintenance_latest,
    }
    values: dict[str, dict[str, Any]] = {}
    provenance: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for name, path in source_paths.items():
        value, item_provenance, error = _load_mapping(path)
        values[name] = value
        if item_provenance:
            item_provenance = dict(item_provenance)
            item_provenance["path"] = _relative(path, paths.project_root)
            provenance[name] = item_provenance
        if error:
            reasons.append(f"{name}_{error}")

    try:
        lock_after = dict(read_lock(paths.chain_lock) or {})
    except Exception:
        return _blocked_snapshot("chain_lock_invalid")
    if _lock_identity(lock_before) != _lock_identity(lock_after):
        return _blocked_snapshot("snapshot_changed_during_read")

    chain = values.get("chain") or {}
    if not _valid_chain_manifest(chain):
        return _blocked_snapshot("chain_manifest_invalid")
    if str(chain.get("run_id")) != str(lock_before.get("run_id") or ""):
        return _blocked_snapshot("chain_lock_manifest_mismatch")

    archive_path = paths.chain_runs / str(chain["run_id"]) / "manifest.json"
    archive, archive_provenance, archive_error = _load_mapping(archive_path)
    if archive_error or archive != chain:
        return _blocked_snapshot("chain_latest_archive_mismatch")
    archive_provenance = dict(archive_provenance)
    archive_provenance["path"] = _relative(archive_path, paths.project_root)
    provenance["chain_archive"] = archive_provenance

    return {
        "status": "ready" if not reasons else "degraded",
        "source_mode": "stable_latest",
        "lock_snapshot": lock_snapshot,
        "reason_codes": _unique(reasons),
        **values,
        "source_provenance": provenance,
    }


def _artifact_day(value: object) -> str:
    text = str(value or "").strip()
    if _DATE_RE.fullmatch(text):
        return text
    if len(text) >= 10 and _DATE_RE.fullmatch(text[:10]):
        return text[:10]
    return ""


def _safe_text(value: object, *, limit: int = 80) -> str:
    # Several artifacts store a sentence list rather than a string (``risk_notes``
    # is the observed case). Falling through to ``str()`` would publish a Python
    # container repr into the brief, so sequences are joined first.
    if isinstance(value, (set, frozenset)):
        value = sorted(str(item) for item in value)
    if isinstance(value, (list, tuple)):
        value = "；".join(
            piece
            for piece in (" ".join(str(item or "").split()) for item in value)
            if piece
        )
    text = " ".join(str(value or "").split())
    if any(term in text for term in PROHIBITED_MESSAGE_TERMS):
        return ""
    return text[: max(0, int(limit))]


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _candidate_rows(workflow: Mapping[str, Any]) -> tuple[list[dict[str, str]], int | None]:
    """Return (bounded detail rows, full unique count).

    The brief reports the full count but only expands a bounded detail list,
    so the count is computed over the complete deduplicated set before the
    detail rows are truncated.
    """

    formal = workflow.get("formal_candidate_intake")
    if isinstance(formal, Mapping):
        source = formal.get("candidates")
        if formal.get("status") != "passed" or not isinstance(source, list):
            return [], None
        if type(formal.get("candidate_count")) is not int or formal["candidate_count"] != len(source):
            return [], None
        rows = [
            {name: _safe_text(raw.get(name), limit=80)
             for name in ("symbol", "name", "strategy_type", "sector", "reason", "risk_notes")}
            for raw in source if isinstance(raw, Mapping) and raw.get("symbol")
        ]
        return (rows[:3], len(rows)) if len(rows) == len(source) else ([], None)
    strategy = workflow.get("auto_strategy")
    if not isinstance(strategy, Mapping):
        return [], None
    rows: list[dict[str, str]] = []
    for key in ("long_candidates", "board_candidates", "standard_candidates"):
        source = strategy.get(key)
        if not isinstance(source, list):
            continue
        for raw in source:
            if not isinstance(raw, Mapping):
                continue
            row = {
                name: _safe_text(raw.get(name), limit=80)
                for name in (
                    "symbol",
                    "name",
                    "strategy_type",
                    "sector",
                    "reason",
                    "risk_notes",
                )
            }
            row["strategy_type"] = row["strategy_type"] or _safe_text(key, limit=40)
            if row["symbol"] and not any(row == existing for existing in rows):
                rows.append(row)
    return rows[:3], len(rows)


def _risk_rows(risk_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    source = risk_payload.get("alerts")
    if not isinstance(source, list):
        return []
    rows: list[dict[str, str]] = []
    for raw in source:
        if not isinstance(raw, Mapping):
            continue
        row = {
            name: _safe_text(raw.get(name), limit=100)
            for name in (
                "severity",
                "alert_type",
                "symbol",
                "sector",
                "message",
                "source",
            )
        }
        if row["message"] or row["alert_type"]:
            rows.append(row)
        if len(rows) >= 3:
            break
    return rows


def _paper_boundary_valid(value: Mapping[str, Any]) -> bool:
    if (
        value.get("paper_only") is True
        and value.get("no_broker") is True
        and value.get("no_real_orders") is True
    ):
        return True
    boundary = str(value.get("broker_boundary") or "")
    return "不接券商" in boundary and "不真实下单" in boundary


def _build_shadow_gate(root: Path, as_of: str) -> tuple[dict[str, Any], str]:
    """Read the day-scoped risk-gate shadow archive for ``as_of``.

    Scoped to the dated archive rather than ``*_latest.json``: latest can carry a
    different day's observation, and reporting yesterday's counts under today's
    date is exactly the scope confusion the brief is supposed to avoid. A missing
    or off-scope archive degrades to a reason code, never to a silent zero.
    """

    archive = root / "results/risk_gate_shadow_daily" / f"{as_of}.json"
    payload, _provenance, error = _load_mapping(archive)
    if error:
        return {}, "shadow_gate_unavailable"
    if _artifact_day(payload.get("as_of")) != as_of:
        return {}, "shadow_gate_scope_mismatch"
    evaluated = payload.get("evaluation_count")
    if isinstance(evaluated, bool) or not isinstance(evaluated, int):
        return {}, "shadow_gate_invalid"

    def _count(name: str) -> int:
        value = payload.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return {
        "evaluation_count": evaluated,
        "would_pass_count": _count("would_pass_count"),
        "would_block_count": _count("would_block_count"),
        "insufficient_evidence_count": _count("insufficient_evidence_count"),
        # Enforcement stays observation-only in Stage 3b; surfacing the flag keeps
        # a reader from mistaking shadow counts for an active risk control.
        "risk_gate_enforced": payload.get("risk_gate_enforced") is True,
    }, ""


def _build_wide_pool(root: Path, as_of: str) -> tuple[dict[str, Any], str]:
    """Summarize canonical daily coverage without re-deriving the pool contract.

    Counts files whose last row already reaches ``as_of``. This is a coverage
    observation over real bytes on disk, not a claim that the weekly maintenance
    succeeded, so a stalled pool shows up as a lower number instead of silence.
    """

    daily_dir = root / "data" / "daily"
    if not daily_dir.is_dir():
        return {}, "wide_pool_unavailable"
    total = 0
    current = 0
    for path in daily_dir.glob("*.csv"):
        total += 1
        if _last_csv_date(path) == as_of:
            current += 1
    if not total:
        return {}, "wide_pool_unavailable"
    return {"daily_current": current, "daily_total": total}, ""


@lru_cache(maxsize=4)
def _open_calendar_dates(root_text: str) -> tuple[str, ...]:
    calendar_path = Path(root_text) / "data/calendar/a_share_trading_calendar.csv"
    try:
        with calendar_path.open("r", newline="", encoding="utf-8-sig") as handle:
            dates = sorted(
                {
                    str(row.get("date") or "")[:10]
                    for row in csv.DictReader(handle)
                    if str(row.get("is_open") or row.get("open") or "1").lower()
                    in {"1", "true", "yes", "y", "open"}
                }
            )
    except (OSError, UnicodeError, csv.Error):
        return ()
    return tuple(item for item in dates if _DATE_RE.fullmatch(item))


def _trading_day_lag(root: Path, target: str, latest: str) -> int | None:
    if not _DATE_RE.fullmatch(target) or not _DATE_RE.fullmatch(latest):
        return None
    dates = list(_open_calendar_dates(str(root.resolve())))
    dates = [item for item in dates if _DATE_RE.fullmatch(item) and item <= target]
    if target not in dates or latest not in dates:
        return None
    return max(0, dates.index(target) - dates.index(latest))


def _last_csv_date(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.readline().decode("utf-8-sig", errors="replace").strip().split(",")
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 8192), os.SEEK_SET)
            lines = handle.read().splitlines()
        if not lines:
            return ""
        row = lines[-1].decode("utf-8-sig", errors="replace")
        values = row.split(",")
        date_index = next(
            (index for index, name in enumerate(header) if name.strip().lower() in {"date", "trade_date", "datetime"}),
            0,
        )
        return values[date_index].strip()[:10] if date_index < len(values) else ""
    except (OSError, UnicodeError):
        return ""


def _included_symbols(path: Path) -> list[str]:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return sorted(
                {
                    str(row.get("symbol") or "").zfill(6)
                    for row in csv.DictReader(handle)
                    if str(row.get("included") or "").strip().lower()
                    in {"1", "true", "yes", "y", "on", "included"}
                }
            )
    except (OSError, UnicodeError, csv.Error):
        return []


def _max_csv_lag(root: Path, directory: Path, symbols: list[str], target: str) -> int | None:
    if not symbols:
        return None
    lags: list[int] = []
    for symbol in symbols:
        latest = _last_csv_date(directory / f"{symbol}.csv")
        lag = _trading_day_lag(root, target, latest)
        if lag is not None:
            lags.append(lag)
    return max(lags, default=0)


def _latest_sector_lag(root: Path, target: str) -> int | None:
    directory = root / "data/sector_history"
    if not directory.is_dir():
        return None
    dates = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_dir() and _DATE_RE.fullmatch(path.name) and (path / "snapshot.json").is_file()
    )
    if not dates:
        return None
    return _trading_day_lag(root, target, dates[-1])


def _latest_wide_daily_contract(
    root: Path, as_of: str
) -> tuple[dict[str, Any], str]:
    directory = root / "data/universe"
    if not directory.is_dir() or not _DATE_RE.fullmatch(as_of):
        return {}, ""
    candidates: list[tuple[str, Path]] = []
    prefix = "weekly_daily_wide_pool_contract_"
    for path in directory.glob(f"{prefix}*.json"):
        date_text = path.stem.removeprefix(prefix)
        if _DATE_RE.fullmatch(date_text) and date_text <= as_of:
            candidates.append((date_text, path))
    for date_text, path in sorted(candidates, reverse=True):
        payload, _, error = _load_mapping(path)
        if error:
            continue
        declared = str(payload.get("target_date") or date_text)
        if declared != date_text:
            continue
        return payload, date_text
    return {}, ""


def _build_freshness_lag(root: Path, as_of: str, chain: Mapping[str, Any]) -> dict[str, Any]:
    core_symbols = _included_symbols(root / "data/universe/low_price_learning_symbols.csv")
    contract, contract_as_of = _latest_wide_daily_contract(root, as_of)
    wide_symbols = [str(item).zfill(6) for item in contract.get("symbols") or []]
    coverage = chain.get("coverage") if isinstance(chain.get("coverage"), Mapping) else {}
    daily_status = coverage.get("daily") if isinstance(coverage.get("daily"), Mapping) else {}
    core_daily = 0 if daily_status.get("status") == "passed" and daily_status.get("stale_count") == 0 else _max_csv_lag(root, root / "data/daily", core_symbols, as_of)
    minute_dir = root / "data/intraday/1m"
    core_minute = _max_csv_lag(root, minute_dir, core_symbols, as_of)
    wide_daily = _max_csv_lag(root, root / "data/daily", wide_symbols, as_of)
    wide_minute = None
    minute_runs = root / "results/data_chain/weekly_minute_wide_pool/runs"
    if minute_runs.is_dir():
        manifests = []
        for path in minute_runs.glob("*/manifest.json"):
            payload, _, error = _load_mapping(path)
            if not error and payload.get("status") in {"completed", "completed_staging_only"}:
                manifests.append(payload)
        if manifests:
            latest_run = max(manifests, key=lambda item: str(item.get("completed_at") or item.get("run_id") or ""))
            pool = latest_run.get("pool_contract") if isinstance(latest_run.get("pool_contract"), Mapping) else {}
            symbols = [str(item).zfill(6) for item in pool.get("symbols") or []]
            wide_minute = _max_csv_lag(root, minute_dir, symbols, as_of)
    return {
        "core_daily": core_daily,
        "core_minute": core_minute,
        "sector_snapshot": _latest_sector_lag(root, as_of),
        "wide_daily": wide_daily,
        "wide_daily_contract_as_of": contract_as_of,
        "wide_daily_contract_lag": (
            _trading_day_lag(root, as_of, contract_as_of) if contract_as_of else None
        ),
        "wide_minute": wide_minute,
    }


def _build_paper_account(
    workflow: Mapping[str, Any], as_of: str
) -> tuple[dict[str, Any], str]:
    dates = workflow.get("dates")
    portfolio = workflow.get("auto_paper_portfolio")
    if not isinstance(dates, Mapping) or not isinstance(portfolio, Mapping):
        return {}, "paper_account_missing"
    if _artifact_day(dates.get("portfolio_valuation_date")) != as_of:
        return {}, "paper_account_scope_mismatch"
    if not _paper_boundary_valid(portfolio):
        return {}, "paper_boundary_invalid"
    summary = portfolio.get("summary")
    if not isinstance(summary, Mapping):
        return {}, "paper_account_invalid"
    total = _finite_number(summary.get("total_equity"))
    previous = _finite_number(summary.get("previous_total_equity"))
    daily_change = _finite_number(summary.get("daily_pnl"))
    if daily_change is None and total is not None and previous is not None:
        daily_change = total - previous
    daily_return = _finite_number(summary.get("daily_return_pct"))
    if total is None or daily_change is None:
        return {}, "paper_account_invalid"
    return {
        "valuation_date": as_of,
        "total_equity": total,
        "previous_total_equity": previous,
        "daily_change": daily_change,
        "daily_return_pct": daily_return,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }, ""


def build_morning_brief(
    project_root: str | Path,
    *,
    now: datetime | None = None,
    snapshot_loader: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    loader = snapshot_loader or load_morning_brief_snapshot
    snapshot = loader(project_root)
    reasons = list(snapshot.get("reason_codes") or [])
    chain = snapshot.get("chain") if isinstance(snapshot.get("chain"), Mapping) else {}
    as_of = str(chain.get("target_date") or "")
    run_id = str(chain.get("run_id") or "")
    generated = now or datetime.now(SHANGHAI)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=SHANGHAI)
    date_check = _brief_business_date(Path(project_root), generated, as_of)
    if date_check["date_reason_code"]:
        reasons.append(date_check["date_reason_code"])
    payload: dict[str, Any] = {
        "schema_version": "1",
        "task_type": "morning_brief",
        "status": "blocked" if snapshot.get("status") == "blocked" else "warning" if date_check["date_reason_code"] else "passed",
        "generated_at": generated.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "as_of": as_of,
        **date_check,
        "run_id": run_id,
        "source_mode": str(snapshot.get("source_mode") or "none"),
        "lock_snapshot": dict(snapshot.get("lock_snapshot") or {}),
        "reason_codes": reasons,
        "data_health": {},
        "candidates": [],
        "candidate_count": None,
        "shadow_gate": {},
        "wide_pool": {},
        "candidate_review_url": "http://127.0.0.1:8000/#daily-focus-section",
        "paper_account": {},
        "risk_items": [],
        "market_observations": [],
        "milestone": {},
        "artifact_scope": {},
        "source_provenance": snapshot.get("source_provenance") or {},
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
        "disclaimer": DISCLAIMER,
    }
    if payload["status"] == "blocked" or not as_of or not run_id:
        payload["reason_codes"] = _unique(reasons or ["verified_snapshot_unavailable"])
        return payload

    coverage = chain.get("coverage") if isinstance(chain.get("coverage"), Mapping) else {}
    daily = coverage.get("daily") if isinstance(coverage.get("daily"), Mapping) else {}
    minute = (
        coverage.get("current_analysis_pool")
        if isinstance(coverage.get("current_analysis_pool"), Mapping)
        else {}
    )
    payload["data_health"] = {
        "chain_status": str(chain.get("status") or "unknown"),
        "daily_status": str(daily.get("status") or "unknown"),
        "daily_requested_count": daily.get("requested_count"),
        "daily_fresh_count": daily.get("fresh_count"),
        "daily_stale_count": daily.get("stale_count"),
        "minute_status": str(minute.get("status") or "unknown"),
        "minute_requested_count": minute.get("requested_count"),
        "minute_effective_complete_count": minute.get("effective_complete_count"),
        "minute_missing_count": minute.get("missing_count"),
        "retry_symbols": [
            _safe_text(item, limit=16) for item in (chain.get("retry_symbols") or [])[:5]
        ],
    }
    payload["freshness_lag"] = _build_freshness_lag(root=Path(project_root), as_of=as_of, chain=chain)

    if snapshot.get("source_mode") == "archived_terminal_run":
        reasons.extend(
            [
                "candidate_review_unavailable_during_chain",
                "paper_account_unavailable_during_chain",
                "milestone_unavailable_during_chain",
            ]
        )
        payload["status"] = "warning"
        payload["reason_codes"] = _unique(reasons)
        return payload

    workflow = (
        snapshot.get("workflow")
        if isinstance(snapshot.get("workflow"), Mapping)
        else {}
    )
    dates = workflow.get("dates") if isinstance(workflow.get("dates"), Mapping) else {}
    workflow_day = _artifact_day(workflow.get("target_date") or dates.get("local_complete_daily_date"))
    workflow_scope_ok = bool(workflow and workflow_day == as_of)
    # Every optional section records the day it actually observed, so a reader can
    # tell *which* artifact is stale instead of only that something was.
    artifact_scope: dict[str, str] = {"workflow": workflow_day}
    scope_mismatch = False
    if workflow_scope_ok:
        raw_candidates, candidate_total = _candidate_rows(workflow)
        formal = workflow.get("formal_candidate_intake")
        if isinstance(formal, Mapping) and (
            _artifact_day(formal.get("target_date")) != as_of
            or str(formal.get("source_run_id") or "") != run_id
        ):
            raw_candidates, candidate_total = [], None
            reasons.append("formal_candidate_source_mismatch")
        if candidate_total is None:
            reasons.append("candidate_review_unavailable")
        payload["candidate_count"] = candidate_total
        # The brief reports the total but only expands a bounded detail list:
        # the preview is a digest for a phone screen, not a data dump.
        payload["candidates"] = raw_candidates
    elif workflow:
        # The workflow snapshot governs both the candidate digest and the risk
        # section; without an explicit code both would look like "nothing today".
        reasons.append("workflow_scope_mismatch")
        scope_mismatch = True

    # Shadow gate and wide-pool coverage read their own artifacts rather than the
    # workflow snapshot, so a stale workflow cannot silently suppress them.
    root = Path(project_root)
    shadow_gate, shadow_reason = _build_shadow_gate(root, as_of)
    payload["shadow_gate"] = shadow_gate
    if shadow_reason:
        reasons.append(shadow_reason)
    wide_pool, wide_reason = _build_wide_pool(root, as_of)
    payload["wide_pool"] = wide_pool
    if wide_reason:
        reasons.append(wide_reason)

    # The paper-account section is independent of the wide-pool coverage probe.
    # It is built unconditionally: a working pool count must never suppress the
    # account, and a failing one must never be reported as an account scope
    # problem.
    artifact_scope["paper_account"] = _artifact_day(
        dates.get("portfolio_valuation_date")
    )
    paper, paper_reason = _build_paper_account(workflow, as_of)
    payload["paper_account"] = paper
    if paper_reason:
        reasons.append(paper_reason)
        scope_mismatch = scope_mismatch or paper_reason.endswith("_scope_mismatch")

    risk = (
        snapshot.get("risk_alerts")
        if isinstance(snapshot.get("risk_alerts"), Mapping)
        else {}
    )
    risk_day = (
        _artifact_day(risk.get("target_date") or risk.get("generated_at"))
        if risk
        else ""
    )
    artifact_scope["risk_alerts"] = risk_day
    if workflow_scope_ok and risk:
        if risk_day != as_of:
            reasons.append("risk_alerts_scope_mismatch")
            scope_mismatch = True
        elif not _paper_boundary_valid(risk):
            reasons.append("paper_boundary_invalid")
        else:
            payload["risk_items"] = _risk_rows(risk)

    market = (
        snapshot.get("market_intelligence")
        if isinstance(snapshot.get("market_intelligence"), Mapping)
        else {}
    )
    market_day = _artifact_day(market.get("target_date")) if market else ""
    artifact_scope["market_intelligence"] = market_day
    if market:
        if market_day != as_of:
            reasons.append("market_intelligence_scope_mismatch")
            scope_mismatch = True
        else:
            summary = market.get("market_intelligence_summary")
            regime = (
                _safe_text(summary.get("market_regime"), limit=40)
                if isinstance(summary, Mapping)
                else ""
            )
            if regime:
                payload["market_observations"] = [{"market_regime": regime}]
            else:
                # In scope but unusable: the empty list must still carry a code.
                reasons.append("market_regime_unavailable")

    readiness = (
        snapshot.get("readiness")
        if isinstance(snapshot.get("readiness"), Mapping)
        else {}
    )
    stage4 = (
        snapshot.get("stage4_maintenance")
        if isinstance(snapshot.get("stage4_maintenance"), Mapping)
        else {}
    )
    eligible = readiness.get("eligible_forward_days")
    readiness_day = ""
    if isinstance(eligible, list) and eligible:
        latest_eligible = eligible[-1]
        if isinstance(latest_eligible, Mapping):
            readiness_day = _safe_text(latest_eligible.get("date"), limit=10)
        elif isinstance(latest_eligible, str):
            readiness_day = _safe_text(latest_eligible, limit=10)
    stage4_day = _artifact_day(stage4.get("as_of")) if stage4 else ""
    artifact_scope["readiness"] = readiness_day
    artifact_scope["stage4_maintenance"] = stage4_day
    if readiness and stage4 and readiness_day and stage4_day:
        # Keep the dated evidence visible even when it is stale.  An older
        # Stage4 result is useful context, but it must remain explicitly
        # off-scope for today's brief and continue to raise the scope warning.
        current_for_as_of = readiness_day == as_of and stage4_day == as_of
        payload["milestone"] = {
            "t0": str(readiness.get("t0") or ""),
            "readiness_status": _safe_text(readiness.get("status"), limit=64),
            "maintenance_status": _safe_text(stage4.get("status"), limit=64),
            "readiness_evidence_as_of": readiness_day,
            "evidence_as_of": stage4_day,
            "current_for_as_of": current_for_as_of,
            "reason_codes": [
                _safe_text(item, limit=80)
                for item in (stage4.get("reason_codes") or [])[:5]
            ],
            "account_replay": _safe_text(stage4.get("account_replay"), limit=64),
            "conclusion_drawn": stage4.get("conclusion_drawn") is True,
        }
        if not current_for_as_of:
            reasons.append("milestone_scope_mismatch")
            if readiness_day != as_of:
                reasons.append("readiness_scope_mismatch")
                scope_mismatch = True
            if stage4_day != as_of:
                reasons.append("stage4_maintenance_scope_mismatch")
                scope_mismatch = True
    elif readiness or stage4:
        reasons.append("milestone_scope_mismatch")
        if readiness and readiness_day != as_of:
            reasons.append("readiness_scope_mismatch")
            scope_mismatch = True
        if stage4 and stage4_day != as_of:
            reasons.append("stage4_maintenance_scope_mismatch")
            scope_mismatch = True

    payload["artifact_scope"] = {"as_of": as_of, **artifact_scope}
    if scope_mismatch:
        # Umbrella code kept for readers that only ask "is anything off-scope".
        reasons.append("artifact_scope_mismatch")
    payload["reason_codes"] = _unique(reasons)
    chain_status = str(chain.get("status") or "")
    if payload["reason_codes"] or chain_status not in {"passed", "completed"}:
        payload["status"] = "warning"
    return payload


def _number_text(value: object) -> str:
    number = _finite_number(value)
    if number is None:
        return "不可用"
    return f"{number:,.2f}"


def _candidate_digest(candidates: list[Any]) -> str:
    """Render the bounded candidate detail as one compact phone-screen line.

    Only symbol/name/type: the per-candidate ``reason`` text is near-identical
    across rows (same rule fired), so repeating it would spend the 800-char push
    budget on redundancy instead of on the symbols the reader actually needs.
    """

    parts: list[str] = []
    for row in candidates[:3]:
        if not isinstance(row, Mapping):
            continue
        symbol = _safe_text(row.get("symbol"), limit=8)
        if not symbol:
            continue
        name = _safe_text(row.get("name"), limit=10)
        # Prefer the project's own Chinese label over the raw enum: the push goes
        # to a phone, where "长线" reads and "long" does not.
        kind = _safe_text(row.get("strategy_label"), limit=6)
        if not kind:
            raw_type = _safe_text(row.get("strategy_type"), limit=12)
            kind = _safe_text(strategy_label(raw_type), limit=6) if raw_type else ""
        piece = symbol
        if name:
            piece += f" {name}"
        if kind:
            piece += f"({kind})"
        parts.append(piece)
    return "、".join(parts) if parts else "明细不可用"


def _return_suffix(value: Any) -> str:
    """Append the signed daily return only when it is a finite number."""

    number = _finite_number(value)
    if number is None:
        return ""
    return f" ({number:+.2f}%)"


def render_morning_brief_markdown(payload: Mapping[str, Any]) -> str:
    as_of = _safe_text(payload.get("as_of"), limit=10) or "不可用"
    run_id = _safe_text(payload.get("run_id"), limit=160) or "不可用"
    health = payload.get("data_health") if isinstance(payload.get("data_health"), Mapping) else {}
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    candidate_total = payload.get("candidate_count")
    if not isinstance(candidate_total, int) or isinstance(candidate_total, bool):
        candidate_total = len(candidates) if "candidate_count" not in payload else None
    account = payload.get("paper_account") if isinstance(payload.get("paper_account"), Mapping) else {}
    risk_items = payload.get("risk_items") if isinstance(payload.get("risk_items"), list) else []
    milestone = payload.get("milestone") if isinstance(payload.get("milestone"), Mapping) else {}
    shadow = payload.get("shadow_gate") if isinstance(payload.get("shadow_gate"), Mapping) else {}
    pool = payload.get("wide_pool") if isinstance(payload.get("wide_pool"), Mapping) else {}
    freshness = payload.get("freshness_lag") if isinstance(payload.get("freshness_lag"), Mapping) else {}
    scope = (
        payload.get("artifact_scope")
        if isinstance(payload.get("artifact_scope"), Mapping)
        else {}
    )
    # "No candidate today" and "the candidate source was off-scope" are different
    # facts; rendering both as 不可用 would hide a stale workflow snapshot.
    workflow_in_scope = bool(
        scope.get("as_of") and scope.get("workflow") == scope.get("as_of")
    )
    reasons = [
        _safe_text(item, limit=72)
        for item in (payload.get("reason_codes") or [])[:6]
        if _safe_text(item, limit=72)
    ]

    def _milestone_text() -> str:
        if not milestone:
            return "- 里程碑：不可用"
        text = (
            "- 里程碑："
            f"T0={_safe_text(milestone.get('t0'), limit=10) or '不可用'}；"
            f"{_safe_text(milestone.get('maintenance_status'), limit=64) or '不可用'}"
        )
        if milestone.get("current_for_as_of") is False:
            evidence_day = _safe_text(milestone.get("evidence_as_of"), limit=10)
            text += f"；证据日 {evidence_day or '不可用'}（已过期）"
        return text

    def _lag_text(key: str) -> str:
        value = freshness.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return "不可用（警示）"
        return f"{value} 天" + ("（警示）" if value > 1 else "")

    daily_fresh = health.get("daily_fresh_count")
    daily_requested = health.get("daily_requested_count")
    minute_complete = health.get("minute_effective_complete_count")
    minute_requested = health.get("minute_requested_count")
    legacy_pool_label = "旧池参考，不影响当前全A红绿；" if payload.get("presentation_version") == 2 else ""
    lines = [
        "# QuantDesk 研究早报",
        f"- 数据日期：{as_of}",
        (
            f"- 早报应对应：{_safe_text(payload.get('expected_as_of'), limit=10) or '未核实'}；日期核对："
            + {"current": "已核对", "stale": "旧日资料", "future": "日期超前"}.get(
                str(payload.get("date_status") or ""), "未核实"
            )
        ),
        f"- 数据链：{run_id} / {_safe_text(health.get('chain_status'), limit=32) or '不可用'}",
        (
            "- 数据健康：日线 "
            f"{daily_fresh if daily_fresh is not None else '不可用'}/"
            f"{daily_requested if daily_requested is not None else '不可用'}；分钟 "
            f"{minute_complete if minute_complete is not None else '不可用'}/"
            f"{minute_requested if minute_requested is not None else '不可用'}"
        ),
        (
            f"- 数据新鲜度（{legacy_pool_label}最大落后交易日）：核心日线 "
            f"{_lag_text('core_daily')}；核心分钟 {_lag_text('core_minute')}；"
            f"板块快照 {_lag_text('sector_snapshot')}；宽池日线 {_lag_text('wide_daily')}；"
            f"宽池分钟 {_lag_text('wide_minute')}"
        ),
        (
            f"- 候选复盘：{candidate_total} 条；{_candidate_digest(candidates)}"
            if candidates
            else (
                "- 候选复盘：0 条（今日无候选）"
                if workflow_in_scope and candidate_total == 0
                else "- 候选复盘：不可用"
            )
        ),
        # 入口独立成行：明细摘要可能挤长，但这个链接是读者点进去核对的唯一路径，
        # 不能因为排版被牺牲。
        f"- 复盘入口：{payload.get('candidate_review_url')}",
        (
            "- 纸面账户：估值 "
            f"{_number_text(account.get('total_equity'))}；日内 "
            f"{_number_text(account.get('daily_change'))}"
            f"{_return_suffix(account.get('daily_return_pct'))}"
            if account
            else "- 纸面账户：不可用"
        ),
        (
            "- 风控影子：评估 "
            f"{shadow.get('evaluation_count')} · 证据不足 "
            f"{shadow.get('insufficient_evidence_count')} · 通过 "
            f"{shadow.get('would_pass_count')}"
            + ("（观察中，未启用拦截）" if not shadow.get("risk_gate_enforced") else "（已启用拦截）")
            if shadow
            else "- 风控影子：不可用"
        ),
        (
            (f"- 宽池日线（{legacy_pool_label.rstrip('；')}）：已至今日 " if legacy_pool_label else "- 宽池日线：已至今日 ")
            + f"{pool.get('daily_current')}/{pool.get('daily_total')}"
            if pool
            else ("- 宽池日线：旧池参考，不影响当前全A红绿；不可用" if legacy_pool_label else "- 宽池日线：不可用")
        ),
        _milestone_text(),
        f"- 风险信息：{len(risk_items)} 条本地观察" if risk_items else "- 风险信息：不可用",
        "- 资讯口径：早报不联网抓取资讯，仅使用已落盘的本地研究工件。",
    ]
    if reasons:
        lines.append("- 降级原因：" + ", ".join(reasons))
    lines.extend(["", DISCLAIMER])
    rendered = "\n".join(lines)
    if len(rendered) <= 800:
        return rendered
    compact = [line for line in lines if not line.startswith("- 风险信息：")]
    rendered = "\n".join(compact)
    if len(rendered) <= 800:
        return rendered
    reason_index = next(
        (index for index, line in enumerate(compact) if line.startswith("- 降级原因：")),
        None,
    )
    if reason_index is not None:
        compact[reason_index] = compact[reason_index][:160]
    rendered = "\n".join(compact)
    if len(rendered) <= 800:
        return rendered
    return rendered[: 800 - len(DISCLAIMER) - 2].rstrip() + "\n\n" + DISCLAIMER


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _delivery_window_open(now: datetime) -> bool:
    local = now.astimezone(SHANGHAI)
    return local.weekday() < 5 and time(8, 30) <= local.time().replace(tzinfo=None) <= time(8, 49, 59)


def _load_delivery_state(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {"schema_version": "1", "channels": {}}, ""
    payload, _, error = _load_mapping(path)
    if error or not isinstance(payload.get("channels"), dict):
        return {}, "delivery_state_invalid"
    return payload, ""


def _resolved_within(root: Path, candidate: Path) -> bool:
    """Reject symlinked or aliased paths that leave the project root.

    Receipts and delivery state are the only durable record that an external
    push was attempted, so a path that resolves outside the root would let a
    prior attempt go unseen and allow a duplicate send.
    """

    try:
        resolved_root = Path(root).resolve(strict=False)
        resolved = Path(candidate).resolve(strict=False)
    except OSError:
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _receipt_attempts(directory: Path, day: date) -> tuple[list[dict[str, Any]], str]:
    """Collect the prior same-day attempts, failing closed on any bad receipt.

    Dedupe cannot key on the delivery-state file alone: the provider may confirm
    a send and the state write still fail, which would otherwise re-push the same
    brief. Receipts are written before state, so they are the authoritative
    record of what was already attempted today.
    """

    if not directory.exists():
        return [], ""
    try:
        entries = sorted(directory.glob(f"{_RECEIPT_PREFIX}*.json"))
    except OSError:
        return [], "delivery_receipt_invalid"
    attempts: list[dict[str, Any]] = []
    for entry in entries:
        if entry.is_symlink():
            return [], "delivery_receipt_invalid"
        payload, _, error = _load_mapping(entry)
        if error:
            return [], "delivery_receipt_invalid"
        observed = _parse_receipt_day(payload.get("observed_at"))
        if observed is None:
            return [], "delivery_receipt_invalid"
        if observed != day:
            continue
        attempts.append(
            {
                "status": str(payload.get("status") or "").strip(),
                "summary_sha256": str(payload.get("summary_sha256") or "").strip(),
            }
        )
    return attempts, ""


def _parse_receipt_day(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI).date()


def _receipt_path(root: Path, now: datetime, content_sha256: str) -> Path:
    stamp = now.astimezone(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
    return (
        root
        / "results/notifications"
        / f"serverchan_morning_brief_{stamp}_{content_sha256[:12]}.json"
    )


def _brief_business_date(root: Path, now: datetime, as_of: object) -> dict[str, str]:
    """Verify every consulted calendar day, then compare the original data date.

    Taking the largest recorded open date can silently jump over a missing or
    malformed row. Walk backwards instead; only the consulted interval matters,
    so provisional future years do not invalidate a verified historical date.
    """
    from quantdesk.trading_calendar import load_a_share_trading_calendar

    local = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    unverified = {
        "expected_as_of": "", "date_status": "unverified",
        "date_reason_code": "brief_delivery_calendar_unverified",
    }
    calendar = load_a_share_trading_calendar(root / "data/calendar/a_share_trading_calendar.csv")
    lookup = calendar.get("is_open_by_date") or {}
    rows = calendar.get("rows_by_date") or {}
    if calendar.get("weekend_only"):
        return {**unverified, "date_reason_detail": "calendar_not_verified"}
    true_flags = {"1", "true", "t", "yes", "y", "是"}
    false_flags = {"0", "false", "f", "no", "n", "否"}
    cursor = local.date()
    expected = ""
    for _ in range(371):
        day = cursor.isoformat()
        row = rows.get(day)
        if day not in lookup or not isinstance(row, Mapping):
            return {**unverified, "date_reason_detail": f"calendar_interval_row_missing:{day}"}
        if row.get("row_count") != "1":
            return {**unverified, "date_reason_detail": f"calendar_interval_row_ambiguous:{day}"}
        flag = str(row.get("is_open_raw") or "").strip().lower()
        if flag not in true_flags | false_flags or (flag in true_flags) != lookup[day]:
            return {**unverified, "date_reason_detail": f"calendar_interval_open_flag_unverified:{day}"}
        if "provisional" in (str(row.get("source") or "") + " " + str(row.get("note") or "")).lower():
            return {**unverified, "date_reason_detail": f"calendar_interval_provisional:{day}"}
        if cursor < local.date() and lookup[day] is True:
            expected = day
            break
        cursor -= timedelta(days=1)
    if not expected:
        return {**unverified, "date_reason_code": "brief_previous_trade_date_unverified"}
    observed = str(as_of or "")
    try:
        valid_date = bool(_DATE_RE.fullmatch(observed)) and date.fromisoformat(observed).isoformat() == observed
    except ValueError:
        valid_date = False
    if not valid_date:
        return {"expected_as_of": expected, "date_status": "unverified", "date_reason_code": "brief_business_date_missing"}
    if observed == expected:
        return {"expected_as_of": expected, "date_status": "current", "date_reason_code": ""}
    return {"expected_as_of": expected,
            "date_status": "stale" if observed < expected else "future",
            "date_reason_code": "brief_business_date_stale" if observed < expected else "brief_business_date_future"}


OWNER_BRIEF_VERSION = "owner-brief-v1-20261103"


def _weekly_exit_replay(root: Path, payload: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Reuse the existing weekly brief; local replay failure never blocks its body."""
    as_of = str(payload.get("as_of") or "")
    today = now.astimezone(SHANGHAI).date()
    if payload.get("status") == "blocked" or payload.get("date_status") != "current":
        return {"status": "skipped", "reason": "brief_data_not_current"}
    if _calendar_says_closed(root, now) or not weekly_review.is_first_session_of_week(as_of, today):
        return {"status": "skipped", "reason": "not_first_trading_session_of_week"}
    try:
        from quantdesk.research.exit_rule_replay import write_report
        return write_report(root, as_of, root / "reports/research")
    except Exception as exc:  # noqa: BLE001 - optional research must not suppress the brief
        return {"status": "warning", "reason": "weekly_replay_failed", "error_type": type(exc).__name__}


def _owner_brief(root: Path, payload: Mapping[str, Any], now: datetime) -> dict[str, str] | None:
    """Plain-language push for the owner (CHARTER §8). ``None`` means use the old text.

    Any failure here falls back to the technical brief instead of losing the push.
    """

    today = now.astimezone(SHANGHAI).date()
    try:
        if payload.get("status") == "blocked" or payload.get("date_status") in {"stale", "future", "unverified"}:
            title, body = owner_view.stale_owner_brief(
                str(payload.get("as_of") or ""), str(payload.get("expected_as_of") or ""), today=today)
        else:
            digest = owner_view.build_owner_digest(
                root, str(payload.get("as_of") or ""), paper_account=payload.get("paper_account"))
            title, body = owner_view.render_owner_brief(digest, today=today)
            as_of = str(payload.get("as_of") or "")
            if weekly_review.is_first_session_of_week(as_of, today):
                # The first brief of a week also carries last week's review (CHARTER §8).
                weekly = weekly_review.render_weekly_review(weekly_review.build_weekly_review(root, as_of))
                body = body + "\n\n" + weekly
                replay_status = payload.get("weekly_exit_replay") or {}
                if replay_status.get("status") == "research_partial":
                    body += f"\n卖法研究表已用现有资料更新到{as_of}；未到期样本和缺项分别列明。"
                elif replay_status.get("status") in {"blocked", "warning"}:
                    body += "\n卖法研究表本次未能更新，旧表不能代表本次结果。"
    except Exception as exc:  # noqa: BLE001 - the old brief is the safety net
        return {"error": type(exc).__name__}
    if not title.strip() or not body.strip() or any(term in title + body for term in PROHIBITED_MESSAGE_TERMS):
        return {"error": "owner_brief_rejected"}
    return {"version": OWNER_BRIEF_VERSION, "title": title, "body": body}


def _calendar_says_closed(root: Path, now: datetime) -> bool:
    """True only when the calendar explicitly marks today closed (weekday holidays)."""

    from quantdesk.trading_calendar import load_a_share_trading_calendar

    try:
        calendar = load_a_share_trading_calendar(root / "data/calendar/a_share_trading_calendar.csv")
    except Exception:  # noqa: BLE001 - unknown calendar keeps the old behaviour
        return False
    if calendar.get("weekend_only"):
        return False
    return (calendar.get("is_open_by_date") or {}).get(now.astimezone(SHANGHAI).date().isoformat()) is False


def run_morning_brief(
    project_root: str | Path,
    *,
    network: bool = False,
    push: bool = False,
    dry_run: bool = True,
    allow_outside_window: bool = False,
    now: datetime | None = None,
    _sendkey_loader: Callable[[], str | None] | None = None,
    sender: Callable[..., dict[str, Any]] = deliver_server_chan,
) -> dict[str, Any]:
    # The secret is never a parameter of this API: callers pass a loader that
    # reads QUANTDESK_SERVERCHAN_SENDKEY, and it is only invoked after every
    # gate below has passed. A plain value parameter would make the key
    # capturable by any caller frame, log, or traceback.
    root = Path(project_root).resolve()
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    started = current.astimezone(SHANGHAI)

    def send_time_check() -> tuple[datetime, str]:
        # Explicit ``now`` remains a deterministic test clock. Real runs must
        # sample wall time again after rendering and immediately before sending.
        checked = now if now is not None else datetime.now(SHANGHAI)
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=SHANGHAI)
        checked = checked.astimezone(SHANGHAI)
        if checked < started:
            return checked, "send_clock_moved_backwards"
        if checked.date() != started.date():
            return checked, "send_clock_day_changed"
        if not allow_outside_window and not _delivery_window_open(checked):
            return checked, "outside_delivery_window"
        return checked, ""

    lock_path = root / "state/morning_brief.lock"
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=0):
            payload = build_morning_brief(root, now=current)
            payload["presentation_version"] = 2
            date_check = _brief_business_date(root, current, payload.get("as_of"))
            payload.update(date_check)
            if date_check["date_reason_code"]:
                payload["reason_codes"] = _unique([*(payload.get("reason_codes") or []), date_check["date_reason_code"]])
                if payload.get("status") != "blocked":
                    payload["status"] = "warning"
            payload["weekly_exit_replay"] = _weekly_exit_replay(root, payload, current)
            technical = render_morning_brief_markdown(payload)
            owner = _owner_brief(root, payload, current)
            if owner and "error" not in owner:
                title, markdown = owner["title"], owner["body"]
                payload["owner_brief"] = dict(owner)
                preview_text = markdown + "\n\n---\n\n## 技术细节\n\n" + technical
            else:
                title = f"QuantDesk 研究早报 {payload.get('as_of') or '数据待核'}"
                markdown = technical
                payload["owner_brief"] = {"error": (owner or {}).get("error", "owner_brief_unavailable")}
                preview_text = technical
            # The technical text stays in the hash, so any change in the underlying
            # snapshot still counts as new content for the same-day dedupe.
            content_sha256 = hashlib.sha256(
                (title.rstrip() + "\n" + markdown.rstrip() + "\n" + technical.rstrip()).encode("utf-8")
            ).hexdigest()
            # Persist the text that was actually generated. Later readers must
            # not re-render old reports using a changed renderer or today's data.
            generated = now if now is not None else datetime.now(SHANGHAI)
            generated = generated.replace(tzinfo=SHANGHAI) if generated.tzinfo is None else generated.astimezone(SHANGHAI)
            payload["generated_at"] = generated.isoformat(timespec="seconds")
            payload["late_generated"] = (generated.hour, generated.minute, generated.second) > (8, 35, 0)
            payload["saved_content"] = {"title": title, "body": markdown, "technical": technical,
                                        "sha256": content_sha256}
            generation_day = generated.date().isoformat()
            archive_json = root / "results/morning_brief" / f"{generation_day}.json"
            archive_md = root / "reports/morning_brief" / f"{generation_day}.md"
            archive_header = (f"# {title}\n\n生成时间：{payload['generated_at']}"
                              + ("（晚生成）" if payload["late_generated"] else "")
                              + f"\n资料日期：{payload.get('as_of') or '未核实'}\n\n")
            preview_json = root / "results/morning_brief/morning_brief_latest.json"
            preview_md = root / "reports/morning_brief/morning_brief_latest.md"
            atomic_write_json(preview_json, payload)
            _atomic_write_text(preview_md, preview_text)
            if not dry_run:
                _atomic_write_text(archive_md, archive_header + preview_text)
                payload["archive_report_sha256"] = hashlib.sha256(archive_md.read_bytes()).hexdigest()
                atomic_write_json(archive_json, payload)

            base_result: dict[str, Any] = {
                "task_type": "morning_brief_delivery",
                "brief_status": payload.get("status"),
                "as_of": payload.get("as_of"),
                "expected_as_of": date_check["expected_as_of"],
                "date_status": date_check["date_status"],
                "date_reason_detail": date_check.get("date_reason_detail", ""),
                "run_id": payload.get("run_id"),
                "content_sha256": content_sha256,
                "external_sent_count": 0,
                "preview_json": _relative(preview_json, root),
                "preview_report": _relative(preview_md, root),
                "archive_json": _relative(archive_json, root) if not dry_run else None,
                "archive_report": _relative(archive_md, root) if not dry_run else None,
                "local_brief_status": "saved" if not dry_run else "preview_only",
                "delivery_optional": True,
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
                "no_real_orders": True,
                "disclaimer": DISCLAIMER,
            }
            if payload.get("status") == "blocked":
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": "snapshot_blocked",
                }
            if dry_run:
                return {
                    **base_result,
                    "status": "preview_generated",
                    "reason_code": "dry_run",
                }
            if not network:
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": "network_not_authorized",
                }
            if not push:
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": "push_not_authorized",
                }
            current, clock_reason = send_time_check()
            if clock_reason:
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": clock_reason,
                    "send_checked_at": current.isoformat(timespec="seconds"),
                }
            if not allow_outside_window and _calendar_says_closed(root, current):
                # A weekday market holiday: yesterday's brief would only be repeated.
                return {
                    **base_result,
                    "status": "skipped_non_trading_day",
                    "reason_code": "non_trading_day",
                }
            if date_check["date_reason_code"]:
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": date_check["date_reason_code"],
                }

            # Every gate below must stay ahead of the secret loader and the
            # transport: an unreadable receipt, an aliased sink, or a prior
            # same-day attempt all mean "do not push again", and none of them
            # should cause the key to be read at all.
            receipt_dir = root / "results/notifications"
            if not _resolved_within(root, receipt_dir):
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": "notification_receipt_path_escape",
                }
            state_path = root / "state/morning_brief_delivery.json"
            if not _resolved_within(root, state_path):
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": "delivery_state_path_escape",
                }
            attempts, receipt_error = _receipt_attempts(
                receipt_dir, current.astimezone(SHANGHAI).date()
            )
            if receipt_error:
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": receipt_error,
                }
            state, state_error = _load_delivery_state(state_path)
            if state_error:
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": state_error,
                }
            # An ambiguous attempt may or may not have reached the user, so the
            # only safe reading is "assume delivered" for the rest of the day.
            if any(item["status"] == "ambiguous" for item in attempts):
                return {
                    **base_result,
                    "status": "skipped_uncertain_prior_attempt",
                    "reason_code": "uncertain_prior_attempt",
                }
            # Dedupe over the whole day's attempted content, not just the last
            # one, so an A -> B -> A sequence does not re-send A.
            if any(
                item["summary_sha256"] == content_sha256
                and item["status"] in {"sent", "ambiguous"}
                for item in attempts
            ):
                return {
                    **base_result,
                    "status": "skipped_duplicate",
                    "reason_code": "duplicate_content",
                }
            channel_state = (state.get("channels") or {}).get("server_chan")
            state_sent_day = None
            if isinstance(channel_state, Mapping):
                try:
                    sent_at_text = str(channel_state.get("sent_at") or "")
                    state_sent_day = (
                        datetime.fromisoformat(
                            sent_at_text.replace("Z", "+00:00")
                        )
                        .astimezone(SHANGHAI)
                        .date()
                        if sent_at_text
                        else None
                    )
                except ValueError:
                    state_sent_day = None
            # The legacy channel-state dedup is day-scoped too: the same snapshot
            # is eligible again on the next delivery day, and only a same-day
            # re-send is a duplicate.
            if (
                isinstance(channel_state, Mapping)
                and state_sent_day == current.astimezone(SHANGHAI).date()
                and channel_state.get("content_sha256") == content_sha256
            ):
                return {
                    **base_result,
                    "status": "skipped_duplicate",
                    "reason_code": "duplicate_content",
                }

            secret = None
            if _sendkey_loader is not None:
                try:
                    secret = _sendkey_loader()
                except Exception:
                    secret = None
            current, clock_reason = send_time_check()
            if clock_reason:
                return {
                    **base_result,
                    "status": "blocked",
                    "reason_code": clock_reason,
                    "send_checked_at": current.isoformat(timespec="seconds"),
                }
            try:
                delivery_raw = sender(
                    title=title,
                    body=markdown,
                    sendkey=secret,
                    enabled=True,
                    channel_enabled=True,
                    network=True,
                    push=True,
                    dry_run=False,
                    now=current,
                )
            except Exception:
                delivery_raw = {
                    "channel": "server_chan",
                    "status": "failed",
                    "reason_code": "sender_failed",
                    "http_status": None,
                }
            delivery = dict(
                redact_payload(delivery_raw, known_secrets=[str(secret or "")])
            )
            receipt = {
                "schema_version": "1",
                "channel": "server_chan",
                "delivery_day": current.astimezone(SHANGHAI).date().isoformat(),
                "observed_at": current.astimezone(SHANGHAI).isoformat(
                    timespec="seconds"
                ),
                "status": str(delivery.get("status") or "failed"),
                "reason_code": str(delivery.get("reason_code") or "provider_failure"),
                "http_status": delivery.get("http_status"),
                "provider_code": delivery.get("provider_code"),
                "transport_error_type": delivery.get("transport_error_type"),
                "transport_error_category": delivery.get("transport_error_category"),
                "transport_error_errno": delivery.get("transport_error_errno"),
                "summary_sha256": content_sha256,
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
                "no_real_orders": True,
            }
            receipt_path = _receipt_path(root, current, content_sha256)
            atomic_write_json(receipt_path, receipt)

            delivery_status = str(delivery.get("status") or "failed")
            if delivery_status == "sent":
                channels = dict(state.get("channels") or {})
                server_chan_state = dict(channels.get("server_chan") or {})
                deliveries = dict(server_chan_state.get("deliveries") or {})
                delivery_day = current.astimezone(SHANGHAI).date().isoformat()
                day_deliveries = dict(deliveries.get(delivery_day) or {})
                day_deliveries[content_sha256] = {
                    "status": "sent",
                    "sent_at": current.astimezone(SHANGHAI).isoformat(
                        timespec="seconds"
                    ),
                    "as_of": payload.get("as_of"),
                }
                deliveries[delivery_day] = day_deliveries
                server_chan_state.update(
                    {
                        "content_sha256": content_sha256,
                        "sent_at": current.astimezone(SHANGHAI).isoformat(
                            timespec="seconds"
                        ),
                        "as_of": payload.get("as_of"),
                        "deliveries": deliveries,
                    }
                )
                channels["server_chan"] = server_chan_state
                try:
                    atomic_write_json(
                        state_path,
                        {"schema_version": "1", "channels": channels},
                    )
                except Exception:
                    # The provider already accepted the message, so the user has
                    # most likely been notified; only the local record failed.
                    # Report ambiguous rather than sent -- the receipt written
                    # above is what stops a duplicate push later today.
                    return {
                        **base_result,
                        "status": "ambiguous",
                        "reason_code": "delivery_state_write_failed",
                        "http_status": delivery.get("http_status"),
                        "provider_code": delivery.get("provider_code"),
                        "receipt_path": _relative(receipt_path, root),
                    }
                return {
                    **base_result,
                    "status": "sent",
                    "reason_code": str(
                        delivery.get("reason_code") or "provider_accepted"
                    ),
                    "external_sent_count": 1,
                    "http_status": delivery.get("http_status"),
                    "provider_code": delivery.get("provider_code"),
                    "receipt_path": _relative(receipt_path, root),
                }
            return {
                **base_result,
                "status": delivery_status
                if delivery_status in {"failed", "ambiguous", "blocked"}
                else "failed",
                "reason_code": str(
                    delivery.get("reason_code") or "provider_failure"
                ),
                "http_status": delivery.get("http_status"),
                "provider_code": delivery.get("provider_code"),
                "receipt_path": _relative(receipt_path, root),
            }
    except TimeoutError:
        return {
            "task_type": "morning_brief_delivery",
            "status": "busy",
            "reason_code": "morning_brief_lock_busy",
            "external_sent_count": 0,
            "research_only": True,
            "paper_only": True,
            "no_broker": True,
            "no_real_orders": True,
            "disclaimer": DISCLAIMER,
        }


__all__ = [
    "DISCLAIMER",
    "MorningBriefPaths",
    "build_morning_brief",
    "load_morning_brief_snapshot",
    "render_morning_brief_markdown",
    "run_morning_brief",
]
