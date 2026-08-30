"""Feature quality audit for local QuantDesk daily features.

This module is diagnostic only.  It reads local feature rows, computes missing
and abnormal-value statistics, and writes audit artifacts.  It never connects
to the network and never modifies feature data or strategy logic.
"""

from __future__ import annotations

# Compatibility markers: audit_feature_quality compute_feature_missing_rates feature_quality_audit_latest.json.
import csv
import math
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, now_text, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_JSON_PATH = RESULTS_DIR / "feature_quality_audit_latest.json"
DEFAULT_REPORT_PATH = REPORTS_DIR / "feature_quality_audit_report_latest.md"

# Raw ``data/daily`` OHLCV rows only carry price/volume basics. They must NOT
# be judged against derived indicator columns (ma/volatility/amount_ma20),
# otherwise every raw daily file is falsely reported as "high missing rate".
RAW_DAILY_FIELDS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_chg",
]

# Derived feature-table rows (``data/features/daily_features_latest.csv``) carry
# rolling indicators. Column names here mirror the real feature-engine schema
# (``volatility_20_pct`` / ``amount_ma20`` / ``turnover``), not legacy aliases.
DERIVED_FEATURE_FIELDS = [
    "symbol",
    "date",
    "close",
    "volume",
    "amount",
    "pct_chg",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "volatility_20_pct",
    "amount_ma20",
    "turnover",
]

# Presence of any of these columns marks a row as coming from the derived
# feature table rather than raw daily OHLCV.
DERIVED_MARKER_FIELDS = frozenset(
    {
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "ma120",
        "volatility_20_pct",
        "amount_ma20",
        "volume_ma20",
        "turnover_ma20",
        "close_vs_ma20_pct",
    }
)

TABLE_KIND_RAW = "raw_daily_ohlcv"
TABLE_KIND_DERIVED = "derived_feature_table"

# Kept for backward compatibility with external callers that imported the old
# name; defaults to the raw-daily field set.
DEFAULT_FEATURE_FIELDS = RAW_DAILY_FIELDS
DISCLAIMER = "仅用于本地数据质量诊断，不构成投资建议。"
EMPTY_DATA_REASON = "未找到本地日线/特征数据，无法完成特征质量审计；请先运行 daily-retain 或导入本地 data/daily。"
EMPTY_DATA_NEXT_ACTION = (
    "python scripts/qd.py daily-retain --symbols 600744,002115 --network false；"
    "或导入本地 data/daily/{symbol}.csv 后重试。"
)


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return target


def _rel(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target)


def _split_symbols(symbols: Any) -> list[str]:
    if isinstance(symbols, str):
        raw = [item.strip() for item in symbols.split(",") if item.strip()]
    elif isinstance(symbols, (list, tuple, set)):
        raw = list(symbols)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none", "null", "na", "n/a"}


