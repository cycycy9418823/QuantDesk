from __future__ import annotations

import json
import logging
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.daily_feature_engine import build_daily_features, daily_feature_status
from quantdesk.daily_session_evidence import (
    load_verified_no_session_dates,
    validate_baostock_session_evidence,
    write_baostock_session_evidence,
)
from quantdesk.daily_price_providers import (
    DAILY_DIR,
    DailyProviderResult,
    daily_provider_supports_symbol,
    fetch_daily,
    latest_date_for_symbol,
    merge_daily_frames,
    normalize_daily_df,
    provider_availability,
    read_local_daily,
    read_local_daily_status_dates_with_warnings,
    write_daily,
)
from quantdesk.learning_universe import load_low_price_universe, low_price_universe_path
from quantdesk.market_clock import (
    AFTERNOON_END,
    is_cn_a_share_trading_day,
    load_cn_trading_calendar,
    to_shanghai_datetime,
)
from quantdesk.price_basis import PRICE_BASIS_RAW, classify_frame_price_basis
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.pipeline_process_guard import daily_writer_proof
from quantdesk.process_deadline import run_process_tasks_until_deadline
from quantdesk.shared import normalize_symbol, now_text, safe_float, truthy
from quantdesk.theme_map_updater import theme_map_status
from quantdesk.trading_calendar import resolve_target_complete_trade_date
from quantdesk.utils import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
DAILY_PRICE_UPDATE_LATEST_JSON = RESULTS_DIR / "daily_price_update_latest.json"
LOG = logging.getLogger(__name__)
DEFAULT_PROVIDERS = [
    "local_existing",
    "mootdx_daily",
    "baostock_daily",
    "tencent_daily",
    "akshare_daily",
    "sina_daily",
]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError as exc:
        LOG.warning("display_path_relative_resolution_failed: path=%s root=%s error=%s", path, PROJECT_ROOT, exc)
        return str(path)


def _read_low_price_symbols(*, included_only: bool = True, limit: int = 0) -> list[dict[str, str]]:
    path = low_price_universe_path()
    if not path.exists():
        return []
    frame = load_low_price_universe(path)
    rows: list[dict[str, str]] = []
    for _, raw in frame.iterrows():
        included = truthy(raw.get("included", True))
        if included_only and not included:
            continue
        symbol = normalize_symbol(raw.get("symbol") or raw.get("code"))
        if not symbol or any(row["symbol"] == symbol for row in rows):
            continue
        rows.append({"symbol": symbol, "name": str(raw.get("name") or symbol), "included": str(included)})
        if limit and len(rows) >= limit:
            break
    return rows


def _read_existing_daily_symbols(limit: int = 0) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(DAILY_DIR.glob("*.csv")):
        symbol = normalize_symbol(path.stem)
        if not symbol:
            continue
        rows.append({"symbol": symbol, "name": symbol, "included": "unknown"})
        if limit and len(rows) >= limit:
            break
    return rows


