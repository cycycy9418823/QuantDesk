from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.research.strategy_pipeline_repair import build_pit_mode_status
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "strategy_viability_precheck_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "strategy_viability_precheck_latest.md"
DISCLAIMER = "Research-only data precheck. No broker, no real orders, no automatic trading."

REQUIRED_DAILY_COLUMNS = {"date", "symbol", "open", "high", "low", "close", "volume", "amount"}
OPTIONAL_DAILY_COLUMNS = {"pct_chg", "change", "turnover"}
DATE_ALIASES = {"date", "trade_date", "日期"}
CHINESE_COLUMNS = {"日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "涨跌幅", "涨跌额", "换手率"}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_json(v) for v in value]
    return value


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return [str(item).strip() for item in next(reader, [])]


def _first_date(path: Path, header: list[str]) -> str | None:
    date_column = next((col for col in header if col in DATE_ALIASES), None)
    if not date_column:
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                raw = str(row.get(date_column) or "").strip()
                if raw:
                    parsed = _normalize_date(raw)
                    if parsed:
                        return parsed
    except Exception:
        return None
    return None


def _normalize_date(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.split(" ")[0].replace("/", "-")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except Exception:
        return None


def _start_date_bucket(date_value: str | None) -> str:
    if not date_value:
        return "unknown"
    year = int(date_value[:4])
    if year < 2010:
        return "before_2010"
    if year < 2015:
        return "2010_2014"
    if year < 2020:
        return "2015_2019"
    return "2020_plus"


def _scan_dir(label: str, path: Path, *, max_file_details: int = 50, max_symbols: int = 0) -> dict[str, Any]:
    all_files = sorted(path.glob("*.csv")) if path.exists() else []
    files = all_files[: int(max_symbols)] if int(max_symbols or 0) > 0 else all_files
    schema_counter: Counter[str] = Counter()
    start_counter: Counter[str] = Counter()
    warnings: list[str] = []
    details: list[dict[str, Any]] = []
    nonstandard = 0
    chinese = 0
    missing_optional = 0
    abnormal_symbol = 0
    for csv_path in files:
        try:
            header = _read_header(csv_path)
        except Exception as exc:  # noqa: BLE001
            nonstandard += 1
            warnings.append(f"{label}:read_failed:{csv_path.name}:{exc}")
            continue
        columns = set(header)
        schema_key = ",".join(header)
        schema_counter[schema_key] += 1
        has_chinese = bool(columns & CHINESE_COLUMNS)
        if has_chinese:
            chinese += 1
        has_required = REQUIRED_DAILY_COLUMNS.issubset(columns) or (("trade_date" in columns or "日期" in columns) and "close" in columns)
        missing_opts = sorted(OPTIONAL_DAILY_COLUMNS - columns)
        if missing_opts:
            missing_optional += 1
        symbol_from_name = csv_path.stem.replace("_qfq", "")
        symbol_ok = bool(re.fullmatch(r"\d{6}", symbol_from_name))
        if not symbol_ok:
            abnormal_symbol += 1
        start_date = _first_date(csv_path, header)
        start_counter[_start_date_bucket(start_date)] += 1
        if not has_required or has_chinese or missing_opts or not symbol_ok:
            nonstandard += int(not has_required)
            if len(details) < max_file_details:
                details.append({
                    "path": _rel(csv_path),
                    "has_required_schema": has_required,
                    "has_chinese_columns": has_chinese,
                    "missing_optional_columns": missing_opts,
                    "symbol_from_filename": symbol_from_name,
                    "symbol_filename_ok": symbol_ok,
                    "start_date": start_date,
                })
    return {
        "label": label,
        "path": _rel(path),
        "exists": path.exists(),
        "csv_count": len(files),
        "available_csv_count": len(all_files),
        "scan_limited": bool(int(max_symbols or 0) > 0 and len(all_files) > len(files)),
        "max_symbols": int(max_symbols or 0),
        "schema_distribution": [{"columns": key, "count": count} for key, count in schema_counter.most_common(20)],
        "schema_variant_count": len(schema_counter),
        "nonstandard_schema_file_count": nonstandard,
        "chinese_column_file_count": chinese,
        "missing_pct_chg_change_turnover_file_count": missing_optional,
        "abnormal_symbol_file_count": abnormal_symbol,
        "start_date_distribution": dict(start_counter),
        "sample_issues": details,
        "warnings": warnings,
    }


def _detect_daily_roots(data_root: Path) -> list[tuple[str, Path]]:
    roots = [
        ("forward_adjusted", data_root / "daily" / "forward_adjusted"),
        ("back_adjusted", data_root / "daily" / "back_adjusted"),
        ("raw", data_root / "daily" / "raw"),
    ]
    legacy = data_root / "daily"
    if legacy.exists() and any(legacy.glob("*.csv")):
        roots.append(("forward_legacy_data_daily", legacy))
    return roots


def _benchmark_status(data_root: Path) -> dict[str, Any]:
    candidates = [
        data_root / "index" / "000300.SH.csv",
        data_root / "index" / "000905.SH.csv",
        data_root / "index" / "399300.SZ.csv",
        data_root / "market" / "index_daily.csv",
    ]
    found = [item for item in candidates if item.exists()]
    return {
        "available": bool(found),
        "candidates": [_rel(item) for item in candidates],
        "found": [_rel(item) for item in found],
    }


def build_strategy_viability_precheck(config: dict | None = None) -> dict[str, Any]:
    cfg = config or {}
    data_root = Path(cfg.get("data_root") or PROJECT_ROOT / "data")
    if not data_root.is_absolute():
        data_root = PROJECT_ROOT / data_root
    warnings: list[str] = []
    max_symbols = int(cfg.get("max_symbols") or 0)
    scans = [_scan_dir(label, path, max_symbols=max_symbols) for label, path in _detect_daily_roots(data_root)]
    if not scans:
        warnings.append("no_daily_csv_roots_found")
    if any(scan.get("scan_limited") for scan in scans):
        warnings.append(f"precheck_scan_limited:max_symbols={max_symbols}")
    if any(scan["label"] == "forward_legacy_data_daily" for scan in scans):
        warnings.append("legacy_data_daily_used_as_forward_adjusted_compatibility_path")
    benchmark = _benchmark_status(data_root)
    if not benchmark["available"]:
        warnings.append("benchmark_index_csv_missing")
    pit_mode = str(cfg.get("pit_mode") or "price_coverage")
    listings_path = data_root / "universe" / "historical_listings.csv"
    pit_status = build_pit_mode_status(pit_mode, data_root=data_root, adjustment=str(cfg.get("adjustment") or "forward"), historical_listings_csv=listings_path, max_symbols=max_symbols)
    historical_listings_exists = bool((pit_status.get("historical_listings") or {}).get("exists"))
    survivorship_bias_risk = str(pit_status.get("survivorship_bias_risk") or ("unknown" if historical_listings_exists else "high"))
    warnings.extend(str(item) for item in pit_status.get("warnings", []))
    if pit_status.get("errors"):
        warnings.extend(str(item) for item in pit_status.get("errors", []))
    partial_dir = data_root / "intraday" / "partial_daily"
    partial_daily_isolated = True
    total_csv_count = sum(int(scan["csv_count"]) for scan in scans)
    total_available_csv_count = sum(int(scan.get("available_csv_count") or scan["csv_count"]) for scan in scans)
    payload = {
        "ok": True,
        "status": "warning" if warnings else "passed",
        "task_type": "strategy_viability_precheck",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "research_only": True,
        "network": False,
        "data_root": _rel(data_root),
        "total_csv_count": total_csv_count,
        "total_available_csv_count": total_available_csv_count,
        "precheck_scan_limited": any(bool(scan.get("scan_limited")) for scan in scans),
        "daily_roots": scans,
        "schema_variant_count": sum(int(scan["schema_variant_count"]) for scan in scans),
        "nonstandard_schema_file_count": sum(int(scan["nonstandard_schema_file_count"]) for scan in scans),
        "chinese_column_file_count": sum(int(scan["chinese_column_file_count"]) for scan in scans),
        "missing_pct_chg_change_turnover_file_count": sum(int(scan["missing_pct_chg_change_turnover_file_count"]) for scan in scans),
        "abnormal_symbol_file_count": sum(int(scan["abnormal_symbol_file_count"]) for scan in scans),
        "benchmark": benchmark,
        "pit_mode": pit_mode,
        "pit_status": pit_status,
        "historical_listings": {"path": _rel(listings_path), "exists": historical_listings_exists},
        "st_delist_coverage": "unknown" if not historical_listings_exists else "partial_or_available",
        "survivorship_bias_risk": survivorship_bias_risk,
        "partial_daily": {"path": _rel(partial_dir), "exists": partial_dir.exists(), "isolated": partial_daily_isolated},
        "warnings": warnings + [item for scan in scans for item in scan.get("warnings", [])],
        "errors": [],
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    payload["warning_count"] = len(payload["warnings"])
    payload["warnings_top"] = payload["warnings"][:10]
    return _safe_json(payload)


def render_precheck_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy Viability Data Precheck",
        "",
        DISCLAIMER,
        "",
        f"- status: `{payload.get('status')}`",
        f"- total_csv_count: `{payload.get('total_csv_count')}`",
        f"- survivorship_bias_risk: `{payload.get('survivorship_bias_risk')}`",
        f"- benchmark_available: `{payload.get('benchmark', {}).get('available')}`",
        f"- historical_listings_exists: `{payload.get('historical_listings', {}).get('exists')}`",
        f"- partial_daily_isolated: `{payload.get('partial_daily', {}).get('isolated')}`",
        "",
        "## Schema Summary",
        "",
        f"- schema_variant_count: `{payload.get('schema_variant_count')}`",
        f"- nonstandard_schema_file_count: `{payload.get('nonstandard_schema_file_count')}`",
        f"- chinese_column_file_count: `{payload.get('chinese_column_file_count')}`",
        f"- missing_pct_chg_change_turnover_file_count: `{payload.get('missing_pct_chg_change_turnover_file_count')}`",
        f"- abnormal_symbol_file_count: `{payload.get('abnormal_symbol_file_count')}`",
        "",
        "## Warnings",
        "",
    ]
    warnings = payload.get("warnings") or []
    lines.extend([f"- {item}" for item in warnings] if warnings else ["- none"])
    return "\n".join(lines) + "\n"


def write_precheck_outputs(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    payload["warning_count"] = len(payload.get("warnings") or [])
    payload["warnings_top"] = list(payload.get("warnings") or [])[:10]
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_precheck_markdown(payload), encoding="utf-8")
    return payload
