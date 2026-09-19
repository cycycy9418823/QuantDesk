from __future__ import annotations

import csv
import json
import logging
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.version import APP_VERSION, API_SCHEMA_VERSION, PACKAGE_VERSION

LOG = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_JSON = PROJECT_ROOT / "results" / "schema_anomaly_report_latest.json"
REPORT_MD = PROJECT_ROOT / "reports" / "schema_anomaly_report_latest.md"

STANDARD_COLUMNS = {
    "date",
    "trade_date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vol",
    "amount",
    "pct_chg",
    "change",
    "turnover",
}
CANONICAL_COLUMNS = {"date", "symbol", "open", "high", "low", "close", "volume", "amount", "pct_chg", "change", "turnover"}
REQUIRED_COLUMNS = {"date", "close"}
OPTIONAL_COLUMNS = {"pct_chg", "change", "turnover"}
CHINESE_ALIASES = {
    "日期": "date",
    "交易日期": "date",
    "股票代码": "symbol",
    "代码": "symbol",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_chg",
    "涨跌额": "change",
    "换手率": "turnover",
}
ALIASES = {
    **CHINESE_ALIASES,
    "trade_date": "date",
    "vol": "volume",
}


def _rel(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError as exc:
        LOG.warning("schema_normalizer_relative_path_failed path=%s root=%s error=%s", target, PROJECT_ROOT, exc)
        return str(target).replace("\\", "/")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError) as exc:
            LOG.warning("schema_normalizer_json_item_failed type=%s error=%s", type(value).__name__, exc)
    return value


def infer_symbol_from_filename(path: str | Path) -> str | None:
    stem = Path(path).stem
    match = re.search(r"(\d{6})", stem)
    return match.group(1) if match else None


def normalize_date_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.split(" ")[0].replace("/", "-")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError as exc:
        LOG.warning("schema_normalizer_date_parse_failed value=%r normalized=%r error=%s", value, text, exc)
        return None


def normalize_columns(columns: list[str]) -> dict[str, str]:
    return {str(col): ALIASES.get(str(col).strip(), str(col).strip()) for col in columns}


