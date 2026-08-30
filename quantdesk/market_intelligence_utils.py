"""Shared helpers for local market intelligence observations.

The helpers in this module are deliberately read-only. They load local CSV
snapshots, normalize symbols, and derive observation metrics without fetching
online data or emitting trading instructions.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.data_quality import normalize_date_text
from quantdesk.scope_policy import resolve_scope
from quantdesk.shared import normalize_symbol, read_csv_rows, safe_float, truthy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOW_PRICE_PATH = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
THEME_MAP_PATH = PROJECT_ROOT / "data" / "universe" / "stock_theme_map.csv"
SPOT_PATH = PROJECT_ROOT / "data" / "cache" / "spot_snapshot_latest.csv"
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
INTRADAY_DIR = PROJECT_ROOT / "data" / "intraday"

UNKNOWN_VALUES = {"", "-", "nan", "none", "null", "未知", "未知板块", "未知行业", "未知题材", "未知概念"}


def project_path(path: str | Path, *, project_root: str | Path | None = None) -> Path:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    target = Path(path)
    return target if target.is_absolute() else root / target


def rel_path(path: str | Path, *, project_root: str | Path | None = None) -> str:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    target = project_path(path, project_root=root)
    try:
        return str(target.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def write_markdown(path: str | Path, lines: list[str]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def is_unknown(value: Any) -> bool:
    text = str(value or "").strip()
    return text.lower() in UNKNOWN_VALUES or text in UNKNOWN_VALUES


def first_text(row: dict[str, Any], keys: list[str], default: str = "") -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return default


def safe_pct(value: Any) -> float | None:
    number = safe_float(value, None)
    if number is None or math.isnan(float(number)):
        return None
    return float(number)


def load_low_price_rows(
    *,
    project_root: str | Path | None = None,
    only_included: bool = True,
    max_symbols: int = 80,
) -> list[dict[str, Any]]:
    path = project_path("data/universe/low_price_learning_symbols.csv", project_root=project_root)
    rows: list[dict[str, Any]] = []
    for row in read_csv_rows(path):
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if not symbol:
            continue
        if only_included and "included" in row and not truthy(row.get("included")):
            continue
        item = {str(key): value for key, value in row.items()}
        item["symbol"] = symbol
        item["name"] = first_text(item, ["name", "名称", "股票名称"], symbol)
        price = safe_float(item.get("price") or item.get("latest_close") or item.get("close"), None)
        if price is not None:
            item["latest_close"] = float(price)
        amount = safe_float(item.get("amount") or item.get("turnover") or item.get("成交额"), None)
        if amount is not None:
            item["amount"] = float(amount)
        rows.append(item)
        if max_symbols and len(rows) >= int(max_symbols):
            break
    return rows


def load_theme_map_rows(*, project_root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    path = project_path("data/universe/stock_theme_map.csv", project_root=project_root)
    mapping: dict[str, dict[str, Any]] = {}
    for row in read_csv_rows(path):
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if not symbol:
            continue
        item = {str(key): value for key, value in row.items()}
        item["symbol"] = symbol
        mapping[symbol] = item
    return mapping


def load_spot_rows(*, project_root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    path = project_path("data/cache/spot_snapshot_latest.csv", project_root=project_root)
    rows: dict[str, dict[str, Any]] = {}
    for row in read_csv_rows(path):
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码") or row.get("代码原始"))
        if not symbol:
            continue
        item = {str(key): value for key, value in row.items()}
        item["symbol"] = symbol
        item["name"] = first_text(item, ["name", "名称"], symbol)
        price = safe_float(item.get("最新价") or item.get("price") or item.get("close"), None)
        if price is not None:
            item["latest_close"] = float(price)
        pct = safe_float(item.get("涨跌幅") or item.get("pct_chg") or item.get("change_pct"), None)
        if pct is not None:
            item["pct_chg"] = float(pct)
        amount = safe_float(item.get("成交额") or item.get("amount"), None)
        if amount is not None:
            item["amount"] = float(amount)
        rows[symbol] = item
    return rows


def read_daily_tail(
    symbol: str,
    *,
    project_root: str | Path | None = None,
    limit: int = 20,
    cutoff_date: str | None = None,
) -> list[dict[str, Any]]:
    path = project_path(Path("data/daily") / f"{normalize_symbol(symbol)}.csv", project_root=project_root)
    rows = read_csv_rows(path)
    if cutoff_date:
        cutoff = normalize_date_text(cutoff_date)
        if not cutoff:
            return []
        raw_dates = [row.get("date") or row.get("trade_date") for row in rows]
        parsed_dates = pd.to_datetime(raw_dates, errors="coerce", format="mixed")
        keep_rows: list[dict[str, Any]] = []
        for row, parsed_date in zip(rows, parsed_dates):
            if pd.isna(parsed_date):
                continue
            if pd.Timestamp(parsed_date).strftime("%Y-%m-%d") <= cutoff:
                keep_rows.append(row)
        rows = keep_rows
    if limit and len(rows) > limit:
        rows = rows[-int(limit):]
    return rows


def enrich_symbol_rows(
    rows: list[dict[str, Any]],
    *,
    project_root: str | Path | None = None,
    cutoff_date: str | None = None,
) -> list[dict[str, Any]]:
    themes = load_theme_map_rows(project_root=project_root)
    spots = load_spot_rows(project_root=project_root)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if not symbol:
            continue
        item = dict(row)
        item["symbol"] = symbol
        theme = themes.get(symbol, {})
        spot = spots.get(symbol, {})
        item["name"] = item.get("name") or spot.get("name") or theme.get("name") or symbol
        item["sector"] = first_text(item, ["sector", "板块"], "") or first_text(theme, ["sector", "板块"], "")
        item["industry"] = first_text(item, ["industry", "行业"], "") or first_text(theme, ["industry", "行业"], "")
        item["primary_theme"] = first_text(item, ["primary_theme", "theme"], "") or first_text(theme, ["primary_theme", "theme_tags"], "")
        item["concept_tags"] = first_text(item, ["concept_tags", "concept"], "") or first_text(theme, ["concept_tags", "concept"], "")
        item["theme_confidence"] = theme.get("theme_confidence") or ("mapped" if theme else "unknown")
        if cutoff_date:
            for key in (
                "latest_close",
                "latest_high",
                "latest_low",
                "latest_open",
                "amount",
                "daily_amount",
                "pct_chg",
                "prev_close",
                "daily_latest_date",
            ):
                item.pop(key, None)
        else:
            if item.get("latest_close") in (None, ""):
                item["latest_close"] = spot.get("latest_close") or safe_float(item.get("price") or item.get("close"), None)
            if item.get("amount") in (None, ""):
                item["amount"] = spot.get("amount") or safe_float(item.get("amount"), None)
            if item.get("pct_chg") in (None, ""):
                item["pct_chg"] = spot.get("pct_chg")
        tail = read_daily_tail(symbol, project_root=project_root, limit=8, cutoff_date=cutoff_date)
        if tail:
            latest = tail[-1]
            item["daily_latest_date"] = latest.get("date") or latest.get("trade_date") or ""
            item["latest_close"] = safe_float(
                latest.get("close"),
                None if cutoff_date else item.get("latest_close"),
            )
            item["latest_high"] = safe_float(latest.get("high"), None)
            item["latest_low"] = safe_float(latest.get("low"), None)
            item["latest_open"] = safe_float(latest.get("open"), None)
            item["daily_amount"] = safe_float(
                latest.get("amount"),
                None if cutoff_date else item.get("amount"),
            )
            if len(tail) >= 2:
                prev_close = safe_float(tail[-2].get("close"), None)
                close = safe_float(latest.get("close"), None)
                if prev_close and close:
                    item["pct_chg"] = round((float(close) / float(prev_close) - 1.0) * 100, 4)
                    item["prev_close"] = float(prev_close)
        enriched.append(item)
    return enriched


def load_scope_symbols(
    *,
    scope: str = "current_analysis_pool",
    symbols: list[str] | None = None,
    project_root: str | Path | None = None,
    max_symbols: int = 80,
    cutoff_date: str | None = None,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    policy = resolve_scope(scope=scope, symbols=symbols, max_symbols=max_symbols, project_root=project_root, force=True)
    resolved_scope = str(policy.get("data_scope") or scope or "current_analysis_pool")
    warnings: list[str] = [str(item) for item in (policy.get("warnings") or []) if item]
    if resolved_scope == "specified_symbols":
        rows = [{"symbol": symbol, "name": symbol} for symbol in (policy.get("symbols") or []) if normalize_symbol(symbol)]
        return enrich_symbol_rows(
            rows[: max_symbols or len(rows)],
            project_root=project_root,
            cutoff_date=cutoff_date,
        ), resolved_scope, warnings
    if scope == "full_a":
        path = project_path("data/universe/a_share_symbols.csv", project_root=project_root)
        rows = []
        for row in read_csv_rows(path):
            symbol = normalize_symbol(row.get("symbol") or row.get("code"))
            if not symbol:
                continue
            rows.append({"symbol": symbol, "name": first_text(row, ["name", "名称"], symbol)})
            if max_symbols and len(rows) >= int(max_symbols):
                break
        warnings.append("scope=full_a uses a capped local sample unless --force true is explicitly accepted by the CLI.")
        return enrich_symbol_rows(rows, project_root=project_root, cutoff_date=cutoff_date), scope, warnings
    rows = [{"symbol": symbol, "name": symbol} for symbol in (policy.get("symbols") or []) if normalize_symbol(symbol)]
    return enrich_symbol_rows(rows, project_root=project_root, cutoff_date=cutoff_date), resolved_scope, warnings


def intraday_file_exists(symbol: str, target_date: str, *, project_root: str | Path | None = None) -> bool:
    root = project_path("data/intraday", project_root=project_root)
    code = normalize_symbol(symbol)
    date_text = str(target_date or "").strip()
    candidates = [
        (root / "1m" / date_text / f"{code}.csv", True),
        (root / date_text / f"{code}.csv", True),
        (root / f"{code}_{date_text}.csv", True),
        (root / "1m" / f"{code}.csv", False),
        (root / f"{code}.csv", False),
    ]
    for path, date_in_path in candidates:
        if not path.exists() or not path.is_file():
            continue
        if date_in_path or not date_text:
            return True
        try:
            with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
                for index, line in enumerate(handle):
                    if date_text in line:
                        return True
                    if index >= 2000:
                        break
        except OSError:
            continue
    return False
