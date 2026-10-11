from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import DAILY_HASH_FIELDS, compute_row_hash
from .repositories import DailyBarRepository, normalize_date, normalize_symbol, to_float
from .sqlite_store import QuantDeskDB

FIELD_ALIASES = {
    "date": ["date", "trade_date", "日期"],
    "open": ["open", "开盘"],
    "high": ["high", "最高"],
    "low": ["low", "最低"],
    "close": ["close", "收盘"],
    "volume": ["volume", "vol", "成交量"],
    "amount": ["amount", "成交额"],
    "pct_chg": ["pct_chg", "pct_change", "涨跌幅"],
    "change": ["change", "涨跌额"],
    "turnover": ["turnover", "换手率"],
}


def _pick(row: dict, key: str) -> Any:
    for name in FIELD_ALIASES.get(key, [key]):
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def scan_csv_files(data_root: str | Path = "data") -> dict:
    root = Path(data_root)
    paths = list(root.rglob("*.csv")) if root.exists() else []
    def count_under(part: str) -> int:
        base = root / part
        return sum(1 for p in paths if base in p.parents)
    counts = {
        "total_csv_count": len(paths),
        "daily_forward_count": count_under("daily/forward_adjusted"),
        "daily_back_count": count_under("daily/back_adjusted"),
        "daily_raw_count": count_under("daily/raw"),
        "partial_daily_count": count_under("intraday/partial_daily"),
        "intraday_csv_count": count_under("intraday"),
        "index_csv_count": count_under("index"),
        "factor_csv_count": count_under("factors"),
        "universe_csv_count": count_under("universe"),
    }
    classified = sum(v for k, v in counts.items() if k != "total_csv_count")
    counts["other_csv_count"] = max(0, len(paths) - classified)
    return counts