def read_csv_header(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return [str(item).strip() for item in next(reader, [])]


def normalize_daily_csv(path: str | Path, *, strict: bool = False) -> tuple[pd.DataFrame, dict[str, Any]]:
    csv_path = Path(path)
    warnings: list[str] = []
    errors: list[str] = []
    symbol_from_file = infer_symbol_from_filename(csv_path)
    frame = read_csv_frame(csv_path, encoding="utf-8-sig")
    csv_read_warnings = list(frame.attrs.get("warnings") or [])
    if any(item.startswith("csv_read_failed:") for item in csv_read_warnings):
        return pd.DataFrame(), {
            "path": _rel(csv_path),
            "ok": False,
            "quarantined": True,
            "warnings": csv_read_warnings,
            "errors": [f"read_failed:{'; '.join(csv_read_warnings)}"],
        }
    if frame.empty:
        return pd.DataFrame(), {"path": _rel(csv_path), "ok": False, "quarantined": True, "warnings": csv_read_warnings, "errors": ["empty_csv"]}
    warnings.extend(csv_read_warnings)
    original_columns = [str(col).strip() for col in frame.columns]
    has_chinese = bool(set(original_columns) & set(CHINESE_ALIASES))
    frame = frame.rename(columns=normalize_columns(original_columns))
    frame = frame.loc[:, ~frame.columns.duplicated(keep="first")]
    if "date" not in frame.columns or "close" not in frame.columns:
        errors.append("missing_required_date_or_close")
        return pd.DataFrame(), {
            "path": _rel(csv_path),
            "ok": False,
            "quarantined": True,
            "has_chinese_columns": has_chinese,
            "missing_required_columns": sorted(REQUIRED_COLUMNS - set(frame.columns)),
            "warnings": warnings,
            "errors": errors,
        }
    if "symbol" not in frame.columns:
        if symbol_from_file:
            frame["symbol"] = symbol_from_file
            warnings.append("symbol_inferred_from_filename")
        else:
            errors.append("symbol_missing_and_filename_inference_failed")
            return pd.DataFrame(), {"path": _rel(csv_path), "ok": False, "quarantined": True, "warnings": warnings, "errors": errors}
    else:
        extracted = frame["symbol"].astype(str).str.extract(r"(\d{6})")[0]
        if symbol_from_file and (extracted.isna().any() or extracted.dropna().str.len().ne(6).any()):
            frame["symbol"] = symbol_from_file
            warnings.append("symbol_restored_from_filename")
        else:
            frame["symbol"] = extracted.fillna(symbol_from_file or "")
    if not symbol_from_file and frame["symbol"].astype(str).str.fullmatch(r"\d{6}").sum() == 0:
        errors.append("symbol_invalid")
        return pd.DataFrame(), {"path": _rel(csv_path), "ok": False, "quarantined": True, "warnings": warnings, "errors": errors}
    for column in ("open", "high", "low"):
        if column not in frame.columns:
            frame[column] = frame["close"]
            warnings.append(f"missing_{column}_filled_with_close")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
        warnings.append("missing_volume_filled_zero")
    if "amount" not in frame.columns:
        frame["amount"] = 0.0
        warnings.append("missing_amount_filled_zero")
    for column in OPTIONAL_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
            warnings.append(f"missing_optional_{column}")
    frame["date"] = frame["date"].map(normalize_date_value)
    for column in ("open", "high", "low", "close", "volume", "amount", "pct_chg", "change", "turnover"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame["pct_chg"].isna().all():
        frame["pct_chg"] = frame.groupby("symbol")["close"].transform(
            lambda values: values.ffill().pct_change(fill_method=None)
        ) * 100.0
        warnings.append("pct_chg_computed_from_close")
    if frame["change"].isna().all():
        frame["change"] = frame.groupby("symbol")["close"].diff()
        warnings.append("change_computed_from_close")
    before = len(frame)
    frame = frame.dropna(subset=["date", "close"])
    if len(frame) < before:
        warnings.append("invalid_date_or_close_rows_dropped")
    frame = frame.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    out = frame[[col for col in ("date", "symbol", "open", "high", "low", "close", "volume", "amount", "pct_chg", "change", "turnover") if col in frame.columns]].reset_index(drop=True)
    meta = {
        "path": _rel(csv_path),
        "ok": True,
        "quarantined": False,
        "has_chinese_columns": has_chinese,
        "original_columns": original_columns,
        "normalized_columns": list(out.columns),
        "missing_optional_columns": [col for col in OPTIONAL_COLUMNS if f"missing_optional_{col}" in warnings],
        "inferred_symbol": "symbol_inferred_from_filename" in warnings or "symbol_restored_from_filename" in warnings,
        "row_count": int(len(out)),
        "first_date": str(out["date"].min()) if not out.empty else "",
        "last_date": str(out["date"].max()) if not out.empty else "",
        "warnings": warnings,
        "errors": errors,
    }
    if strict and errors:
        meta["ok"] = False
        meta["quarantined"] = True
    return out, _json_safe(meta)


def scan_daily_schema(data_root: str | Path = "data", *, max_invalid_files: int = 100, max_symbols: int = 0) -> dict[str, Any]:
    root = Path(data_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    roots = [
        root / "daily" / "forward_adjusted",
        root / "daily" / "back_adjusted",
        root / "daily" / "raw",
    ]
    legacy = root / "daily"
    if legacy.exists() and any(legacy.glob("*.csv")):
        roots.append(legacy)
    files: list[Path] = []
    for directory in roots:
        if directory.exists():
            files.extend(sorted(directory.glob("*.csv")))
    seen: set[Path] = set()
    unique_files = []
    for item in files:
        resolved = item.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_files.append(item)
    total_available_csv = len(unique_files)
    if int(max_symbols or 0) > 0:
        unique_files = unique_files[: int(max_symbols)]
    schema_counter: Counter[str] = Counter()
    invalid_files: list[dict[str, Any]] = []
    warnings: list[str] = []
    standard_schema_count = 0
    chinese_schema_count = 0
    missing_optional_fields_count = 0
    inferred_symbol_count = 0
    quarantined_count = 0
    for path in unique_files:
        try:
            header = read_csv_header(path)
        except Exception as exc:  # noqa: BLE001
            quarantined_count += 1
            invalid_files.append({"path": _rel(path), "errors": [f"read_failed:{exc}"]})
            continue
        normalized = [ALIASES.get(col, col) for col in header]
        schema_counter[",".join(normalized)] += 1
        columns = set(normalized)
        if REQUIRED_COLUMNS.issubset(columns) and not (set(header) & set(CHINESE_ALIASES)) and OPTIONAL_COLUMNS.issubset(columns):
            standard_schema_count += 1
        if set(header) & set(CHINESE_ALIASES):
            chinese_schema_count += 1
        if not OPTIONAL_COLUMNS.issubset(columns):
            missing_optional_fields_count += 1
        inferred_symbol = "symbol" not in columns and infer_symbol_from_filename(path) is not None
        if inferred_symbol:
            inferred_symbol_count += 1
        quarantined = not REQUIRED_COLUMNS.issubset(columns)
        if quarantined:
            quarantined_count += 1
            if len(invalid_files) < max_invalid_files:
                invalid_files.append({"path": _rel(path), "errors": ["missing_required_date_or_close"], "warnings": []})
        for optional in sorted(OPTIONAL_COLUMNS - columns):
            warnings.append(f"missing_optional_{optional}")
    payload = {
        "ok": True,
        "status": "warning" if quarantined_count or missing_optional_fields_count or chinese_schema_count else "passed",
        "task_type": "schema_anomaly_report",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "package_version": PACKAGE_VERSION,
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "research_only": True,
        "network": False,
        "data_root": _rel(root),
        "total_csv": len(unique_files),
        "total_available_csv": total_available_csv,
        "scan_limited": bool(int(max_symbols or 0) > 0 and total_available_csv > len(unique_files)),
        "max_symbols": int(max_symbols or 0),
        "standard_schema_count": standard_schema_count,
        "schema_variant_count": len(schema_counter),
        "missing_optional_fields_count": missing_optional_fields_count,
        "chinese_schema_count": chinese_schema_count,
        "inferred_symbol_count": inferred_symbol_count,
        "quarantined_count": quarantined_count,
        "invalid_files": invalid_files,
        "schema_distribution": [{"columns": key, "count": count} for key, count in schema_counter.most_common(20)],
        "warnings": list(dict.fromkeys((([f"schema_scan_limited:max_symbols={int(max_symbols or 0)}"] if int(max_symbols or 0) > 0 and total_available_csv > len(unique_files) else []) + warnings)))[:100],
        "errors": [],
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": "Load-time schema normalization only. Original CSV files are not overwritten.",
    }
    payload["warning_count"] = len(payload["warnings"])
    payload["warnings_top"] = payload["warnings"][:10]
    return _json_safe(payload)


def render_schema_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Schema Anomaly Report",
        "",
        str(payload.get("disclaimer") or ""),
        "",
        f"- total_csv: `{payload.get('total_csv')}`",
        f"- standard_schema_count: `{payload.get('standard_schema_count')}`",
        f"- chinese_schema_count: `{payload.get('chinese_schema_count')}`",
        f"- missing_optional_fields_count: `{payload.get('missing_optional_fields_count')}`",
        f"- inferred_symbol_count: `{payload.get('inferred_symbol_count')}`",
        f"- quarantined_count: `{payload.get('quarantined_count')}`",
        "",
        "## Invalid Files",
        "",
    ]
    invalid = payload.get("invalid_files") or []
    if invalid:
        for item in invalid[:50]:
            lines.append(f"- `{item.get('path')}`: {', '.join(item.get('errors') or item.get('warnings') or [])}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def write_schema_anomaly_report(payload: dict[str, Any], *, result_path: str | Path = RESULT_JSON, report_path: str | Path = REPORT_MD) -> dict[str, Any]:
    result = Path(result_path)
    report = Path(report_path)
    result.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": _rel(result), "markdown": _rel(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_schema_report(payload), encoding="utf-8")
    return payload