def _to_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _is_inf(value: Any) -> bool:
    if isinstance(value, float) and math.isinf(value):
        return True
    text = str(value).strip().lower()
    return text in {"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


def _load_rows_from_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_feature_rows(
    *,
    rows: Any = None,
    symbols: Any = None,
    target_date: str = "",
    input_dir: str | Path = "data/daily",
    max_files: int = 50,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str], dict[str, Any]]:
    warnings: list[str] = []
    errors: list[str] = []
    sources: list[str] = []
    scan_meta: dict[str, Any] = {
        "review_scope": "provided_rows",
        "checked_file_count": 0,
        "unchecked_file_count": 0,
        "max_files": int(max_files),
        "full_scan_required_for_formal_conclusion": False,
    }
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, dict)], sources, warnings, errors, scan_meta
    if isinstance(rows, dict):
        maybe_rows = rows.get("rows") or rows.get("features") or rows.get("data")
        if isinstance(maybe_rows, list):
            return [dict(row) for row in maybe_rows if isinstance(row, dict)], sources, warnings, errors, scan_meta

    selected = _split_symbols(symbols)
    base = _project_path(input_dir)
    all_candidates: list[Path] = []
    # NOTE: do not silently prepend the derived feature table when auditing the
    # raw ``data/daily`` directory. Mixing a handful of derived rows (which carry
    # ma/volatility columns) into the far larger pool of raw OHLCV rows made the
    # derived families look almost entirely missing and produced false
    # ``high_missing_rate`` warnings. Callers that want the derived table should
    # pass ``input_dir=data/features`` (or the file path) explicitly.
    if selected:
        all_candidates.extend(base / f"{symbol}.csv" for symbol in selected)
    elif base.exists() and base.is_file():
        all_candidates.append(base)
    elif base.exists() and base.is_dir():
        all_candidates.extend(sorted(base.glob("*.csv")))

    limit = max(0, int(max_files))
    candidates = all_candidates if limit == 0 else all_candidates[:limit]
    unchecked_file_count = max(0, len(all_candidates) - len(candidates))
    scan_meta = {
        "review_scope": "sampled" if unchecked_file_count else "full",
        "checked_file_count": len(candidates),
        "unchecked_file_count": unchecked_file_count,
        "max_files": limit,
        "full_scan_required_for_formal_conclusion": bool(unchecked_file_count),
    }

    loaded: list[dict[str, Any]] = []
    target = str(target_date or "")
    for path in candidates:
        try:
            file_rows = _load_rows_from_csv(path)
        except Exception as exc:
            errors.append(f"failed_to_read:{_rel(path)}:{short_error_text(exc)}")
            continue
        if not file_rows:
            continue
        sources.append(_rel(path))
        for row in file_rows:
            symbol = normalize_symbol(row.get("symbol") or row.get("code"))
            if selected and symbol not in selected:
                continue
            if target:
                row_date = str(row.get("date") or row.get("trade_date") or row.get("latest_date") or "")
                if row_date and row_date > target:
                    continue
            if symbol:
                row["symbol"] = symbol
            loaded.append(dict(row))
    if not loaded:
        warnings.append("feature_rows_missing")
        if not base.exists():
            warnings.append(f"input_dir_missing:{_rel(base)}")
    return loaded, sources, warnings, errors, scan_meta


def classify_table_kind(rows: list[dict[str, Any]]) -> str:
    """Classify rows as raw daily OHLCV or a derived feature table.

    A table is treated as *derived* when any row exposes a derived indicator
    column (ma/volatility/amount_ma20 ...). Raw ``data/daily`` files only carry
    OHLCV/amount/volume/pct_chg and must not be judged against derived columns,
    otherwise every raw daily file is falsely flagged as high-missing-rate.
    """
    for row in rows:
        if any(key in DERIVED_MARKER_FIELDS for key in row):
            return TABLE_KIND_DERIVED
    return TABLE_KIND_RAW


def _feature_names(
    rows: list[dict[str, Any]],
    requested: list[str] | None = None,
    *,
    table_kind: str | None = None,
) -> list[str]:
    if requested is not None:
        base_fields = requested
    elif table_kind == TABLE_KIND_DERIVED:
        base_fields = DERIVED_FEATURE_FIELDS
    else:
        base_fields = RAW_DAILY_FIELDS
    names: list[str] = []
    for name in base_fields:
        if name not in names:
            names.append(name)
    # Only surface score columns as extra audited features; do NOT auto-add
    # derived indicator columns onto a raw-daily audit.
    for row in rows:
        for key in row:
            if key not in names and (key.startswith("strategy_score") or key.endswith("_score")):
                names.append(key)
    return names


def compute_feature_missing_rates(rows: list[dict[str, Any]], features: list[str]) -> dict[str, float]:
    total = len(rows)
    return {
        feature: (sum(1 for row in rows if _is_missing(row.get(feature))) / total if total else 1.0)
        for feature in features
    }


def compute_feature_zero_rates(rows: list[dict[str, Any]], features: list[str]) -> dict[str, float]:
    total = len(rows)
    result: dict[str, float] = {}
    for feature in features:
        zero_count = 0
        for row in rows:
            value = _to_float(row.get(feature))
            if value == 0:
                zero_count += 1
        result[feature] = zero_count / total if total else 0.0
    return result


