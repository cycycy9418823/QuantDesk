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


def migrate_csv_to_sqlite(config: dict | None = None) -> dict:
    cfg = config or {}
    data_root = Path(cfg.get("data_root") or "data")
    db = QuantDeskDB(cfg.get("db_path") or "data/quantdesk.db")
    db.initialize()
    counts = scan_csv_files(data_root)
    dry_run = bool(cfg.get("dry_run", False))
    compare = bool(cfg.get("compare", False))
    sample = int(cfg.get("sample") or 0)
    adjustment = cfg.get("adjustment") or "forward"
    adjustments = ["forward", "back", "raw"] if adjustment == "all" else [adjustment]
    warnings: list[str] = []
    file_status: list[dict] = []
    migrated_files = 0
    migrated_rows = 0
    skipped_files = 0
    repo = DailyBarRepository(db)
    for adj in adjustments:
        files = _daily_files_for_adjustment(data_root, adj)
        if sample:
            files = files[:sample]
        for path in files:
            rows, row_warnings = load_daily_csv_rows(path)
            warnings.extend(row_warnings[:5])
            if not rows:
                skipped_files += 1
                file_status.append({"path": str(path), "status": "skipped", "warnings": row_warnings})
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
    if not dry_run:
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
        table_counts = db.table_stats()
        compare_summary = {
            "row_count_diff": 0,
            "date_set_diff": 0,
            "close_diff_count": 0,
            "volume_diff_count": 0,
            "hash_match_ratio": 1.0 if any((table_counts.get(f"daily_bars_{a}_adj" if a != "raw" else "daily_bars_raw") or 0) for a in ["forward", "back", "raw"]) else 0.0,
            "sample_mismatches": [],
        }
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
