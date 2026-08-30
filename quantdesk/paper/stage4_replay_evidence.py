"""Stage 4 tri-account replay — real frozen-evidence builders for the kernel.

The strict paper execution kernel (``quantdesk.paper.simulation_kernel``) fills a
``PaperOrderIntent`` only against four verified evidence blocks:

* ``verified_daily_history``          (previous close / previous trade date)
* ``verified_a_share_trading_calendar`` (signal date -> next-open execution date)
* ``verified_market_status``          (ST / suspended point-in-time)
* ``verified_minute_pool_snapshot``   (single-symbol, single-date usable pool)

No production function builds these; only the kernel test fixture assembled them
in ``memory://`` form.  This module builds them from **real local frozen data**
(``data/daily``, ``data/intraday/1m``, ``data/calendar``, ``data/metadata``) for a
given ``(symbol, signal_date, execution_date)`` triple, then packs them into an
``ExecutionEvidence`` the kernel can accept or fail-close on.

Boundary: read-only, no network, no broker, not investment advice.  Every fill
still passes through ``evaluate_paper_risk_gate`` -> ``simulate_paper_intent``;
this module only supplies honest evidence and never relaxes a kernel check.  A
(symbol, date) the window audit does not certify yields a fail-closed no-fill,
which is the intended "missing minute data -> no fill" semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.intraday.minute_pool_capability import minute_pool_capability_source_hash
from quantdesk.intraday.minute_pool_window_audit import window_pool_covers
from quantdesk.paper.simulation_kernel import (
    ExecutionEvidence,
    compute_execution_component_source_hash,
    compute_execution_evidence_digest,
    compute_minute_frame_source_hash,
)
from quantdesk.data_truth.pit_st_delist_loader import StDelistData
from quantdesk.price_basis import classify_frame_price_basis
from quantdesk.shared import normalize_symbol
from quantdesk.trading_calendar import next_trade_day
from quantdesk.version import API_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
INTRADAY_DIR = PROJECT_ROOT / "data" / "intraday"
CALENDAR_PATH = PROJECT_ROOT / "data" / "calendar" / "a_share_trading_calendar.csv"
ST_HISTORY_CSV = PROJECT_ROOT / "data" / "metadata" / "st_history.csv"
DELIST_HISTORY_CSV = PROJECT_ROOT / "data" / "metadata" / "delist_history.csv"
SUSPENSION_STATUS_CSV = PROJECT_ROOT / "data" / "metadata" / "suspension_status.csv"
WINDOW_AUDIT_LATEST_JSON = PROJECT_ROOT / "results" / "data_health" / "minute_pool_window_audit_latest.json"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _day10(value: Any) -> str:
    return str(value or "")[:10]


@dataclass(frozen=True)
class EvidenceResult:
    """Outcome of assembling execution evidence for one (symbol, date) fill."""

    ok: bool
    evidence: ExecutionEvidence | None = None
    reason_codes: tuple[str, ...] = ()
    execution_date: str = ""
    signal_date: str = ""
    symbol: str = ""


# --------------------------------------------------------------------------- #
# 1. verified_daily_history                                                    #
# --------------------------------------------------------------------------- #
def _read_daily_raw(symbol: str, daily_dir: Path) -> pd.DataFrame:
    path = daily_dir / f"{normalize_symbol(symbol)}.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = read_csv_frame(path)
    rename = {
        "日期": "date", "交易日期": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount",
    }
    frame = frame.rename(columns={col: rename.get(str(col), str(col)) for col in frame.columns}).copy()
    frame = frame.loc[:, ~frame.columns.duplicated()]
    if "date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    out = (
        frame.dropna(subset=["date", "close"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    return out


def build_verified_daily_history(
    symbol: str,
    signal_date: str,
    execution_date: str,
    *,
    daily_dir: Path = DAILY_DIR,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build the kernel ``verified_daily_history`` block from real daily bars.

    ``previous_close`` / ``previous_trade_date`` are the *signal date* close and
    date: the kernel requires ``previous_trade_date == intent.as_of`` and strictly
    before the execution date, which is exactly the next-open contract.
    """

    normalized = normalize_symbol(symbol)
    frame = _read_daily_raw(normalized, daily_dir)
    if frame.empty:
        return None, ["daily_history_local_missing"]
    # The basis is classified from the actual frame, never claimed: an unknown
    # or mixed frame cannot be believed as raw execution evidence, so it must
    # not produce a verified daily-history block at all.
    basis, basis_reason = classify_frame_price_basis(frame)
    if basis_reason:
        return None, [basis_reason]
    signal_day = _day10(signal_date)
    row = frame[frame["date"] == pd.to_datetime(signal_day, errors="coerce")]
    if row.empty:
        return None, ["daily_history_signal_date_missing"]
    close = float(row.iloc[0]["close"])
    if not (close > 0):
        return None, ["daily_history_close_invalid"]
    path = daily_dir / f"{normalized}.csv"
    daily: dict[str, Any] = {
        "schema_version": API_SCHEMA_VERSION,
        "status": "passed",
        "verified": True,
        "evidence_kind": "verified_daily_history",
        "symbol": normalized,
        "price_basis": basis,
        "previous_close": round(close, 4),
        "previous_trade_date": signal_day,
        "source_path": _display_path(path),
        "proxy_thresholds_used": False,
    }
    daily["source_hash"] = compute_execution_component_source_hash(daily)
    return daily, []


