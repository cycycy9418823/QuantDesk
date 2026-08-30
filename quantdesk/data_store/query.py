from __future__ import annotations

import re
from typing import Any

from .repositories import daily_table_for_adjustment, normalize_symbol
from .sqlite_store import QuantDeskDB

QUERY_WHITELIST_FIELDS = {
    "daily": ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "change", "turnover"],
    "fundamentals": ["symbol", "report_date", "publish_date", "pe", "pb", "ps", "roe", "roa", "revenue", "net_profit", "revenue_yoy", "net_profit_yoy", "market_cap"],
    "sectors": ["symbol", "sector_name", "sector_type", "source"],
    "index": ["symbol", "date", "open", "high", "low", "close", "volume", "amount"],
}

QUERY_WHITELIST_OPS = ["=", "!=", ">", "<", ">=", "<=", "LIKE", "IN", "BETWEEN"]
FORBIDDEN_SQL = re.compile(r";|--|/\*|\*/|\b(DROP|INSERT|UPDATE|DELETE|ALTER|ATTACH|PRAGMA)\b", re.I)


def _parse_value(text: str) -> Any:
    text = text.strip().strip("'").strip('"')
    try:
        return float(text)
    except ValueError:
        return text


def parse_safe_where(where: str, table_kind: str) -> tuple[str, list]:
    if not where:
        return "", []
    if FORBIDDEN_SQL.search(where):
        raise ValueError("forbidden SQL token in where")
    fields = set(QUERY_WHITELIST_FIELDS.get(table_kind, []))
    clauses = [part.strip() for part in re.split(r"\s+AND\s+", where, flags=re.I) if part.strip()]
    sql_parts: list[str] = []
    params: list[Any] = []
    for clause in clauses:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+BETWEEN\s+(.+?)\s+AND\s+(.+)$", clause, re.I)
        if m:
            field = m.group(1)
            if field not in fields:
                raise ValueError(f"field not allowed: {field}")
            sql_parts.append(f"{field} BETWEEN ? AND ?")
            params.extend([_parse_value(m.group(2)), _parse_value(m.group(3))])
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+IN\s*\((.+)\)$", clause, re.I)
        if m:
            field = m.group(1)
            if field not in fields:
                raise ValueError(f"field not allowed: {field}")
            values = [_parse_value(v) for v in m.group(2).split(",")]
            sql_parts.append(f"{field} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|!=|=|>|<|LIKE)\s*(.+)$", clause, re.I)
        if not m:
            raise ValueError(f"unsupported where clause: {clause}")
        field, op, value = m.group(1), m.group(2).upper(), m.group(3)
        if field not in fields:
            raise ValueError(f"field not allowed: {field}")
        if op not in QUERY_WHITELIST_OPS:
            raise ValueError(f"operator not allowed: {op}")
        sql_parts.append(f"{field} {op} ?")
        params.append(_parse_value(value))
    return " AND ".join(sql_parts), params


def run_safe_query(kind: str, where: str | None = None, limit: int = 100, adjustment: str = "forward", db_path: str = "data/quantdesk.db", **kwargs) -> dict:
    db = QuantDeskDB(db_path)
    db.initialize()
    table_map = {
        "daily": daily_table_for_adjustment(adjustment),
        "cross-section": daily_table_for_adjustment(adjustment),
        "fundamentals": "fundamentals",
        "sectors": "stock_sectors",
        "index": "index_daily",
    }
    table = table_map.get(kind)
    if not table:
        return {"ok": False, "status": "failed", "error": f"unsupported query kind: {kind}"}
    table_kind = "daily" if kind in {"daily", "cross-section"} else ("index" if kind == "index" else kind)
    clauses = []
    params: list[Any] = []
    if kwargs.get("symbol"):
        field = "symbol"
        clauses.append(f"{field} = ?")
        params.append(normalize_symbol(kwargs["symbol"]) if table_kind != "index" else kwargs["symbol"])
    if kwargs.get("date"):
        clauses.append("date = ?")
        params.append(kwargs["date"])
    safe_sql, safe_params = parse_safe_where(where or "", table_kind)
    if safe_sql:
        clauses.append(safe_sql)
        params.extend(safe_params)
    sql = f"SELECT * FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " LIMIT ?"
    params.append(int(limit or 100))
    rows = db.execute(sql, params)
    payload = {
        "ok": True,
        "status": "passed",
        "task_type": "data_query",
        "kind": kind,
        "row_count": len(rows),
        "rows": rows,
        "warning_count": 0,
        "warnings_top": [],
        "output_paths": {},
        "disclaimer": "SQLite is a shadow data layer. CSV remains the compatibility fallback.",
    }
    db.close()
    return payload
