from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.csv_io import read_csv_frame  # noqa: E402
from quantdesk.point_in_time_universe import (  # noqa: E402
    SCHEMA_COLUMNS,
    load_historical_listings,
    summarize_pit_universe_status,
)

from quantdesk.shared import now_text, truthy  # noqa: E402
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION  # noqa: E402

RESULT_JSON = PROJECT_ROOT / "results" / "historical_listings_validate_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "historical_listings_validate_latest.md"


def _rel(path: str | Path) -> str:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


def validate_historical_listings(path: str | Path = "data/universe/historical_listings.csv") -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    errors: list[str] = []
    warnings: list[str] = []
    required_columns_missing: list[str] = []
    duplicate_symbols: list[str] = []
    invalid_dates: list[dict[str, Any]] = []
    source_distribution: dict[str, int] = {}
    confidence_distribution: dict[str, int] = {}
    raw = pd.DataFrame()
    if not target.exists():
        errors.append(f"missing_historical_listings:{_rel(target)}")
    else:
        raw = read_csv_frame(target, dtype=str, encoding="utf-8-sig")
        csv_read_warnings = list(raw.attrs.get("warnings") or [])
        warnings.extend(csv_read_warnings)
        if any(item.startswith("csv_read_failed:") for item in csv_read_warnings):
            errors.append(f"invalid_csv:{'; '.join(csv_read_warnings)}")
    if not raw.empty:
        required_columns_missing = [column for column in SCHEMA_COLUMNS if column not in raw.columns]
        if required_columns_missing:
            errors.append("required_columns_missing")
        if "symbol" in raw.columns:
            symbols = raw["symbol"].fillna("").astype(str).str.strip()
            empty_count = int((symbols == "").sum())
            if empty_count:
                errors.append(f"empty_symbol_count:{empty_count}")
            duplicate_symbols = sorted(symbols[symbols.duplicated() & (symbols != "")].unique().tolist())[:100]
            if duplicate_symbols:
                warnings.append(f"duplicate_symbol_count:{len(duplicate_symbols)}")
        for column in ("list_date", "first_price_date", "last_price_date", "delist_date", "st_start_date", "st_end_date"):
            if column not in raw.columns:
                continue
            values = raw[column].fillna("").astype(str).str.strip()
            parsed = pd.to_datetime(values.where(values != "", pd.NA), errors="coerce")
            bad = values[(values != "") & parsed.isna()]
            if not bad.empty:
                invalid_dates.append({"column": column, "count": int(len(bad))})
        if {"first_price_date", "last_price_date"}.issubset(raw.columns):
            first = pd.to_datetime(raw["first_price_date"], errors="coerce")
            last = pd.to_datetime(raw["last_price_date"], errors="coerce")
            bad_order = int(((first.notna()) & (last.notna()) & (first > last)).sum())
            if bad_order:
                invalid_dates.append({"column": "first_price_date,last_price_date", "count": bad_order, "error": "first_after_last"})
        source_distribution = dict(Counter(raw.get("source", pd.Series(dtype=str)).fillna("").astype(str)))
        confidence_distribution = dict(Counter(raw.get("confidence", pd.Series(dtype=str)).fillna("").astype(str)))
        if not raw.get("delist_date", pd.Series(dtype=str)).fillna("").astype(str).str.strip().any():
            warnings.append("missing_real_delist_date_history")
        if not raw.get("st_start_date", pd.Series(dtype=str)).fillna("").astype(str).str.strip().any():
            warnings.append("missing_historical_st_periods")
        if source_distribution.get("price_csv_coverage"):
            warnings.append("degraded_pit_price_csv_coverage")
    listings, load_warnings = load_historical_listings(target)
    pit_status = summarize_pit_universe_status(listings, target)
    warnings.extend(load_warnings)
    warnings.extend(pit_status.get("warnings") or [])
    row_count = int(len(raw)) if not raw.empty else 0
    symbol_count = int(raw["symbol"].nunique()) if not raw.empty and "symbol" in raw.columns else 0
    if row_count <= 0:
        errors.append("row_count_zero")
    if invalid_dates:
        errors.append("invalid_dates")
    status = "failed" if errors else ("warning" if warnings else "passed")
    payload = {
        "ok": not errors,
        "status": status,
        "task_type": "validate_historical_listings",
        "generated_at": now_text(),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "path": _rel(target),
        "row_count": row_count,
        "symbol_count": symbol_count,
        "required_columns_missing": required_columns_missing,
        "duplicate_symbols": duplicate_symbols,
        "invalid_dates": invalid_dates,
        "source_distribution": source_distribution,
        "confidence_distribution": confidence_distribution,
        "pit_quality": pit_status.get("pit_quality") or ("degraded" if source_distribution.get("price_csv_coverage") else "unknown"),
        "survivorship_bias_risk": pit_status.get("survivorship_bias_risk") or "high",
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item))[:200],
        "errors": list(dict.fromkeys(str(item) for item in errors if item)),
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": "Validation only. price_csv_coverage listings remain degraded PIT data.",
    }
    payload["warning_count"] = len(payload["warnings"])
    payload["warnings_top"] = payload["warnings"][:10]
    return _json_safe(payload)


def render_report(payload: dict[str, Any]) -> str:
    return "\n".join([
        "# Historical Listings Validation",
        "",
        f"- status: `{payload.get('status')}`",
        f"- row_count: `{payload.get('row_count')}`",
        f"- symbol_count: `{payload.get('symbol_count')}`",
        f"- pit_quality: `{payload.get('pit_quality')}`",
        f"- survivorship_bias_risk: `{payload.get('survivorship_bias_risk')}`",
        f"- required_columns_missing: `{payload.get('required_columns_missing')}`",
        f"- invalid_dates: `{payload.get('invalid_dates')}`",
        "",
        "## Warnings",
        *(f"- {item}" for item in (payload.get("warnings") or ["none"])[:50]),
        "",
    ])


def write_outputs(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    result = Path(result_path)
    report = Path(report_path)
    if not result.is_absolute():
        result = PROJECT_ROOT / result
    if not report.is_absolute():
        report = PROJECT_ROOT / report
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_report(payload), encoding="utf-8")
    return payload


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate local historical listings CSV for PIT universe research.")
    parser.add_argument("--path", "--csv", dest="path", default="data/universe/historical_listings.csv")
    parser.add_argument("--json", nargs="?", const="true", default="false")
    parser.add_argument("--summary", nargs="?", const="true", default="false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = write_outputs(validate_historical_listings(args.path))
    if truthy(args.json) or truthy(args.summary):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(render_report(payload))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