def load_daily_csv_rows(path: Path, default_symbol: str | None = None, source: str = "csv") -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    if not path.exists():
        return [], [f"csv_missing:{path}"]
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                date = normalize_date(_pick(raw, "date"))
                close = to_float(_pick(raw, "close"))
                if not date or close is None:
                    warnings.append(f"row_skipped_missing_date_or_close:{path.name}")
                    continue
                symbol = normalize_symbol(raw.get("symbol") or default_symbol or path.stem.split(".")[0])
                row = {
                    "symbol": symbol,
                    "date": date,
                    "open": to_float(_pick(raw, "open")),
                    "high": to_float(_pick(raw, "high")),
                    "low": to_float(_pick(raw, "low")),
                    "close": close,
                    "volume": to_float(_pick(raw, "volume")),
                    "amount": to_float(_pick(raw, "amount")),
                    "pct_chg": to_float(_pick(raw, "pct_chg")),
                    "change": to_float(_pick(raw, "change")),
                    "turnover": to_float(_pick(raw, "turnover")),
                    "source": source,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                row["row_hash"] = compute_row_hash(row, DAILY_HASH_FIELDS)
                rows.append(row)
    except Exception as exc:
        return [], [f"csv_read_failed:{path}:{exc}"]
    if not rows:
        warnings.append(f"file_skipped_no_valid_rows:{path.name}")
    return rows, warnings


def _daily_files_for_adjustment(root: Path, adjustment: str) -> list[Path]:
    mapping = {
        "forward": root / "daily" / "forward_adjusted",
        "back": root / "daily" / "back_adjusted",
        "raw": root / "daily" / "raw",
    }
    base = mapping[adjustment]
    return sorted(base.glob("*.csv")) if base.exists() else []


def _comparison_row(row: dict, *, canonical_symbol: bool = True) -> dict:
    """Hash current values, never the persisted row_hash metadata."""
    return {
        "symbol": normalize_symbol(row.get("symbol")) if canonical_symbol else str(row.get("symbol") or ""),
        "date": normalize_date(row.get("date")),
        **{field: to_float(row.get(field)) for field in DAILY_HASH_FIELDS if field not in {"symbol", "date"}},
    }


def _compare_selected_csv_rows(
    repo: DailyBarRepository,
    selected: list[tuple[str, Path, tuple[str, ...]]],
    input_warnings: dict[str, list[str]],
) -> dict:
    """Compare one adjustment/symbol group at a time, not all-A history in RAM."""
    groups: dict[tuple[str, str], list[Path]] = {}
    for adjustment, path, symbols in selected:
        for symbol in symbols:
            groups.setdefault((adjustment, symbol), []).append(path)
    errors: list[dict] = []
    mismatches: list[dict] = []
    sample_missing: list[tuple[str, str, str]] = []
    sample_extra: list[tuple[str, str, str]] = []
    csv_count = db_count = common_count = missing_count = extra_count = 0
    matches = close_diff = volume_diff = stale_hashes = duplicate_count = 0
    for (adjustment, symbol), paths in sorted(groups.items()):
        csv_rows: dict[str, dict] = {}
        for path in paths:
            rows, warnings = load_daily_csv_rows(path)
            if warnings:
                input_warnings[str(path)] = warnings
            for row in rows:
                if normalize_symbol(row.get("symbol")) != symbol:
                    continue
                normalized = _comparison_row(row)
                date = normalized["date"]
                duplicate_count += int(date in csv_rows)
                csv_rows[date] = normalized
        csv_count += len(csv_rows)
        try:
            actual = repo.query_daily_bars(symbol, adjustment=adjustment)
        except Exception as exc:
            errors.append({"adjustment": adjustment, "symbol": symbol, "error": str(exc)})
            continue
        db_rows: dict[str, dict] = {}
        for row in actual:
            normalized = _comparison_row(row)
            db_rows[normalized["date"]] = normalized
            stale_hashes += int(row.get("row_hash") != compute_row_hash(_comparison_row(row, canonical_symbol=False), DAILY_HASH_FIELDS))
        common = sorted(csv_rows.keys() & db_rows.keys())
        missing = sorted(csv_rows.keys() - db_rows.keys())
        extra = sorted(db_rows.keys() - csv_rows.keys())
        db_count += len(db_rows)
        common_count += len(common)
        missing_count += len(missing)
        extra_count += len(extra)
        sample_missing.extend((adjustment, symbol, day) for day in missing[:20 - len(sample_missing)])
        sample_extra.extend((adjustment, symbol, day) for day in extra[:20 - len(sample_extra)])
        for day in common:
            expected, observed = csv_rows[day], db_rows[day]
            same = compute_row_hash(expected, DAILY_HASH_FIELDS) == compute_row_hash(observed, DAILY_HASH_FIELDS)
            matches += int(same)
            close_diff += int(expected["close"] != observed["close"])
            volume_diff += int(expected["volume"] != observed["volume"])
            if not same and len(mismatches) < 20:
                mismatches.append({"adjustment": adjustment, "symbol": symbol, "date": day, "csv": expected, "database": observed})
    status = "matched"
    if errors:
        status = "comparison_error"
    elif not csv_count:
        status = "no_evidence"
    elif missing_count or extra_count or matches != common_count or close_diff or volume_diff or stale_hashes or duplicate_count:
        status = "mismatch"
    elif input_warnings:
        status = "partial_input"
    evaluated = bool(csv_count) and not errors
    return {
        "status": status,
        "scope": "selected_csv_files",
        "selected_files": [{"adjustment": adj, "path": str(path)} for adj, path, _symbols in selected],
        "selected_file_count": len(selected),
        "compared_file_count": sum(bool(symbols) for _adj, _path, symbols in selected),
        "input_row_policy": "valid_normalized_rows; input warnings prevent matched status",
        "input_warnings": input_warnings,
        "csv_row_count": csv_count, "database_row_count": db_count,
        "common_row_count": common_count, "hash_match_count": matches,
        "hash_mismatch_count": common_count - matches if evaluated else None,
        "hash_match_ratio": matches / common_count if common_count and evaluated else None,
        "hash_match_ratio_denominator": "common_row_count; not overall equality",
        "row_count_diff": db_count - csv_count if evaluated else None,
        "date_set_diff": missing_count + extra_count if evaluated else None,
        "missing_in_db_count": missing_count if evaluated else None,
        "extra_in_db_count": extra_count if evaluated else None,
        "close_diff_count": close_diff if evaluated else None,
        "volume_diff_count": volume_diff if evaluated else None,
        "stored_hash_mismatch_count": stale_hashes if evaluated else None,
        "duplicate_csv_key_count": duplicate_count,
        "sample_mismatches": mismatches,
        "sample_missing_keys": sample_missing, "sample_extra_keys": sample_extra,
        "comparison_errors": errors,
    }


def migrate_csv_to_sqlite(config: dict | None = None) -> dict:
    cfg = config or {}
    data_root = Path(cfg.get("data_root") or "data")
    db = QuantDeskDB(cfg.get("db_path") or "data/quantdesk.db")
    db.initialize()
    counts = scan_csv_files(data_root)
    dry_run = bool(cfg.get("dry_run", False))
    compare_only = cfg.get("action") == "compare"
    compare = bool(cfg.get("compare", False)) or compare_only
    sample = int(cfg.get("sample") or 0)
    adjustment = cfg.get("adjustment") or "forward"
    adjustments = ["forward", "back", "raw"] if adjustment == "all" else [adjustment]
    warnings: list[str] = []
    file_status: list[dict] = []
    migrated_files = 0
    migrated_rows = 0
    skipped_files = 0
    repo = DailyBarRepository(db)
    selected: list[tuple[str, Path, tuple[str, ...]]] = []
    comparison_input_warnings: dict[str, list[str]] = {}
    for adj in adjustments:
        files = _daily_files_for_adjustment(data_root, adj)
        if sample:
            files = files[:sample]
        for path in files:
            rows, row_warnings = load_daily_csv_rows(path)
            warnings.extend(row_warnings[:5])
            if compare:
                selected.append((adj, path, tuple(sorted({row["symbol"] for row in rows}))))
                if row_warnings:
                    comparison_input_warnings[str(path)] = row_warnings
            if not rows:
                skipped_files += 1
                file_status.append({"path": str(path), "status": "skipped", "warnings": row_warnings})
                continue
            if compare_only:
                file_status.append({"path": str(path), "status": "compared", "row_count": len(rows)})
                continue
            if not dry_run:
                result = repo.upsert_daily_bars_batch(rows, adj, batch_size=int(cfg.get("batch_size") or 1000))
                migrated_rows += result["row_count"]
            else:
                migrated_rows += len(rows)
            migrated_files += 1
            file_status.append({"path": str(path), "status": "loaded" if not dry_run else "dry_run", "row_count": len(rows)})
    partial_files = sorted((data_root / "intraday" / "partial_daily").glob("*.csv")) if (data_root / "intraday" / "partial_daily").exists() else []
    partial_rows = 0
    if not dry_run and not compare_only:
        for path in (partial_files[:sample] if sample else partial_files):
            rows, row_warnings = load_daily_csv_rows(path)
            warnings.extend(row_warnings[:5])
            payload = []
            for row in rows:
                payload.append({
                    "symbol": row["symbol"],
                    "date": row["date"],
                    "open": row.get("open"),
                    "high_so_far": row.get("high"),
                    "low_so_far": row.get("low"),
                    "last_price": row.get("close"),
                    "volume_so_far": row.get("volume"),
                    "amount_so_far": row.get("amount"),
                    "source": "partial_daily_csv",
                    "is_complete": 0,
                    "updated_at": row.get("updated_at"),
                    "row_hash": row.get("row_hash"),
                })
            if payload:
                db.upsert_many("intraday_partial_daily", payload, ["symbol", "date", "source"])
                partial_rows += len(payload)
    compare_summary = {}
    if compare:
        compare_summary = _compare_selected_csv_rows(repo, selected, comparison_input_warnings)
        if compare_summary["status"] != "matched":
            warnings.append("csv_database_comparison:" + compare_summary["status"])
    table_counts = db.table_stats()
    status = "warning" if warnings else "passed"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "csv_to_sqlite_migration",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "full": bool(cfg.get("full", False)),
        "adjustment": adjustment,
        "csv_counts": counts,
        "migrated_file_count": migrated_files,
        "migrated_row_count": migrated_rows,
        "partial_daily_migrated_row_count": partial_rows,
        "skipped_file_count": skipped_files,
        "table_row_counts": table_counts,
        "compare_summary": compare_summary,
        "file_status": file_status[:50],
        "warning_count": len(warnings),
        "warnings_top": warnings[:10],
        "warnings": warnings,
        "output_paths": {"db_path": str(db.db_path)},
        "disclaimer": "SQLite is a shadow data layer. CSV remains the compatibility fallback.",
    }
    db.close()
    return payload
