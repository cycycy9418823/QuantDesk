from __future__ import annotations

import re
from typing import Any, Iterable

from .repositories import (
    SymbolIdentityConflict,
    daily_table_for_adjustment,
    normalize_date,
    normalize_symbol,
    reconcile_identity_rows,
    symbol_identity_aliases,
)
from .sqlite_store import QuantDeskDB

QUERY_WHITELIST_FIELDS = {
    "daily": ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "change", "turnover"],
    "fundamentals": ["symbol", "report_date", "publish_date", "pe", "pb", "ps", "roe", "roa", "revenue", "net_profit", "revenue_yoy", "net_profit_yoy", "market_cap"],
    "sectors": ["symbol", "sector_name", "sector_type", "source"],
    "index": ["symbol", "date", "open", "high", "low", "close", "volume", "amount"],
}

QUERY_WHITELIST_OPS = ["=", "!=", ">", "<", ">=", "<=", "LIKE", "IN", "BETWEEN"]
FORBIDDEN_SQL = re.compile(r";|--|/\*|\*/|\b(DROP|INSERT|UPDATE|DELETE|ALTER|ATTACH|PRAGMA)\b", re.I)

# Keep each expansion query far below SQLite's expression-depth and host
# parameter limits so a large normal universe/history cannot build one
# unbounded OR tree.
_EXPANSION_PARAM_BUDGET = 900
_MAX_GROUPS_PER_BATCH = 100


def _parse_value(text: str, field: str | None = None) -> Any:
    text = text.strip().strip("'").strip('"')
    if field == "symbol":
        return text
    try:
        return float(text)
    except ValueError:
        return text


def parse_where_clauses(where: str, table_kind: str) -> list[dict[str, Any]]:
    """Parse a whitelisted ``where`` string into structured clauses."""

    if not where:
        return []
    if FORBIDDEN_SQL.search(where):
        raise ValueError("forbidden SQL token in where")
    fields = set(QUERY_WHITELIST_FIELDS.get(table_kind, []))
    clauses: list[dict[str, Any]] = []
    for part in re.split(r"\s+AND\s+", where, flags=re.I):
        clause = part.strip()
        if not clause:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+BETWEEN\s+(.+?)\s+AND\s+(.+)$", clause, re.I)
        if m:
            field = m.group(1)
            if field not in fields:
                raise ValueError(f"field not allowed: {field}")
            clauses.append({"field": field, "op": "BETWEEN", "values": [_parse_value(m.group(2), field), _parse_value(m.group(3), field)]})
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+IN\s*\((.+)\)$", clause, re.I)
        if m:
            field = m.group(1)
            if field not in fields:
                raise ValueError(f"field not allowed: {field}")
            clauses.append({"field": field, "op": "IN", "values": [_parse_value(v, field) for v in m.group(2).split(",")]})
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|!=|=|>|<|LIKE)\s*(.+)$", clause, re.I)
        if not m:
            raise ValueError(f"unsupported where clause: {clause}")
        field, op, value = m.group(1), m.group(2).upper(), m.group(3)
        if field not in fields:
            raise ValueError(f"field not allowed: {field}")
        if op not in QUERY_WHITELIST_OPS:
            raise ValueError(f"operator not allowed: {op}")
        clauses.append({"field": field, "op": op, "values": [_parse_value(value, field)]})
    return clauses


def _clause_sql(clause: dict[str, Any]) -> tuple[str, list[Any]]:
    field, op = clause["field"], clause["op"]
    values = list(clause["values"])
    if op == "BETWEEN":
        return f"{field} BETWEEN ? AND ?", values
    if op == "IN":
        return f"{field} IN ({', '.join('?' for _ in values)})", values
    return f"{field} {op} ?", values


def _symbol_aliases_for_values(values: Iterable[Any]) -> list[str]:
    aliases: set[str] = set()
    for value in values:
        aliases.update(symbol_identity_aliases(value))
    return sorted(aliases)


def _symbol_clause_sql(clause: dict[str, Any]) -> tuple[str, list[Any]]:
    """Make a first-pass ``symbol`` predicate understand legacy aliases.

    ``symbol = '600000.SH'`` (and ``sh600000``/``IN (...)``) must select the
    same canonical stored row as the ``symbol`` keyword argument. ``LIKE`` stays
    a pattern operator. ``!=`` excludes the whole equivalent identity.
    """

    aliases = _symbol_aliases_for_values(clause["values"])
    if clause["op"] == "!=":
        if not aliases:
            return "1 = 1", []
        return f"symbol NOT IN ({', '.join('?' for _ in aliases)})", aliases
    if not aliases:
        return "1 = 0", []
    return f"symbol IN ({', '.join('?' for _ in aliases)})", aliases


def parse_safe_where(where: str, table_kind: str) -> tuple[str, list]:
    parts: list[str] = []
    params: list[Any] = []
    for clause in parse_where_clauses(where, table_kind):
        sql, values = _clause_sql(clause)
        parts.append(sql)
        params.extend(values)
    return " AND ".join(parts), params


