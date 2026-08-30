"""Generate stock_theme_map_import.csv drafts.

The draft generator is local paper-research tooling only. It never connects to
brokers, never places orders, and never writes real account information. AKShare
per-symbol lookup is only attempted from user-triggered CLI/API flows.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.shared import normalize_symbol, now_text, read_json, truthy
from quantdesk.shared import write_csv_rows as shared_write_csv_rows
from quantdesk.theme_map_providers import (
    clean_text,
    local_provider,
    per_symbol_info_provider,
    row_has_mapping,
    sanitize_row,
)
from quantdesk.theme_map_seed import THEME_MAP_SEED_ROWS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = PROJECT_ROOT / "results"
STATE_DIR = PROJECT_ROOT / "state"
UNIVERSE_DIR = DATA_DIR / "universe"

AUTO_STRATEGY_LATEST_JSON = RESULTS_DIR / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = RESULTS_DIR / "auto_paper_portfolio_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = STATE_DIR / "auto_paper_portfolio.json"
STOCK_THEME_MAP_CSV = UNIVERSE_DIR / "stock_theme_map.csv"
STOCK_THEME_MAP_PENDING_CSV = UNIVERSE_DIR / "stock_theme_map_pending.csv"
LOW_PRICE_SYMBOLS_CSV = UNIVERSE_DIR / "low_price_learning_symbols.csv"
SPOT_SNAPSHOT_LATEST_CSV = DATA_DIR / "cache" / "spot_snapshot_latest.csv"
LIMIT_UP_REVIEW_CACHE = DATA_DIR / "cache" / "limit_up_review_latest.json"
CANDIDATE_LEDGER_CSV = DATA_DIR / "tracking" / "candidate_ledger.csv"
STOCK_THEME_MAP_IMPORT_CSV = UNIVERSE_DIR / "stock_theme_map_import.csv"
LOG = logging.getLogger(__name__)

IMPORT_COLUMNS = [
    "symbol",
    "name",
    "sector",
    "industry",
    "concept_tags",
    "theme_tags",
    "primary_theme",
    "source",
    "updated_at",
    "notes",
]

UNKNOWN_ROW = {
    "sector": "未知板块",
    "industry": "未知行业",
    "concept_tags": "未知概念",
    "theme_tags": "未知题材",
    "primary_theme": "未知题材",
}


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _resolve_path(path: str | Path | None, default: Path) -> Path:
    if path is None or str(path).strip() == "":
        return default
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _resolve_project_csv_path(path: str | Path | None, default: Path) -> Path:
    """Resolve a user-supplied CSV path while keeping writes inside the project.

    This guard is intentionally in the generator module, not only in the API
    layer, so direct calls cannot write arbitrary files via ``..`` traversal or
    absolute paths outside the repository.
    """
    if path is None or str(path).strip() == "":
        candidate = default
    else:
        raw = Path(path)
        candidate = raw if raw.is_absolute() else PROJECT_ROOT / raw
    resolved = candidate.resolve()
    project_root = PROJECT_ROOT.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"output path must stay inside project root: {path}") from exc
    if resolved.suffix.lower() != ".csv":
        raise ValueError(f"output path must be a .csv file: {path}")
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except Exception:
            continue
    return []


def _write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    cleaned_rows = [{col: clean_text(row.get(col)) for col in IMPORT_COLUMNS} for row in rows]
    shared_write_csv_rows(path, cleaned_rows, fieldnames=IMPORT_COLUMNS, encoding="utf-8-sig")


def _add_symbol(rows: dict[str, dict[str, str]], value: Any, name: Any = "", origin: str = "") -> None:
    symbol = normalize_symbol(value)
    if not symbol:
        return
    current = rows.setdefault(symbol, {"symbol": symbol, "name": "", "origin": ""})
    if not current.get("name") and clean_text(name):
        current["name"] = clean_text(name)
    if origin:
        origins = [part for part in current.get("origin", "").split(";") if part]
        if origin not in origins:
            origins.append(origin)
        current["origin"] = ";".join(origins)


def _iter_positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    positions = payload.get("positions")
    if isinstance(positions, dict):
        return [row for row in positions.values() if isinstance(row, dict)]
    if isinstance(positions, list):
        return [row for row in positions if isinstance(row, dict)]
    state = payload.get("portfolio")
    if isinstance(state, dict):
        return _iter_positions(state)
    return []


def collect_current_symbols() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    strategy = _read_json(AUTO_STRATEGY_LATEST_JSON)
    for key, origin in (("long_candidates", "auto_strategy_long"), ("board_candidates", "auto_strategy_board")):
        for item in strategy.get(key) or []:
            if isinstance(item, dict):
                _add_symbol(rows, item.get("symbol") or item.get("code"), item.get("name"), origin)
    latest = _read_json(AUTO_PAPER_PORTFOLIO_LATEST_JSON)
    for item in _iter_positions(latest):
        _add_symbol(rows, item.get("symbol") or item.get("code"), item.get("name"), "auto_paper_latest")
    state = _read_json(AUTO_PAPER_PORTFOLIO_STATE_JSON)
    for item in _iter_positions(state):
        _add_symbol(rows, item.get("symbol") or item.get("code"), item.get("name"), "auto_paper_state")
    return rows


def collect_dashboard_candidate_symbols(limit: int = 18) -> dict[str, dict[str, str]]:
    """Collect the same visible candidate-ledger scope used by the 8000 UI."""
    rows: dict[str, dict[str, str]] = {}
    latest_dates: dict[str, str] = {}
    if CANDIDATE_LEDGER_CSV.exists():
        frame = read_csv_frame(CANDIDATE_LEDGER_CSV, dtype=str)
        for warning in frame.attrs.get("warnings", []):
            LOG.warning("candidate_ledger_csv_warning: path=%s warning=%s", CANDIDATE_LEDGER_CSV, warning)
        if "entry_date" in frame.columns:
            frame["entry_date"] = pd.to_datetime(frame["entry_date"], errors="coerce")
            frame = frame.sort_values("entry_date", ascending=False)
        ledger_rows = [dict(row) for _, row in frame.iterrows()]
    else:
        ledger_rows = []
    for item in ledger_rows:
        symbol = normalize_symbol(item.get("ts_code") or item.get("symbol") or item.get("code"))
        if not symbol:
            continue
        _add_symbol(rows, symbol, item.get("stock_name") or item.get("name"), "dashboard_candidate_ledger")
        latest_dates[symbol] = max(latest_dates.get(symbol, ""), str(item.get("entry_date") or "")[:10])
    ordered_symbols = sorted(rows, key=lambda symbol: latest_dates.get(symbol, ""), reverse=True)
    limited = ordered_symbols[: max(0, int(limit or 18))]
    return {symbol: rows[symbol] for symbol in limited}


def collect_pending_symbols() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for item in _read_csv_rows(STOCK_THEME_MAP_PENDING_CSV):
        _add_symbol(rows, item.get("symbol") or item.get("code"), item.get("name"), "theme_map_pending")
    return rows


def collect_low_price_symbols() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for item in _read_csv_rows(LOW_PRICE_SYMBOLS_CSV):
        if truthy(item.get("included")):
            _add_symbol(rows, item.get("symbol") or item.get("code"), item.get("name"), "low_price_included")
    return rows


def collect_symbols(source: str = "current", limit: int = 0) -> list[dict[str, str]]:
    source = (source or "current").strip().lower()
    if source not in {"current", "pending", "low-price", "all"}:
        source = "current"
    collected: dict[str, dict[str, str]] = {}
    groups: list[dict[str, dict[str, str]]]
    if source == "current":
        groups = [collect_current_symbols(), collect_dashboard_candidate_symbols()]
    elif source == "pending":
        groups = [collect_pending_symbols()]
    elif source == "low-price":
        groups = [collect_low_price_symbols()]
    else:
        groups = [
            collect_current_symbols(),
            collect_dashboard_candidate_symbols(),
            collect_pending_symbols(),
            collect_low_price_symbols(),
        ]
    for group in groups:
        for symbol, row in group.items():
            if symbol not in collected:
                collected[symbol] = row
            else:
                if not collected[symbol].get("name") and row.get("name"):
                    collected[symbol]["name"] = row["name"]
                origins = [part for part in collected[symbol].get("origin", "").split(";") if part]
                for part in row.get("origin", "").split(";"):
                    if part and part not in origins:
                        origins.append(part)
                collected[symbol]["origin"] = ";".join(origins)
    rows = list(collected.values())
    return rows[:limit] if limit and limit > 0 else rows


def _existing_import_by_symbol(path: Path) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for row in _read_csv_rows(path):
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码"))
        if not symbol:
            continue
        item = {col: clean_text(row.get(col)) for col in IMPORT_COLUMNS}
        item["symbol"] = symbol
        for src_key, dst_key in (
            ("代码", "symbol"),
            ("名称", "name"),
            ("板块", "sector"),
            ("行业", "industry"),
            ("概念", "concept_tags"),
            ("题材", "theme_tags"),
            ("主线题材", "primary_theme"),
        ):
            if clean_text(row.get(src_key)) and not clean_text(item.get(dst_key)):
                item[dst_key] = clean_text(row.get(src_key))
        mapping[symbol] = item
    return mapping


def _seed_mapping() -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for row in THEME_MAP_SEED_ROWS:
        item = sanitize_row(row, source="seed")
        item["notes"] = clean_text(row.get("notes"), "本地种子映射，仅用于补足当前观察样本。")
        mapping[item["symbol"]] = item
    return mapping


def _official_rows() -> list[dict[str, str]]:
    return _read_csv_rows(STOCK_THEME_MAP_CSV)


def _limit_up_review_cache_mapping(symbol_rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    if not LIMIT_UP_REVIEW_CACHE.exists():
        return {}
    requested = {row["symbol"] for row in symbol_rows}
    try:
        payload = json.loads(LIMIT_UP_REVIEW_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    mapping: dict[str, dict[str, str]] = {}
    for raw in payload.get("rows") or []:
        if not isinstance(raw, dict):
            continue
        symbol = normalize_symbol(raw.get("symbol") or raw.get("code"))
        if symbol not in requested:
            continue
        industry = clean_text(raw.get("industry") or raw.get("sector") or raw.get("primary_theme"))
        primary_theme = clean_text(raw.get("primary_theme") or industry)
        if not industry and not primary_theme:
            continue
        row = sanitize_row(
            {
                "symbol": symbol,
                "name": raw.get("name"),
                "sector": raw.get("sector") or industry,
                "industry": industry,
                "concept_tags": raw.get("concept_tags") or primary_theme,
                "theme_tags": raw.get("theme_tags") or primary_theme,
                "primary_theme": primary_theme,
                "source": "limit_up_review_cache",
            },
            source="limit_up_review_cache",
        )
        if row_has_mapping(row):
            row["notes"] = "来自本地涨停复盘缓存的 provider 行业归因。"
            mapping[symbol] = row
    return mapping


def _build_fill_mapping(
    symbol_rows: list[dict[str, str]],
    *,
    network: bool = False,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    symbols = [{"symbol": row["symbol"], "name": row.get("name", "")} for row in symbol_rows]
    mapping: dict[str, dict[str, str]] = {}
    provider_results: list[dict[str, Any]] = []

    existing_rows = _official_rows()
    for row in existing_rows:
        symbol = normalize_symbol(row.get("symbol"))
        if symbol and row_has_mapping(row):
            item = sanitize_row(row, source=clean_text(row.get("source"), "stock_theme_map"))
            item["notes"] = "来自现有 stock_theme_map.csv。"
            mapping[symbol] = item
    existing_count = sum(1 for row in symbol_rows if row["symbol"] in mapping)
    provider_results.append({
        "provider": "stock_theme_map",
        "status": "ok" if existing_count else "warning",
        "mapped": existing_count,
        "failed_reason": "" if existing_count else "现有 stock_theme_map.csv 未覆盖当前待补股票。",
    })

    local_mapping, local_result = local_provider(
        symbols,
        spot_path=SPOT_SNAPSHOT_LATEST_CSV,
        low_price_path=LOW_PRICE_SYMBOLS_CSV,
        existing_rows=existing_rows,
    )
    for symbol, row in local_mapping.items():
        if symbol not in mapping and row_has_mapping(row):
            item = sanitize_row(row, source=clean_text(row.get("source"), "local"))
            item["notes"] = "来自本地字段提取。"
            mapping[symbol] = item
    provider_results.append(local_result.to_dict())

    cache_mapping = _limit_up_review_cache_mapping(symbol_rows)
    cache_count = 0
    for symbol, row in cache_mapping.items():
        if symbol not in mapping and row_has_mapping(row):
            mapping[symbol] = row
            cache_count += 1
    provider_results.append({
        "provider": "limit_up_review_cache",
        "status": "ok" if cache_count else "warning",
        "mapped": cache_count,
        "failed_reason": "" if cache_count else "本地涨停复盘缓存未覆盖剩余待补股票。",
        "message": "本地涨停复盘缓存只作为题材归因线索，不代表真实可成交或投资建议。",
    })

    missing_for_per_symbol = [row for row in symbols if row["symbol"] not in mapping]
    if missing_for_per_symbol and network:
        per_mapping, per_result = per_symbol_info_provider(missing_for_per_symbol)
    elif missing_for_per_symbol:
        per_mapping, per_result = {}, None
        provider_results.append({
            "provider": "per_symbol_info",
            "status": "skipped",
            "mapped": 0,
            "failed_reason": "默认不联网；如需使用 akshare 个股基础信息，请走显式联网刷新。",
            "message": "已跳过联网个股基础信息补全。",
        })
    else:
        per_mapping, per_result = {}, None
    for symbol, row in per_mapping.items():
        if symbol not in mapping and row_has_mapping(row):
            item = sanitize_row(row, source="per_symbol_info")
            item["notes"] = "来自 akshare 个股基础信息接口。"
            mapping[symbol] = item
    if per_result is not None:
        provider_results.append(per_result.to_dict())

    seed = _seed_mapping()
    seed_count = 0
    for row in symbol_rows:
        symbol = row["symbol"]
        if symbol not in mapping and symbol in seed:
            mapping[symbol] = seed[symbol]
            seed_count += 1
    provider_results.append({
        "provider": "seed",
        "status": "ok" if seed_count else "warning",
        "mapped": seed_count,
        "failed_reason": "" if seed_count else "本地 seed 未覆盖剩余未知股票。",
        "message": "本地种子映射仅用于补足当前观察样本。",
    })
    return mapping, provider_results


def _draft_row(symbol_row: dict[str, str], mapping: dict[str, dict[str, str]], now: str) -> dict[str, str]:
    symbol = symbol_row["symbol"]
    if symbol in mapping and row_has_mapping(mapping[symbol]):
        source = clean_text(mapping[symbol].get("source"), "draft")
        row = {col: clean_text(mapping[symbol].get(col)) for col in IMPORT_COLUMNS}
        row["symbol"] = symbol
        row["name"] = clean_text(row.get("name"), clean_text(symbol_row.get("name"), symbol))
        row["source"] = source
        row["updated_at"] = now
        row["notes"] = clean_text(row.get("notes"), f"自动草稿填充；来源：{source}；原始队列：{symbol_row.get('origin', '')}")
        return row
    row = {
        "symbol": symbol,
        "name": clean_text(symbol_row.get("name"), symbol),
        **UNKNOWN_ROW,
        "source": "pending_manual",
        "updated_at": now,
        "notes": f"自动生成待补草稿；未找到可靠板块/题材来源；原始队列：{symbol_row.get('origin', '')}",
    }
    return row


def generate_import_draft(
    *,
    source: str = "current",
    overwrite: bool = False,
    network: bool = False,
    limit: int = 0,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    output = _resolve_project_csv_path(output_path, STOCK_THEME_MAP_IMPORT_CSV)
    symbols = collect_symbols(source=source, limit=limit)
    existing_import = _existing_import_by_symbol(output)
    now = now_text()
    fill_mapping, provider_results = _build_fill_mapping(symbols, network=network)

    rows: list[dict[str, str]] = []
    filled_count = 0
    pending_manual_count = 0
    preserved_count = 0
    seen: set[str] = set()
    for symbol_row in symbols:
        symbol = symbol_row["symbol"]
        seen.add(symbol)
        if symbol in existing_import and not overwrite:
            row = {col: clean_text(existing_import[symbol].get(col)) for col in IMPORT_COLUMNS}
            row["symbol"] = symbol
            row["name"] = clean_text(row.get("name"), clean_text(symbol_row.get("name"), symbol))
            preserved_count += 1
        else:
            row = _draft_row(symbol_row, fill_mapping, now)
        if row.get("source") == "pending_manual" or not row_has_mapping(row):
            pending_manual_count += 1
        else:
            filled_count += 1
        rows.append(row)

    if output.exists() and not overwrite:
        for symbol, row in existing_import.items():
            if symbol not in seen:
                rows.append(row)

    _write_csv_rows(output, rows)
    return {
        "ok": True,
        "path": _display_path(output),
        "absolute_path": str(output),
        "source": source,
        "overwrite": bool(overwrite),
        "network": bool(network),
        "total": len(symbols),
        "written_rows": len(rows),
        "filled_count": filled_count,
        "pending_manual_count": pending_manual_count,
        "preserved_count": preserved_count,
        "provider_results": provider_results,
        "message": "已生成板块/题材导入草稿。",
        "disclaimer": "仅用于本地板块/题材映射补全，不接券商、不真实下单、不构成投资建议。",
    }


def preview_import_draft(limit: int = 20, path: str | Path | None = None) -> dict[str, Any]:
    source = _resolve_path(path, STOCK_THEME_MAP_IMPORT_CSV)
    rows = _read_csv_rows(source)
    return {
        "ok": True,
        "exists": source.exists(),
        "path": _display_path(source),
        "total": len(rows),
        "rows": rows[: max(0, int(limit or 20))],
        "message": "导入草稿预览只读，不会生成文件、不会联网、不会导入正式映射。",
    }
