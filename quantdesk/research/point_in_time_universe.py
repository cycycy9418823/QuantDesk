from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from quantdesk.research._serialization import json_safe as _json_safe

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.shared import normalize_symbol, now_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

DEFAULT_LISTINGS_CSV = Path("data/universe/historical_listings.csv")
DEFAULT_METADATA_DIR = Path("data/metadata")
DISCLAIMER = "Research diagnostics only. Not investment advice."
DATE_COLUMNS = (
    "list_date",
    "delist_date",
    "st_start_date",
    "st_end_date",
    "suspend_start_date",
    "suspend_end_date",
    "suspension_start_date",
    "suspension_end_date",
)
SCHEMA_COLUMNS = (
    "symbol",
    "name",
    "list_date",
    "delist_date",
    "is_active",
    "is_st_current",
    "st_start_date",
    "st_end_date",
    "suspend_start_date",
    "suspend_end_date",
    "suspension_start_date",
    "suspension_end_date",
    "suspension_periods",
    "market",
    "board",
    "first_price_date",
    "last_price_date",
    "source",
    "confidence",
    "survivorship_bias_risk",
    "notes",
    "updated_at",
)
PIT_EXCLUSION_REASON_CODES = (
    "not_listed_yet",
    "delisted",
    "st_excluded",
    "suspended_excluded",
    "missing_listing",
    "missing_list_date",
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if target.is_absolute():
        return target
    return Path(__file__).resolve().parents[2] / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    root = Path(__file__).resolve().parents[2]
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _truthy(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "active", "listed"}:
        return True
    if text in {"0", "false", "no", "n", "inactive", "delisted"}:
        return False
    return None


def _symbol_set_from_csv(path: Path, *, date_columns: tuple[str, ...] = ()) -> tuple[set[str], int, list[str], list[str]]:
    if not path.exists():
        return set(), 0, [], []
    wanted = {"symbol", "code", "ts_code", *date_columns}
    frame = read_csv_frame(path, dtype=str)
    warnings = [str(item) for item in frame.attrs.get("warnings", []) if item]
    if frame.empty:
        return set(), 0, list(frame.columns), warnings
    keep_columns = [column for column in frame.columns if str(column).strip() in wanted]
    frame = frame[keep_columns].copy()
    symbol_col = next((col for col in ("symbol", "code", "ts_code") if col in frame.columns), "")
    if not symbol_col:
        return set(), len(frame), list(frame.columns), warnings
    if date_columns:
        present_dates = [col for col in date_columns if col in frame.columns]
        if present_dates:
            has_date = pd.Series(False, index=frame.index)
            for col in present_dates:
                has_date = has_date | frame[col].fillna("").astype(str).str.strip().ne("")
            frame = frame[has_date].copy()
    raw_symbols = [normalize_symbol(item) for item in frame[symbol_col].dropna().astype(str)]
    invalid_count = sum(1 for item in raw_symbols if not (item.isdigit() and len(item) == 6))
    if invalid_count:
        warnings.append(f"metadata_invalid_symbol_count:{path.name}:{invalid_count}")
    symbols = {item for item in raw_symbols if item.isdigit() and len(item) == 6}
    return symbols, len(frame), list(frame.columns), warnings


def _metadata_dir_for(listings_path: str | Path | None, metadata_dir: str | Path | None) -> Path | None:
    if metadata_dir is not None:
        return _project_path(metadata_dir)
    if listings_path is None:
        return None
    target = _project_path(listings_path)
    # Expected layout: data/universe/historical_listings.csv + data/metadata/*.csv.
    if target.parent.name == "universe":
        return target.parent.parent / "metadata"
    return _project_path(DEFAULT_METADATA_DIR)


def _metadata_coverage(
    *,
    listing_symbols: set[str],
    listings_path: str | Path | None = None,
    metadata_dir: str | Path | None = None,
) -> dict[str, Any]:
    target = _metadata_dir_for(listings_path, metadata_dir)
    if target is None:
        return {
            "available": False,
            "metadata_dir": "",
            "st_symbols": 0,
            "delist_symbols": 0,
            "suspension_symbols": 0,
            "st_matched_symbols": 0,
            "delist_matched_symbols": 0,
            "suspension_matched_symbols": 0,
            "row_counts": {},
        }
    st_symbols, st_rows, _, st_warnings = _symbol_set_from_csv(
        target / "st_history.csv",
        date_columns=("st_start_date", "start_date"),
    )
    delist_symbols, delist_rows, _, delist_warnings = _symbol_set_from_csv(
        target / "delist_history.csv",
        date_columns=("delist_date", "delisting_date", "last_trade_date"),
    )
    suspension_symbols, suspension_rows, _, suspension_warnings = _symbol_set_from_csv(
        target / "suspension_status.csv",
        date_columns=("trade_date",),
    )
    if not suspension_symbols:
        suspension_symbols, suspension_rows, _, suspension_warnings = _symbol_set_from_csv(
            target / "suspension_history.csv",
            date_columns=("trade_date",),
        )
    warnings = list(dict.fromkeys(st_warnings + delist_warnings + suspension_warnings))
    matched = lambda symbols: len(symbols & listing_symbols) if listing_symbols else len(symbols)
    return {
        "available": bool(st_symbols or delist_symbols or suspension_symbols),
        "metadata_dir": _display_path(target),
        "st_symbols": len(st_symbols),
        "delist_symbols": len(delist_symbols),
        "suspension_symbols": len(suspension_symbols),
        "st_matched_symbols": matched(st_symbols),
        "delist_matched_symbols": matched(delist_symbols),
        "suspension_matched_symbols": matched(suspension_symbols),
        "row_counts": {
            "st_history": int(st_rows),
            "delist_history": int(delist_rows),
            "suspension_status_or_history": int(suspension_rows),
        },
        "warnings": warnings,
    }


def load_historical_listings(path: str | Path = DEFAULT_LISTINGS_CSV) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    target = _project_path(path)
    if not target.exists():
        return pd.DataFrame(), [f"missing_historical_listings: {_display_path(target)}"]
    frame = read_csv_frame(target, dtype=str)
    warnings.extend(str(item) for item in frame.attrs.get("warnings", []) if item)
    if frame.empty:
        return pd.DataFrame(), [f"empty_historical_listings: {_display_path(target)}"]
    frame = frame.rename(columns={column: str(column).strip() for column in frame.columns}).copy()
    if "symbol" not in frame.columns:
        return pd.DataFrame(), ["historical_listings_missing_required_column:symbol"]
    missing_optional = [column for column in SCHEMA_COLUMNS if column not in frame.columns]
    for column in missing_optional:
        warnings.append(f"historical_listings_missing_optional_column:{column}")
        frame[column] = ""
    frame["symbol"] = frame["symbol"].map(normalize_symbol)
    invalid_symbol_count = int((frame["symbol"] == "").sum())
    if invalid_symbol_count:
        warnings.append(f"invalid_symbol_count:{invalid_symbol_count}")
    frame = frame[frame["symbol"] != ""].copy()
    for column in DATE_COLUMNS:
        if column in frame.columns:
            raw_nonempty = frame[column].fillna("").astype(str).str.strip() != ""
            parsed = pd.to_datetime(frame[column], errors="coerce")
            bad_count = int((raw_nonempty & parsed.isna()).sum())
            if bad_count:
                warnings.append(f"invalid_date_count:{column}:{bad_count}")
            frame[column] = parsed
    if "is_active" in frame.columns:
        frame["is_active_bool"] = frame["is_active"].map(_truthy)
    else:
        frame["is_active_bool"] = pd.NA
    duplicate_count = int(frame["symbol"].duplicated().sum())
    if duplicate_count:
        warnings.append(f"duplicate_symbol_count:{duplicate_count}; keeping latest updated_at/list_date row")
        sort_cols = [column for column in ("updated_at", "list_date") if column in frame.columns]
        if sort_cols:
            for column in sort_cols:
                if column == "updated_at":
                    frame[column] = pd.to_datetime(frame[column], errors="coerce")
            frame = frame.sort_values(sort_cols)
        frame = frame.drop_duplicates("symbol", keep="last")
    frame.attrs["source_path"] = _display_path(target)
    frame.attrs["warnings"] = list(dict.fromkeys(warnings))
    return frame.reset_index(drop=True), list(dict.fromkeys(warnings))


def _in_range(target: pd.Timestamp, start: Any, end: Any) -> bool:
    if pd.isna(start):
        return False
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if not pd.isna(end) else pd.Timestamp.max
    return start_ts <= target <= end_ts


def parse_suspension_periods(value: Any) -> list[tuple[pd.Timestamp, pd.Timestamp | None]]:
    text = str(value or "").strip()
    if not text:
        return []
    periods: list[tuple[pd.Timestamp, pd.Timestamp | None]] = []
    for chunk in text.replace(";", "|").split("|"):
        item = chunk.strip()
        if not item:
            continue
        if "~" in item:
            start_text, end_text = item.split("~", 1)
        elif "," in item:
            start_text, end_text = item.split(",", 1)
        else:
            start_text, end_text = item, ""
        start = pd.to_datetime(start_text.strip(), errors="coerce")
        end = pd.to_datetime(end_text.strip(), errors="coerce") if end_text.strip() else pd.NaT
        if pd.isna(start):
            continue
        periods.append((pd.Timestamp(start), None if pd.isna(end) else pd.Timestamp(end)))
    return periods


def build_listing_index(listings: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Build the normalized symbol lookup used by all per-date PIT checks."""
    if listings is None or listings.empty or "symbol" not in listings.columns:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for row in listings.to_dict(orient="records"):
        symbol = normalize_symbol(row.get("symbol"))
        if symbol:
            index[symbol] = row
    return index


def _row_for_symbol(
    symbol: str,
    listings: pd.DataFrame,
    *,
    listing_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_symbol(symbol)
    if listing_index is not None:
        return listing_index.get(normalized)
    if listings is None or listings.empty or "symbol" not in listings.columns:
        return None
    rows = listings[listings["symbol"].astype(str).map(normalize_symbol) == normalized]
    if rows.empty:
        return None
    return rows.iloc[-1].to_dict()


def is_tradable_at(
    symbol: str,
    date: Any,
    listings: pd.DataFrame,
    *,
    include_st: bool = False,
    include_suspended: bool = False,
    listing_index: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    target = pd.to_datetime(date, errors="coerce")
    meta = {"symbol": normalize_symbol(symbol), "date": str(date), "reason": "", "pit_universe": False}
    if pd.isna(target):
        meta["reason"] = "invalid_date"
        return False, meta
    row = _row_for_symbol(symbol, listings, listing_index=listing_index)
    if row is None:
        meta["reason"] = "missing_listing"
        return False, meta
    target = pd.Timestamp(target)
    list_date = row.get("list_date")
    delist_date = row.get("delist_date")
    if pd.isna(list_date):
        meta["reason"] = "missing_list_date"
        return False, meta
    if pd.Timestamp(list_date) > target:
        meta["reason"] = "not_listed_yet"
        return False, meta
    # Contract: delist_date is the final tradable session. Exclusion begins on
    # the next date; current is_active state must not be projected backward.
    if not pd.isna(delist_date) and target > pd.Timestamp(delist_date):
        meta["reason"] = "delisted"
        return False, meta
    if not include_st and _in_range(target, row.get("st_start_date"), row.get("st_end_date")):
        meta["reason"] = "st_excluded"
        return False, meta
    suspended_range = _in_range(target, row.get("suspend_start_date"), row.get("suspend_end_date")) or _in_range(target, row.get("suspension_start_date"), row.get("suspension_end_date"))
    suspended_period = any(start <= target <= (end or pd.Timestamp.max) for start, end in parse_suspension_periods(row.get("suspension_periods")))
    if not include_suspended and (suspended_range or suspended_period):
        meta["reason"] = "suspended_excluded"
        return False, meta
    meta["reason"] = "tradable"
    meta["pit_universe"] = True
    return True, meta


def filter_universe_for_rebalance_date(
    candidates: list[str] | pd.DataFrame,
    date: Any,
    listings: pd.DataFrame,
    *,
    include_st: bool = False,
    include_suspended: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    symbols = candidates["symbol"].astype(str).tolist() if isinstance(candidates, pd.DataFrame) and "symbol" in candidates.columns else list(candidates or [])
    included: list[str] = []
    excluded: dict[str, int] = {}
    listing_index = build_listing_index(listings)
    for symbol in symbols:
        ok, meta = is_tradable_at(
            symbol,
            date,
            listings,
            include_st=include_st,
            include_suspended=include_suspended,
            listing_index=listing_index,
        )
        if ok:
            included.append(normalize_symbol(symbol))
        else:
            reason = str(meta.get("reason") or "excluded")
            excluded[reason] = excluded.get(reason, 0) + 1
    audit = build_pit_filter_audit(symbols, included, date, listings, excluded_counts=excluded)
    return sorted(dict.fromkeys(included)), audit


def _pit_coverage_info(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "list_date_coverage": _safe_float(status.get("list_date_coverage")),
        "delist_date_coverage": _safe_float(status.get("delist_date_coverage")),
        "st_coverage": _safe_float(status.get("st_coverage")),
        "suspension_coverage": _safe_float(status.get("suspend_coverage")),
        "metadata_coverage": dict(status.get("metadata_coverage") or {}),
    }


def filter_rows_for_pit_membership(
    rows: pd.DataFrame,
    listings: pd.DataFrame,
    *,
    symbol_column: str = "symbol",
    date_column: str = "date",
    include_st: bool = False,
    include_suspended: bool = False,
    listing_index: dict[str, dict[str, Any]] | None = None,
    pit_status: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Filter symbol/date rows before research signals and return a date-level audit."""
    frame = rows.copy()
    status = dict(pit_status or summarize_pit_universe_status(listings))
    index = listing_index if listing_index is not None else build_listing_index(listings)
    reason_counts: dict[str, int] = {}
    by_date: dict[str, dict[str, Any]] = {}
    keep_mask: list[bool] = []

    if symbol_column not in frame.columns or date_column not in frame.columns:
        missing = [column for column in (symbol_column, date_column) if column not in frame.columns]
        warnings = list(status.get("warnings") or []) + [
            f"pit_rows_missing_required_columns:{','.join(missing)}"
        ]
        audit = {
            "pit_universe_used": False,
            "target_date": "",
            "checked_dates": [],
            "checked_date_count": 0,
            "requested_row_count": int(len(frame)),
            "included_row_count": 0,
            "excluded_row_count": int(len(frame)),
            "requested_count": int(len(frame)),
            "included_count": 0,
            "excluded_count": int(len(frame)),
            "coverage": 0.0,
            "reason_counts": {"missing_required_columns": int(len(frame))} if len(frame) else {},
            "excluded_counts": {"missing_required_columns": int(len(frame))} if len(frame) else {},
            "by_date": {},
            "coverage_info": _pit_coverage_info(status),
            "survivorship_bias_risk": str(status.get("survivorship_bias_risk") or "high"),
            "pit_status": status,
            "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        }
        return frame.iloc[0:0].copy(), audit

    for symbol, date in frame[[symbol_column, date_column]].itertuples(index=False, name=None):
        target = pd.to_datetime(date, errors="coerce")
        date_key = pd.Timestamp(target).strftime("%Y-%m-%d") if not pd.isna(target) else str(date)
        date_audit = by_date.setdefault(
            date_key,
            {
                "requested_row_count": 0,
                "included_row_count": 0,
                "excluded_row_count": 0,
                "reason_counts": {},
            },
        )
        date_audit["requested_row_count"] += 1
        ok, meta = is_tradable_at(
            str(symbol),
            date,
            listings,
            include_st=include_st,
            include_suspended=include_suspended,
            listing_index=index,
        )
        keep_mask.append(bool(ok))
        if ok:
            date_audit["included_row_count"] += 1
            continue
        reason = str(meta.get("reason") or "excluded")
        date_audit["excluded_row_count"] += 1
        date_reasons = date_audit["reason_counts"]
        date_reasons[reason] = int(date_reasons.get(reason, 0)) + 1
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    for date_audit in by_date.values():
        date_audit["reason_counts"] = dict(sorted(date_audit["reason_counts"].items()))
    requested_count = int(len(frame))
    included_count = int(sum(keep_mask))
    excluded_count = max(0, requested_count - included_count)
    checked_dates = sorted(by_date)
    coverage_info = _pit_coverage_info(status)
    risk = str(status.get("survivorship_bias_risk") or "high")
    audit = {
        "pit_universe_used": bool(status.get("pit_universe")),
        "target_date": checked_dates[-1] if checked_dates else "",
        "checked_dates": checked_dates,
        "checked_date_count": len(checked_dates),
        "requested_row_count": requested_count,
        "included_row_count": included_count,
        "excluded_row_count": excluded_count,
        # Compatibility aliases retained for existing single-date consumers.
        "requested_count": requested_count,
        "included_count": included_count,
        "excluded_count": excluded_count,
        "coverage": round(included_count / requested_count, 6) if requested_count else 0.0,
        "reason_counts": dict(sorted(reason_counts.items())),
        "excluded_counts": dict(sorted(reason_counts.items())),
        "by_date": dict(sorted(by_date.items())),
        "coverage_info": coverage_info,
        **coverage_info,
        "survivorship_bias_risk": risk,
        "pit_status": status,
        "warnings": list(dict.fromkeys(str(item) for item in status.get("warnings") or [] if item)),
    }
    included = frame.iloc[[index for index, keep in enumerate(keep_mask) if keep]].copy()
    return included.reset_index(drop=True), audit


def build_pit_filter_audit(
    requested_symbols: list[str],
    included_symbols: list[str],
    date: Any,
    listings: pd.DataFrame,
    *,
    excluded_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    status = summarize_pit_universe_status(listings)
    requested = [normalize_symbol(item) for item in requested_symbols if normalize_symbol(item)]
    included = [normalize_symbol(item) for item in included_symbols if normalize_symbol(item)]
    target = pd.to_datetime(date, errors="coerce")
    target_date = str(pd.Timestamp(target).date()) if not pd.isna(target) else str(date)
    excluded = dict(sorted((excluded_counts or {}).items()))
    requested_count = len(requested)
    included_count = len(included)
    excluded_count = max(0, requested_count - included_count)
    coverage_info = _pit_coverage_info(status)
    return {
        "pit_universe_used": bool(status.get("pit_universe")),
        "target_date": target_date,
        "checked_dates": [target_date],
        "checked_date_count": 1,
        "requested_row_count": requested_count,
        "included_row_count": included_count,
        "excluded_row_count": excluded_count,
        "requested_count": requested_count,
        "included_count": included_count,
        "excluded_count": excluded_count,
        "coverage": round(included_count / requested_count, 6) if requested else 0.0,
        "reason_counts": excluded,
        "excluded_counts": excluded,
        "by_date": {
            target_date: {
                "requested_row_count": requested_count,
                "included_row_count": included_count,
                "excluded_row_count": excluded_count,
                "reason_counts": excluded,
            }
        },
        "coverage_info": coverage_info,
        **coverage_info,
        "survivorship_bias_risk": str(status.get("survivorship_bias_risk") or "high"),
        "pit_status": status,
        "warnings": list(status.get("warnings") or []),
    }


def compare_pit_vs_non_pit_results(non_pit: dict[str, Any], pit: dict[str, Any]) -> dict[str, Any]:
    non_metrics = non_pit.get("metrics") if isinstance(non_pit.get("metrics"), dict) else {}
    pit_metrics = pit.get("metrics") if isinstance(pit.get("metrics"), dict) else {}
    keys = sorted(set(non_metrics) | set(pit_metrics))
    metric_diff = {key: {"non_pit": non_metrics.get(key), "pit": pit_metrics.get(key)} for key in keys if non_metrics.get(key) != pit_metrics.get(key)}
    return {
        "available": bool(non_metrics or pit_metrics),
        "non_pit_status": non_pit.get("status") or "",
        "pit_status": pit.get("status") or "",
        "metric_diff": metric_diff,
        "disclaimer": "PIT comparison is diagnostic only and does not change default backtest logic.",
    }


def universe_at(
    target_date: Any,
    listings: pd.DataFrame,
    *,
    include_st: bool = False,
    include_suspended: bool = False,
    active_only: bool = True,
    listing_index: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    target = pd.to_datetime(target_date, errors="coerce")
    warnings = list(getattr(listings, "attrs", {}).get("warnings", []) or [])
    source_path = str(getattr(listings, "attrs", {}).get("source_path", ""))
    excluded = {"not_listed_yet": 0, "delisted": 0, "st": 0, "suspended": 0, "invalid_dates": 0}
    if pd.isna(target):
        return [], {
            "target_date": str(target_date),
            "symbol_count": 0,
            "include_st": bool(include_st),
            "include_suspended": bool(include_suspended),
            "active_only": bool(active_only),
            "source_path": source_path,
            "pit_universe": False,
            "warnings": warnings + ["invalid_target_date"],
            "excluded_counts": excluded,
        }
    if listings is None or listings.empty:
        return [], {
            "target_date": pd.Timestamp(target).strftime("%Y-%m-%d"),
            "symbol_count": 0,
            "include_st": bool(include_st),
            "include_suspended": bool(include_suspended),
            "active_only": bool(active_only),
            "source_path": source_path,
            "pit_universe": False,
            "warnings": warnings + ["missing_historical_listings"],
            "excluded_counts": excluded,
        }
    frame = listings.copy()
    if "list_date" not in frame.columns:
        warnings.append("list_date_missing: cannot prove point-in-time listing eligibility")
        frame["list_date"] = pd.NaT
    if "delist_date" not in frame.columns:
        warnings.append("delist_date_missing: delisting filter unavailable")
        frame["delist_date"] = pd.NaT
    selected: list[str] = []
    index = listing_index if listing_index is not None else build_listing_index(frame)
    reason_to_legacy = {
        "not_listed_yet": "not_listed_yet",
        "delisted": "delisted",
        "st_excluded": "st",
        "suspended_excluded": "suspended",
        "missing_listing": "invalid_dates",
        "missing_list_date": "invalid_dates",
        "invalid_date": "invalid_dates",
    }
    for row in frame.to_dict(orient="records"):
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            excluded["invalid_dates"] += 1
            continue
        tradable, detail = is_tradable_at(
            symbol,
            target,
            frame,
            include_st=include_st,
            include_suspended=include_suspended,
            listing_index=index,
        )
        if tradable:
            selected.append(symbol)
            continue
        legacy_reason = reason_to_legacy.get(str(detail.get("reason") or ""), "invalid_dates")
        excluded[legacy_reason] += 1
    meta = {
        "target_date": pd.Timestamp(target).strftime("%Y-%m-%d"),
        "symbol_count": len(selected),
        "include_st": bool(include_st),
        "include_suspended": bool(include_suspended),
        "active_only": bool(active_only),
        "source_path": source_path,
        "pit_universe": bool(selected) and "list_date_missing: cannot prove point-in-time listing eligibility" not in warnings,
        "warnings": list(dict.fromkeys(warnings)),
        "excluded_counts": excluded,
    }
    return sorted(dict.fromkeys(selected)), meta


def build_pit_universe_panel(
    dates: list[Any] | pd.Series,
    listings: pd.DataFrame,
    *,
    include_st: bool = False,
    include_suspended: bool = False,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    listing_index = build_listing_index(listings)
    for date in dates:
        symbols, meta = universe_at(
            date,
            listings,
            include_st=include_st,
            include_suspended=include_suspended,
            listing_index=listing_index,
        )
        warnings.extend(meta.get("warnings") or [])
        target = meta.get("target_date") or str(date)
        for symbol in symbols:
            rows.append({"date": target, "symbol": symbol, "is_in_universe": True})
    panel = pd.DataFrame(rows, columns=["date", "symbol", "is_in_universe"])
    if not panel.empty:
        panel["date"] = pd.to_datetime(panel["date"], errors="coerce")
        panel["symbol"] = panel["symbol"].astype(str)
        panel["is_in_universe"] = panel["is_in_universe"].fillna(False).astype(bool)
    panel.attrs["warnings"] = list(dict.fromkeys(warnings))
    return panel


def summarize_pit_universe_status(
    listings: pd.DataFrame,
    path: str | Path | None = None,
    *,
    metadata_dir: str | Path | None = None,
) -> dict[str, Any]:
    warnings = list(getattr(listings, "attrs", {}).get("warnings", []) or [])
    if listings is None or listings.empty:
        if not warnings:
            warnings.append(f"missing_historical_listings: {_display_path(path or DEFAULT_LISTINGS_CSV)}")
        return {
            "available": False,
            "pit_universe": False,
            "historical_listings_exists": False,
            "historical_listings_row_count": 0,
            "symbol_count": 0,
            "source": "",
            "pit_quality": "missing",
            "survivorship_bias_risk": "high",
            "list_date_coverage": 0.0,
            "delist_date_coverage": 0.0,
            "st_coverage": 0.0,
            "suspend_coverage": 0.0,
            "warnings": list(dict.fromkeys(warnings)),
            "suggested_next_action": "Create data/universe/historical_listings.csv with list/delist/ST/suspend history before enabling PIT research filters.",
        }
    count = max(1, len(listings))
    listing_symbols = (
        {normalize_symbol(item) for item in listings["symbol"].dropna().astype(str)}
        if "symbol" in listings.columns
        else set()
    )
    listing_symbols.discard("")
    metadata = _metadata_coverage(listing_symbols=listing_symbols, listings_path=path, metadata_dir=metadata_dir)
    list_cov = _safe_float(listings.get("list_date", pd.Series(dtype=object)).notna().mean())
    delist_cov = _safe_float(listings.get("delist_date", pd.Series(dtype=object)).notna().mean())
    st_cov = _safe_float((listings.get("st_start_date", pd.Series(dtype=object)).notna() | listings.get("st_end_date", pd.Series(dtype=object)).notna()).sum() / count)
    suspend_cov = _safe_float((listings.get("suspend_start_date", pd.Series(dtype=object)).notna() | listings.get("suspend_end_date", pd.Series(dtype=object)).notna()).sum() / count)
    if metadata.get("available"):
        delist_cov = max(delist_cov, _safe_float(float(metadata.get("delist_matched_symbols") or 0) / count))
        st_cov = max(st_cov, _safe_float(float(metadata.get("st_matched_symbols") or 0) / count))
        suspend_cov = max(suspend_cov, _safe_float(float(metadata.get("suspension_matched_symbols") or 0) / count))
    source_values = listings.get("source", pd.Series(dtype=object)).fillna("").astype(str).str.strip()
    source = str(source_values[source_values != ""].mode().iloc[0]) if not source_values[source_values != ""].empty else ""
    degraded = source == "price_csv_coverage" or bool(listings.get("confidence", pd.Series(dtype=object)).astype(str).str.contains("low", case=False, na=False).any())
    evidence_complete = bool(
        list_cov >= 1.0
        and delist_cov >= 1.0
        and st_cov >= 1.0
        and suspend_cov >= 1.0
        and not degraded
    )
    degraded_warnings = ["degraded_pit_price_csv_coverage"] if degraded else []
    if degraded:
        # Round 21: price-csv-coverage listings use the first locally available
        # price date; consumers must see that it is NOT the official listing date.
        degraded_warnings.append("list_date_is_first_available_local_price_date_not_official_listing_date")
    if not evidence_complete:
        degraded_warnings.append("incomplete_pit_evidence_survivorship_bias_risk_high")
    if degraded and not metadata.get("st_symbols") and st_cov <= 0.0:
        degraded_warnings.append("historical_st_periods_unavailable")
    metadata_warnings = list(metadata.get("warnings") or [])
    warnings_out = list(dict.fromkeys(warnings + degraded_warnings + metadata_warnings))
    enriched = bool(degraded and metadata.get("available"))
    status_source = f"{source}+local_metadata" if enriched and source else (source or ("local_metadata" if metadata.get("available") else ""))
    return {
        "available": True,
        "pit_universe": bool(list_cov > 0),
        "historical_listings_exists": True,
        "historical_listings_row_count": int(len(listings)),
        "symbol_count": int(listings["symbol"].nunique()) if "symbol" in listings.columns else 0,
        "source": status_source,
        "pit_quality": "partial_metadata_enriched" if enriched else ("degraded" if degraded else "full_or_user_supplied"),
        "survivorship_bias_risk": "unknown" if evidence_complete else "high",
        "list_date_coverage": list_cov,
        "delist_date_coverage": delist_cov,
        "st_coverage": st_cov,
        "suspend_coverage": suspend_cov,
        "metadata_coverage": metadata,
        "warnings": warnings_out,
        "suggested_next_action": "Use --date YYYY-MM-DD to inspect point-in-time universe membership before enabling research filters.",
    }


def build_pit_status_payload(
    *,
    target_date: Any = None,
    listings_csv: str | Path = DEFAULT_LISTINGS_CSV,
    include_st: bool = False,
    include_suspended: bool = False,
    active_only: bool = True,
) -> dict[str, Any]:
    listings, load_warnings = load_historical_listings(listings_csv)
    pit_status = summarize_pit_universe_status(listings, listings_csv)
    date_value = target_date or now_text()[:10]
    symbols, meta = universe_at(date_value, listings, include_st=include_st, include_suspended=include_suspended, active_only=active_only)
    warnings = list(dict.fromkeys(load_warnings + list(meta.get("warnings") or []) + list(pit_status.get("warnings") or [])))
    status = "passed" if pit_status.get("pit_universe") else "warning"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "point_in_time_universe",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "target_date": meta.get("target_date") or str(date_value),
        "network": False,
        "config": {
            "listings_csv": _display_path(listings_csv),
            "include_st": bool(include_st),
            "include_suspended": bool(include_suspended),
            "active_only": bool(active_only),
        },
        "pit_status": pit_status,
        "symbol_count": len(symbols),
        "symbols_preview": symbols[:20],
        "excluded_counts": meta.get("excluded_counts") or {},
        "warnings": warnings,
        "output_paths": {},
        "suggested_next_action": pit_status.get("suggested_next_action") or "Review PIT universe status.",
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(payload)


def write_pit_status_outputs(payload: dict[str, Any], *, output_dir: str | Path = "results") -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    stamp = now_text().replace("-", "").replace(":", "").replace("T", "_").replace(" ", "_")
    latest_json = output_path / "pit_universe_status_latest.json"
    timestamp_json = output_path / f"pit_universe_status_{stamp}.json"
    payload = _json_safe(dict(payload))
    payload["output_paths"] = {
        "latest_json": str(latest_json).replace("\\", "/"),
        "timestamp_json": str(timestamp_json).replace("\\", "/"),
    }
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    return payload
