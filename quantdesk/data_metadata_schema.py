from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantdesk.candidate_monitoring import PROHIBITED_USAGE, write_json, write_text
from quantdesk.csv_io import read_csv_frame
from quantdesk.data_truth.adjustment_factor_audit import audit_adjustment_factor_file_set

TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS: dict[str, list[str]] = {
    "security_master.csv": ["symbol", "name", "list_date", "delist_date", "exchange", "board", "status", "source", "provider"],
    "delist_history.csv": ["symbol", "name", "delist_date", "last_trade_date", "delist_reason", "source", "provider"],
    "delist_events.csv": ["symbol", "delist_date", "delist_reason", "last_trade_date", "source", "provider"],
    "st_history.csv": ["symbol", "name", "st_start_date", "st_end_date", "st_type", "source", "provider", "change_reason"],
    "suspension_history.csv": ["trade_date", "symbol", "suspend_timing", "suspend_type", "source", "provider"],
    "suspension_status.csv": ["trade_date", "symbol", "is_suspended", "suspension_reason", "source", "provider"],
    "adjustment_factors.csv": ["trade_date", "symbol", "adj_factor", "source", "provider"],
    "limit_price_history.csv": ["trade_date", "symbol", "pre_close", "up_limit", "down_limit", "source", "provider"],
    "pre_close_history.csv": ["trade_date", "symbol", "pre_close", "source", "provider"],
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "pit_universe": {"file": "pit_universe.csv", "columns": ["trade_date", "symbol", "is_in_universe", "reason", "source", "updated_at"], "bools": ["is_in_universe"], "keys": ["trade_date", "symbol"]},
    "security_master": {"file": "security_master.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["security_master.csv"], "bools": [], "keys": ["symbol"]},
    "st_history": {"file": "st_history.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["st_history.csv"], "bools": [], "keys": ["symbol", "st_start_date", "st_end_date", "st_type"], "duplicate_policy": "warning"},
    "suspension_status": {"file": "suspension_status.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["suspension_status.csv"], "bools": ["is_suspended"], "keys": ["trade_date", "symbol"]},
    "suspension_history": {"file": "suspension_history.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["suspension_history.csv"], "bools": [], "keys": ["trade_date", "symbol"]},
    "delist_events": {"file": "delist_events.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["delist_events.csv"], "bools": [], "keys": ["symbol", "delist_date"]},
    "delist_history": {"file": "delist_history.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["delist_history.csv"], "bools": [], "keys": ["symbol", "delist_date", "last_trade_date", "delist_reason"], "duplicate_policy": "warning"},
    "adjustment_factors": {"file": "adjustment_factors.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["adjustment_factors.csv"], "bools": [], "keys": ["trade_date", "symbol"]},
    "pre_close_history": {"file": "pre_close_history.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["pre_close_history.csv"], "bools": [], "keys": ["trade_date", "symbol"]},
    "limit_price_history": {"file": "limit_price_history.csv", "columns": TUSHARE_PROXY_METADATA_OUTPUT_COLUMNS["limit_price_history.csv"], "bools": [], "keys": ["trade_date", "symbol"]},
    "benchmark_metadata": {"file": "benchmark_metadata.csv", "columns": ["trade_date", "benchmark_symbol", "close", "adj_factor", "pre_close", "source"], "bools": [], "keys": ["trade_date", "benchmark_symbol"]},
    "holdings_snapshot": {"file": "holdings_snapshot.csv", "columns": ["strategy_id", "rebalance_date", "signal_date", "trade_date", "symbol", "weight", "selected_reason", "filter_flags", "price", "amount", "volume", "is_st", "is_suspended", "delist_risk_flag", "data_source"], "bools": ["is_st", "is_suspended", "delist_risk_flag"], "keys": ["strategy_id", "rebalance_date", "symbol"]},
}

# Optional metadata inputs. These describe research conveniences (point-in-time
# universe, benchmark series, holdings snapshots) that are NOT required to run
# the local research/backtest flow. When they are absent we must NOT hard-fail
# and block the flow — we surface them as ``coverage=absent`` (degraded), and
# never invent data to fill them. If formal versions are needed the user must
# provide them explicitly.
OPTIONAL_METADATA: frozenset[str] = frozenset(
    {"pit_universe", "benchmark_metadata", "holdings_snapshot"}
)


def _bad_number_count(frame: pd.DataFrame) -> int:
    numeric = frame.select_dtypes(include=[np.number])
    if numeric.empty:
        return 0
    return int((~np.isfinite(numeric.to_numpy(dtype=float))).sum())


def _present_text_mask(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip()
    return text.notna() & ~text.str.lower().isin(["", "nan", "none", "nat"])


def _read_validation_sample(path: Path, *, max_rows: int = 50_000) -> pd.DataFrame:
    return read_csv_frame(path, dtype=str, nrows=max_rows)


def _metadata_file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size_bytes": 0}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = handle.readline().strip()
    except OSError as exc:
        header = f"read_error:{type(exc).__name__}:{exc}"
    return {"exists": True, "size_bytes": int(path.stat().st_size), "header": header}


def _adjustment_canonical_status(metadata_dir: Path) -> dict[str, Any]:
    canonical = metadata_dir / "adjustment_factors.csv"
    legacy_alias = metadata_dir / "adjustment_factor.csv"
    canonical_status = _metadata_file_status(canonical)
    alias_status = _metadata_file_status(legacy_alias)
    warnings: list[str] = []
    status = "missing"
    if canonical_status["exists"]:
        status = "canonical_available"
    if canonical_status["exists"] and alias_status["exists"]:
        if canonical_status.get("size_bytes") != alias_status.get("size_bytes"):
            status = "canonical_available_alias_size_mismatch"
            warnings.append("legacy adjustment_factor.csv exists and differs in byte size; readers should use adjustment_factors.csv")
        elif canonical_status.get("header") != alias_status.get("header"):
            status = "canonical_available_alias_header_mismatch"
            warnings.append("legacy adjustment_factor.csv exists but header differs; readers should use adjustment_factors.csv")
        else:
            file_set = audit_adjustment_factor_file_set(metadata_dir, sample_rows=1000)
            if file_set.get("file_set_status") == "canonical_with_divergent_alias":
                status = "canonical_available_alias_sample_mismatch"
                warnings.append("legacy adjustment_factor.csv exists and differs from adjustment_factors.csv sample; readers should use adjustment_factors.csv")
            else:
                warnings.append("legacy adjustment_factor.csv duplicate is present; canonical file is adjustment_factors.csv")
    return {
        "canonical_file": "adjustment_factors.csv",
        "legacy_alias_file": "adjustment_factor.csv",
        "status": status,
        "canonical": canonical_status,
        "legacy_alias": alias_status,
        "warnings": warnings,
    }


def validate_metadata_dir(metadata_dir: Path) -> dict[str, Any]:
    if not metadata_dir.exists():
        return {
            "task_id": "data_metadata_validation",
            "status": "metadata_missing",
            "research_only": True,
            "metadata_dir": str(metadata_dir),
            "results": [],
            "missing_files": [schema["file"] for schema in SCHEMAS.values()],
            "prohibited_usage": PROHIBITED_USAGE,
        }
    results = []
    for name, schema in SCHEMAS.items():
        path = metadata_dir / schema["file"]
        errors: list[str] = []
        is_optional = name in OPTIONAL_METADATA
        if not path.exists():
            if is_optional:
                # Optional inputs that are simply not provided are reported as
                # absent coverage, not as a hard failure. This keeps the local
                # research flow unblocked without inventing data.
                results.append({
                    "category": name,
                    "file": schema["file"],
                    "valid": True,
                    "exists": False,
                    "optional": True,
                    "coverage": "absent",
                    "errors": [],
                    "note": "optional metadata not provided; coverage=absent (research flow not blocked)",
                })
            else:
                results.append({"category": name, "file": schema["file"], "valid": False, "exists": False, "optional": False, "coverage": "absent", "errors": ["missing file"]})
            continue
        try:
            frame = _read_validation_sample(path)
        except Exception as exc:
            results.append({"category": name, "file": schema["file"], "valid": False, "errors": [f"csv read failed: {exc}"]})
            continue
        read_warnings = list(frame.attrs.get("warnings", []))
        missing = [col for col in schema["columns"] if col not in frame.columns]
        if missing:
            errors.append(f"missing columns: {missing}")
        date_cols = [col for col in frame.columns if col.endswith("_date") or col == "trade_date"]
        for col in date_cols:
            present = _present_text_mask(frame[col])
            parsed = pd.to_datetime(frame.loc[present, col], errors="coerce")
            if bool(parsed.isna().any()):
                errors.append(f"bad date values: {col}")
        symbol_cols = [col for col in ["symbol", "benchmark_symbol"] if col in frame]
        for col in symbol_cols:
            if frame[col].astype(str).map(lambda v: not bool(re.match(r"^[A-Za-z0-9_.-]+$", v))).any():
                errors.append(f"bad symbol values: {col}")
        for col in schema["bools"]:
            if col in frame and ~frame[col].isin([True, False, 0, 1, "true", "false", "True", "False"]).all():
                errors.append(f"bad boolean values: {col}")
        if _bad_number_count(frame):
            errors.append("NaN/inf numeric values")
        if schema["keys"] and set(schema["keys"]).issubset(frame.columns) and frame.duplicated(schema["keys"]).any():
            duplicate_message = f"duplicate keys in validation sample: {schema['keys']}"
            if schema.get("duplicate_policy") == "warning":
                read_warnings.append(duplicate_message)
            else:
                errors.append(duplicate_message)
        if "source" in schema["columns"] and "source" in frame and frame["source"].isna().any():
            errors.append("source missing")
        results.append({"category": name, "file": schema["file"], "valid": not errors, "exists": True, "optional": is_optional, "coverage": "present" if not errors else "degraded", "errors": errors, "sample_row_count": int(len(frame)), "sampled": True, "read_warnings": read_warnings})
    failed = [item for item in results if not item["valid"]]
    # Optional metadata that is simply absent is degraded coverage, not a hard
    # failure: it must not block the local research flow.
    absent_optional = [item for item in results if item.get("optional") and not item.get("exists", True)]
    canonical_status = _adjustment_canonical_status(metadata_dir)
    if failed:
        status = "failed"
    elif absent_optional:
        status = "degraded"
    else:
        status = "passed"
    return {
        "task_id": "data_metadata_validation",
        "status": status,
        "research_only": True,
        "metadata_dir": str(metadata_dir),
        "results": results,
        "missing_files": [item["file"] for item in results if "missing file" in item.get("errors", [])],
        "absent_optional_files": [item["file"] for item in absent_optional],
        "failed_count": len(failed),
        "adjustment_factor_canonical": canonical_status,
        "warnings": list(canonical_status.get("warnings", [])),
        "prohibited_usage": PROHIBITED_USAGE,
    }


def render_validation_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Data Metadata Validation", "", f"Status: `{payload.get('status')}`", "", "| category | valid | errors |", "|---|---:|---|"]
    for item in payload.get("results", []):
        lines.append(f"| {item.get('category')} | `{item.get('valid')}` | `{item.get('errors')}` |")
    lines += ["", "Boundary: Research-only. Not trading advice. No live trading. No broker connection. No auto execution. No buy/sell recommendation. No real-money allocation instruction."]
    return "\n".join(lines) + "\n"


def write_metadata_validation(metadata_dir: Path, output: Path, report: Path) -> dict[str, Any]:
    payload = validate_metadata_dir(metadata_dir)
    write_json(output, payload)
    write_text(report, render_validation_markdown(payload))
    return payload
