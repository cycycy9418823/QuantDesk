from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .hash_utils import DAILY_HASH_FIELDS, FUNDAMENTAL_HASH_FIELDS, SECTOR_HASH_FIELDS, compute_row_hash
from .schema import DAILY_TABLES
from .sqlite_store import QuantDeskDB


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def normalize_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip()
    if "." in text:
        left, right = text.split(".", 1)
        return f"{left.zfill(6) if left.isdigit() else left}.{right}"
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split(" ")[0].replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    parts = text.split("-")
    if len(parts) == 3:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return text


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def daily_table_for_adjustment(adjustment: str) -> str:
    mapping = {
        "forward": "daily_bars_forward_adj",
        "forward_adj": "daily_bars_forward_adj",
        "back": "daily_bars_back_adj",
        "back_adj": "daily_bars_back_adj",
        "raw": "daily_bars_raw",
    }
    table = mapping.get((adjustment or "forward").lower())
    if table not in DAILY_TABLES:
        raise ValueError(f"unsupported adjustment: {adjustment}")
    return table


class DailyBarRepository:
    def __init__(self, db: QuantDeskDB):
        self.db = db

    def _normalize_row(self, row: dict) -> dict:
        out = {
            "symbol": normalize_symbol(row.get("symbol")),
            "date": normalize_date(row.get("date") or row.get("trade_date")),
            "open": to_float(row.get("open")),
            "high": to_float(row.get("high")),
            "low": to_float(row.get("low")),
            "close": to_float(row.get("close")),
            "volume": to_float(row.get("volume") if row.get("volume") is not None else row.get("vol")),
            "amount": to_float(row.get("amount")),
            "pct_chg": to_float(row.get("pct_chg") if row.get("pct_chg") is not None else row.get("pct_change")),
            "change": to_float(row.get("change")),
            "turnover": to_float(row.get("turnover")),
            "source": row.get("source") or "local",
            "updated_at": row.get("updated_at") or now_text(),
        }
        out["row_hash"] = compute_row_hash(out, DAILY_HASH_FIELDS)
        return json_safe(out)

    def upsert_daily_bars(self, rows: list[dict], adjustment: str = "forward") -> dict:
        return self.upsert_daily_bars_batch(rows, adjustment=adjustment)

    def upsert_daily_bars_batch(self, rows: list[dict], adjustment: str = "forward", batch_size: int = 1000) -> dict:
        table = daily_table_for_adjustment(adjustment)
        normalized = [self._normalize_row(row) for row in rows if row.get("symbol") and (row.get("date") or row.get("trade_date"))]
        total = 0
        for i in range(0, len(normalized), batch_size):
            total += self.db.upsert_many(table, normalized[i:i + batch_size], ["symbol", "date"])["row_count"]
        return {"ok": True, "status": "passed", "table": table, "row_count": total}

    def query_daily_bars(self, symbol: str, start_date: str | None = None, end_date: str | None = None, adjustment: str = "forward") -> list[dict]:
        table = daily_table_for_adjustment(adjustment)
        params: list[Any] = [normalize_symbol(symbol)]
        where = ["symbol = ?"]
        if start_date:
            where.append("date >= ?")
            params.append(normalize_date(start_date))
        if end_date:
            where.append("date <= ?")
            params.append(normalize_date(end_date))
        return self.db.execute(f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY date", params)

    def query_cross_section(self, date: str, adjustment: str = "forward", limit: int | None = None) -> list[dict]:
        table = daily_table_for_adjustment(adjustment)
        sql = f"SELECT * FROM {table} WHERE date = ? ORDER BY symbol"
        if limit:
            sql += " LIMIT ?"
            return self.db.execute(sql, [normalize_date(date), int(limit)])
        return self.db.execute(sql, [normalize_date(date)])

    def latest_date(self, symbol: str | None = None, adjustment: str = "forward") -> str | None:
        table = daily_table_for_adjustment(adjustment)
        if symbol:
            row = self.db.execute_one(f"SELECT MAX(date) AS date FROM {table} WHERE symbol = ?", [normalize_symbol(symbol)])
        else:
            row = self.db.execute_one(f"SELECT MAX(date) AS date FROM {table}")
        return row.get("date") if row else None

    def compare_with_csv(self, symbol: str, adjustment: str, csv_path: str | Path) -> dict:
        from .migration import load_daily_csv_rows
        csv_rows, warnings = load_daily_csv_rows(Path(csv_path), default_symbol=symbol)
        sqlite_rows = self.query_daily_bars(symbol, adjustment=adjustment)
        csv_by_date = {row["date"]: row for row in csv_rows}
        db_by_date = {row["date"]: row for row in sqlite_rows}
        common = sorted(set(csv_by_date) & set(db_by_date))
        hash_matches = sum(1 for d in common if csv_by_date[d].get("row_hash") == db_by_date[d].get("row_hash"))
        return {
            "row_count_diff": len(sqlite_rows) - len(csv_rows),
            "date_set_diff": len(set(csv_by_date) ^ set(db_by_date)),
            "hash_match_ratio": hash_matches / len(common) if common else 0.0,
            "warnings": warnings,
        }


class FundamentalRepository:
    def __init__(self, db: QuantDeskDB):
        self.db = db

    def upsert_fundamentals(self, rows: list[dict]) -> dict:
        normalized = []
        for row in rows:
            out = dict(row)
            out["symbol"] = normalize_symbol(row.get("symbol"))
            out["report_date"] = normalize_date(row.get("report_date"))
            out["publish_date"] = normalize_date(row.get("publish_date")) if row.get("publish_date") else None
            out["raw_json"] = row.get("raw_json") if isinstance(row.get("raw_json"), str) else json.dumps(row.get("raw_json", {}), ensure_ascii=False)
            out["source"] = row.get("source") or "local"
            out["updated_at"] = row.get("updated_at") or now_text()
            out["row_hash"] = compute_row_hash(out, FUNDAMENTAL_HASH_FIELDS)
            normalized.append(json_safe(out))
        return self.db.upsert_many("fundamentals", normalized, ["symbol", "report_date"])

    def query_fundamentals(self, symbol: str, report_date: str | None = None) -> list[dict]:
        if report_date:
            return self.db.execute("SELECT * FROM fundamentals WHERE symbol=? AND report_date=? ORDER BY report_date", [normalize_symbol(symbol), normalize_date(report_date)])
        return self.db.execute("SELECT * FROM fundamentals WHERE symbol=? ORDER BY report_date", [normalize_symbol(symbol)])

    def latest_fundamentals(self, symbol: str) -> dict | None:
        return self.db.execute_one("SELECT * FROM fundamentals WHERE symbol=? ORDER BY report_date DESC LIMIT 1", [normalize_symbol(symbol)])


class SectorRepository:
    def __init__(self, db: QuantDeskDB):
        self.db = db

    def upsert_sector_mappings(self, rows: list[dict]) -> dict:
        normalized = []
        for row in rows:
            out = {
                "symbol": normalize_symbol(row.get("symbol")),
                "sector_name": str(row.get("sector_name") or row.get("sector") or "").strip(),
                "sector_type": str(row.get("sector_type") or "industry").strip(),
                "source": str(row.get("source") or "local").strip(),
                "updated_at": row.get("updated_at") or now_text(),
            }
            out["row_hash"] = compute_row_hash(out, SECTOR_HASH_FIELDS)
            normalized.append(out)
        return self.db.upsert_many("stock_sectors", normalized, ["symbol", "sector_name", "sector_type", "source"])

    def get_sectors(self, symbol: str) -> list[dict]:
        return self.db.execute("SELECT * FROM stock_sectors WHERE symbol=? ORDER BY sector_type, sector_name", [normalize_symbol(symbol)])

    def get_members(self, sector_name: str, sector_type: str | None = None) -> list[dict]:
        if sector_type:
            return self.db.execute("SELECT * FROM stock_sectors WHERE sector_name=? AND sector_type=? ORDER BY symbol", [sector_name, sector_type])
        return self.db.execute("SELECT * FROM stock_sectors WHERE sector_name=? ORDER BY symbol", [sector_name])

    def sector_counts(self) -> list[dict]:
        return self.db.execute("SELECT sector_name, sector_type, COUNT(*) AS count FROM stock_sectors GROUP BY sector_name, sector_type ORDER BY count DESC")


class IndexRepository:
    def __init__(self, db: QuantDeskDB):
        self.db = db

    def upsert_index_daily(self, rows: list[dict]) -> dict:
        normalized = []
        for row in rows:
            out = {
                "symbol": str(row.get("symbol") or row.get("index_symbol") or "").strip(),
                "date": normalize_date(row.get("date") or row.get("trade_date")),
                "open": to_float(row.get("open")),
                "high": to_float(row.get("high")),
                "low": to_float(row.get("low")),
                "close": to_float(row.get("close")),
                "volume": to_float(row.get("volume") if row.get("volume") is not None else row.get("vol")),
                "amount": to_float(row.get("amount")),
                "source": row.get("source") or "local",
                "updated_at": row.get("updated_at") or now_text(),
            }
            out["row_hash"] = compute_row_hash(out, DAILY_HASH_FIELDS)
            normalized.append(json_safe(out))
        return self.db.upsert_many("index_daily", normalized, ["symbol", "date"])

    def query_index_daily(self, symbol: str, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
        params = [symbol]
        where = ["symbol = ?"]
        if start_date:
            where.append("date >= ?")
            params.append(normalize_date(start_date))
        if end_date:
            where.append("date <= ?")
            params.append(normalize_date(end_date))
        return self.db.execute(f"SELECT * FROM index_daily WHERE {' AND '.join(where)} ORDER BY date", params)


class DataQualityRepository:
    def __init__(self, db: QuantDeskDB):
        self.db = db

    def insert_issues(self, issues: list[dict]) -> dict:
        rows = []
        for issue in issues:
            out = dict(issue)
            out.setdefault("issue_id", uuid.uuid4().hex[:16])
            out.setdefault("detected_at", now_text())
            out.setdefault("repair_status", "unresolved")
            rows.append(out)
        return self.db.upsert_many("data_quality_issues", rows, ["issue_id"])

    def list_open_issues(self) -> list[dict]:
        return self.db.execute("SELECT * FROM data_quality_issues WHERE repair_status != 'repaired' ORDER BY detected_at DESC")

    def mark_repaired(self, issue_id: str) -> dict:
        self.db.execute("UPDATE data_quality_issues SET repair_status='repaired', repaired_at=? WHERE issue_id=?", [now_text(), issue_id])
        return {"ok": True, "status": "passed", "issue_id": issue_id}


class UpdateLogRepository:
    def __init__(self, db: QuantDeskDB):
        self.db = db

    def log_update(self, task_type: str, source: str, status: str, row_count: int, warnings: list[str], errors: list[str], details: dict | None = None) -> dict:
        row = {
            "update_id": uuid.uuid4().hex[:16],
            "task_type": task_type,
            "source": source,
            "started_at": now_text(),
            "finished_at": now_text(),
            "status": status,
            "row_count": int(row_count),
            "warning_count": len(warnings),
            "error_count": len(errors),
            "details_json": json.dumps(json_safe({"warnings": warnings, "errors": errors, "details": details or {}}), ensure_ascii=False),
        }
        return self.db.upsert_many("data_update_log", [row], ["update_id"])

    def list_updates(self, limit: int = 50) -> list[dict]:
        return self.db.execute("SELECT * FROM data_update_log ORDER BY finished_at DESC LIMIT ?", [int(limit)])
