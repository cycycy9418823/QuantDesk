"""Read-only dry-run import-plan builder for staged vendor evidence.

This module turns an *audited* staged vendor pack (mootdx minute OHLCV or
BaoStock evidence pack) into a **dry-run import plan**. It never executes an
import, never writes canonical market data, and never opens a socket.

Hard contract:

* It first runs :func:`build_staged_provider_evidence_audit`. If the audit does
  not reach ``ready_for_manual_import_review`` (i.e. the download is still
  running or the pack failed integrity), this module refuses to produce a
  positive import plan and simply propagates the audit's ``recommended_action``.
* Only after the audit passes does it perform a read-only comparison against the
  existing canonical data (``data/intraday/1m`` for mootdx;
  ``data/metadata`` / ``data/calendar`` / ``data/benchmark`` for BaoStock).
* It never overwrites existing formal data. Conflicting rows are reported as
  ``blocked_conflict`` and are never marked importable.
* BaoStock adjustment factors use a different definition than the canonical
  Tushare ``adj_factor``; the two cannot be proven equivalent offline, so the
  plan emits ``factor_semantics_unresolved`` and blocks any factor import.
* ``formal_eligible`` stays ``False`` until a human confirms the import and the
  pack is re-audited.

The four allowed ``recommended_action`` values are:

* ``wait_for_download_completion`` — audit says the download is still running;
* ``repair_staging`` — audit found integrity problems;
* ``factor_semantics_unresolved`` — BaoStock factor semantics can't be proven
  equivalent to the canonical adj_factor definition offline;
* ``ready_for_manual_apply_review`` — the pack passed and a human may now review
  the dry-run plan for a user-confirmed apply (never automatic).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.providers.mootdx_provider import normalize_mootdx_zero_sentinels
from quantdesk.shared import normalize_symbol, now_text, read_json, write_json, write_text
from quantdesk.staged_provider_evidence_audit import (
    _minute_of_day,
    _parse_session_bounds,
    _to_float_series,
    build_staged_provider_evidence_audit,
    detect_staged_provider,
)
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results" / "data_health"
REPORTS_DIR = PROJECT_ROOT / "reports"

DEFAULT_OUTPUT_PATH = RESULTS_DIR / "staged_import_plan_latest.json"
DEFAULT_REPORT_PATH = REPORTS_DIR / "staged_import_plan_latest.md"

CANONICAL_INTRADAY_1M_DIR = Path("data") / "intraday" / "1m"
CANONICAL_METADATA_DIR = Path("data") / "metadata"
CANONICAL_CALENDAR_DIR = Path("data") / "calendar"
CANONICAL_BENCHMARK_DIR = Path("data") / "benchmark"
CANONICAL_UNIVERSE_DIR = Path("data") / "universe"

# Price-conflict tolerance for an existing canonical minute bar vs a staged bar
# at the same (symbol, datetime). A staged value within either tolerance of the
# canonical value is treated as agreement; anything beyond is a hard conflict.
PRICE_ABS_TOL = 1e-4
PRICE_REL_TOL = 0.001  # 0.1%

RECOMMENDED_ACTIONS = {
    "wait_for_download_completion",
    "repair_staging",
    "factor_semantics_unresolved",
    "ready_for_manual_apply_review",
}

DISCLAIMER = (
    "仅用于本地暂存数据导入方案的 dry-run 审查，不接券商、不真实下单、不构成投资建议；"
    "本模块只生成导入计划，绝不执行 --apply，绝不写正式数据；"
    "暂存数据在人工确认导入并重新审计前不是正式策略证据。"
)

# BaoStock -> canonical field-semantics reference for adjustment factors. This
# documents why the two cannot be blindly concatenated.
BAOSTOCK_FACTOR_SEMANTICS = {
    "staged_provider": "baostock",
    "staged_columns": ["symbol", "code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"],
    "staged_factor_date_field": "dividOperateDate",
    "staged_factor_meaning": (
        "BaoStock returns three factors per dividend/split operate-date: foreAdjustFactor (前复权), "
        "backAdjustFactor (后复权), adjustFactor (累计). They are anchored on the latest bar (前复权 "
        "normalizes the most-recent price to 1.0) and only change on operate dates."
    ),
    "canonical_provider": "tushare_compatible_proxy",
    "canonical_columns": ["trade_date", "symbol", "adj_factor", "source", "provider"],
    "canonical_factor_date_field": "trade_date",
    "canonical_factor_meaning": (
        "Canonical adj_factor is a per-trade-date cumulative factor in the Tushare convention "
        "(anchored on the earliest listing, forward-cumulative), stored for every trade_date."
    ),
    "why_cannot_concatenate": (
        "Different anchor bar (BaoStock latest-bar vs Tushare earliest-bar), different date grain "
        "(operate-date sparse vs per-trade-date dense), and different normalization. A raw join would "
        "silently corrupt the adjustment basis. Equivalence cannot be proven offline without a shared "
        "reference series, so factor import stays blocked."
    ),
    "conversion_rule": "unresolved_requires_human_reconciliation",
}

BAOSTOCK_REFERENCE_FIELD_MAPPINGS: dict[str, list[dict[str, str]]] = {
    "trade_calendar": [
        {"source": "calendar_date", "target": "date", "transform": "normalize_iso_date"},
        {"source": "is_trading_day", "target": "is_open", "transform": "0->false;1->true"},
    ],
    "security_master": [
        {
            "source": "code",
            "target": "symbol",
            "transform": "strip_exchange_prefix_and_normalize_6_digit_symbol",
        },
        {"source": "code", "target": "exchange", "transform": "sh->SSE;sz->SZSE"},
        {"source": "code", "target": "board", "transform": "classify_board_from_symbol"},
        {"source": "code_name", "target": "name", "transform": "trim_text"},
        {"source": "ipoDate", "target": "list_date", "transform": "normalize_iso_date"},
        {"source": "outDate", "target": "delist_date", "transform": "blank->null;else_normalize_iso_date"},
        {"source": "status", "target": "status", "transform": "1->L;0->D"},
    ],
    "industry_classification": [
        {"source": "updateDate", "target": "updated_at", "transform": "normalize_iso_date"},
        {
            "source": "code",
            "target": "symbol",
            "transform": "strip_exchange_prefix_and_normalize_6_digit_symbol",
        },
        {"source": "code_name", "target": "name", "transform": "trim_text"},
        {"source": "industry", "target": "industry", "transform": "trim_text"},
        {
            "source": "industryClassification",
            "target": "audit_metadata.classification_system",
            "transform": "audit_only_not_stored_in_theme_map",
        },
    ],
    "index_constituents": [
        {"source": "updateDate", "target": "snapshot_date", "transform": "normalize_iso_date"},
        {
            "source": "code",
            "target": "symbol",
            "transform": "strip_exchange_prefix_and_normalize_6_digit_symbol",
        },
        {"source": "code_name", "target": "name", "transform": "audit_only_not_stored_in_symbol_list"},
    ],
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _root(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else PROJECT_ROOT


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _resolve_staging_dir(
    root: Path,
    staging_dir: str | Path | None,
    provider: str,
) -> tuple[Path | None, str]:
    """Resolve an explicit staging_dir, or auto-detect the newest pack for a provider."""

    if staging_dir:
        sd = Path(staging_dir)
        if not sd.is_absolute():
            sd = root / sd
        summary = read_json(sd / "summary.json", {}) if (sd / "summary.json").exists() else {}
        detected = detect_staged_provider(summary if isinstance(summary, dict) else {}, sd)
        return sd, detected

    staging_root = root / "data" / "vendor_staging"
    if not staging_root.is_dir():
        return None, "unknown"
    candidates: list[tuple[float, Path, str]] = []
    for child in sorted(staging_root.iterdir()):
        if not child.is_dir():
            continue
        summary_path = child / "summary.json"
        summary = read_json(summary_path, {}) if summary_path.exists() else {}
        detected = detect_staged_provider(summary if isinstance(summary, dict) else {}, child)
        if detected != provider:
            continue
        try:
            mtime = summary_path.stat().st_mtime if summary_path.exists() else child.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, child, detected))
    if not candidates:
        return None, "unknown"
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, sd, detected = candidates[0]
    return sd, detected


def _read_csv(path: Path) -> pd.DataFrame:
    return read_csv_frame(path, dtype=str)


def _price_conflict(staged: float, canonical: float) -> bool:
    """True when staged and canonical prices differ beyond both tolerances."""

    if pd.isna(staged) or pd.isna(canonical):
        return True
    diff = abs(float(staged) - float(canonical))
    if diff <= PRICE_ABS_TOL:
        return False
    denom = max(abs(float(canonical)), 1e-9)
    return (diff / denom) > PRICE_REL_TOL


# --------------------------------------------------------------------------- #
# mootdx dry-run plan
# --------------------------------------------------------------------------- #
def _plan_mootdx_symbol(
    staged_path: Path,
    canonical_path: Path,
    symbol: str,
) -> dict[str, Any]:
    """Compare one staged minute CSV against its canonical counterpart (read-only)."""

    entry: dict[str, Any] = {
        "symbol": symbol,
        "new_rows": 0,
        "exact_duplicate_rows": 0,
        "price_nonconflicting_overlap_rows": 0,
        "overlap_semantics": (
            "OHLC price-compatible under the configured tolerances; "
            "the legacy exact_duplicate_rows field does not compare volume/amount"
        ),
        "mootdx_zero_sentinel_rows": 0,
        "mootdx_zero_sentinel_fields": 0,
        "price_conflict_rows": 0,
        "price_conflict_samples": [],
        "bad_ohlcv_rows": 0,
        "out_of_session_rows": 0,
        "blocked_conflict": False,
        "reason_codes": [],
    }

    staged = _read_csv(staged_path)
    if staged.empty:
        entry["reason_codes"].append("empty_staged_csv")
        entry["status"] = "rejected"
        return entry

    staged, sentinel_diagnostics = normalize_mootdx_zero_sentinels(staged)
    entry["mootdx_zero_sentinel_rows"] = int(
        sentinel_diagnostics.get("row_count") or 0
    )
    entry["mootdx_zero_sentinel_fields"] = int(
        sentinel_diagnostics.get("field_count") or 0
    )

    required = ["symbol", "datetime", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in staged.columns]
    if missing:
        entry["reason_codes"].append(f"missing_columns:{','.join(missing)}")
        entry["status"] = "rejected"
        return entry

    s_dt = pd.to_datetime(staged["datetime"], errors="coerce")
    s_o = _to_float_series(staged["open"])
    s_h = _to_float_series(staged["high"])
    s_l = _to_float_series(staged["low"])
    s_c = _to_float_series(staged["close"])
    s_v = _to_float_series(staged["volume"])

    # bad OHLCV rows (structural): unparsable dt, nonpositive/nan price, bad envelope, neg volume
    price_bad = (
        s_o.isna() | s_h.isna() | s_l.isna() | s_c.isna()
        | (s_o <= 0) | (s_h <= 0) | (s_l <= 0) | (s_c <= 0)
    )
    envelope_bad = (s_h < s_l) | (s_h < s_o) | (s_h < s_c) | (s_l > s_o) | (s_l > s_c)
    envelope_bad = envelope_bad.fillna(True)
    vol_bad = s_v.isna() | (s_v < 0)
    dt_bad = s_dt.isna()
    bad_mask = price_bad | envelope_bad | vol_bad | dt_bad
    entry["bad_ohlcv_rows"] = int(bad_mask.sum())

    # out-of-session rows (only for parseable datetimes)
    ms, me, as_, ae = _parse_session_bounds()
    valid_dt_mask = ~dt_bad
    if valid_dt_mask.any():
        minutes = s_dt[valid_dt_mask].map(_minute_of_day)
        in_morning = (minutes >= ms) & (minutes <= me)
        in_afternoon = (minutes >= as_) & (minutes <= ae)
        out_mask = ~(in_morning | in_afternoon)
        entry["out_of_session_rows"] = int(out_mask.sum())

    # good staged rows eligible for comparison: parseable dt, good ohlcv, in-session
    good_mask = valid_dt_mask & (~bad_mask)
    # also require in-session
    if valid_dt_mask.any():
        minute_series = s_dt.map(lambda v: _minute_of_day(v) if pd.notna(v) else -1)
        in_sess = ((minute_series >= ms) & (minute_series <= me)) | ((minute_series >= as_) & (minute_series <= ae))
        good_mask = good_mask & in_sess

    staged_good = pd.DataFrame(
        {
            "datetime": s_dt[good_mask],
            "close": s_c[good_mask],
            "open": s_o[good_mask],
            "high": s_h[good_mask],
            "low": s_l[good_mask],
        }
    ).dropna(subset=["datetime"])

    # canonical comparison (read-only)
    canonical_exists = canonical_path.exists()
    entry["canonical_exists"] = bool(canonical_exists)
    canon_map: dict[Any, dict[str, float]] = {}
    canon_first = canon_last = None
    canon_rows = 0
    if canonical_exists:
        canon = _read_csv(canonical_path)
        if not canon.empty and "datetime" in canon.columns:
            c_dt = pd.to_datetime(canon["datetime"], errors="coerce")
            canonical_price_fields = [
                field for field in ("open", "high", "low", "close") if field in canon.columns
            ]
            canonical_prices = {
                field: _to_float_series(canon[field]) for field in canonical_price_fields
            }
            canon_rows = int(c_dt.notna().sum())
            valid = c_dt.notna()
            if valid.any():
                canon_first = c_dt[valid].min()
                canon_last = c_dt[valid].max()
            for index in canon.index[valid]:
                canon_map[pd.Timestamp(c_dt.loc[index])] = {
                    field: canonical_prices[field].loc[index]
                    for field in canonical_price_fields
                }

    new_rows = 0
    exact_dup = 0
    conflict = 0
    conflict_samples: list[dict[str, Any]] = []
    for row in staged_good.itertuples(index=False):
        key = pd.Timestamp(row.datetime)
        if key not in canon_map:
            new_rows += 1
            continue
        canonical_row = canon_map[key]
        conflicting_fields: list[str] = []
        staged_values: dict[str, float] = {}
        canonical_values: dict[str, float] = {}
        for field in ("open", "high", "low", "close"):
            if field not in canonical_row:
                continue
            staged_value = float(getattr(row, field))
            canonical_value = float(canonical_row[field])
            if _price_conflict(staged_value, canonical_value):
                conflicting_fields.append(field)
                staged_values[field] = staged_value
                canonical_values[field] = canonical_value
        if conflicting_fields:
            conflict += 1
            if len(conflict_samples) < 20:
                conflict_samples.append(
                    {
                        "datetime": key.strftime("%Y-%m-%d %H:%M:%S"),
                        "fields": conflicting_fields,
                        "staged": staged_values,
                        "canonical": canonical_values,
                    }
                )
        else:
            exact_dup += 1

    entry["new_rows"] = int(new_rows)
    entry["exact_duplicate_rows"] = int(exact_dup)
    entry["price_nonconflicting_overlap_rows"] = int(exact_dup)
    entry["price_conflict_rows"] = int(conflict)
    entry["price_conflict_samples"] = conflict_samples

    # date ranges / coverage before/after (never overwrite: after = union view only)
    def _fmt(ts: Any) -> str:
        return pd.Timestamp(ts).strftime("%Y-%m-%d") if ts is not None and pd.notna(ts) else ""

    staged_dates = set(staged_good["datetime"].map(lambda v: pd.Timestamp(v).strftime("%Y-%m-%d")))
    canon_dates: set[str] = set()
    if canonical_exists and canon_map:
        canon_dates = {pd.Timestamp(k).strftime("%Y-%m-%d") for k in canon_map}
    entry["before"] = {
        "canonical_rows": int(canon_rows),
        "canonical_first_date": _fmt(canon_first),
        "canonical_last_date": _fmt(canon_last),
        "canonical_covered_days": len(canon_dates),
    }
    entry["staged"] = {
        "good_rows": int(len(staged_good)),
        "staged_first_date": _fmt(staged_good["datetime"].min()) if not staged_good.empty else "",
        "staged_last_date": _fmt(staged_good["datetime"].max()) if not staged_good.empty else "",
        "staged_covered_days": len(staged_dates),
    }
    entry["after_if_added"] = {
        "rows": int(canon_rows + new_rows),  # only new rows would ever be added; conflicts blocked
        "covered_days": len(canon_dates | staged_dates),
        "note": "after reflects appending ONLY new_rows; conflicts and duplicates are never written",
    }

    if conflict > 0:
        entry["blocked_conflict"] = True
        entry["reason_codes"].append("price_conflict")
    if entry["bad_ohlcv_rows"] > 0:
        entry["reason_codes"].append("bad_ohlcv")
    if entry["out_of_session_rows"] > 0:
        entry["reason_codes"].append("out_of_session")

    if entry["blocked_conflict"]:
        entry["status"] = "blocked_conflict"
    elif new_rows > 0:
        entry["status"] = "new_data_available"
    else:
        entry["status"] = "no_new_data"
    return entry


def _mootdx_downloaded_candidates(symbols: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (normalize_symbol(sym), rec)
        for sym, rec in symbols.items()
        if isinstance(rec, dict) and str(rec.get("status")) == "downloaded"
    ]


def _mootdx_file_backed_excluded_candidates(
    symbols: dict[str, Any],
    minute_dir: Path,
) -> list[tuple[str, dict[str, Any]]]:
    """Return physical staged CSVs that are not ``downloaded`` candidates.

    A ``partial`` file is still useful diagnostic evidence, but its mere
    presence must never promote it into the positive import candidate pool.
    Scanning from disk also makes the file-coverage statement auditable when a
    summary omits a physical CSV.
    """

    by_symbol = {normalize_symbol(sym): rec for sym, rec in symbols.items() if isinstance(rec, dict)}
    excluded: list[tuple[str, dict[str, Any]]] = []
    if not minute_dir.exists():
        return excluded
    for staged_path in sorted(minute_dir.glob("*.csv")):
        symbol = normalize_symbol(staged_path.stem)
        source = by_symbol.get(symbol) or {}
        if str(source.get("status") or "unknown") == "downloaded":
            continue
        record = dict(source)
        record.setdefault("status", "untracked_file")
        record.setdefault("reason_code", str(record.get("status") or "untracked_file"))
        record["file"] = str(staged_path)
        excluded.append((symbol, record))
    return excluded


def _mootdx_non_importable_reason_codes(symbols: dict[str, Any]) -> dict[str, dict[str, int]]:
    non_importable_reason_codes: dict[str, dict[str, int]] = {}
    for rec in symbols.values():
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "unknown")
        if status == "downloaded":
            continue
        reason = str(rec.get("reason_code") or status)
        bucket = non_importable_reason_codes.setdefault(status, {})
        bucket[reason] = bucket.get(reason, 0) + 1
    return non_importable_reason_codes


def _scan_mootdx_symbols(
    candidates: list[tuple[str, dict[str, Any]]],
    minute_dir: Path,
    canonical_dir: Path,
    root: Path,
    *,
    max_symbols: int,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    full_scan = int(max_symbols) <= 0
    sampled = (not full_scan) and len(candidates) > max_symbols
    sample = candidates if full_scan else candidates[: max_symbols if max_symbols else len(candidates)]
    per_symbol: list[dict[str, Any]] = []
    totals = {
        "new_rows": 0,
        "exact_duplicate_rows": 0,
        "price_nonconflicting_overlap_rows": 0,
        "mootdx_zero_sentinel_rows": 0,
        "mootdx_zero_sentinel_fields": 0,
        "price_conflict_rows": 0,
        "bad_ohlcv_rows": 0,
        "out_of_session_rows": 0,
        "symbols_with_new_data": 0,
        "symbols_blocked_conflict": 0,
        "symbols_rejected": 0,
    }
    for sym, rec in sample:
        file_ref = rec.get("file")
        staged_path = Path(str(file_ref)) if file_ref else minute_dir / f"{sym}.csv"
        if not staged_path.is_absolute():
            staged_path = root / staged_path
        entry = _plan_mootdx_symbol(staged_path, canonical_dir / f"{sym}.csv", sym)
        per_symbol.append(entry)
        for field in (
            "new_rows",
            "exact_duplicate_rows",
            "price_nonconflicting_overlap_rows",
            "mootdx_zero_sentinel_rows",
            "mootdx_zero_sentinel_fields",
            "price_conflict_rows",
            "bad_ohlcv_rows",
            "out_of_session_rows",
        ):
            totals[field] += int(entry.get(field, 0))
        if entry.get("status") == "new_data_available":
            totals["symbols_with_new_data"] += 1
        elif entry.get("status") == "blocked_conflict":
            totals["symbols_blocked_conflict"] += 1
        elif entry.get("status") == "rejected":
            totals["symbols_rejected"] += 1
    checked = len(sample)
    scope = {
        "review_scope": "full_scan" if full_scan else "sampled",
        "downloaded_candidate_count": len(candidates),
        "checked_symbol_count": checked,
        "unchecked_symbol_count": max(0, len(candidates) - checked),
        "full_scan_required_before_apply": (not full_scan) or checked < len(candidates),
        "sampled": sampled,
        "max_symbols": int(max_symbols),
    }
    return per_symbol, totals, scope


def _scan_mootdx_excluded_files(
    candidates: list[tuple[str, dict[str, Any]]],
    minute_dir: Path,
    canonical_dir: Path,
    root: Path,
    *,
    max_symbols: int,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    per_symbol, totals, scope = _scan_mootdx_symbols(
        candidates, minute_dir, canonical_dir, root, max_symbols=max_symbols
    )
    for entry, (_, record) in zip(per_symbol, candidates):
        vendor_status = str(record.get("status") or "unknown")
        vendor_reason = str(record.get("reason_code") or vendor_status)
        reasons = [str(value) for value in entry.get("reason_codes") or [] if str(value)]
        if vendor_reason not in reasons:
            reasons.append(vendor_reason)
        entry.update(
            {
                "vendor_status": vendor_status,
                "vendor_reason_code": vendor_reason,
                "diagnostic_status": str(entry.get("status") or "unknown"),
                "review_status": f"excluded_{vendor_status}",
                "diagnostic_only": True,
                "importable": False,
                "reason_codes": reasons,
            }
        )
    return per_symbol, totals, scope


def _build_mootdx_plan(
    staging_dir: Path,
    audit: dict[str, Any],
    root: Path,
    *,
    max_symbols: int,
) -> dict[str, Any]:
    summary = read_json(staging_dir / "summary.json", {})
    symbols = summary.get("symbols") if isinstance(summary.get("symbols"), dict) else {}
    minute_dir = staging_dir / "raw" / "1min"
    canonical_dir = root / CANONICAL_INTRADAY_1M_DIR
    candidates = _mootdx_downloaded_candidates(symbols)
    excluded_candidates = _mootdx_file_backed_excluded_candidates(symbols, minute_dir)
    per_symbol, totals, review_scope = _scan_mootdx_symbols(
        candidates, minute_dir, canonical_dir, root, max_symbols=max_symbols
    )
    excluded_entries, excluded_totals, excluded_scope = _scan_mootdx_excluded_files(
        excluded_candidates, minute_dir, canonical_dir, root, max_symbols=max_symbols
    )
    disk_count = len(list(minute_dir.glob("*.csv"))) if minute_dir.exists() else 0
    checked_downloaded = len(per_symbol)
    checked_excluded = len(excluded_entries)
    checked_disk = min(disk_count, checked_downloaded + checked_excluded)
    unchecked_disk = max(0, disk_count - checked_disk)
    review_scope["full_scan_required_before_apply"] = bool(
        review_scope.get("full_scan_required_before_apply") or unchecked_disk > 0
    )
    return {
        "provider": "mootdx",
        "dataset": "intraday_1m",
        "canonical_dir": _rel(canonical_dir, root),
        "downloaded_candidate_count": len(candidates),
        **review_scope,
        "non_importable_reason_codes": _mootdx_non_importable_reason_codes(symbols),
        "file_scan_coverage": {
            "disk_csv_count": disk_count,
            "checked_downloaded_file_count": checked_downloaded,
            "checked_excluded_file_count": checked_excluded,
            "checked_disk_file_count": checked_disk,
            "unchecked_disk_file_count": unchecked_disk,
        },
        "totals": totals,
        "excluded_diagnostic_totals": excluded_totals,
        "excluded_file_candidate_count": len(excluded_candidates),
        "excluded_file_checked_count": int(excluded_scope.get("checked_symbol_count", 0)),
        "excluded_file_unchecked_count": int(excluded_scope.get("unchecked_symbol_count", 0)),
        "excluded_file_diagnostics": excluded_entries,
        "excluded_file_diagnostics_truncated": False,
        "per_symbol": per_symbol,
        "per_symbol_truncated": False,
        "overwrite_policy": "never_overwrite_existing_canonical_rows",
        "overlap_semantics": (
            "price_nonconflicting_overlap_rows compares OHLC under the configured tolerances; "
            "legacy exact_duplicate_rows is retained only for compatibility and does not compare volume/amount"
        ),
    }


def _build_mootdx_diagnostic_plan(
    staging_dir: Path,
    audit: dict[str, Any],
    root: Path,
    *,
    max_symbols: int,
) -> dict[str, Any]:
    summary = read_json(staging_dir / "summary.json", {})
    symbols = summary.get("symbols") if isinstance(summary.get("symbols"), dict) else {}
    minute_dir = staging_dir / "raw" / "1min"
    canonical_dir = root / CANONICAL_INTRADAY_1M_DIR
    candidates = _mootdx_downloaded_candidates(symbols)
    per_symbol, totals, scope = _scan_mootdx_symbols(
        candidates, minute_dir, canonical_dir, root, max_symbols=max_symbols
    )
    return {
        "provider": "mootdx",
        "dataset": "intraday_1m",
        "diagnostic_only": True,
        "importable": False,
        "canonical_dir": _rel(canonical_dir, root),
        "downloaded_candidate_count": len(candidates),
        **scope,
        "non_importable_reason_codes": _mootdx_non_importable_reason_codes(symbols),
        "totals": totals,
        "per_symbol": per_symbol,
        "per_symbol_truncated": False,
        "overwrite_policy": "never_overwrite_existing_canonical_rows",
        "note": (
            "staged audit did NOT pass; these are diagnostic bad-row statistics only. "
            "recommended_action stays blocking and no import is authorized."
        ),
    }


def _compare_reference_table(
    staged_path: Path,
    canonical_path: Path,
    key_columns: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "staged_exists": staged_path.exists(),
        "canonical_exists": canonical_path.exists(),
        "staged_rows": 0,
        "canonical_rows": 0,
        "new_rows": 0,
        "duplicate_rows": 0,
        "key_columns": key_columns,
        "reason_codes": [],
    }
    if not staged_path.exists():
        result["reason_codes"].append("staged_missing")
        return result
    staged = _read_csv(staged_path)
    result["staged_rows"] = int(len(staged))
    missing = [column for column in key_columns if column not in staged.columns]
    if missing:
        result["reason_codes"].append(f"staged_missing_key_columns:{','.join(missing)}")
        return result
    staged_keys = set(map(tuple, staged[key_columns].astype(str).values.tolist()))
    if not canonical_path.exists():
        result["reason_codes"].append("canonical_missing_all_new")
        result["new_rows"] = len(staged_keys)
        return result
    canonical = _read_csv(canonical_path)
    result["canonical_rows"] = int(len(canonical))
    canonical_missing = [column for column in key_columns if column not in canonical.columns]
    if canonical_missing:
        result["reason_codes"].extend(
            [f"canonical_missing_key_columns:{','.join(canonical_missing)}", "schema_mismatch_manual_map_required"]
        )
        result["new_rows"] = len(staged_keys)
        return result
    canonical_keys = set(map(tuple, canonical[key_columns].astype(str).values.tolist()))
    result["new_rows"] = len(staged_keys - canonical_keys)
    result["duplicate_rows"] = len(staged_keys & canonical_keys)
    return result


def _normalise_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    return "" if pd.isna(parsed) else pd.Timestamp(parsed).strftime("%Y-%m-%d")


def _normalise_open_flag(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "open"}:
        return True
    if text in {"0", "false", "no", "n", "closed"}:
        return False
    return None


def _baostock_calendar_preview(staged_path: Path, canonical_path: Path) -> dict[str, int]:
    preview = {
        "candidate_rows": 0,
        "overlap_key_rows": 0,
        "new_key_rows": 0,
        "value_conflict_rows": 0,
        "invalid_key_rows": 0,
    }
    if not staged_path.exists():
        return preview
    staged = _read_csv(staged_path)
    if "calendar_date" not in staged.columns:
        preview["invalid_key_rows"] = int(len(staged))
        return preview
    canonical_map: dict[str, bool | None] = {}
    if canonical_path.exists():
        canonical = _read_csv(canonical_path)
        if "date" in canonical.columns:
            for row in canonical.itertuples(index=False):
                key = _normalise_date_text(getattr(row, "date", ""))
                if key:
                    canonical_map[key] = _normalise_open_flag(getattr(row, "is_open", ""))
    for row in staged.itertuples(index=False):
        key = _normalise_date_text(getattr(row, "calendar_date", ""))
        if not key:
            preview["invalid_key_rows"] += 1
            continue
        preview["candidate_rows"] += 1
        if key not in canonical_map:
            preview["new_key_rows"] += 1
            continue
        preview["overlap_key_rows"] += 1
        staged_flag = _normalise_open_flag(getattr(row, "is_trading_day", ""))
        if staged_flag is None or canonical_map[key] is None or staged_flag != canonical_map[key]:
            preview["value_conflict_rows"] += 1
    return preview


def _baostock_exchange(code: Any) -> str:
    text = str(code or "").strip().lower()
    if text.startswith("sh"):
        return "SSE"
    if text.startswith("sz"):
        return "SZSE"
    if text.startswith("bj"):
        return "BSE"
    return ""


def _baostock_listing_status(value: Any) -> str:
    text = str(value or "").strip()
    return "L" if text == "1" else "D" if text == "0" else ""


def _baostock_security_master_preview(staged_path: Path, canonical_path: Path) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "staged_rows": 0,
        "candidate_rows_after_filter": 0,
        "excluded_non_equity_rows": 0,
        "overlap_key_rows": 0,
        "new_key_rows": 0,
        "invalid_symbol_rows": 0,
        "conflict_symbol_count": 0,
        "field_conflict_counts": {field: 0 for field in ("name", "list_date", "delist_date", "exchange", "status")},
    }
    if not staged_path.exists():
        return preview
    staged = _read_csv(staged_path)
    preview["staged_rows"] = int(len(staged))
    if "type" not in staged.columns:
        return preview
    candidates = staged[staged["type"].astype(str).str.strip() == "1"].copy()
    preview["candidate_rows_after_filter"] = int(len(candidates))
    preview["excluded_non_equity_rows"] = int(len(staged) - len(candidates))
    canonical_map: dict[str, dict[str, str]] = {}
    if canonical_path.exists():
        canonical = _read_csv(canonical_path)
        if "symbol" in canonical.columns:
            for row in canonical.to_dict(orient="records"):
                symbol = normalize_symbol(row.get("symbol"))
                if symbol:
                    canonical_map[symbol] = {str(key): str(value or "").strip() for key, value in row.items()}
    conflict_symbols: set[str] = set()
    for row in candidates.to_dict(orient="records"):
        symbol = normalize_symbol(row.get("code"))
        if len(symbol) != 6 or not symbol.isdigit():
            preview["invalid_symbol_rows"] += 1
            continue
        canonical_row = canonical_map.get(symbol)
        if canonical_row is None:
            preview["new_key_rows"] += 1
            continue
        preview["overlap_key_rows"] += 1
        staged_values = {
            "name": str(row.get("code_name") or "").strip(),
            "list_date": _normalise_date_text(row.get("ipoDate")),
            "delist_date": _normalise_date_text(row.get("outDate")),
            "exchange": _baostock_exchange(row.get("code")),
            "status": _baostock_listing_status(row.get("status")),
        }
        canonical_values = {
            "name": str(canonical_row.get("name") or "").strip(),
            "list_date": _normalise_date_text(canonical_row.get("list_date")),
            "delist_date": _normalise_date_text(canonical_row.get("delist_date")),
            "exchange": str(canonical_row.get("exchange") or "").strip(),
            "status": str(canonical_row.get("status") or "").strip(),
        }
        for field, value in staged_values.items():
            if value != canonical_values[field]:
                preview["field_conflict_counts"][field] += 1
                conflict_symbols.add(symbol)
    preview["conflict_symbol_count"] = len(conflict_symbols)
    return preview


def _snapshot_dates(path: Path, column: str = "updateDate") -> list[str]:
    if not path.exists():
        return []
    frame = _read_csv(path)
    if column not in frame.columns:
        return []
    return sorted({date for value in frame[column].tolist() if (date := _normalise_date_text(value))})


def _staged_row_count(path: Path) -> int:
    return int(len(_read_csv(path))) if path.exists() else 0


def _baostock_index_snapshot_preview(
    staged_path: Path,
    existing_history: dict[str, Any],
    index_name: str,
) -> dict[str, int]:
    preview = {"candidate_rows": 0, "overlap_key_rows": 0, "new_key_rows": 0, "invalid_key_rows": 0}
    if not staged_path.exists():
        return preview
    staged = _read_csv(staged_path)
    if "updateDate" not in staged.columns or "code" not in staged.columns:
        preview["invalid_key_rows"] = int(len(staged))
        return preview
    raw_index = existing_history.get(index_name) if isinstance(existing_history, dict) else {}
    history = raw_index if isinstance(raw_index, dict) else {}
    for row in staged.itertuples(index=False):
        effective_date = _normalise_date_text(getattr(row, "updateDate", ""))
        symbol = normalize_symbol(getattr(row, "code", ""))
        if not effective_date or len(symbol) != 6 or not symbol.isdigit():
            preview["invalid_key_rows"] += 1
            continue
        preview["candidate_rows"] += 1
        existing = history.get(effective_date)
        existing_symbols = {normalize_symbol(value) for value in existing} if isinstance(existing, list) else set()
        if symbol in existing_symbols:
            preview["overlap_key_rows"] += 1
        else:
            preview["new_key_rows"] += 1
    return preview


def _build_baostock_plan(
    staging_dir: Path,
    audit: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    reference_dir = staging_dir / "reference"
    metadata_dir = root / CANONICAL_METADATA_DIR
    calendar_dir = root / CANONICAL_CALENDAR_DIR
    universe_dir = root / CANONICAL_UNIVERSE_DIR
    datasets: dict[str, dict[str, Any]] = {}

    calendar_staged = reference_dir / "trade_calendar.csv"
    calendar_target = calendar_dir / "a_share_trading_calendar.csv"
    calendar_raw = _compare_reference_table(calendar_staged, calendar_target, ["calendar_date"])
    calendar_preview = _baostock_calendar_preview(calendar_staged, calendar_target)
    datasets["trade_calendar"] = {
        **calendar_raw,
        "raw_schema_reason_codes": calendar_raw.get("reason_codes") or [],
        "new_rows": calendar_preview["new_key_rows"],
        "duplicate_rows": calendar_preview["overlap_key_rows"],
        "reason_codes": [
            "mapping_designed_manual_review_required",
            "apply_locked",
            *(["mapped_value_conflict"] if calendar_preview["value_conflict_rows"] else []),
        ],
        "canonical_target": _rel(calendar_target, root),
        "mapping_status": "mapping_designed_manual_review_required",
        "apply_authorized": False,
        "source_key": ["calendar_date"],
        "target_key": ["date"],
        "field_mapping": BAOSTOCK_REFERENCE_FIELD_MAPPINGS["trade_calendar"],
        "generated_fields": {"source": "baostock", "note": "provider_calendar_staged_review"},
        "mapped_preview": calendar_preview,
        "import_policy": "append_missing_dates_only_same_date_conflict_blocks_never_overwrite",
        "semantics_note": "Mapped preview only; same-date conflicts block and no apply path exists.",
    }

    master_staged = reference_dir / "security_master.csv"
    master_target = metadata_dir / "security_master.csv"
    master_raw = _compare_reference_table(master_staged, master_target, ["code"])
    master_preview = _baostock_security_master_preview(master_staged, master_target)
    datasets["security_master"] = {
        **master_raw,
        "raw_schema_reason_codes": master_raw.get("reason_codes") or [],
        "new_rows": master_preview["new_key_rows"],
        "duplicate_rows": master_preview["overlap_key_rows"],
        "reason_codes": [
            "mapping_designed_manual_review_required",
            "non_equity_types_excluded",
            "apply_locked",
            *(["mapped_field_conflicts_require_review"] if master_preview["conflict_symbol_count"] else []),
        ],
        "canonical_target": _rel(master_target, root),
        "mapping_status": "mapping_designed_manual_review_required",
        "apply_authorized": False,
        "source_key": ["code"],
        "target_key": ["symbol"],
        "field_mapping": BAOSTOCK_REFERENCE_FIELD_MAPPINGS["security_master"],
        "generated_fields": {"source": "baostock", "provider": "baostock"},
        "row_filter": {
            "expression": "type == '1'",
            "purpose": "a_share_equity_only",
            "excluded_type_policy": "type_2_index_type_4_convertible_bond_type_5_etf_stay_staged",
        },
        "mapped_preview": master_preview,
        "import_policy": "append_missing_equities_only_existing_field_conflicts_require_manual_review",
        "semantics_note": "Only type=1 equities enter the preview; existing canonical fields are never overwritten.",
    }

    industry_staged = reference_dir / "industry_classification.csv"
    industry_target = universe_dir / "stock_theme_map.csv"
    industry_raw = _compare_reference_table(industry_staged, industry_target, ["code"])
    datasets["industry_classification"] = {
        **industry_raw,
        "reason_codes": ["pending_only_adapter_required", "snapshot_only_not_pit_history", "apply_locked"],
        "canonical_target": _rel(industry_target, root),
        "mapping_status": "mapping_designed_adapter_required",
        "apply_authorized": False,
        "source_key": ["updateDate", "code"],
        "target_key": ["symbol"],
        "field_mapping": BAOSTOCK_REFERENCE_FIELD_MAPPINGS["industry_classification"],
        "generated_fields": {"source": "baostock_csrc"},
        "proposed_target_schema": [
            "symbol", "name", "sector", "industry", "concept_tags", "theme_tags", "primary_theme", "source", "updated_at"
        ],
        "preserved_target_fields": ["sector", "concept_tags", "theme_tags", "primary_theme"],
        "row_update_policy": "fill_only_blank_or_pending_industry_preserve_other_theme_fields",
        "temporal_policy": {
            "snapshot_only": True,
            "snapshot_dates": _snapshot_dates(industry_staged),
            "historical_backfill_allowed": False,
            "pit_eligible_before_snapshot": False,
        },
        "semantics_note": "Current CSRC snapshot only; preserve non-pending classifications and theme fields.",
    }

    index_target = universe_dir / "index_constituents.json"
    existing_history = read_json(index_target, {}) if index_target.exists() else {}
    index_specs = {
        "hs300_constituents": ("hs300", "000300"),
        "zz500_constituents": ("zz500", "000905"),
        "sz50_constituents": ("sz50", "000016"),
    }
    for name, (index_name, index_symbol) in index_specs.items():
        staged_path = reference_dir / f"{name}.csv"
        staged_rows = _staged_row_count(staged_path)
        snapshot_dates = _snapshot_dates(staged_path)
        mapped_preview = _baostock_index_snapshot_preview(staged_path, existing_history, index_name)
        datasets[name] = {
            "staged_exists": staged_path.exists(),
            "canonical_exists": index_target.exists(),
            "staged_rows": staged_rows,
            "canonical_rows": mapped_preview["overlap_key_rows"],
            "new_rows": mapped_preview["new_key_rows"],
            "duplicate_rows": mapped_preview["overlap_key_rows"],
            "reason_codes": ["snapshot_only_not_history", "apply_locked"],
            "canonical_target": _rel(index_target, root),
            "mapping_status": "mapping_designed_snapshot_only",
            "apply_authorized": False,
            "source_key": ["updateDate", "code"],
            "target_key": ["index_name", "effective_date", "symbol"],
            "field_mapping": BAOSTOCK_REFERENCE_FIELD_MAPPINGS["index_constituents"],
            "index_name": index_name,
            "index_symbol": index_symbol,
            "generated_fields": {"index_name": index_name, "index_symbol": index_symbol, "source": "baostock"},
            "mapped_preview": mapped_preview,
            "temporal_policy": {
                "snapshot_only": True,
                "snapshot_dates": snapshot_dates,
                "historical_membership_claim_allowed": False,
                "forward_fill_before_snapshot_allowed": False,
            },
            "import_policy": "append_one_effective_date_snapshot_never_backfill_history",
            "semantics_note": (
                "The file is one effective-date membership snapshot. It may not be backfilled into earlier "
                "dates or presented as historical membership. No apply path exists."
            ),
        }

    # adjustment factors: ALWAYS blocked — semantics can't be proven equivalent offline
    af_summary = audit.get("adjustment_factors") if isinstance(audit.get("adjustment_factors"), dict) else {}
    af_counts = af_summary.get("summary_counts") if isinstance(af_summary.get("summary_counts"), dict) else {}
    adjustment_factors = {
        "status": "factor_semantics_unresolved",
        "blocked": True,
        "reason_codes": ["factor_semantics_unresolved"],
        "staged_status_counts": af_counts,
        "unsupported_bse": int(af_summary.get("unsupported_bse", 0)) if isinstance(af_summary, dict) else 0,
        "semantics_mapping": BAOSTOCK_FACTOR_SEMANTICS,
        "canonical_target": _rel(metadata_dir / "adjustment_factors.csv", root),
        "note": (
            "BaoStock adjustment factors are NOT concatenated with the canonical Tushare adj_factor. "
            "Offline equivalence cannot be proven; import stays blocked until a human reconciles the "
            "definitions."
        ),
    }

    return {
        "provider": "baostock",
        "datasets": datasets,
        "adjustment_factors": adjustment_factors,
        "overwrite_policy": "never_overwrite_existing_canonical_rows",
    }


# --------------------------------------------------------------------------- #
# top-level plan
# --------------------------------------------------------------------------- #
def build_staged_import_plan(
    *,
    provider: str = "mootdx",
    staging_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    max_symbols: int = 60,
    apply: bool = False,
    output_path: str | Path | None = None,
    report_path: str | Path | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """Build a read-only dry-run import plan for a staged vendor pack.

    Never executes an import. If ``apply`` is truthy this function refuses and
    returns an error payload (real apply is intentionally not implemented).
    """

    root = _root(project_root)

    base: dict[str, Any] = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "staged_import_plan",
        "generated_at": now_text(),
        "network": False,
        "is_real_order": False,
        "formal_eligible": False,
        "dry_run": True,
        "apply_executed": False,
        "provider_requested": provider,
        "disclaimer": DISCLAIMER,
    }

    # Hard refusal: real apply is not implemented.
    if apply:
        base.update(
            {
                "ok": False,
                "recommended_action": "repair_staging",
                "apply_rejected": True,
                "failed_reason": "apply_not_implemented",
                "message": (
                    "real apply is not implemented; this command is dry-run only. "
                    "Re-run without --apply to produce an import plan."
                ),
            }
        )
        return _finish(base, output_path, report_path, root, write)

    sd, detected = _resolve_staging_dir(root, staging_dir, provider)
    if sd is None or not sd.is_dir():
        base.update(
            {
                "ok": False,
                "recommended_action": "repair_staging",
                "failed_reason": "staging_dir_not_found",
                "provider_detected": detected,
            }
        )
        return _finish(base, output_path, report_path, root, write)

    base["staging_dir"] = _rel(sd, root)
    base["provider_detected"] = detected

    # Audit first — refuse to plan unless it passed.
    audit = build_staged_provider_evidence_audit(
        sd, project_root=root, max_files_deep_check=max_symbols, write=False
    )
    audit_action = str(audit.get("recommended_action") or "")
    base["audit_recommended_action"] = audit_action
    base["audit_liveness"] = audit.get("liveness")
    base["audit_errors"] = list(audit.get("errors") or [])

    if audit_action != "ready_for_manual_import_review":
        # Propagate the audit gate; never produce a positive (importable) plan.
        propagated = (
            "wait_for_download_completion"
            if audit_action == "wait_for_download_completion"
            else "repair_staging"
        )
        # For a failed-integrity pack we may surface a DIAGNOSTIC-ONLY view of the
        # audit's bad-row statistics to help repair. It is explicitly NOT an
        # import plan: no per-symbol importable rows, no apply-review verdict.
        diagnostic_only_plan: dict[str, Any] = {}
        if propagated == "repair_staging" and detected == "mootdx":
            # A read-only diagnostic view of bad-row stats for repair. It is
            # explicitly importable=False and does NOT change the blocking action.
            diagnostic_only_plan = _build_mootdx_diagnostic_plan(
                sd, audit.get("audit") or {}, root, max_symbols=max_symbols
            )
        base.update(
            {
                "ok": True,
                "recommended_action": propagated,
                "plan": {},
                "diagnostic_only_plan": diagnostic_only_plan,
                "message": (
                    "staged evidence audit did not pass; no import plan is generated. "
                    f"audit_recommended_action={audit_action}."
                ),
            }
        )
        return _finish(base, output_path, report_path, root, write)

    if detected == "mootdx":
        plan = _build_mootdx_plan(sd, audit.get("audit") or {}, root, max_symbols=max_symbols)
        base["plan"] = plan
        base["recommended_action"] = "ready_for_manual_apply_review"
        # ready_for_manual_apply_review == "a human may now REVIEW the dry-run",
        # NOT "validated" and NOT "apply is allowed". If a full scan was not done,
        # a full scan (max_symbols=0) is required before any future apply.
        base["review_scope_note"] = (
            "ready_for_manual_apply_review means a human may review this dry-run; it does NOT "
            "mean the pack is fully validated or that apply is permitted. Sampled runs must not "
            "be extrapolated to all symbols; run max_symbols=0 for a full scan before any apply."
        )
        base["full_scan_required_before_apply"] = bool(plan.get("review_scope") == "sampled")
    elif detected == "baostock":
        plan = _build_baostock_plan(sd, audit.get("audit") or {}, root)
        base["plan"] = plan
        # BaoStock factor semantics are unresolved -> the whole pack cannot reach
        # a clean apply-review until a human reconciles factor definitions.
        base["recommended_action"] = "factor_semantics_unresolved"
    else:
        base.update(
            {
                "ok": False,
                "recommended_action": "repair_staging",
                "failed_reason": f"unsupported_provider:{detected}",
            }
        )
        return _finish(base, output_path, report_path, root, write)

    return _finish(base, output_path, report_path, root, write)


def _finish(
    payload: dict[str, Any],
    output_path: str | Path | None,
    report_path: str | Path | None,
    root: Path,
    write: bool,
) -> dict[str, Any]:
    action = payload.get("recommended_action")
    if action not in RECOMMENDED_ACTIONS:
        payload["recommended_action"] = "repair_staging"
        payload.setdefault("errors", []).append(f"invalid_recommended_action:{action}")
    # Contract invariants that must never drift.
    payload["formal_eligible"] = False
    payload["apply_executed"] = False
    payload["dry_run"] = True
    if write:
        out = Path(output_path) if output_path else DEFAULT_OUTPUT_PATH
        if not out.is_absolute():
            out = root / out
        write_json(out, payload)
        payload["output_path"] = _rel(out, root)
        rep = Path(report_path) if report_path else DEFAULT_REPORT_PATH
        if not rep.is_absolute():
            rep = root / rep
        write_text(rep, render_staged_import_plan_report(payload))
        payload["report_path"] = _rel(rep, root)
    return payload


# --------------------------------------------------------------------------- #
# markdown report
# --------------------------------------------------------------------------- #
def render_staged_import_plan_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 暂存数据导入方案 dry-run 报告",
        "",
        payload.get("disclaimer", DISCLAIMER),
        "",
        "## 概要",
        "",
        f"- provider（请求/检测）：{payload.get('provider_requested')} / {payload.get('provider_detected', '-')}",
        f"- 暂存目录：{payload.get('staging_dir', '-')}",
        f"- 审计结论：{payload.get('audit_recommended_action', '-')}",
        f"- recommended_action：{payload.get('recommended_action', '-')}",
        f"- formal_eligible：{payload.get('formal_eligible')}（人工确认导入并重新审计前恒为 false）",
        f"- dry_run：{payload.get('dry_run')}　apply_executed：{payload.get('apply_executed')}",
        "",
    ]

    if payload.get("apply_rejected"):
        lines.extend(["## Apply 被拒绝", "", f"- {payload.get('message')}", ""])
        return "\n".join(lines)

    if payload.get("recommended_action") in {"wait_for_download_completion", "repair_staging"} and not payload.get("plan"):
        lines.extend(
            [
                "## 未生成导入计划",
                "",
                f"- {payload.get('message', '暂存审计未通过，不生成导入计划。')}",
                "",
            ]
        )
        return "\n".join(lines)

    plan = payload.get("plan") or {}
    if payload.get("provider_detected") == "mootdx":
        totals = plan.get("totals", {})
        coverage = plan.get("file_scan_coverage", {})
        excluded_totals = plan.get("excluded_diagnostic_totals", {})
        lines.extend(
            [
                "## mootdx 分钟线 dry-run",
                "",
                f"- review_scope：{plan.get('review_scope')}",
                f"- downloaded 候选 symbol 数（未经检查，非可导入结论）：{plan.get('downloaded_candidate_count')}",
                f"- 已检查 symbol 数：{plan.get('checked_symbol_count')}",
                f"- 未检查 symbol 数：{plan.get('unchecked_symbol_count')}",
                f"- apply 前需全量扫描：{plan.get('full_scan_required_before_apply')}",
                f"- 新增行（仅限已检查 symbol）：{totals.get('new_rows')}",
                f"- 价格无冲突重叠行：{totals.get('price_nonconflicting_overlap_rows')}",
                f"- mootdx 原始零值哨兵行（导入时应归零）：{totals.get('mootdx_zero_sentinel_rows')}",
                f"- 价格冲突行：{totals.get('price_conflict_rows')}",
                f"- 坏 OHLCV 行：{totals.get('bad_ohlcv_rows')}",
                f"- 盘外行：{totals.get('out_of_session_rows')}",
                f"- 有新数据的 symbol：{totals.get('symbols_with_new_data')}",
                f"- 因冲突被阻断的 symbol：{totals.get('symbols_blocked_conflict')}",
                f"- 非可导入状态 reason_code：{plan.get('non_importable_reason_codes')}",
                f"- 磁盘 CSV / 已检查 / 未检查：{coverage.get('disk_csv_count')} / "
                f"{coverage.get('checked_disk_file_count')} / {coverage.get('unchecked_disk_file_count')}",
                f"- 被排除但有实体文件的诊断数：{plan.get('excluded_file_checked_count')}（始终 importable=false）",
                f"- 被排除文件诊断：新增={excluded_totals.get('new_rows')} "
                f"价格无冲突重叠={excluded_totals.get('price_nonconflicting_overlap_rows')} 冲突={excluded_totals.get('price_conflict_rows')} "
                f"坏OHLCV={excluded_totals.get('bad_ohlcv_rows')} 盘外={excluded_totals.get('out_of_session_rows')}",
                f"- 覆盖策略：{plan.get('overwrite_policy')}",
                "- 注意：抽样结果不得外推到全部 downloaded 候选；ready_for_manual_apply_review 仅代表可进入人工复核，不代表已全量验证或允许 apply。",
                "",
            ]
        )
    elif payload.get("provider_detected") == "baostock":
        lines.append("## BaoStock 证据包 dry-run")
        lines.append("")
        for name, ds in (plan.get("datasets") or {}).items():
            lines.append(
                f"- {name}: staged_rows={ds.get('staged_rows')} 新增={ds.get('new_rows')} "
                f"重复={ds.get('duplicate_rows')} reason_codes={ds.get('reason_codes')}"
            )
        lines.extend(["", "### 基础表字段映射", ""])
        for name, ds in (plan.get("datasets") or {}).items():
            mappings = ds.get("field_mapping") or []
            mapping_text = "；".join(
                f"{row.get('source')} → {row.get('target')}（{row.get('transform')}）"
                for row in mappings
            )
            lines.extend(
                [
                    f"#### {name}",
                    "",
                    f"- target：{ds.get('canonical_target')}",
                    f"- mapping_status：{ds.get('mapping_status')}；apply_authorized={ds.get('apply_authorized')}",
                    f"- 字段：{mapping_text}",
                ]
            )
            row_filter = ds.get("row_filter")
            if row_filter:
                lines.append(
                    f"- 行过滤：{row_filter.get('expression')}（{row_filter.get('purpose')}）；"
                    f"{row_filter.get('excluded_type_policy')}"
                )
            temporal = ds.get("temporal_policy") or {}
            snapshot_dates = temporal.get("snapshot_dates") or []
            if temporal.get("snapshot_only") and snapshot_dates:
                date_text = "、".join(str(value) for value in snapshot_dates)
                if name.endswith("_constituents"):
                    lines.append(f"- 时间边界：仅代表 {date_text} 当日快照；不得回填或冒充历史成分。")
                else:
                    lines.append(f"- 时间边界：仅代表 {date_text} 当日行业快照；不得冒充历史行业区间。")
            lines.extend([f"- 规则：{ds.get('import_policy') or ds.get('row_update_policy')}", ""])
        af = plan.get("adjustment_factors", {})
        lines.extend(
            [
                "",
                "### 复权因子（阻断）",
                "",
                f"- status：{af.get('status')}（blocked={af.get('blocked')}）",
                f"- 北交所 unsupported：{af.get('unsupported_bse')}",
                f"- 原因：{af.get('note')}",
                "",
                "#### 字段语义对照",
                "",
                f"- 暂存列：{af.get('semantics_mapping', {}).get('staged_columns')}",
                f"- 暂存因子基准：{af.get('semantics_mapping', {}).get('staged_factor_meaning')}",
                f"- canonical 列：{af.get('semantics_mapping', {}).get('canonical_columns')}",
                f"- canonical 因子基准：{af.get('semantics_mapping', {}).get('canonical_factor_meaning')}",
                f"- 为何不能直接拼接：{af.get('semantics_mapping', {}).get('why_cannot_concatenate')}",
                "",
            ]
        )

    lines.extend(
        [
            "## 边界",
            "",
            "- 本报告只读，不写 data/daily、data/intraday、data/metadata、calendar、benchmark。",
            "- 不执行任何 import --apply；ready_for_manual_apply_review 仅表示可进入人工复核。",
            "- 价格冲突行只标记 blocked_conflict，从不建议覆盖已有正式数据。",
            "- 暂存数据在人工确认导入并重新审计前不是正式策略证据。",
            "",
        ]
    )
    return "\n".join(lines)


def write_staged_import_plan_outputs(
    *,
    provider: str = "mootdx",
    staging_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    max_symbols: int = 60,
    apply: bool = False,
    output_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the dry-run plan and write JSON + Markdown (convenience for CLI)."""

    return build_staged_import_plan(
        provider=provider,
        staging_dir=staging_dir,
        project_root=project_root,
        max_symbols=max_symbols,
        apply=apply,
        output_path=output_path,
        report_path=report_path,
        write=True,
    )


__all__ = [
    "build_staged_import_plan",
    "write_staged_import_plan_outputs",
    "render_staged_import_plan_report",
    "BAOSTOCK_FACTOR_SEMANTICS",
    "RECOMMENDED_ACTIONS",
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_REPORT_PATH",
    "DISCLAIMER",
]