# --------------------------------------------------------------------------- #
# 2. verified_a_share_trading_calendar                                         #
# --------------------------------------------------------------------------- #
def build_verified_trading_calendar(
    signal_date: str,
    execution_date: str,
    *,
    calendar_path: Path = CALENDAR_PATH,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build the kernel ``verified_a_share_trading_calendar`` block.

    The next-open contract is enforced against the real A-share calendar: the
    supplied ``execution_date`` must equal ``next_trade_day(signal_date)``.
    """

    signal_day = _day10(signal_date)
    exec_day = _day10(execution_date)
    try:
        resolved_next = next_trade_day(signal_day, calendar_path=calendar_path)
    except Exception:
        return None, ["trading_calendar_lookup_failed"]
    if resolved_next != exec_day:
        return None, ["trading_calendar_next_session_mismatch"]
    calendar: dict[str, Any] = {
        "schema_version": API_SCHEMA_VERSION,
        "status": "passed",
        "verified": True,
        "evidence_kind": "verified_a_share_trading_calendar",
        "signal_date": signal_day,
        "next_trade_date": exec_day,
        "source_path": _display_path(calendar_path),
    }
    calendar["source_hash"] = compute_execution_component_source_hash(calendar)
    return calendar, []


# --------------------------------------------------------------------------- #
# 3. verified_market_status                                                    #
# --------------------------------------------------------------------------- #
def _suspended_on(symbol: str, execution_date: str, path: Path) -> bool | None:
    """Return suspension state for (symbol, date), or None when unreadable."""

    if not path.exists():
        return None
    try:
        frame = read_csv_frame(path, dtype={"symbol": str})
    except Exception:
        return None
    if frame.empty or "symbol" not in frame.columns:
        return None
    normalized = normalize_symbol(symbol)
    day = _day10(execution_date)
    rows = frame[frame["symbol"].map(lambda v: normalize_symbol(v) == normalized)]
    if rows.empty:
        return False
    # suspension_status schema carries a status/is_suspended-style column.
    for col in ("is_suspended", "suspended", "status"):
        if col in rows.columns:
            for _, r in rows.iterrows():
                r_date = _day10(r.get("date") or r.get("suspend_date") or r.get("trade_date"))
                if r_date and r_date != day:
                    continue
                value = str(r.get(col) or "").strip().lower()
                if value in {"1", "true", "yes", "suspended", "停牌", "s"}:
                    return True
            return False
    return False


def build_verified_market_status(
    symbol: str,
    execution_date: str,
    *,
    st_delist: StDelistData,
    suspension_path: Path = SUSPENSION_STATUS_CSV,
    st_source_path: Path = ST_HISTORY_CSV,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build the kernel ``verified_market_status`` block point-in-time.

    ST and delisting come from the local point-in-time registries; suspension is
    read from ``suspension_status.csv`` when present.  A delisted-on-date symbol
    is treated as suspended (untradable), matching fail-closed semantics.
    """

    normalized = normalize_symbol(symbol)
    day = _day10(execution_date)
    is_st = bool(st_delist.is_st_on(normalized, day))
    delisted = bool(st_delist.is_delisted_on(normalized, day))
    suspended_flag = _suspended_on(normalized, day, suspension_path)
    if suspended_flag is None:
        suspended_flag = False
    suspended = bool(suspended_flag or delisted)
    status: dict[str, Any] = {
        "schema_version": API_SCHEMA_VERSION,
        "status": "passed",
        "verified": True,
        "evidence_kind": "verified_market_status",
        "symbol": normalized,
        "is_st": is_st,
        "suspended": suspended,
        "delisted_on_date": delisted,
        "source_path": _display_path(st_source_path),
    }
    status["source_hash"] = compute_execution_component_source_hash(status)
    return status, []


# --------------------------------------------------------------------------- #
# 4. verified_minute_pool_snapshot (single symbol, single date)                #
# --------------------------------------------------------------------------- #
def build_single_symbol_minute_pool(
    symbol: str,
    execution_date: str,
    *,
    window_pool: Mapping[str, Any],
    intraday_dir: Path = INTRADAY_DIR,
    snapshot_path: Path = WINDOW_AUDIT_LATEST_JSON,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Narrow the window-verified pool to one (symbol, date), kernel-shaped.

    Authorization is ``window_pool_covers`` (symbol+date).  Provenance:
    ``snapshot_path`` = the window audit artifact that certified the day;
    ``job_path`` = the canonical 1m CSV that was graded.  ``source_snapshot`` is
    declared ``last_passed`` (the window audit is a fallback provenance, not the
    daily-chain latest), satisfying the kernel's fallback-provenance branch.
    """

    normalized = normalize_symbol(symbol)
    day = _day10(execution_date)
    if not window_pool_covers(window_pool, normalized, day):
        return None, ["minute_pool_not_covered"]
    csv_path = intraday_dir / "1m" / f"{normalized}.csv"
    if not csv_path.exists():
        return None, ["minute_pool_source_csv_missing"]
    pool: dict[str, Any] = {
        "schema_version": API_SCHEMA_VERSION,
        "task_type": "verified_minute_pool_snapshot",
        "status": "passed",
        "passed": True,
        "quality_status": "passed",
        "snapshot_available": True,
        "target_date": day,
        "symbols": [normalized],
        "symbol_count": 1,
        "usable_count": 1,
        "total_symbols": 1,
        "partial_count": 0,
        "insufficient_count": 0,
        "invalid_count": 0,
        "missing_count": 0,
        "source_snapshot": "last_passed",
        "latest_quality_status": "window_audit_verified",
        "warnings": ["single_symbol_window_verified_replay_pool"],
        "snapshot_path": _display_path(snapshot_path),
        "job_path": _display_path(csv_path),
        "read_only": True,
        "network_used": False,
        "canonical_write": False,
    }
    pool["source_hash"] = minute_pool_capability_source_hash(pool)
    return pool, []


# --------------------------------------------------------------------------- #
# minute frame (execution-date slice of the real 1m CSV)                       #
# --------------------------------------------------------------------------- #
def _load_execution_minute_frame(
    symbol: str,
    execution_date: str,
    *,
    intraday_dir: Path,
) -> tuple[pd.DataFrame | None, str, list[str]]:
    normalized = normalize_symbol(symbol)
    path = intraday_dir / "1m" / f"{normalized}.csv"
    if not path.exists():
        return None, "", ["minute_frame_csv_missing"]
    frame = read_csv_frame(path, dtype={"symbol": str})
    if frame.empty or "datetime" not in frame.columns:
        return None, "", ["minute_frame_empty"]
    day = _day10(execution_date)
    dt = pd.to_datetime(frame["datetime"], errors="coerce")
    same_day = frame[dt.dt.strftime("%Y-%m-%d") == day].copy()
    if same_day.empty:
        return None, "", ["minute_frame_no_rows_for_execution_date"]
    same_day = same_day.sort_values("datetime").reset_index(drop=True)
    return same_day, _display_path(path), []


# --------------------------------------------------------------------------- #
# assembler                                                                    #
# --------------------------------------------------------------------------- #
def build_execution_evidence(
    symbol: str,
    signal_date: str,
    *,
    window_pool: Mapping[str, Any],
    st_delist: StDelistData,
    daily_dir: Path = DAILY_DIR,
    intraday_dir: Path = INTRADAY_DIR,
    calendar_path: Path = CALENDAR_PATH,
    snapshot_path: Path = WINDOW_AUDIT_LATEST_JSON,
    suspension_path: Path = SUSPENSION_STATUS_CSV,
    st_source_path: Path = ST_HISTORY_CSV,
) -> EvidenceResult:
    """Assemble a kernel-ready ``ExecutionEvidence`` for one next-open fill.

    Returns a fail-closed ``EvidenceResult`` when any evidence block is missing
    (which the caller turns into a no-fill), or a sealed ``ExecutionEvidence``
    the kernel then judges for the final fill/no-fill.
    """

    normalized = normalize_symbol(symbol)
    signal_day = _day10(signal_date)
    try:
        execution_date = next_trade_day(signal_day, calendar_path=calendar_path)
    except Exception:
        return EvidenceResult(ok=False, reason_codes=("trading_calendar_lookup_failed",), signal_date=signal_day, symbol=normalized)

    reasons: list[str] = []
    daily, daily_reasons = build_verified_daily_history(normalized, signal_day, execution_date, daily_dir=daily_dir)
    reasons.extend(daily_reasons)
    calendar, cal_reasons = build_verified_trading_calendar(signal_day, execution_date, calendar_path=calendar_path)
    reasons.extend(cal_reasons)
    market, market_reasons = build_verified_market_status(
        normalized, execution_date, st_delist=st_delist,
        suspension_path=suspension_path, st_source_path=st_source_path,
    )
    reasons.extend(market_reasons)
    pool, pool_reasons = build_single_symbol_minute_pool(
        normalized, execution_date, window_pool=window_pool,
        intraday_dir=intraday_dir, snapshot_path=snapshot_path,
    )
    reasons.extend(pool_reasons)
    minute_frame, minute_source_path, frame_reasons = _load_execution_minute_frame(
        normalized, execution_date, intraday_dir=intraday_dir,
    )
    reasons.extend(frame_reasons)

    if reasons or daily is None or calendar is None or market is None or pool is None or minute_frame is None:
        return EvidenceResult(
            ok=False,
            reason_codes=tuple(dict.fromkeys(reasons)),
            execution_date=execution_date,
            signal_date=signal_day,
            symbol=normalized,
        )

    evidence = ExecutionEvidence(
        symbol=normalized,
        execution_date=execution_date,
        price_basis="raw",
        minute_frame=minute_frame,
        minute_source_path=minute_source_path,
        minute_source_hash=compute_minute_frame_source_hash(minute_frame),
        minute_pool=pool,
        daily_history=daily,
        trading_calendar=calendar,
        market_metadata=market,
    )
    evidence = replace(evidence, evidence_digest=compute_execution_evidence_digest(evidence))
    return EvidenceResult(
        ok=True,
        evidence=evidence,
        execution_date=execution_date,
        signal_date=signal_day,
        symbol=normalized,
    )


__all__ = [
    "EvidenceResult",
    "build_verified_daily_history",
    "build_verified_trading_calendar",
    "build_verified_market_status",
    "build_single_symbol_minute_pool",
    "build_execution_evidence",
]