def _parse_symbols_arg(symbols: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if symbols is None:
        return []
    if isinstance(symbols, str):
        raw_items = symbols.split(",")
    else:
        raw_items = list(symbols)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        symbol = normalize_symbol(item)
        if symbol and symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


def _name_lookup_from_universe() -> dict[str, str]:
    path = low_price_universe_path()
    if not path.exists():
        return {}
    try:
        frame = load_low_price_universe(path)
    except Exception as exc:  # noqa: BLE001 - universe lookup is best-effort but failure must be visible
        LOG.warning("low_price_universe_name_lookup_failed: path=%s error=%s", path, exc)
        return {}
    lookup: dict[str, str] = {}
    for _, raw in frame.iterrows():
        symbol = normalize_symbol(raw.get("symbol") or raw.get("code"))
        if symbol and symbol not in lookup:
            lookup[symbol] = str(raw.get("name") or symbol)
    return lookup


def resolve_symbols(
    *,
    only_low_price: bool = True,
    limit: int = 0,
    symbols: str | list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    explicit_symbols = _parse_symbols_arg(symbols)
    if explicit_symbols:
        lookup = _name_lookup_from_universe()
        rows = [{"symbol": symbol, "name": lookup.get(symbol, symbol), "included": "explicit"} for symbol in explicit_symbols]
        return rows[:limit] if limit and limit > 0 else rows
    if only_low_price:
        return _read_low_price_symbols(included_only=True, limit=limit)
    rows = _read_low_price_symbols(included_only=False, limit=limit)
    if rows:
        return rows
    return _read_existing_daily_symbols(limit=limit)


def infer_target_date(target_date: str | None = None, now: datetime | None = None) -> tuple[str, str]:
    resolved = resolve_target_complete_trade_date(target_date, current=now)
    warnings = resolved.get("target_date_warnings") or []
    note = str(resolved.get("target_date_note") or "")
    if warnings:
        note = (note + "; " if note else "") + "; ".join(str(item) for item in warnings if item)
    return str(resolved.get("target_complete_trade_date") or resolved.get("target_date") or ""), note


def _target_is_safe_to_write(target_date: str) -> tuple[bool, str]:
    parsed = pd.to_datetime(target_date, errors="coerce")
    if pd.isna(parsed):
        return False, "target_date 无效。"
    target = pd.Timestamp(parsed).date()
    current = to_shanghai_datetime()
    today = current.date()
    calendar = load_cn_trading_calendar()
    if target > today:
        return False, "target_date 是未来日期，不能写入完整日线缓存。"
    if target == today and is_cn_a_share_trading_day(today, calendar) and current.time() < AFTERNOON_END:
        return False, "当前日期尚未收盘，provider 返回的盘中数据不会写入完整日线缓存。"
    return True, ""


def _missing_recent_trade_dates(
    frame: pd.DataFrame,
    target_date: str,
    *,
    calendar: dict[str, Any] | None = None,
    lookback_days: int = 7,
    verified_no_session_dates: set[str] | None = None,
) -> list[str]:
    """Return proven missing sessions in the recent target-date window.

    A latest row alone is not enough to prove that an incremental update is
    complete.  A nearby observation immediately before the bounded lookback
    window proves that the symbol already had history, so the whole window is
    checked instead of letting an older unresolved gap disappear as the target
    date advances.  Without that bounded predecessor, the check still starts at
    the first recent row so newly listed/resumed symbols are not backfilled with
    invented pre-listing sessions.  When the local exchange calendar is
    unavailable, no gap is asserted.
    """

    if frame is None or frame.empty or "date" not in frame.columns:
        return []
    target = pd.to_datetime(target_date, errors="coerce")
    if pd.isna(target):
        return []
    target = pd.Timestamp(target).normalize()
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna().dt.normalize()
    dates = dates[dates <= target]
    if dates.empty:
        return []
    window_start = target - pd.Timedelta(days=max(1, int(lookback_days)))
    recent = dates[dates >= window_start]
    if recent.empty:
        return []
    predecessor_floor = window_start - pd.Timedelta(days=max(1, int(lookback_days)))
    bounded_predecessors = dates[(dates < window_start) & (dates >= predecessor_floor)]
    first_expected = window_start if not bounded_predecessors.empty else pd.Timestamp(recent.min())
    first_expected_text = pd.Timestamp(first_expected).strftime("%Y-%m-%d")
    target_text = target.strftime("%Y-%m-%d")
    lookup = (calendar or load_cn_trading_calendar()).get("is_open_by_date") or {}
    if not isinstance(lookup, dict) or not lookup:
        return []
    observed = {pd.Timestamp(item).strftime("%Y-%m-%d") for item in recent.tolist()}
    expected = sorted(
        str(day)[:10]
        for day, is_open in lookup.items()
        if bool(is_open) and first_expected_text <= str(day)[:10] <= target_text
    )
    no_session = set(verified_no_session_dates or set())
    return [
        day for day in expected
        if day not in observed and day not in no_session
    ]


def _missing_recent_trade_date_strings(
    date_values: list[str],
    target_date: str,
    *,
    open_trade_dates: list[str],
    lookback_days: int = 7,
    verified_no_session_dates: set[str] | None = None,
) -> list[str]:
    """Fast equivalent of ``_missing_recent_trade_dates`` for ISO date strings."""

    try:
        target = date.fromisoformat(str(target_date)[:10])
    except ValueError:
        return []
    target_text = target.isoformat()
    normalized_dates = {str(value)[:10] for value in date_values if str(value)[:10] <= target_text}
    if not normalized_dates:
        return []

    bounded_days = max(1, int(lookback_days))
    window_start_text = (target - timedelta(days=bounded_days)).isoformat()
    recent = {item for item in normalized_dates if item >= window_start_text}
    if not recent:
        return []
    predecessor_floor_text = (target - timedelta(days=bounded_days * 2)).isoformat()
    has_bounded_predecessor = any(
        predecessor_floor_text <= item < window_start_text for item in normalized_dates
    )
    first_expected_text = window_start_text if has_bounded_predecessor else min(recent)
    if not open_trade_dates:
        return []
    start = bisect_left(open_trade_dates, first_expected_text)
    end = bisect_right(open_trade_dates, target_text)
    no_session = set(verified_no_session_dates or set())
    return [
        day for day in open_trade_dates[start:end]
        if day not in recent and day not in no_session
    ]


def local_daily_status_for_symbols(
    symbols: list[dict[str, str]],
    *,
    target_date: str,
    daily_dir: Path | None = None,
    metadata_dir: Path | None = None,
    calendar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_by_symbol: dict[str, str] = {}
    missing_symbols: list[str] = []
    stale_symbols: list[str] = []
    fresh_symbols: list[str] = []
    missing_recent_trade_dates_by_symbol: dict[str, list[str]] = {}
    latest_required_trade_date_by_symbol: dict[str, str] = {}
    effective_latest_by_symbol: dict[str, str] = {}
    raw_positive_volume_target_symbols: list[str] = []
    raw_zero_volume_target_symbols: list[str] = []
    warnings: list[str] = []
    if not symbols:
        return {
            "latest_by_symbol": {},
            "latest_local_date": "",
            "max_local_date": "",
            "latest_date_min": "",
            "latest_date_max": "",
            "coverage_date": "",
            "raw_bar_coverage_date": "",
            "latest_required_trade_date_by_symbol": {},
            "effective_latest_by_symbol": {},
            "verified_no_session_dates_by_symbol": {},
            "verified_no_session_symbols": [],
            "raw_positive_volume_target_symbols": [],
            "raw_zero_volume_target_symbols": [],
            "fresh_symbols": [],
            "missing_symbols": [],
            "stale_symbols": [],
            "missing_recent_trade_dates_by_symbol": {},
            "recent_gap_symbol_count": 0,
            "recent_gap_date_count": 0,
            "fresh_count": 0,
            "missing_count": 0,
            "stale_count": 0,
            "is_stale": True,
            "data_freshness_status": "no_universe",
            "warning_kind": "no_universe",
            "warnings": ["当前没有可分析股票，无法判断本地日线新鲜度。"],
            "message": "当前没有可分析股票，无法更新 current_analysis_pool；请先运行 data-bootstrap 或指定 --symbols。",
        }
    resolved_daily_dir = Path(daily_dir or DAILY_DIR)
    resolved_metadata_dir = Path(
        metadata_dir or (resolved_daily_dir.parent / "metadata")
    )
    calendar_payload = calendar or load_cn_trading_calendar()
    calendar_lookup = calendar_payload.get("is_open_by_date") or {}
    open_trade_dates = (
        sorted(str(day)[:10] for day, is_open in calendar_lookup.items() if bool(is_open))
        if isinstance(calendar_lookup, dict)
        else []
    )
    requested_symbols = [
        normalize_symbol(row.get("symbol"))
        for row in symbols
        if normalize_symbol(row.get("symbol"))
    ]
    (
        verified_no_session_dates_by_symbol,
        _session_evidence_by_symbol,
        session_evidence_warnings,
    ) = load_verified_no_session_dates(
        requested_symbols,
        metadata_dir=resolved_metadata_dir,
    )
    warnings.extend(session_evidence_warnings)
    for row in symbols:
        symbol = normalize_symbol(row.get("symbol"))
        frame, read_warnings = read_local_daily_status_dates_with_warnings(
            symbol,
            daily_dir=resolved_daily_dir,
        )
        warnings.extend(read_warnings)
        date_values = [str(value)[:10] for value in frame.get("date", pd.Series(dtype="string")).tolist() if value]
        basis, basis_reason = classify_frame_price_basis(frame)
        target_rows = frame[
            frame.get("date", pd.Series(dtype="string")).astype(str).str[:10].eq(target_date)
        ]
        target_volumes = pd.to_numeric(
            target_rows.get("volume", pd.Series(dtype="float64")),
            errors="coerce",
        )
        if (
            basis == PRICE_BASIS_RAW
            and not basis_reason
            and not target_rows.empty
            and len(target_volumes) == len(target_rows)
            and target_volumes.notna().all()
            and (target_volumes >= 0).all()
        ):
            if (target_volumes > 0).any():
                raw_positive_volume_target_symbols.append(symbol)
            elif (target_volumes == 0).all():
                raw_zero_volume_target_symbols.append(symbol)
        latest = max(date_values, default="")
        latest_by_symbol[symbol] = latest
        verified_no_session_dates = set(
            verified_no_session_dates_by_symbol.get(symbol) or set()
        )
        missing_recent = _missing_recent_trade_date_strings(
            date_values,
            target_date,
            open_trade_dates=open_trade_dates,
            verified_no_session_dates=verified_no_session_dates,
        )
        if missing_recent:
            missing_recent_trade_dates_by_symbol[symbol] = missing_recent
            warnings.append(f"recent_trading_day_gap:{symbol}:{','.join(missing_recent)}")
        eligible_open_dates = [
            day
            for day in open_trade_dates
            if day <= target_date and day not in verified_no_session_dates
        ]
        latest_required = max(eligible_open_dates, default=target_date)
        latest_required_trade_date_by_symbol[symbol] = latest_required
        if not latest:
            missing_symbols.append(symbol)
            stale_symbols.append(symbol)
            effective_latest_by_symbol[symbol] = ""
        elif latest < latest_required or missing_recent:
            stale_symbols.append(symbol)
            if missing_recent:
                first_gap = min(missing_recent)
                effective_latest_by_symbol[symbol] = max(
                    (
                        value
                        for value in date_values
                        if value < first_gap
                    ),
                    default="",
                )
            else:
                effective_latest_by_symbol[symbol] = latest
        else:
            fresh_symbols.append(symbol)
            effective_latest_by_symbol[symbol] = target_date
    present_dates = [value for value in latest_by_symbol.values() if value]
    effective_dates = [
        value for value in effective_latest_by_symbol.values() if value
    ]
    latest_local_date = min(present_dates) if present_dates else ""
    max_local_date = max(present_dates) if present_dates else ""
    raw_bar_coverage_date = (
        latest_local_date
        if present_dates and len(present_dates) == len(latest_by_symbol)
        else ""
    )
    coverage_date = (
        min(effective_dates)
        if effective_dates and len(effective_dates) == len(effective_latest_by_symbol)
        else ""
    )
    return {
        "latest_by_symbol": latest_by_symbol,
        "latest_local_date": latest_local_date,
        "max_local_date": max_local_date,
        "latest_date_min": latest_local_date,
        "latest_date_max": max_local_date,
        "coverage_date": coverage_date,
        "raw_bar_coverage_date": raw_bar_coverage_date,
        "latest_required_trade_date_by_symbol": latest_required_trade_date_by_symbol,
        "effective_latest_by_symbol": effective_latest_by_symbol,
        "fresh_symbols": fresh_symbols,
        "missing_symbols": missing_symbols,
        "stale_symbols": stale_symbols,
        "missing_recent_trade_dates_by_symbol": missing_recent_trade_dates_by_symbol,
        "verified_no_session_dates_by_symbol": {
            symbol: sorted(dates)
            for symbol, dates in verified_no_session_dates_by_symbol.items()
        },
        "verified_no_session_symbols": sorted(
            symbol
            for symbol, dates in verified_no_session_dates_by_symbol.items()
            if target_date in dates
        ),
        "raw_positive_volume_target_symbols": sorted(
            raw_positive_volume_target_symbols
        ),
        "raw_zero_volume_target_symbols": sorted(
            raw_zero_volume_target_symbols
        ),
        "recent_gap_symbol_count": len(missing_recent_trade_dates_by_symbol),
        "recent_gap_date_count": sum(len(items) for items in missing_recent_trade_dates_by_symbol.values()),
        "fresh_count": len(fresh_symbols),
        "missing_count": len(missing_symbols),
        "stale_count": len(stale_symbols),
        "is_stale": bool(stale_symbols),
        "warnings": warnings,
        "message": (
            (
                f"本地日线虽到 {max_local_date or '-'}，但最近交易日区间存在缺口，不能视为完整 fresh。"
                if missing_recent_trade_dates_by_symbol and max_local_date >= target_date
                else f"本地日线最新完整日期为 {latest_local_date or '-'}，理论最新完整交易日可能为 {target_date}，存在滞后。"
            )
            if stale_symbols
            else f"本地日线已覆盖理论目标完整交易日 {target_date}。"
        ),
    }


def _ordered_symbols(symbols: list[dict[str, str]], values: set[str] | list[str]) -> list[str]:
    value_set = set(values)
    ordered = [normalize_symbol(row.get("symbol")) for row in symbols if normalize_symbol(row.get("symbol")) in value_set]
    extras = sorted(symbol for symbol in value_set if symbol not in set(ordered))
    return ordered + extras


def _add_provider_symbol_lists(
    row: dict[str, Any],
    *,
    attempted_symbols: list[str],
    updated_symbols: list[str] | None = None,
    failed_symbols: list[str] | None = None,
) -> dict[str, Any]:
    row["processed_symbols"] = list(attempted_symbols)
    row["attempted_symbols"] = list(attempted_symbols)
    row["updated_symbol_list"] = list(updated_symbols or [])
    row["failed_symbol_list"] = list(failed_symbols or [])
    return row


def _latest_update_trust_summary(
    latest_result: dict[str, Any],
    *,
    current_target: str,
    current_is_stale: bool,
) -> dict[str, Any]:
    current_freshness = "stale" if current_is_stale else "fresh"
    if not latest_result:
        return {
            "status": "missing",
            "trustworthy_for_current_status": False,
            "reason_codes": ["latest_update_missing"],
            "current_target_date": current_target,
            "latest_target_date": "",
            "current_freshness_status": current_freshness,
            "latest_freshness_status": "unknown",
        }

    latest_target = str(
        latest_result.get("effective_target_date")
        or latest_result.get("target_complete_trade_date")
        or latest_result.get("target_date")
        or ""
    )[:10]
    latest_freshness = str(latest_result.get("data_freshness_status") or "").strip().lower()
    if latest_freshness not in {"fresh", "stale"}:
        has_after_counts = "stale_count_after" in latest_result or "missing_count_after" in latest_result
        if has_after_counts:
            latest_freshness = (
                "stale"
                if int(latest_result.get("stale_count_after") or 0) > 0
                or int(latest_result.get("missing_count_after") or 0) > 0
                else "fresh"
            )
        else:
            latest_freshness = "unknown"

    reason_codes: list[str] = []
    if not latest_target:
        reason_codes.append("latest_target_unknown")
    elif latest_target != current_target:
        reason_codes.append("target_date_mismatch")
    if latest_freshness == "unknown":
        reason_codes.append("latest_freshness_unknown")
    elif latest_freshness != current_freshness:
        reason_codes.append("current_freshness_conflict")

    trustworthy = not reason_codes
    return {
        "status": "current" if trustworthy else "historical_only",
        "trustworthy_for_current_status": trustworthy,
        "reason_codes": reason_codes,
        "current_target_date": current_target,
        "latest_target_date": latest_target,
        "current_freshness_status": current_freshness,
        "latest_freshness_status": latest_freshness,
    }


def daily_price_status(
    *,
    only_low_price: bool = True,
    target_date: str | None = None,
    limit: int = 0,
    symbols: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    target, note = infer_target_date(target_date)
    symbol_rows = resolve_symbols(only_low_price=only_low_price, limit=max(0, int(limit or 0)), symbols=symbols)
    local_status = local_daily_status_for_symbols(symbol_rows, target_date=target)
    no_universe = len(symbol_rows) == 0
    latest_result = {}
    if DAILY_PRICE_UPDATE_LATEST_JSON.exists():
        try:
            latest_result = json.loads(DAILY_PRICE_UPDATE_LATEST_JSON.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - latest result cache is optional but failures matter
            LOG.warning("daily_price_latest_result_read_failed: path=%s error=%s", DAILY_PRICE_UPDATE_LATEST_JSON, exc)
            latest_result = {}
    theme = theme_map_status()
    provider_rows = [provider_availability(name) for name in DEFAULT_PROVIDERS]
    latest_update_trust = _latest_update_trust_summary(
        latest_result,
        current_target=target,
        current_is_stale=bool(local_status.get("is_stale")),
    )
    status_warnings = list(local_status.get("warnings") or [])
    if latest_update_trust["status"] == "historical_only":
        status_warnings.append(
            "latest_update_historical_only:" + ",".join(latest_update_trust["reason_codes"])
        )
    return {
        "ok": True,
        "status": "warning" if no_universe or local_status.get("is_stale") else "passed",
        "data_freshness_status": local_status.get("data_freshness_status") or ("stale" if local_status.get("is_stale") else "fresh"),
        "warning_kind": local_status.get("warning_kind") or ("data_stale" if local_status.get("is_stale") else ""),
        "warnings": status_warnings,
        "scope": "low_price_included_only" if only_low_price else "local_symbols_only",
        "only_low_price": bool(only_low_price),
        "explicit_symbols": _parse_symbols_arg(symbols),
        "full_scan": False,
        "network": False,
        "target_date": target,
        "target_date_note": note,
        "total_symbols": len(symbol_rows),
        "processed_symbols": [row["symbol"] for row in symbol_rows],
        "latest_by_symbol": local_status["latest_by_symbol"],
        "latest_local_date": local_status["latest_local_date"],
        "max_local_date": local_status["max_local_date"],
        "latest_date_min": local_status["latest_date_min"],
        "latest_date_max": local_status["latest_date_max"],
        "coverage_date": local_status["coverage_date"],
        "fresh_symbols": local_status["fresh_symbols"][:50],
        "fresh_count": local_status["fresh_count"],
        "stale_count": local_status["stale_count"],
        "missing_count": local_status["missing_count"],
        "recent_gap_symbol_count": local_status.get("recent_gap_symbol_count", 0),
        "recent_gap_date_count": local_status.get("recent_gap_date_count", 0),
        "missing_recent_trade_dates_by_symbol": local_status.get("missing_recent_trade_dates_by_symbol", {}),
        "is_stale": local_status["is_stale"],
        "message": local_status["message"],
        "stale_symbols": local_status["stale_symbols"][:50],
        "missing_symbols": local_status["missing_symbols"][:50],
        "latest_result_path": _display_path(DAILY_PRICE_UPDATE_LATEST_JSON),
        "latest_update": latest_result,
        "latest_update_trust": latest_update_trust,
        "provider_status": provider_rows,
        "theme_map_health": {
            "mapped_count": theme.get("mapped_count", 0),
            "unknown_count": theme.get("unknown_count", 0),
            "pending_count": theme.get("pending_count", 0),
            "failed_reason": theme.get("failed_reason", ""),
        },
        "disclaimer": "日线状态检查只读，不联网、不写 data/daily、不接券商、不构成投资建议。",
    }


def _json_safe_latest_result() -> dict[str, Any]:
    try:
        return json.loads(DAILY_PRICE_UPDATE_LATEST_JSON.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - API/status payload should survive corrupt cache
        LOG.warning("daily_price_latest_result_json_failed: path=%s error=%s", DAILY_PRICE_UPDATE_LATEST_JSON, exc)
        return {}


def _fetch_start_date(current_latest: str, target_date: str) -> str:
    target_floor = (
        pd.Timestamp(target_date) - pd.Timedelta(days=7)
    ).strftime("%Y-%m-%d")
    if current_latest:
        parsed = pd.to_datetime(current_latest, errors="coerce")
        if pd.notna(parsed):
            latest_floor = (
                pd.Timestamp(parsed) - pd.Timedelta(days=7)
            ).strftime("%Y-%m-%d")
            # Historical Stage 6 catchup can target a date earlier than the
            # canonical maximum. Fetch the complete target-date continuity
            # window so explicit suspension evidence is not truncated merely
            # because later rows already exist.
            return min(latest_floor, target_floor)
    return (pd.Timestamp(target_date) - pd.Timedelta(days=380)).strftime("%Y-%m-%d")


def _latest_date(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "date" not in frame.columns:
        return ""
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    return "" if dates.empty else dates.max().strftime("%Y-%m-%d")


SPOT_CLOSE_EARLIEST = "15:00:00"
SPOT_FETCH_SKEW_SECONDS = 5
_NULL_TEXT = {"", "nan", "nat", "none", "null", "<na>"}


def _spot_row_close_reason(row: dict[str, Any], target_date: str) -> str:
    """'' when a spot-cache row is the target day's closing quote by its own evidence.

    The provider's own time decides: it must be dated ``target_date`` and be at
    or after 15:00 (a 14:30 round's cumulative amount is not the day's
    amount). Sina sends a clock time only; its day is the row's quote_date,
    else the day the collector fetched it - never the reader's clock. A quote
    stamped after its own recorded fetch is not trusted.
    """

    raw_time = str(row.get("quote_time") or "").strip()
    if raw_time.lower() in _NULL_TEXT:
        return "quote_time_missing"
    raw_date = str(row.get("quote_date") or "").strip()[:10]
    raw_date = "" if raw_date.lower() in _NULL_TEXT else raw_date
    fetched_text = str(row.get("fetched_at") or "").strip()
    fetched = None
    if fetched_text.lower() not in _NULL_TEXT:
        try:
            fetched = to_shanghai_datetime(datetime.fromisoformat(fetched_text))
        except ValueError:
            return "fetched_at_unparseable"
    if fetched is None:
        # A row without a fetch record comes from the writer that filled the
        # quote time with its own clock (10/09: 3600 rows "15:00:01"; 89 of the
        # 121 that even matched the close carried less than the day's amount).
        return "fetched_at_missing"
    if " " in raw_time or "T" in raw_time:
        text = raw_time.replace("T", " ")
        day, clock = text[:10], text[11:19]
    else:
        clock = raw_time[:8]
        day = raw_date or (fetched.date().isoformat() if fetched is not None else "")
        if not day:
            return "quote_date_unknown"
    try:
        quoted = datetime.fromisoformat(f"{day} {clock if len(clock) == 8 else clock + ':00'}")
    except ValueError:
        return "quote_time_unparseable"
    if raw_date and raw_date != quoted.date().isoformat():
        return "quote_date_conflict"
    if quoted.date().isoformat() != target_date:
        return "quote_date_not_target"
    if quoted.strftime("%H:%M:%S") < SPOT_CLOSE_EARLIEST:
        return "quote_before_close"
    if fetched is not None:
        if fetched.date().isoformat() != target_date:
            return "fetched_not_target_day"
        if quoted > fetched.replace(tzinfo=None) + timedelta(seconds=SPOT_FETCH_SKEW_SECONDS):
            return "quote_after_fetch"
    return ""


def _backfill_target_amount_from_spot_snapshot(symbols: list[str], target_date: str) -> dict[str, Any]:
    """Fill missing target-date amount from the same-day full-market snapshot.

    This is deliberately narrow: it only runs after the target date is safe to
    write, only touches the requested symbols, and only fills rows already
    present for ``target_date``. It never creates a daily bar from realtime data.
    """
    cache_path = PROJECT_ROOT / "data" / "cache" / "spot_snapshot_latest.csv"
    if not cache_path.exists():
        return {"provider": "spot_snapshot_amount_fill", "status": "missing_cache", "attempted": 0, "updated": 0, "updated_symbol_list": []}
    spot = read_csv_frame(cache_path)
    csv_read_warnings = [str(item) for item in spot.attrs.get("warnings", []) or []]
    if spot.empty or "amount" not in spot.columns:
        return {"provider": "spot_snapshot_amount_fill", "status": "cache_no_amount", "attempted": 0, "updated": 0, "updated_symbol_list": [], "warnings": csv_read_warnings}
    spot = spot.copy()
    symbol_col = "symbol" if "symbol" in spot.columns else ""
    if not symbol_col:
        return {"provider": "spot_snapshot_amount_fill", "status": "cache_no_symbol", "attempted": 0, "updated": 0, "updated_symbol_list": [], "warnings": csv_read_warnings}
    spot["symbol"] = spot[symbol_col].astype(str).map(normalize_symbol)
    if "quote_time" not in spot.columns:
        # Without the provider's own time the cache proves nothing about the
        # target day's close (previously this case was not filtered at all).
        return {"provider": "spot_snapshot_amount_fill", "status": "cache_no_quote_time", "attempted": 0, "updated": 0, "updated_symbol_list": [], "warnings": csv_read_warnings}
    row_reasons = [_spot_row_close_reason(row, target_date) for row in spot.to_dict("records")]
    rejected_rows = dict(Counter(reason for reason in row_reasons if reason))
    spot = spot.loc[[not reason for reason in row_reasons]].copy()
    if spot.empty:
        return {"provider": "spot_snapshot_amount_fill", "status": "cache_date_mismatch", "attempted": 0, "updated": 0, "updated_symbol_list": [], "warnings": csv_read_warnings, "rejected_rows": rejected_rows}
    spot_amount: dict[str, float] = {}
    spot_volume: dict[str, float] = {}
    spot_price: dict[str, float] = {}
    for _, row in spot.iterrows():
        symbol = normalize_symbol(row.get("symbol"))
        amount = safe_float(row.get("amount"))
        if symbol and amount is not None and amount > 0:
            spot_amount[symbol] = amount
        price = safe_float(row.get("price"))
        if symbol and price is not None and price > 0:
            spot_price[symbol] = price
        volume = safe_float(row.get("volume"))
        if symbol and volume is not None and volume > 0:
            spot_volume[symbol] = volume
    updated_symbols: list[str] = []
    price_mismatch: list[str] = []
    attempted = 0
    for symbol in [normalize_symbol(item) for item in symbols if normalize_symbol(item)]:
        amount = spot_amount.get(symbol)
        if amount is None:
            continue
        path = DAILY_DIR / f"{symbol}.csv"
        if not path.exists():
            continue
        with interprocess_file_lock(DAILY_DIR / ".locks" / f"{symbol}.lock"):
            frame = read_local_daily(symbol, daily_dir=DAILY_DIR)
            if frame.empty or "date" not in frame.columns:
                continue
            mask = frame["date"].astype(str).str[:10] == target_date
            if not mask.any():
                continue
            attempted += 1
            latest_index = frame.loc[mask].index[-1]
            existing_amount = safe_float(frame.at[latest_index, "amount"] if "amount" in frame.columns else None)
            if existing_amount is not None and existing_amount > 0:
                continue
            # The snapshot row must describe this very bar: its price equals
            # the bar's close. A stale or intraday quote is not borrowed.
            daily_close = safe_float(frame.at[latest_index, "close"] if "close" in frame.columns else None)
            spot_close = spot_price.get(symbol)
            if daily_close is None or spot_close is None or abs(daily_close - spot_close) > 1e-6:
                price_mismatch.append(symbol)
                continue
            frame.at[latest_index, "amount"] = amount
            volume = spot_volume.get(symbol)
            if volume is not None and volume > 0:
                existing_volume = safe_float(frame.at[latest_index, "volume"] if "volume" in frame.columns else None)
                if existing_volume is None or existing_volume <= 0 or existing_volume * 50 < volume:
                    frame.at[latest_index, "volume"] = volume
            frame.at[latest_index, "source"] = "daily_price+spot_snapshot_amount"
            frame.at[latest_index, "updated_at"] = now_text()
            write_daily(symbol, frame, daily_dir=DAILY_DIR)
            updated_symbols.append(symbol)
    return {
        "provider": "spot_snapshot_amount_fill",
        "status": "ok" if updated_symbols else "skipped",
        "attempted": attempted,
        "updated": len(updated_symbols),
        "failed": 0,
        "latest_date": target_date,
        "updated_symbol_list": updated_symbols,
        "failed_symbol_list": [],
        "rejected_rows": rejected_rows,
        "price_mismatch_count": len(price_mismatch),
        "price_mismatch_symbols": price_mismatch[:50],
        "warnings": csv_read_warnings + ["same_day_spot_snapshot_amount_fill; research_only; no_new_daily_bar_created"],
    }


def _compact_failure_reasons(reasons: list[str]) -> str:
    counts = Counter(reason for reason in reasons if reason)
    if not counts:
        return ""
    return "; ".join(f"{reason} ({count})" for reason, count in counts.most_common(3))


def _known_unsupported_provider_reason(
    provider_name: str,
    symbol: str,
) -> str:
    """Skip providers that cannot address this symbol at all.

    委托给 ``daily_provider_supports_symbol``（以各 provider 真实的代码映射为准），
    而不是在这里再维护一份硬编前缀表。原实现只挡 ``920`` 开头，会漏掉同属北交所的
    ``4xxx``/``8xxx``：那些代码同样不在 sina/baostock 的映射里，硬派过去只会拿到
    ``provider_unsupported_symbol``，白耗一次 provider 预算，并把"该源不支持该板"
    混进通用失败计数。
    """

    if not daily_provider_supports_symbol(provider_name, symbol):
        return f"provider_unsupported_symbol:{provider_name}:{symbol}"
    return ""


def _merge_failed_symbol_reason(current: str, candidate: str) -> str:
    if current and candidate.startswith("provider_unsupported_symbol:"):
        return current
    return candidate or current


def _existing_canonical_raw_basis_failures(
    symbol_rows: list[dict[str, str]],
    *,
    daily_dir: Path | None = None,
) -> dict[str, str]:
    failures: dict[str, str] = {}
    resolved_daily_dir = Path(daily_dir or DAILY_DIR)
    for row in symbol_rows:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        try:
            existing = read_local_daily(
                symbol,
                daily_dir=resolved_daily_dir,
            )
            if existing.empty:
                continue
            basis, reason = classify_frame_price_basis(existing)
            if basis != PRICE_BASIS_RAW or reason:
                failures[symbol] = (
                    "existing_canonical_price_basis_not_raw:"
                    f"{symbol}:{reason or basis}"
                )
        except Exception as exc:  # noqa: BLE001
            failures[symbol] = (
                "existing_canonical_price_basis_read_failed:"
                f"{symbol}:{type(exc).__name__}:{exc}"
            )
    return failures


def _fetch_merge_write_symbol_unlocked(
    provider_name: str,
    symbol: str,
    *,
    target: str,
    network: bool,
    calendar: dict[str, Any],
    daily_dir: Path,
    metadata_dir: Path,
) -> dict[str, Any]:
    """Fetch and atomically update one independent symbol."""

    current_latest = latest_date_for_symbol(symbol, daily_dir=daily_dir)
    start_date = _fetch_start_date(current_latest, target)
    warnings: list[str] = []
    fresh_latest = ""
    unsupported_reason = _known_unsupported_provider_reason(
        provider_name,
        symbol,
    )
    if unsupported_reason:
        return {
            "symbol": symbol,
            "updated": False,
            "latest_date": current_latest,
            "warnings": warnings,
            "reason": unsupported_reason,
        }
    try:
        existing = read_local_daily(symbol, daily_dir=daily_dir)
        warnings.extend(str(item) for item in existing.attrs.get("warnings", []) if item)
        if not existing.empty or existing.attrs.get("observed_price_basis") or existing.attrs.get("price_basis_reason"):
            existing_basis, existing_basis_reason = classify_frame_price_basis(existing)
            if existing_basis != PRICE_BASIS_RAW or existing_basis_reason:
                return {
                    "symbol": symbol,
                    "updated": False,
                    "latest_date": current_latest,
                    "warnings": warnings,
                    "reason": (
                        "existing_canonical_price_basis_not_raw:"
                        f"{symbol}:{existing_basis_reason or existing_basis}"
                    ),
                }
        fresh, fetch_warnings = fetch_daily(
            provider_name,
            symbol,
            start_date,
            target,
            network=network,
        )
        warnings.extend(fetch_warnings)
        if not fresh.empty or fresh.attrs.get("observed_price_basis") or fresh.attrs.get("price_basis_reason"):
            fresh_basis, fresh_basis_reason = classify_frame_price_basis(fresh)
            if fresh_basis != PRICE_BASIS_RAW or fresh_basis_reason:
                return {
                    "symbol": symbol,
                    "updated": False,
                    "latest_date": current_latest,
                    "warnings": warnings,
                    "reason": (
                        "provider_price_basis_not_raw:"
                        f"{symbol}:{fresh_basis_reason or fresh_basis}"
                    ),
                }
        # Complete the standard columns without inventing missing O/H/L values.
        # The validity filter otherwise leaves frames with missing columns intact.
        fresh, normalize_warnings = normalize_daily_df(fresh, symbol)
        warnings.extend(normalize_warnings)
        # Shared validity gate (quantdesk.data_truth.daily_bar_validity):
        # the fast snapshot path and this slow path must reject the same rows.
        # Vendors encode "suspended today" as zero-volume flat rows; those must
        # never be written as normal bars (repo convention: no bar on a
        # suspended day). Existing local history is never rewritten here.
        from quantdesk.data_truth.daily_bar_validity import filter_invalid_daily_bars

        fresh, invalid_reasons = filter_invalid_daily_bars(fresh, symbol=symbol)
        if invalid_reasons:
            warnings.extend(invalid_reasons)
        persisted_no_session, _persisted_evidence, persisted_warnings = (
            load_verified_no_session_dates([symbol], metadata_dir=metadata_dir)
        )
        warnings.extend(persisted_warnings)
        verified_suspended_dates: set[str] = set(
            persisted_no_session.get(symbol) or set()
        )
        validated_session_evidence: dict[str, Any] = {}
        if provider_name == "baostock_daily":
            candidate_evidence = fresh.attrs.get("suspension_evidence")
            if candidate_evidence:
                validated_session_evidence = validate_baostock_session_evidence(
                    candidate_evidence,
                    expected_symbol=symbol,
                    expected_start_date=start_date,
                    expected_end_date=target,
                )
                if not validated_session_evidence.get("valid"):
                    return {
                        "symbol": symbol,
                        "updated": False,
                        "latest_date": current_latest,
                        "warnings": warnings,
                        "reason": (
                            "invalid_baostock_session_evidence:"
                            + ",".join(
                                validated_session_evidence.get("reason_codes") or []
                            )
                        ),
                    }
                verified_suspended_dates = {
                    day
                    for day in validated_session_evidence.get("suspended_dates") or []
                    if day <= target
                }
        fresh_latest = _latest_date(fresh)
        target_suspended = target in verified_suspended_dates
        if (
            fresh.empty or not fresh_latest or fresh_latest < target
        ) and not target_suspended:
            return {
                "symbol": symbol,
                "updated": False,
                "latest_date": fresh_latest,
                "warnings": warnings,
                "reason": (
                    f"provider_missing_target_date:{symbol}:{target}:"
                    f"latest={fresh_latest or '-'}"
                ),
            }
        if not fresh.empty:
            merged = merge_daily_frames(existing, fresh)
        else:
            merged = existing.copy()
        warnings.extend(
            str(item)
            for item in (merged.attrs.get("warnings") or [])
            if item
        )
        merged_basis, merged_basis_reason = classify_frame_price_basis(merged)
        if merged_basis != PRICE_BASIS_RAW or merged_basis_reason:
            return {
                "symbol": symbol,
                "updated": False,
                "latest_date": fresh_latest,
                "warnings": warnings,
                "reason": (
                    "merged_price_basis_not_raw:"
                    f"{symbol}:{merged_basis_reason or merged_basis}"
                ),
            }
        # Keep the raw calendar gaps here: warning generation must see dates
        # later proven as suspensions instead of losing their evidence trail.
        missing_interval = _missing_recent_trade_dates(
            merged,
            target,
            calendar=calendar,
            verified_no_session_dates=None,
        )
        if missing_interval:
            exempted = sorted(set(missing_interval) & verified_suspended_dates)
            unexplained = sorted(set(missing_interval) - verified_suspended_dates)
            if exempted:
                warnings.append(
                    "interval_gap_exempted_by_explicit_suspension:"
                    + symbol
                    + ":"
                    + ",".join(exempted)
                )
            if unexplained:
                return {
                    "symbol": symbol,
                    "updated": False,
                    "latest_date": fresh_latest,
                    "warnings": warnings,
                    "reason": f"provider_interval_gap:{symbol}:{','.join(unexplained)}",
                }
        if validated_session_evidence.get("valid"):
            sidecar_path = write_baostock_session_evidence(
                validated_session_evidence["evidence"],
                metadata_dir=metadata_dir,
                expected_symbol=symbol,
                expected_start_date=start_date,
                expected_end_date=target,
            )
            warnings.append(
                "baostock_session_evidence_persisted:" + str(sidecar_path)
            )
        if target_suspended:
            bar_updated = bool(
                fresh_latest and fresh_latest > current_latest
            )
            if bar_updated:
                write_daily(symbol, merged, daily_dir=daily_dir)
            return {
                "symbol": symbol,
                "resolved": True,
                "verified_no_session": True,
                "updated": bar_updated,
                "latest_date": fresh_latest or current_latest,
                "warnings": warnings,
                "reason": "",
            }
        write_daily(symbol, merged, daily_dir=daily_dir)
        if latest_date_for_symbol(symbol, daily_dir=daily_dir) >= target:
            return {
                "symbol": symbol,
                "resolved": True,
                "verified_no_session": False,
                "updated": True,
                "latest_date": fresh_latest,
                "warnings": warnings,
                "reason": "",
            }
        return {
            "symbol": symbol,
            "updated": False,
            "latest_date": fresh_latest,
            "warnings": warnings,
            "reason": f"写入后仍未达到目标日期 {target}",
        }
    except Exception as exc:  # noqa: BLE001
        LOG.warning(
            "daily_price_provider_symbol_failed: provider=%s symbol=%s target=%s error=%s",
            provider_name,
            symbol,
            target,
            exc,
        )
        return {
            "symbol": symbol,
            "updated": False,
            "latest_date": fresh_latest,
            "warnings": warnings,
            "reason": str(exc),
        }


def _fetch_merge_write_symbol(
    provider_name: str,
    symbol: str,
    *,
    target: str,
    network: bool,
    calendar: dict[str, Any],
    daily_dir: Path,
    metadata_dir: Path,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    lock_path = daily_dir / ".locks" / f"{normalized}.lock"
    with interprocess_file_lock(lock_path):
        return _fetch_merge_write_symbol_unlocked(
            provider_name,
            normalized,
            target=target,
            network=network,
            calendar=calendar,
            daily_dir=daily_dir,
            metadata_dir=metadata_dir,
        )


def _provider_process_worker(
    symbol: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    # A spawned worker does not inherit the parent's registered lease; it
    # re-verifies the parent's proof before any canonical write (charter s3).
    from quantdesk.pipeline_process_guard import adopt_daily_writer_proof

    with adopt_daily_writer_proof(context.get("writer_proof")):
        return _fetch_merge_write_symbol(
            str(context["provider_name"]),
            symbol,
            target=str(context["target"]),
            network=bool(context["network"]),
            calendar=dict(context["calendar"]),
            daily_dir=Path(str(context["daily_dir"])),
            metadata_dir=Path(str(context["metadata_dir"])),
        )


_PROVIDER_PROCESS_WORKER = _provider_process_worker


def processing_notice(*, limit: int = 0, symbols: str | list[str] | tuple[str, ...] | None = None, processed_symbols: list[str] | None = None) -> str:
    processed = ", ".join(processed_symbols or [])
    explicit = _parse_symbols_arg(symbols)
    if explicit:
        return f"本次按 --symbols 仅处理以下股票：{processed or '-'}。"
    if limit and limit > 0:
        return f"本次 limit={limit}，仅处理以下股票：{processed or '-'}。"
    return f"本次实际处理股票列表：{processed or '-'}。"


def _attach_raw_price_basis_contract(
    payload: dict[str, Any],
    *,
    require_raw_price_basis: bool,
    invalid_price_basis_symbols: list[str],
    invalid_price_basis_reasons: dict[str, str],
) -> dict[str, Any]:
    payload["require_raw_price_basis"] = bool(require_raw_price_basis)
    payload["invalid_price_basis_symbols"] = list(invalid_price_basis_symbols)
    payload["invalid_price_basis_reasons"] = dict(invalid_price_basis_reasons)
    return payload


def _attach_provider_timeout_contract(
    payload: dict[str, Any],
    *,
    provider_timeout_seconds: float | None,
    provider_timeout_overrides: dict[str, float],
    provider_timeout_triggered: bool,
    provider_global_timeout_triggered: bool,
    provider_timeout_events: list[dict[str, Any]],
) -> dict[str, Any]:
    payload["provider_timeout_seconds"] = (
        None
        if provider_timeout_seconds is None
        else max(0.0, float(provider_timeout_seconds))
    )
    payload["provider_timeout_overrides"] = dict(provider_timeout_overrides)
    payload["provider_timeout_triggered"] = bool(provider_timeout_triggered)
    payload["provider_global_timeout_triggered"] = bool(
        provider_global_timeout_triggered
    )
    payload["provider_timeout_events"] = list(provider_timeout_events)
    return payload

def update_daily_prices(
    *,
    only_low_price: bool = True,
    target_date: str | None = None,
    providers: list[str] | None = None,
    workers: int = 4,
    network: bool = False,
    dry_run: bool = False,
    limit: int = 0,
    rebuild_features: bool = False,
    symbols: str | list[str] | tuple[str, ...] | None = None,
    output_path: Path | None = None,
    write_result: bool = True,
    run_id: str = "",
    require_raw_price_basis: bool = False,
    project_root: Path | None = None,
    daily_dir: Path | None = None,
    metadata_dir: Path | None = None,
    provider_timeout_seconds: float | None = None,
    provider_timeout_overrides: dict[str, float] | None = None,
    sleep_seconds: float = 0.0,
) -> dict[str, Any]:
    project_root_explicit = project_root is not None
    requested_project_root = Path(
        project_root or PROJECT_ROOT
    ).resolve()
    default_daily_dir = Path(DAILY_DIR)
    default_metadata_dir = default_daily_dir.parent / "metadata"
    resolved_daily_dir = Path(
        daily_dir
        or (
            requested_project_root / "data" / "daily"
            if project_root_explicit
            else default_daily_dir
        )
    )
    resolved_metadata_dir = Path(
        metadata_dir
        or (
            requested_project_root / "data" / "metadata"
            if project_root_explicit
            else resolved_daily_dir.parent / "metadata"
        )
    )
    default_output_path = (
        DAILY_PRICE_UPDATE_LATEST_JSON
        if requested_project_root == PROJECT_ROOT.resolve()
        else requested_project_root
        / "results"
        / "daily_price_update_latest.json"
    )
    output_path = Path(output_path or default_output_path)
    sleep_seconds = max(0.0, float(sleep_seconds or 0.0))
    worker_count = max(1, min(int(workers or 1), 8))
    # A caller asking for an inter-symbol delay must not accidentally retain
    # parallel network requests.  Serial execution is the only meaningful
    # interpretation of a provider rate limit.
    if sleep_seconds > 0:
        worker_count = 1
    network = bool(network and not dry_run)
    target, note = infer_target_date(target_date)
    safe_to_write, unsafe_reason = _target_is_safe_to_write(target)
    nondefault_write_scope = bool(
        requested_project_root != PROJECT_ROOT.resolve()
        or resolved_daily_dir.resolve() != default_daily_dir.resolve()
        or resolved_metadata_dir.resolve()
        != default_metadata_dir.resolve()
    )
    if network and nondefault_write_scope:
        safe_to_write = False
        unsafe_reason = (
            "nondefault_project_root_network_write_blocked:"
            + str(requested_project_root)
        )
    if network and safe_to_write:
        # data/daily has one writer (charter s3, handbook H7b): the UI button,
        # bootstrap and CLI fallback are refused before any provider request.
        from quantdesk.pipeline_process_guard import (
            CanonicalDailyWriteRefused,
            assert_canonical_daily_write,
        )

        try:
            assert_canonical_daily_write(resolved_daily_dir)
        except CanonicalDailyWriteRefused as exc:
            safe_to_write = False
            unsafe_reason = str(exc)
    selected_providers = [str(item).strip() for item in (providers or DEFAULT_PROVIDERS) if str(item).strip()]
    selected_providers = selected_providers or list(DEFAULT_PROVIDERS)
    symbol_rows = resolve_symbols(only_low_price=only_low_price, limit=max(0, int(limit or 0)), symbols=symbols)
    processed_symbols = [row["symbol"] for row in symbol_rows]
    calendar = load_cn_trading_calendar()
    before = local_daily_status_for_symbols(
        symbol_rows,
        target_date=target,
        daily_dir=resolved_daily_dir,
        metadata_dir=resolved_metadata_dir,
        calendar=calendar,
    )
    raw_basis_failures = (
        _existing_canonical_raw_basis_failures(
            symbol_rows,
            daily_dir=resolved_daily_dir,
        )
        if require_raw_price_basis
        else {}
    )
    invalid_price_basis_symbols = [
        symbol for symbol in processed_symbols if symbol in raw_basis_failures
    ]
    remaining_set = set(before["stale_symbols"]) | set(invalid_price_basis_symbols)
    remaining = [symbol for symbol in processed_symbols if symbol in remaining_set]
    provider_results: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    updated_symbols: set[str] = set()
    verified_no_session_symbols: set[str] = set()
    unchanged_symbols: set[str] = {normalize_symbol(row.get("symbol")) for row in symbol_rows} - set(remaining)
    failed_symbol_reasons: dict[str, str] = dict(raw_basis_failures)
    provider_timeout = (
        None
        if provider_timeout_seconds is None
        else max(0.0, float(provider_timeout_seconds))
    )
    timeout_overrides = {
        str(name).strip(): max(0.0, float(value))
        for name, value in (provider_timeout_overrides or {}).items()
        if str(name).strip()
    }
    provider_timeout_triggered = False
    provider_global_timeout_triggered = False
    provider_timeout_events: list[dict[str, Any]] = []

    if unsafe_reason:
        warnings.append(unsafe_reason)
    if invalid_price_basis_symbols:
        warnings.append(
            "existing_canonical_price_basis_not_raw:"
            + ",".join(invalid_price_basis_symbols)
        )

    if not remaining:
        for provider_name in selected_providers:
            provider_results.append(
                _add_provider_symbol_lists(
                    DailyProviderResult(provider=provider_name, status="skipped", attempted=0, latest_date=before["max_local_date"]).to_dict(),
                    attempted_symbols=[],
                )
            )
        if network and safe_to_write and not dry_run:
            amount_fill = _backfill_target_amount_from_spot_snapshot(processed_symbols, target)
            if amount_fill.get("updated"):
                updated_symbols.update(normalize_symbol(item) for item in amount_fill.get("updated_symbol_list", []))
                unchanged_symbols.difference_update(updated_symbols)
            provider_results.append(amount_fill)
        after = local_daily_status_for_symbols(
            symbol_rows,
            target_date=target,
            daily_dir=resolved_daily_dir,
            metadata_dir=resolved_metadata_dir,
            calendar=calendar,
        )
        payload = _build_update_payload(
            target_date=target,
            target_date_note=note,
            before=before,
            after=after,
            symbols=symbol_rows,
            explicit_symbols=_parse_symbols_arg(symbols),
            providers=selected_providers,
            provider_results=provider_results,
            updated_symbols=updated_symbols,
            unchanged_symbols=unchanged_symbols,
            failed_symbols=set(),
            failed_symbol_reasons=failed_symbol_reasons,
            warnings=warnings,
            errors=errors,
            dry_run=dry_run,
            network=network,
            workers=workers,
            only_low_price=only_low_price,
            status="dry_run" if dry_run else ("warning" if warnings else "completed"),
        )
        _attach_raw_price_basis_contract(
            payload,
            require_raw_price_basis=require_raw_price_basis,
            invalid_price_basis_symbols=invalid_price_basis_symbols,
            invalid_price_basis_reasons=raw_basis_failures,
        )
        _attach_provider_timeout_contract(
            payload,
            provider_timeout_seconds=provider_timeout,
            provider_timeout_overrides=timeout_overrides,
            provider_timeout_triggered=False,
            provider_global_timeout_triggered=False,
            provider_timeout_events=[],
        )
        payload["process_note"] = processing_notice(limit=limit, symbols=symbols, processed_symbols=processed_symbols)
        payload["run_id"] = str(run_id or "")
        if write_result:
            atomic_write_json(output_path, payload)
            payload["path"] = _display_path(output_path)
        else:
            payload["path"] = ""
        return payload

    if dry_run or not network or not safe_to_write:
        for provider_name in selected_providers:
            availability = provider_availability(provider_name)
            status = availability["status"]
            reason = availability.get("failed_reason", "")
            if dry_run and status == "available":
                reason = "dry-run：仅检查计划和 provider 可用性，不联网、不写 data/daily。"
            elif not network and status == "available":
                reason = "network=false：未显式授权联网补全，provider 请求已跳过。"
            provider_results.append(
                _add_provider_symbol_lists(
                    DailyProviderResult(
                        provider=provider_name,
                        status=status if status != "available" else "skipped",
                        attempted=len(remaining),
                        updated=0,
                        failed=0 if status == "available" else len(remaining),
                        latest_date=before["max_local_date"],
                        failed_reason=reason,
                    ).to_dict(),
                    attempted_symbols=list(remaining),
                    failed_symbols=[] if status == "available" else list(remaining),
                )
            )
        payload = _build_update_payload(
            target_date=target,
            target_date_note=note,
            before=before,
            after=before,
            symbols=symbol_rows,
            explicit_symbols=_parse_symbols_arg(symbols),
            providers=selected_providers,
            provider_results=provider_results,
            updated_symbols=updated_symbols,
            unchanged_symbols=unchanged_symbols,
            failed_symbols=(
                set(invalid_price_basis_symbols) if not dry_run else set()
            ) | (set(remaining) if not dry_run and not safe_to_write else set()),
            failed_symbol_reasons=failed_symbol_reasons,
            warnings=warnings,
            errors=errors,
            dry_run=dry_run,
            network=network,
            workers=workers,
            only_low_price=only_low_price,
            status=(
                "dry_run"
                if dry_run
                else "failed" if invalid_price_basis_symbols
                else ("skipped_network_false" if not network else "blocked")
            ),
        )
        _attach_raw_price_basis_contract(
            payload,
            require_raw_price_basis=require_raw_price_basis,
            invalid_price_basis_symbols=invalid_price_basis_symbols,
            invalid_price_basis_reasons=raw_basis_failures,
        )
        _attach_provider_timeout_contract(
            payload,
            provider_timeout_seconds=provider_timeout,
            provider_timeout_overrides=timeout_overrides,
            provider_timeout_triggered=False,
            provider_global_timeout_triggered=False,
            provider_timeout_events=[],
        )
        payload["process_note"] = processing_notice(limit=limit, symbols=symbols, processed_symbols=processed_symbols)
        payload["run_id"] = str(run_id or "")
        if write_result:
            atomic_write_json(output_path, payload)
            payload["path"] = _display_path(output_path)
        else:
            payload["path"] = ""
        return payload

    provider_deadline = (
        None
        if provider_timeout is None
        else time.monotonic() + provider_timeout
    )
    for provider_name in selected_providers:
        if not remaining:
            provider_results.append(
                _add_provider_symbol_lists(
                    DailyProviderResult(provider=provider_name, status="skipped", attempted=0, latest_date=target).to_dict(),
                    attempted_symbols=[],
                )
            )
            continue
        availability = provider_availability(provider_name)
        if availability["status"] != "available":
            reason = str(availability.get("failed_reason") or availability["status"])
            provider_results.append(
                _add_provider_symbol_lists(
                    DailyProviderResult(
                        provider=provider_name,
                        status=str(availability["status"]),
                        attempted=len(remaining),
                        failed=len(remaining),
                        latest_date=before["max_local_date"],
                        failed_reason=reason,
                    ).to_dict(),
                    attempted_symbols=list(remaining),
                    failed_symbols=list(remaining),
                )
            )
            for symbol in remaining:
                failed_symbol_reasons.setdefault(symbol, reason)
            continue
        attempted = len(remaining)
        updated = 0
        failed = 0
        provider_latest = ""
        provider_warnings: list[str] = []
        provider_failures: list[str] = []
        attempted_symbols = list(remaining)
        provider_updated_symbols: list[str] = []
        provider_verified_no_session_symbols: list[str] = []
        provider_failed_symbols: list[str] = []
        still_remaining: list[str] = []
        provider_stage_timed_out = False
        provider_timeout_scope = ""
        global_remaining = (
            None
            if provider_deadline is None
            else max(0.0, provider_deadline - time.monotonic())
        )
        provider_slice = timeout_overrides.get(provider_name)
        stage_timeout_candidates = [
            value
            for value in (global_remaining, provider_slice)
            if value is not None
        ]
        stage_timeout = (
            min(stage_timeout_candidates)
            if stage_timeout_candidates
            else None
        )
        if stage_timeout is not None:
            provider_timeout_scope = (
                "provider_slice"
                if provider_slice is not None
                and (
                    global_remaining is None
                    or provider_slice < global_remaining
                )
                else "global_budget"
            )
            stage_result = run_process_tasks_until_deadline(
                _PROVIDER_PROCESS_WORKER,
                remaining,
                context={
                    "provider_name": provider_name,
                    "target": target,
                    "network": network,
                    "calendar": calendar,
                    "daily_dir": str(resolved_daily_dir),
                    "metadata_dir": str(resolved_metadata_dir),
                    "writer_proof": daily_writer_proof(),
                },
                max_workers=min(worker_count, len(remaining)),
                timeout_seconds=stage_timeout,
            )
            outcomes = []
            for index, symbol in enumerate(remaining):
                if index in stage_result.results:
                    outcomes.append(stage_result.results[index])
                elif index in stage_result.failures:
                    outcomes.append(
                        {
                            "symbol": symbol,
                            "updated": False,
                            "latest_date": "",
                            "warnings": [],
                            "reason": (
                                "daily_provider_worker_failed:"
                                + stage_result.failures[index]
                            ),
                        }
                    )
                else:
                    unfinished_reason = (
                        "daily_provider_timeout"
                        if stage_result.timed_out
                        else "daily_provider_worker_exit"
                    )
                    outcomes.append(
                        {
                            "symbol": symbol,
                            "updated": False,
                            "latest_date": "",
                            "warnings": [],
                            "reason": unfinished_reason,
                        }
                    )
            provider_stage_timed_out = bool(stage_result.timed_out)
            if provider_stage_timed_out:
                if (
                    provider_deadline is not None
                    and time.monotonic() >= provider_deadline
                ):
                    provider_timeout_scope = "global_budget"
                provider_timeout_triggered = True
                if provider_timeout_scope == "global_budget":
                    provider_global_timeout_triggered = True
                    errors.append("daily_provider_timeout")
                unfinished_symbols = [
                    remaining[index]
                    for index in stage_result.unfinished_indices
                ]
                provider_timeout_events.append(
                    {
                        "provider": provider_name,
                        "scope": provider_timeout_scope,
                        "timeout_seconds": stage_timeout,
                        "unfinished_symbol_list": unfinished_symbols,
                        "reason": "daily_provider_timeout",
                    }
                )
                warnings.append(
                    "daily_provider_timeout:"
                    f"provider={provider_name}:"
                    f"scope={provider_timeout_scope}:"
                    f"unfinished={len(stage_result.unfinished_indices)}"
                )
        elif worker_count == 1 or len(remaining) <= 1:
            outcomes = []
            for index, symbol in enumerate(remaining):
                if index and network and sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                outcomes.append(
                    _fetch_merge_write_symbol(
                        provider_name,
                        symbol,
                        target=target,
                        network=network,
                        calendar=calendar,
                        daily_dir=resolved_daily_dir,
                        metadata_dir=resolved_metadata_dir,
                    )
                )
        else:
            with ThreadPoolExecutor(
                max_workers=min(worker_count, len(remaining)),
                thread_name_prefix=f"daily-{provider_name}",
            ) as executor:
                outcomes = list(
                    executor.map(
                        lambda symbol, provider=provider_name: _fetch_merge_write_symbol(
                            provider,
                            symbol,
                            target=target,
                            network=network,
                            calendar=calendar,
                            daily_dir=resolved_daily_dir,
                            metadata_dir=resolved_metadata_dir,
                        ),
                        remaining,
                    )
                )
        for outcome in outcomes:
            symbol = str(outcome.get("symbol") or "")
            provider_warnings.extend(outcome.get("warnings") or [])
            fresh_latest = str(outcome.get("latest_date") or "")
            provider_latest = max(
                [value for value in [provider_latest, fresh_latest] if value],
                default="",
            )
            if outcome.get("updated"):
                updated += 1
                updated_symbols.add(symbol)
                unchanged_symbols.discard(symbol)
                provider_updated_symbols.append(symbol)
            if outcome.get("verified_no_session"):
                verified_no_session_symbols.add(symbol)
                provider_verified_no_session_symbols.append(symbol)
            if outcome.get("resolved"):
                if not outcome.get("updated"):
                    unchanged_symbols.add(symbol)
                failed_symbol_reasons.pop(symbol, None)
                continue
            failed += 1
            reason = str(outcome.get("reason") or "provider update failed")
            provider_failures.append(reason)
            failed_symbol_reasons[symbol] = _merge_failed_symbol_reason(
                failed_symbol_reasons.get(symbol, ""),
                reason,
            )
            provider_failed_symbols.append(symbol)
            still_remaining.append(symbol)
        resolved = attempted - failed
        status = (
            "timeout"
            if provider_stage_timed_out
            else "ok"
            if resolved and not failed
            else "warning"
            if resolved
            else "failed"
        )
        provider_row = _add_provider_symbol_lists(
            DailyProviderResult(
                provider=provider_name,
                status=status,
                attempted=attempted,
                updated=updated,
                failed=failed,
                latest_date=provider_latest,
                failed_reason=_compact_failure_reasons(provider_failures),
                warnings=provider_warnings[:10],
            ).to_dict(),
            attempted_symbols=attempted_symbols,
            updated_symbols=provider_updated_symbols,
            failed_symbols=provider_failed_symbols,
        )
        provider_row["verified_no_session"] = len(
            provider_verified_no_session_symbols
        )
        provider_row["verified_no_session_symbol_list"] = list(
            provider_verified_no_session_symbols
        )
        provider_results.append(provider_row)
        remaining = still_remaining
        # A provider slice timeout only retires that provider: the whole point
        # of the slice budget is to let the remaining fallback chain run. Only
        # an exhausted global budget stops the chain.
        if provider_global_timeout_triggered or (
            provider_deadline is not None
            and time.monotonic() >= provider_deadline
        ):
            break

    global_budget_exhausted = bool(
        provider_global_timeout_triggered
        or (
            provider_deadline is not None
            and time.monotonic() >= provider_deadline
        )
    )
    failed_symbols = set(remaining)
    if network and safe_to_write and not global_budget_exhausted:
        amount_fill = _backfill_target_amount_from_spot_snapshot(processed_symbols, target)
        if amount_fill.get("updated"):
            filled_symbols = {normalize_symbol(item) for item in amount_fill.get("updated_symbol_list", [])}
            updated_symbols.update(filled_symbols)
            unchanged_symbols.difference_update(filled_symbols)
        provider_results.append(amount_fill)
    after = local_daily_status_for_symbols(
            symbol_rows,
            target_date=target,
            daily_dir=resolved_daily_dir,
            metadata_dir=resolved_metadata_dir,
            calendar=calendar,
        )
    if after["is_stale"]:
        warnings.append(
            f"日线仍停留在 {after['latest_local_date'] or '-'}，因此特征和复盘可能仍基于旧完整日线。"
        )
    features_payload: dict[str, Any] = {}
    if rebuild_features and updated_symbols:
        try:
            features_payload = build_daily_features(only_low_price=only_low_price)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("daily_feature_rebuild_after_update_failed: %s", exc)
            warnings.append(f"日线更新后生成 daily_features 失败：{exc}")
            features_payload = {"ok": False, "message": str(exc)}
    payload = _build_update_payload(
        target_date=target,
        target_date_note=note,
        before=before,
        after=after,
        symbols=symbol_rows,
        explicit_symbols=_parse_symbols_arg(symbols),
        providers=selected_providers,
        provider_results=provider_results,
        updated_symbols=updated_symbols,
        unchanged_symbols=unchanged_symbols,
        failed_symbols=failed_symbols,
        failed_symbol_reasons=failed_symbol_reasons,
        warnings=warnings,
        errors=errors,
        dry_run=False,
        network=network,
        workers=workers,
        only_low_price=only_low_price,
        status=(
            ("partial" if updated_symbols else "failed")
            if failed_symbols
            else "warning"
            if provider_timeout_triggered
            else "completed"
        ),
        features_payload=features_payload,
        verified_no_session_symbols=verified_no_session_symbols,
    )
    _attach_raw_price_basis_contract(
        payload,
        require_raw_price_basis=require_raw_price_basis,
        invalid_price_basis_symbols=invalid_price_basis_symbols,
        invalid_price_basis_reasons=raw_basis_failures,
    )
    _attach_provider_timeout_contract(
        payload,
        provider_timeout_seconds=provider_timeout,
        provider_timeout_overrides=timeout_overrides,
        provider_timeout_triggered=provider_timeout_triggered,
        provider_global_timeout_triggered=provider_global_timeout_triggered,
        provider_timeout_events=provider_timeout_events,
    )
    payload["process_note"] = processing_notice(limit=limit, symbols=symbols, processed_symbols=processed_symbols)
    payload["run_id"] = str(run_id or "")
    if write_result:
        atomic_write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    else:
        payload["path"] = ""
    return payload


def _build_update_payload(
    *,
    target_date: str,
    target_date_note: str,
    before: dict[str, Any],
    after: dict[str, Any],
    symbols: list[dict[str, str]],
    explicit_symbols: list[str] | None = None,
    providers: list[str],
    provider_results: list[dict[str, Any]],
    updated_symbols: set[str],
    unchanged_symbols: set[str],
    failed_symbols: set[str],
    failed_symbol_reasons: dict[str, str],
    warnings: list[str],
    errors: list[str],
    dry_run: bool,
    network: bool,
    workers: int,
    only_low_price: bool,
    status: str,
    features_payload: dict[str, Any] | None = None,
    verified_no_session_symbols: set[str] | None = None,
) -> dict[str, Any]:
    theme = theme_map_status()
    feature = daily_feature_status()
    processed_symbols = [normalize_symbol(row.get("symbol")) for row in symbols if normalize_symbol(row.get("symbol"))]
    warning_list = list(warnings)
    if not processed_symbols:
        warning_list.append("当前没有可分析股票，无法更新 current_analysis_pool。")
    updated_symbol_list = _ordered_symbols(symbols, updated_symbols)
    unchanged_symbol_list = _ordered_symbols(symbols, unchanged_symbols)
    failed_symbol_list = _ordered_symbols(symbols, failed_symbols)
    verified_no_session_set = set(verified_no_session_symbols or set()) | set(
        after.get("verified_no_session_symbols") or []
    )
    verified_no_session_symbol_list = _ordered_symbols(
        symbols,
        verified_no_session_set,
    )
    raw_positive_volume_target_symbol_list = _ordered_symbols(
        symbols,
        list(after.get("raw_positive_volume_target_symbols") or []),
    )
    raw_zero_volume_target_symbol_list = _ordered_symbols(
        symbols,
        list(after.get("raw_zero_volume_target_symbols") or []),
    )
    return {
        "ok": True,
        "status": "warning" if not processed_symbols and status in {"completed", "dry_run", "blocked"} else status,
        "warning_kind": "no_universe" if not processed_symbols else "",
        "data_freshness_status": "no_universe" if not processed_symbols else ("stale" if after.get("is_stale") else "fresh"),
        "scope": "low_price_included_only" if only_low_price else "local_symbols_only",
        "only_low_price": bool(only_low_price),
        "explicit_symbols": list(explicit_symbols or []),
        "full_scan": False,
        "network": bool(network),
        "dry_run": bool(dry_run),
        "workers": max(1, min(int(workers or 1), 8)),
        "target_date": target_date,
        "target_date_note": target_date_note,
        "latest_local_date_before": before.get("latest_local_date", ""),
        "latest_local_date_after": after.get("latest_local_date", ""),
        "max_local_date_before": before.get("max_local_date", ""),
        "max_local_date_after": after.get("max_local_date", ""),
        "coverage_date_after": after.get("coverage_date", ""),
        "raw_bar_coverage_date_after": after.get(
            "raw_bar_coverage_date",
            after.get("latest_local_date", ""),
        ),
        "total_symbols": len(symbols),
        "processed_symbols": processed_symbols,
        "updated_symbols": len(updated_symbols),
        "updated_symbol_list": updated_symbol_list,
        "unchanged_symbols": len(unchanged_symbols),
        "unchanged_symbol_list": unchanged_symbol_list,
        "verified_no_session_symbols": len(verified_no_session_symbol_list),
        "verified_no_session_symbol_list": verified_no_session_symbol_list,
        "raw_positive_volume_target_symbols": len(
            raw_positive_volume_target_symbol_list
        ),
        "raw_positive_volume_target_symbol_list": (
            raw_positive_volume_target_symbol_list
        ),
        "raw_zero_volume_target_symbols": len(
            raw_zero_volume_target_symbol_list
        ),
        "raw_zero_volume_target_symbol_list": raw_zero_volume_target_symbol_list,
        "failed_symbols": len(failed_symbols),
        "failed_symbol_list": failed_symbol_list,
        "stale_count_before": before.get("stale_count", 0),
        "stale_count_after": after.get("stale_count", 0),
        "fresh_symbol_list_after": list(after.get("fresh_symbols") or []),
        "stale_symbol_list_after": list(after.get("stale_symbols") or []),
        "missing_symbol_list_after": list(after.get("missing_symbols") or []),
        "latest_by_symbol_after": dict(after.get("latest_by_symbol") or {}),
        "recent_gap_symbol_count_after": after.get("recent_gap_symbol_count", 0),
        "recent_gap_date_count_after": after.get("recent_gap_date_count", 0),
        "missing_recent_trade_dates_by_symbol": after.get("missing_recent_trade_dates_by_symbol", {}),
        "missing_count_before": before.get("missing_count", 0),
        "missing_count_after": after.get("missing_count", 0),
        "providers": providers,
        "provider_results": provider_results,
        "failed_symbol_reasons": dict(sorted(failed_symbol_reasons.items())) if failed_symbol_reasons else {},
        "warnings": warning_list,
        "errors": errors,
        "theme_map_health": {
            "mapped_count": theme.get("mapped_count", 0),
            "unknown_count": theme.get("unknown_count", 0),
            "pending_count": theme.get("pending_count", 0),
            "failed_reason": theme.get("failed_reason", ""),
        },
        "daily_features": {
            "latest_date": feature.get("latest_date", ""),
            "feature_count": feature.get("feature_count", 0),
            "rebuild_result": features_payload or {},
        },
        "path": _display_path(DAILY_PRICE_UPDATE_LATEST_JSON),
        "updated_at": now_text(),
        "message": after.get("message", ""),
        "disclaimer": "日线 provider fallback 仅补本地学习池缓存；不接券商、不真实下单、不构成投资建议。",
    }