def _conflict_payload(kind: str, exc: SymbolIdentityConflict) -> dict:
    return {
        "ok": False,
        "status": "identity_conflict",
        "task_type": "data_query",
        "kind": kind,
        "row_count": 0,
        "rows": [],
        "warning_count": 1,
        "warnings_top": [str(exc)],
        "identity_conflict": str(exc),
        "output_paths": {},
        "disclaimer": "SQLite is a shadow data layer. CSV remains the compatibility fallback.",
    }


def _batched_groups(groups: set[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Chunk matched identity/date groups into bounded parameter batches."""

    batches: list[list[tuple[str, str]]] = []
    batch: list[tuple[str, str]] = []
    budget = 0
    for identity, day in sorted(groups):
        cost = len(symbol_identity_aliases(identity)) + 1
        if batch and (budget + cost > _EXPANSION_PARAM_BUDGET or len(batch) >= _MAX_GROUPS_PER_BATCH):
            batches.append(batch)
            batch, budget = [], 0
        batch.append((identity, day))
        budget += cost
    if batch:
        batches.append(batch)
    return batches


def _run_filtered_daily(kind: str, table: str, table_kind: str, where: str | None, limit: int, adjustment: str, db: QuantDeskDB, **kwargs) -> dict:
    """Answer a daily/cross-section query with SQLite semantics and reconcile.

    Strategy: let SQLite decide the rows the query actually selects, with
    ``symbol`` predicates taught legacy alias equivalence so the first pass
    already understands the canonical identity (date/NULL/operator semantics
    stay parameterized SQLite). Then expand each *matched* identity/date group
    to all of its aliases for conflict reconciliation in bounded batches, so a
    large history/universe cannot build an unbounded OR tree. A conflict inside
    a matched group stays visible; a group whose aliases are all unmatched
    cannot block the result.
    """

    clauses = parse_where_clauses(where or "", table_kind)
    sql_parts: list[str] = []
    params: list[Any] = []
    if kwargs.get("symbol"):
        aliases = symbol_identity_aliases(kwargs["symbol"])
        if not aliases:
            sql_parts.append("1 = 0")
        else:
            sql_parts.append(f"symbol IN ({', '.join('?' for _ in aliases)})")
            params.extend(aliases)
    if kwargs.get("date"):
        sql_parts.append("date = ?")
        params.append(normalize_date(kwargs["date"]))
    for clause in clauses:
        if clause["field"] == "symbol" and clause["op"] in {"=", "!=", "IN"}:
            sql, values = _symbol_clause_sql(clause)
        else:
            sql, values = _clause_sql(clause)
        sql_parts.append(sql)
        params.extend(values)

    sql = f"SELECT * FROM {table}"
    if sql_parts:
        sql += " WHERE " + " AND ".join(sql_parts)
    matched = db.execute(sql, params)

    matched_groups = {(normalize_symbol(item.get("symbol")), str(item.get("date") or "")) for item in matched}
    reconciled: dict[tuple[str, str], dict] = {}
    for batch in _batched_groups(matched_groups):
        parts: list[str] = []
        batch_params: list[Any] = []
        for identity, day in batch:
            aliases = symbol_identity_aliases(identity)
            if not aliases:
                continue
            parts.append(f"(symbol IN ({', '.join('?' for _ in aliases)}) AND date = ?)")
            batch_params.extend(aliases)
            batch_params.append(day)
        if not parts:
            continue
        expanded = db.execute(f"SELECT * FROM {table} WHERE " + " OR ".join(parts), batch_params)
        try:
            for item in reconcile_identity_rows(expanded):
                reconciled[(normalize_symbol(item.get("symbol")), str(item.get("date") or ""))] = item
        except SymbolIdentityConflict as exc:
            db.close()
            return _conflict_payload(kind, exc)

    rows = [reconciled[key] for key in matched_groups if key in reconciled]
    rows.sort(key=lambda item: (str(item.get("date") or ""), normalize_symbol(item.get("symbol"))))
    if limit:
        rows = rows[: int(limit)]
    db.close()
    return {
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
        db.close()
        return {"ok": False, "status": "failed", "error": f"unsupported query kind: {kind}"}
    table_kind = "daily" if kind in {"daily", "cross-section"} else ("index" if kind == "index" else kind)
    if table_kind == "daily":
        return _run_filtered_daily(kind, table, table_kind, where, limit, adjustment, db, **kwargs)

    clauses = []
    params: list[Any] = []
    if kwargs.get("symbol"):
        clauses.append("symbol = ?")
        params.append(kwargs["symbol"] if table_kind == "index" else normalize_symbol(kwargs["symbol"]))
    if kwargs.get("date"):
        clauses.append("date = ?")
        params.append(normalize_date(kwargs["date"]))
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
