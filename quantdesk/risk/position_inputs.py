from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quantdesk.platform_common import PROJECT_ROOT, clean_number, synthetic_candidates

DEFAULT_INPUT_PATHS = {
    "auto_paper_portfolio": "state/auto_paper_portfolio.json",
    "paper_account": "state/paper_account.json",
    "paper_watchlist": "state/paper_watchlist.json",
    "multi_strategy_account_book": "results/multi_strategy_account_book_latest.json",
    "intraday_live_workbench": "results/intraday_live_workbench_latest.json",
    "stock_picking_flow": "results/stock_picking_flow_latest.json",
}

SOURCE_PRIORITY = {
    "paper_portfolio": 1,
    "paper_account": 1,
    "multi_strategy_book": 2,
    "intraday_live": 3,
    "paper_watchlist": 4,
    "stock_picking_flow": 5,
}


def _resolve(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def load_json_if_exists(path: str | Path) -> tuple[dict | None, list[str]]:
    target = _resolve(path)
    if not target.exists():
        return None, [f"missing:{target.as_posix()}"]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, [f"invalid_json:{target.as_posix()}:{exc}"]
    if not isinstance(payload, dict):
        return None, [f"invalid_json_object:{target.as_posix()}"]
    return payload, []


def _first(row: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def normalize_position_row(row: dict, *, source: str) -> dict:
    symbol = str(_first(row, ("symbol", "code", "ticker"), "") or "").strip()
    latest_price = clean_number(_first(row, ("latest_price", "current_price", "last_price", "close", "latest_reference_price"), None), default=0.0)
    reference_price = clean_number(_first(row, ("reference_price", "initial_reference_price", "entry_price", "cost_price", "close"), latest_price), default=0.0)
    quantity = clean_number(_first(row, ("quantity", "qty", "shares", "position_qty"), 0), default=0.0)
    market_value = clean_number(_first(row, ("market_value", "value", "notional"), 0), default=0.0)
    if not market_value and quantity and latest_price:
        market_value = round(quantity * latest_price, 4)
    return {
        "symbol": symbol,
        "name": str(_first(row, ("name", "display_name"), "") or ""),
        "sector": str(_first(row, ("sector", "industry", "theme"), "") or ""),
        "theme": str(_first(row, ("theme", "concept"), "") or ""),
        "quantity": quantity,
        "reference_price": reference_price or None,
        "latest_price": latest_price or reference_price or None,
        "market_value": market_value or None,
        "weight": clean_number(_first(row, ("weight", "portfolio_weight"), 0), default=0.0) or None,
        "entry_date": _first(row, ("entry_date", "added_at", "created_at", "as_of_date"), None),
        "entry_price": clean_number(_first(row, ("entry_price", "cost_price", "initial_reference_price"), reference_price), default=0.0) or None,
        "strategy": str(_first(row, ("strategy", "primary_strategy"), "") or ""),
        "source": source,
        "research_only": True,
    }


def _rows_from_payload(payload: dict | None, source: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    keys = (
        "positions",
        "holdings",
        "watchlist",
        "items",
        "top_candidates",
        "paper_watchlist_suggestions",
        "recommendations",
        "virtual_positions",
        "intraday_positions",
    )
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [normalize_position_row(row, source=source) for row in value if isinstance(row, dict)]
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    if isinstance(account.get("positions"), list):
        return [normalize_position_row(row, source=source) for row in account["positions"] if isinstance(row, dict)]
    book = payload.get("book") if isinstance(payload.get("book"), dict) else {}
    if isinstance(book.get("positions"), list):
        return [normalize_position_row(row, source=source) for row in book["positions"] if isinstance(row, dict)]
    return []


def _config_paths(config: dict | None) -> dict[str, str]:
    cfg = dict((config or {}).get("risk_position_inputs") or {})
    direct = dict(config or {})
    return {
        "auto_paper_portfolio": direct.get("positions_json") or cfg.get("auto_paper_portfolio_json") or DEFAULT_INPUT_PATHS["auto_paper_portfolio"],
        "paper_account": cfg.get("paper_account_json") or DEFAULT_INPUT_PATHS["paper_account"],
        "paper_watchlist": cfg.get("paper_watchlist_json") or direct.get("watchlist_path") or DEFAULT_INPUT_PATHS["paper_watchlist"],
        "multi_strategy_account_book": cfg.get("multi_strategy_book_json") or DEFAULT_INPUT_PATHS["multi_strategy_account_book"],
        "intraday_live_workbench": direct.get("intraday_json") or cfg.get("intraday_live_workbench_json") or DEFAULT_INPUT_PATHS["intraday_live_workbench"],
        "stock_picking_flow": cfg.get("stock_picking_flow_json") or DEFAULT_INPUT_PATHS["stock_picking_flow"],
    }


def merge_position_sources(sources: list[dict]) -> dict:
    merged: dict[str, dict] = {}
    source_status: dict[str, str] = {}
    warnings: list[str] = []
    for source_payload in sources:
        source = str(source_payload.get("source") or "unknown")
        rows = [row for row in source_payload.get("positions", []) if isinstance(row, dict) and row.get("symbol")]
        source_status[source] = source_payload.get("status") or ("loaded" if rows else "missing")
        warnings.extend(str(item) for item in source_payload.get("warnings", []) if item)
        for row in rows:
            symbol = str(row.get("symbol") or "")
            current_priority = SOURCE_PRIORITY.get(str(row.get("source")), 99)
            existing = merged.get(symbol)
            existing_priority = SOURCE_PRIORITY.get(str((existing or {}).get("source")), 99)
            if existing is None or current_priority < existing_priority:
                merged[symbol] = dict(row)
            elif existing is not None:
                combined = dict(existing)
                for key, value in row.items():
                    if combined.get(key) in (None, "", 0) and value not in (None, "", 0):
                        combined[key] = value
                merged[symbol] = combined
    total_value = sum(clean_number(row.get("market_value")) for row in merged.values())
    if total_value > 0:
        for row in merged.values():
            if row.get("weight") in (None, 0):
                row["weight"] = round(clean_number(row.get("market_value")) / total_value, 6)
    return {"positions": list(merged.values()), "source_status": source_status, "warnings": list(dict.fromkeys(warnings))}


def _synthetic_positions() -> list[dict]:
    rows = []
    for idx, row in enumerate(synthetic_candidates()):
        item = normalize_position_row(
            {
                **row,
                "quantity": 1000,
                "entry_price": clean_number(row.get("close")) * (1.08 if idx == 0 else 0.96),
                "latest_price": row.get("close"),
                "weight": 0.24 if idx == 0 else 0.12,
                "entry_date": "2026-05-01",
            },
            source="paper_portfolio",
        )
        rows.append(item)
    return rows


def load_paper_positions(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    paths = _config_paths(cfg)
    sources = []
    for key, path in paths.items():
        payload, warnings = load_json_if_exists(path)
        source_name = {
            "auto_paper_portfolio": "paper_portfolio",
            "paper_account": "paper_account",
            "paper_watchlist": "paper_watchlist",
            "multi_strategy_account_book": "multi_strategy_book",
            "intraday_live_workbench": "intraday_live",
            "stock_picking_flow": "stock_picking_flow",
        }[key]
        rows = _rows_from_payload(payload, source_name)
        status = "loaded" if rows else ("invalid" if warnings and not warnings[0].startswith("missing:") else "missing")
        sources.append({"source": key, "status": status, "positions": rows, "warnings": warnings})
    merged = merge_position_sources(sources)
    positions = merged["positions"]
    warnings = list(merged["warnings"])
    if not positions and bool(cfg.get("synthetic_test_data") or (cfg.get("risk_position_inputs") or {}).get("synthetic_when_missing")):
        positions = _synthetic_positions()
        merged["source_status"]["synthetic"] = "loaded"
    if not positions:
        warnings.append("risk_position_inputs_missing")
    status = "passed" if positions else "warning"
    payload = {
        "ok": True,
        "status": status,
        "positions": positions,
        "position_count": len(positions),
        "source_status": merged["source_status"],
        "warnings": list(dict.fromkeys(warnings)),
        "research_only": True,
        "network": False,
    }
    payload["input_status"] = build_position_input_status(payload)
    return payload


def build_position_input_status(payload: dict) -> dict:
    return {
        "status": payload.get("status") or "warning",
        "position_count": len(payload.get("positions") or []),
        "source_status": dict(payload.get("source_status") or {}),
        "warning_count": len(payload.get("warnings") or []),
    }
