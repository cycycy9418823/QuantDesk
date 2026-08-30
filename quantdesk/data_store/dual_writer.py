from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .repositories import DailyBarRepository, UpdateLogRepository
from .sqlite_store import QuantDeskDB


@dataclass
class DualWriteResult:
    ok: bool
    status: str
    csv_written: bool
    sqlite_written: bool
    csv_path: str | None
    sqlite_table: str | None
    row_count: int
    retry_recommended: bool
    warnings: list[str]
    errors: list[str]


def atomic_write_csv(path: str | Path, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0].keys()) if rows else ["symbol", "date", "close"]
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_daily_dual(rows: list[dict], csv_path: str | Path | None, adjustment: str = "forward", db_path: str = "data/quantdesk.db") -> dict:
    warnings: list[str] = []
    errors: list[str] = []
    csv_written = False
    sqlite_written = False
    sqlite_table = None
    db = QuantDeskDB(db_path)
    if csv_path:
        try:
            atomic_write_csv(csv_path, rows)
            csv_written = True
        except Exception as exc:
            errors.append(f"csv_write_failed:{exc}")
            warnings.append("csv_fallback_incomplete")
    try:
        db.initialize()
        repo = DailyBarRepository(db)
        result = repo.upsert_daily_bars_batch(rows, adjustment=adjustment)
        sqlite_written = True
        sqlite_table = result.get("table")
    except Exception as exc:
        errors.append(f"sqlite_write_failed:{exc}")
    csv_requested = bool(csv_path)
    if (csv_written or not csv_requested) and sqlite_written:
        status, ok, retry = "passed", True, False
    elif csv_written or sqlite_written:
        # Round 21 adjudication: readers consume BOTH stores (CSV-first with
        # SQLite fallback via read_daily_prefer_sqlite), so a one-sided write
        # leaves silently divergent stores. Partial success is fail-closed
        # instead of warning+ok so callers must retry or reconcile explicitly.
        status, ok, retry = "failed_partial", False, True
        errors.append("dual_write_partial_failure_fail_closed")
    else:
        status, ok, retry = "failed", False, True
        errors.append("both_csv_and_sqlite_failed")
    try:
        if sqlite_written:
            UpdateLogRepository(db).log_update("dual_write_daily", "dual_writer", status, len(rows), warnings, errors, {"csv_path": str(csv_path) if csv_path else None})
    except Exception as exc:
        warnings.append(f"update_log_write_failed:{exc}")
        if status == "passed":
            status, ok, retry = "warning", True, True
    db.close()
    return asdict(DualWriteResult(ok, status, csv_written, sqlite_written, str(csv_path) if csv_path else None, sqlite_table, len(rows), retry, warnings, errors))


def read_daily_prefer_sqlite(symbol: str, adjustment: str = "forward", csv_path: str | Path | None = None, config: dict | None = None):
    cfg = config or {}
    prefer_sqlite = bool(cfg.get("read_prefer_sqlite", False))
    db_path = cfg.get("db_path") or "data/quantdesk.db"
    warnings: list[str] = []

    def read_sqlite():
        db = QuantDeskDB(db_path)
        db.initialize()
        rows = DailyBarRepository(db).query_daily_bars(symbol, adjustment=adjustment)
        db.close()
        return rows

    def read_csv():
        from .migration import load_daily_csv_rows
        if not csv_path:
            return []
        rows, row_warnings = load_daily_csv_rows(Path(csv_path), default_symbol=symbol)
        warnings.extend(row_warnings)
        return rows

    first_name, second_name = ("sqlite", "csv") if prefer_sqlite else ("csv", "sqlite")
    first, second = (read_sqlite, read_csv) if prefer_sqlite else (read_csv, read_sqlite)
    fallback = False
    try:
        rows = first()
        source = first_name
    except Exception as exc:
        warnings.append(f"{first_name}_read_failed:{exc}")
        rows = []
        source = second_name
        fallback = True
    if not rows:
        try:
            rows = second()
        except Exception as exc:
            warnings.append(f"{second_name}_read_failed:{exc}")
            rows = []
        source = second_name
        fallback = True
    return rows, {"read_source": source, "fallback_used": fallback, "adjustment": adjustment, "row_count": len(rows), "warnings": warnings}