def compute_feature_inf_nan_counts(rows: list[dict[str, Any]], features: list[str]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for feature in features:
        nan_count = 0
        inf_count = 0
        for row in rows:
            value = row.get(feature)
            if _is_missing(value):
                nan_count += 1
            elif _is_inf(value):
                inf_count += 1
            else:
                numeric = _to_float(value)
                if numeric is not None and math.isnan(numeric):
                    nan_count += 1
                elif numeric is not None and math.isinf(numeric):
                    inf_count += 1
        result[feature] = {"nan_count": nan_count, "inf_count": inf_count}
    return result


def compute_feature_coverage_by_symbol(rows: list[dict[str, Any]], features: list[str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code")) or "unknown"
        grouped.setdefault(symbol, []).append(row)
    return {
        symbol: {
            "row_count": len(items),
            "valid_features": sum(
                1 for feature in features if any(not _is_missing(row.get(feature)) for row in items)
            ),
        }
        for symbol, items in sorted(grouped.items())
    }


def compute_feature_coverage_by_date(rows: list[dict[str, Any]], features: list[str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        date = str(row.get("date") or row.get("trade_date") or row.get("latest_date") or "unknown")
        grouped.setdefault(date, []).append(row)
    return {
        date: {
            "row_count": len(items),
            "valid_features": sum(
                1 for feature in features if any(not _is_missing(row.get(feature)) for row in items)
            ),
        }
        for date, items in sorted(grouped.items())
    }


def detect_feature_quality_warnings(
    feature_stats: dict[str, dict[str, Any]],
    *,
    missing_threshold: float = 0.3,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for feature, stats in feature_stats.items():
        missing_rate = float(stats.get("missing_rate") or 0)
        inf_count = int(stats.get("inf_count") or 0)
        if missing_rate >= missing_threshold:
            warnings.append(
                {
                    "type": "high_missing_rate",
                    "feature": feature,
                    "missing_rate": round(missing_rate, 6),
                    "message": "feature missing rate is high",
                }
            )
        if inf_count:
            warnings.append(
                {
                    "type": "inf_value",
                    "feature": feature,
                    "inf_count": inf_count,
                    "message": "feature contains infinite values",
                }
            )
    return warnings


def audit_feature_quality(
    rows: Any = None,
    *,
    symbols: Any = None,
    target_date: str = "",
    input_dir: str | Path = "data/daily",
    network: bool = False,
    missing_threshold: float = 0.3,
    max_files: int = 50,
    write: bool = False,
    output_path: str | Path = DEFAULT_JSON_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    feature_rows, sources, load_warnings, load_errors, scan_meta = _load_feature_rows(
        rows=rows,
        symbols=symbols,
        target_date=target_date,
        input_dir=input_dir,
        max_files=max_files,
    )
    row_count = len(feature_rows)
    table_kind = classify_table_kind(feature_rows) if row_count else TABLE_KIND_RAW
    features = _feature_names(feature_rows, table_kind=table_kind) if row_count else []
    symbols_seen = sorted(
        {
            normalize_symbol(row.get("symbol") or row.get("code"))
            for row in feature_rows
            if normalize_symbol(row.get("symbol") or row.get("code"))
        }
    )
    dates = sorted(
        {
            str(row.get("date") or row.get("trade_date") or row.get("latest_date") or "")
            for row in feature_rows
            if str(row.get("date") or row.get("trade_date") or row.get("latest_date") or "")
        }
    )
    missing_rates = compute_feature_missing_rates(feature_rows, features)
    zero_rates = compute_feature_zero_rates(feature_rows, features)
    abnormal_counts = compute_feature_inf_nan_counts(feature_rows, features)
    feature_stats: dict[str, dict[str, Any]] = {}
    for feature in features:
        missing_rate = missing_rates.get(feature, 1.0)
        nan_count = abnormal_counts.get(feature, {}).get("nan_count", 0)
        inf_count = abnormal_counts.get(feature, {}).get("inf_count", 0)
        valid_count = max(0, row_count - nan_count - inf_count)
        status = "warning" if missing_rate >= missing_threshold or inf_count else "passed"
        feature_stats[feature] = {
            "missing_rate": round(missing_rate, 6),
            "zero_rate": round(zero_rates.get(feature, 0.0), 6),
            "nan_count": nan_count,
            "inf_count": inf_count,
            "valid_count": valid_count,
            "status": status,
        }
    if row_count == 0:
        warning_items = [
            {
                "type": "insufficient_data",
                "message": EMPTY_DATA_REASON,
                "suggested_next_action": EMPTY_DATA_NEXT_ACTION,
            }
        ]
    else:
        warning_items = detect_feature_quality_warnings(feature_stats, missing_threshold=missing_threshold)
    warnings = [*load_warnings, *[item["message"] + f":{item.get('feature')}" for item in warning_items if item.get("feature")]]
    if row_count == 0:
        warnings.append(EMPTY_DATA_REASON)
    status = "failed" if load_errors else ("warning" if row_count == 0 or warning_items or load_warnings else "passed")
    payload = {
        "ok": status != "failed",
        "ok_for_cli": status in {"passed", "warning"},
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "task_type": "feature_quality_audit",
        "status": status,
        "table_kind": table_kind,
        "network": bool(network),
        "insufficient_data": row_count == 0,
        "failed_reason": "; ".join(load_errors),
        "skipped_reason": EMPTY_DATA_REASON if row_count == 0 else "",
        "suggested_next_action": EMPTY_DATA_NEXT_ACTION if row_count == 0 else "",
        "feature_count": len(features),
        "row_count": row_count,
        "symbol_count": len(symbols_seen),
        "date_range": [dates[0], dates[-1]] if dates else ["", ""],
        "symbols": symbols_seen,
        "sources": sources,
        **scan_meta,
        "features": feature_stats,
        "coverage_by_symbol": compute_feature_coverage_by_symbol(feature_rows, features),
        "coverage_by_date": compute_feature_coverage_by_date(feature_rows, features),
        "warning_items": warning_items,
        "warnings": [short_error_text(item) for item in warnings if item],
        "errors": [short_error_text(item) for item in load_errors if item],
        "generated_at": now_text(),
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_json(output_path, payload)
        write_feature_quality_report(payload, report_path)
    return payload


def summarize_feature_quality(payload: dict[str, Any]) -> dict[str, Any]:
    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    if payload.get("insufficient_data"):
        high_missing: list[str] = []
    else:
        high_missing = [
            name
            for name, stats in features.items()
            if isinstance(stats, dict) and float(stats.get("missing_rate") or 0) >= 0.3
        ]
    return {
        "status": payload.get("status") or "missing",
        "row_count": int(payload.get("row_count") or 0),
        "feature_count": int(payload.get("feature_count") or 0),
        "insufficient_data": bool(payload.get("insufficient_data")),
        "skipped_reason": payload.get("skipped_reason") or "",
        "suggested_next_action": payload.get("suggested_next_action") or "",
        "high_missing_features": high_missing,
        "high_missing_feature_count": len(high_missing),
        "warning_count": len(payload.get("warnings") or []),
        "generated_at": payload.get("generated_at"),
        "disclaimer": DISCLAIMER,
    }


def build_feature_quality_report(payload: dict[str, Any]) -> str:
    summary = summarize_feature_quality(payload)
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    return "\n".join(
        [
            "# QuantDesk Feature Quality Audit",
            "",
            DISCLAIMER,
            "",
            f"- status: `{payload.get('status') or '-'}`",
            f"- row_count: `{payload.get('row_count') or 0}`",
            f"- feature_count: `{payload.get('feature_count') or 0}`",
            f"- symbol_count: `{payload.get('symbol_count') or 0}`",
            f"- date_range: `{', '.join(payload.get('date_range') or [])}`",
            f"- high_missing_feature_count: `{summary.get('high_missing_feature_count')}`",
            f"- insufficient_data: `{payload.get('insufficient_data')}`",
            f"- skipped_reason: {payload.get('skipped_reason') or '-'}",
            f"- suggested_next_action: {payload.get('suggested_next_action') or '-'}",
            "",
            "## High Missing Features",
            *(f"- {item}" for item in (summary.get("high_missing_features") or [])),
            "",
            "## Warnings",
            *(f"- {item}" for item in warnings[:50]),
            "",
            "本报告只检查本地特征质量，不联网、不修改数据、不构成投资建议。",
        ]
    )


def write_feature_quality_report(payload: dict[str, Any], output_path: str | Path = DEFAULT_REPORT_PATH) -> Path:
    target = _project_path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_feature_quality_report(payload), encoding="utf-8")
    return target
