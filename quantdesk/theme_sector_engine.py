"""Local theme / sector mapping for learning-universe views.

The mapping is intentionally local and optional.  Missing theme information
must never block paper simulation; it only improves grouping and explanation.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.shared import normalize_symbol, truthy
from quantdesk.utils import atomic_write_json, safe_float

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THEME_MAP_PATH = PROJECT_ROOT / "data" / "universe" / "stock_theme_map.csv"
THEME_PENDING_PATH = PROJECT_ROOT / "data" / "universe" / "stock_theme_map_pending.csv"
LOW_PRICE_UNIVERSE_PATH = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
SPOT_SNAPSHOT_PATH = PROJECT_ROOT / "data" / "cache" / "spot_snapshot_latest.csv"
THEME_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "stock_theme_map_template.csv"
THEME_SECTOR_SUMMARY_JSON = PROJECT_ROOT / "results" / "theme_sector_summary_latest.json"
AUTO_STRATEGY_LATEST_JSON = PROJECT_ROOT / "results" / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = PROJECT_ROOT / "results" / "auto_paper_portfolio_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = PROJECT_ROOT / "state" / "auto_paper_portfolio.json"

THEME_COLUMNS = [
    "symbol",
    "name",
    "sector",
    "industry",
    "concept_tags",
    "theme_tags",
    "primary_theme",
    "source",
    "updated_at",
]

UNKNOWN_THEME = {
    "sector": "未知板块",
    "industry": "未知行业",
    "concept_tags": "",
    "theme_tags": "",
    "primary_theme": "未知题材",
    "theme_confidence": "unknown",
}


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_theme_rows(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=THEME_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: str(row.get(key) or "") for key in THEME_COLUMNS})
    os.replace(tmp, path)
    return path


def included_low_price_rows(
    path: Path | None = None,
    *,
    only_included: bool = True,
    limit: int = 0,
) -> list[dict[str, str]]:
    source = path or LOW_PRICE_UNIVERSE_PATH
    rows = []
    for row in _read_csv_dicts(source):
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if not symbol:
            continue
        if only_included and "included" in row and not truthy(row.get("included")):
            continue
        item = {str(key): str(value or "").strip() for key, value in row.items()}
        item["symbol"] = symbol
        rows.append(item)
        if limit and len(rows) >= limit:
            break
    return rows


def _first_text(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _merge_tags(*values: str) -> str:
    tags: list[str] = []
    for value in values:
        normalized = str(value or "").replace("，", ";").replace(",", ";").replace("|", ";")
        for part in normalized.split(";"):
            tag = part.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return ";".join(tags)


def _theme_row_from_sources(symbol: str, name: str, low_row: dict[str, Any], spot_row: dict[str, Any]) -> dict[str, Any]:
    sector = _first_text(low_row, ["sector", "板块", "market_sector"]) or _first_text(spot_row, ["sector", "板块", "市场板块"])
    industry = _first_text(low_row, ["industry", "行业"]) or _first_text(spot_row, ["industry", "行业", "所属行业"])
    concept_tags = _merge_tags(
        _first_text(low_row, ["concept_tags", "concept", "概念", "概念标签"]),
        _first_text(spot_row, ["concept_tags", "concept", "概念", "概念标签"]),
    )
    theme_tags = _merge_tags(
        _first_text(low_row, ["theme_tags", "theme", "题材", "主题"]),
        _first_text(spot_row, ["theme_tags", "theme", "题材", "主题"]),
    )
    primary_theme = (
        _first_text(low_row, ["primary_theme", "主线题材", "主题"])
        or _first_text(spot_row, ["primary_theme", "主线题材", "主题"])
        or (theme_tags.split(";")[0] if theme_tags else "")
        or (concept_tags.split(";")[0] if concept_tags else "")
        or industry
        or sector
        or UNKNOWN_THEME["primary_theme"]
    )
    return {
        "symbol": symbol,
        "name": name,
        "sector": sector or UNKNOWN_THEME["sector"],
        "industry": industry or UNKNOWN_THEME["industry"],
        "concept_tags": concept_tags,
        "theme_tags": theme_tags,
        "primary_theme": primary_theme,
        "source": "local",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def create_theme_template(path: Path | None = None) -> Path:
    target = path or THEME_TEMPLATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=THEME_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "symbol": "601778",
                "name": "示例股票",
                "sector": "电力设备",
                "industry": "光伏设备",
                "concept_tags": "低价观察;新能源",
                "theme_tags": "光伏;绿色电力",
                "primary_theme": "光伏",
                "source": "manual",
                "updated_at": datetime.now().date().isoformat(),
            }
        )
    return target


# Round 21 F811 cleanup: the original implementation here was dead code
# fully shadowed by the v2.8.5/v2.8.6 overrides below and was removed.


def theme_rows_from_map(mapping: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for symbol in sorted(mapping):
        row = {key: mapping[symbol].get(key, "") for key in THEME_COLUMNS}
        row["symbol"] = normalize_symbol(row.get("symbol") or symbol)
        rows.append(row)
    return rows


def build_local_theme_rows(
    *,
    low_price_path: Path | None = None,
    spot_path: Path | None = None,
    existing_path: Path | None = None,
    overwrite_manual: bool = False,
    only_low_price: bool = True,
    limit: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    low_rows = included_low_price_rows(low_price_path, only_included=only_low_price, limit=limit)
    spot_rows = {
        normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码")): row
        for row in _read_csv_dicts(spot_path or SPOT_SNAPSHOT_PATH)
    }
    existing = load_theme_map(existing_path or THEME_MAP_PATH)
    rows: list[dict[str, Any]] = []
    for low_row in low_rows:
        symbol = normalize_symbol(low_row.get("symbol") or low_row.get("code"))
        if not symbol:
            continue
        current = existing.get(symbol, {})
        if current.get("source") == "manual" and not overwrite_manual:
            rows.append({key: current.get(key, "") for key in THEME_COLUMNS})
            continue
        name = _first_text(low_row, ["name", "名称", "股票名称"]) or _first_text(spot_rows.get(symbol, {}), ["name", "名称", "股票名称"]) or current.get("name") or symbol
        local_row = _theme_row_from_sources(symbol, name, low_row, spot_rows.get(symbol, {}))
        local_has_data = _row_has_theme_data(local_row)
        if current:
            for key in ("sector", "industry", "primary_theme"):
                if _is_unknown_value(key, local_row.get(key)) and not _is_unknown_value(key, current.get(key)):
                    local_row[key] = current.get(key, "")
            for key in ("concept_tags", "theme_tags"):
                if not str(local_row.get(key) or "").strip() and str(current.get(key) or "").strip():
                    local_row[key] = current.get(key, "")
            if not local_has_data and _row_has_theme_data(current):
                local_row["source"] = current.get("source") or "local"
                local_row["updated_at"] = current.get("updated_at") or local_row["updated_at"]
        if not _row_has_theme_data(local_row):
            local_row["source"] = "pending"
        rows.append(local_row)
    summary = {
        "total_low_price": len(low_rows),
        "row_count": len(rows),
        "manual_count": sum(1 for row in rows if row.get("source") == "manual"),
        "local_count": sum(1 for row in rows if row.get("source") == "local"),
        "unknown_count": sum(1 for row in rows if _row_needs_pending(row)),
    }
    return rows, summary


def build_pending_theme_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = []
    for row in rows:
        if _row_needs_pending(row):
            item = {key: row.get(key, "") for key in THEME_COLUMNS}
            item["sector"] = item.get("sector") if not _is_unknown_value("sector", item.get("sector")) else UNKNOWN_THEME["sector"]
            item["industry"] = item.get("industry") if not _is_unknown_value("industry", item.get("industry")) else UNKNOWN_THEME["industry"]
            item["concept_tags"] = item.get("concept_tags") or UNKNOWN_THEME["primary_theme"]
            item["theme_tags"] = item.get("theme_tags") or UNKNOWN_THEME["primary_theme"]
            item["primary_theme"] = item.get("primary_theme") if not _is_unknown_value("primary_theme", item.get("primary_theme")) else UNKNOWN_THEME["primary_theme"]
            item["source"] = "pending"
            pending.append(item)
    return pending


# Round 21 F811 cleanup: the original implementation here was dead code
# fully shadowed by the v2.8.5/v2.8.6 overrides below and was removed.


def summarize_theme_sector(
    universe_items: list[dict[str, Any]],
    long_candidates: list[dict[str, Any]],
    board_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    long_symbols = {normalize_symbol(row.get("symbol")) for row in long_candidates}
    board_symbols = {normalize_symbol(row.get("symbol")) for row in board_candidates}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in universe_items:
        symbol = normalize_symbol(item.get("symbol"))
        theme = str(item.get("primary_theme") or UNKNOWN_THEME["primary_theme"])
        sector = str(item.get("sector") or UNKNOWN_THEME["sector"])
        key = (theme, sector)
        row = grouped.setdefault(
            key,
            {
                "primary_theme": theme,
                "sector": sector,
                "low_price_count": 0,
                "simulation_ready_count": 0,
                "long_candidate_count": 0,
                "board_candidate_count": 0,
                "amount_ma20_values": [],
                "limit_up_like_count": 0,
                "symbols": [],
            },
        )
        row["low_price_count"] += 1
        if item.get("simulation_ready"):
            row["simulation_ready_count"] += 1
        if symbol in long_symbols:
            row["long_candidate_count"] += 1
        if symbol in board_symbols:
            row["board_candidate_count"] += 1
        amount_ma20 = safe_float(item.get("amount_ma20"), None)
        if amount_ma20 is not None:
            row["amount_ma20_values"].append(amount_ma20)
        if safe_float(item.get("close"), 0.0) and safe_float(item.get("prev_close"), 0.0):
            prev_close = safe_float(item.get("prev_close"), 0.0) or 0.0
            close = safe_float(item.get("close"), 0.0) or 0.0
            if prev_close > 0 and close / prev_close - 1 >= 0.075:
                row["limit_up_like_count"] += 1
        row["symbols"].append(symbol)

    rows: list[dict[str, Any]] = []
    for row in grouped.values():
        values = row.pop("amount_ma20_values", [])
        avg_amount = sum(values) / len(values) if values else None
        heat_score = (
            row["simulation_ready_count"] * 2
            + row["long_candidate_count"] * 3
            + row["board_candidate_count"] * 4
            + row["limit_up_like_count"] * 2
        )
        rows.append(
            {
                **row,
                "average_amount_ma20": round(avg_amount, 2) if avg_amount is not None else None,
                "theme_heat_score": round(float(heat_score), 2),
                "note": "本地题材映射仅用于学习分组，不构成投资建议。",
            }
        )
    rows.sort(key=lambda x: (x.get("theme_heat_score") or 0, x.get("simulation_ready_count") or 0), reverse=True)
    return {
        "ok": True,
        "exists": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": rows,
        "message": "题材板块汇总已生成；缺少映射的股票归为未知板块/未知题材。",
    }


def save_theme_sector_summary(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or THEME_SECTOR_SUMMARY_JSON
    atomic_write_json(target, payload)
    return target


# v2.8.5 compatibility overrides: use the stricter theme-map cleaner/counting
# from theme_map_updater so API status, strategy enrichment, and previews agree.
from quantdesk.theme_map_updater import (  # noqa: E402
    UNKNOWN_CONCEPT as _V285_UNKNOWN_CONCEPT,
)
from quantdesk.theme_map_updater import (
    UNKNOWN_INDUSTRY as _V285_UNKNOWN_INDUSTRY,
)
from quantdesk.theme_map_updater import (
    UNKNOWN_SECTOR as _V285_UNKNOWN_SECTOR,
)
from quantdesk.theme_map_updater import (
    UNKNOWN_THEME as _V285_UNKNOWN_THEME,
)
from quantdesk.theme_map_updater import (
    _is_unknown_text_v285,
    _read_theme_rows_v285,
    _row_has_mapping_v285,
    _sanitize_theme_row_v285,
    _theme_map_stats_v285,
)

UNKNOWN_THEME = {
    "sector": _V285_UNKNOWN_SECTOR,
    "industry": _V285_UNKNOWN_INDUSTRY,
    "concept_tags": _V285_UNKNOWN_CONCEPT,
    "theme_tags": _V285_UNKNOWN_THEME,
    "primary_theme": _V285_UNKNOWN_THEME,
    "theme_confidence": "unknown",
}


def _is_unknown_value(key: str, value: Any) -> bool:  # type: ignore[override]
    return _is_unknown_text_v285(value)


def _row_needs_pending(row: dict[str, Any]) -> bool:  # type: ignore[override]
    return not _row_has_mapping_v285(row)


def _row_has_theme_data(row: dict[str, Any]) -> bool:  # type: ignore[override]
    return _row_has_mapping_v285(row)


def load_theme_map(path: Path | None = None) -> dict[str, dict[str, Any]]:  # type: ignore[override]
    source = path or THEME_MAP_PATH
    rows: dict[str, dict[str, Any]] = {}
    for row in _read_theme_rows_v285(source):
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        item = _sanitize_theme_row_v285(row)
        item["theme_confidence"] = "mapped" if _row_has_mapping_v285(item) else "unknown"
        rows[symbol] = item
    return rows


# Round 21 F811 cleanup: the original implementation here was dead code
# fully shadowed by the v2.8.5/v2.8.6 overrides below and was removed.


def get_theme_for_symbol(symbol: str, theme_map: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:  # type: ignore[override]
    mapping = theme_map if theme_map is not None else load_theme_map()
    found = mapping.get(normalize_symbol(symbol))
    if found:
        return dict(found)
    return {"symbol": normalize_symbol(symbol), **UNKNOWN_THEME}


def apply_theme_to_item(item: dict[str, Any], theme_map: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:  # type: ignore[override]
    enriched = dict(item)
    theme = get_theme_for_symbol(str(enriched.get("symbol") or ""), theme_map)
    for key in ["sector", "industry", "concept_tags", "theme_tags", "primary_theme", "theme_confidence"]:
        value = enriched.get(key)
        enriched[key] = theme.get(key) if _is_unknown_text_v285(value) else value
        if _is_unknown_text_v285(enriched.get(key)):
            enriched[key] = UNKNOWN_THEME.get(key, "")
    return enriched


# v2.8.6: expose provider fallback diagnostics from theme_map_updater.
from quantdesk.theme_map_updater import (
    theme_map_preview as _theme_map_preview_v286,
)
from quantdesk.theme_map_updater import (  # noqa: E402
    theme_map_status as _theme_map_status_v286,
)
from quantdesk.theme_map_updater import (
    update_theme_map as _update_theme_map_v286,
)


def update_stock_theme_map(  # type: ignore[override]
    *,
    source: str = "auto",
    overwrite_manual: bool = False,
    only_low_price: bool = True,
    output_path: Path | None = None,
    pending_output_path: Path | None = None,
    low_price_path: Path | None = None,
    spot_path: Path | None = None,
    limit: int = 0,
    input_path: Path | None = None,
) -> dict[str, Any]:
    return _update_theme_map_v286(
        source=source,
        overwrite_manual=overwrite_manual,
        only_low_price=only_low_price,
        output_path=output_path,
        pending_path=pending_output_path,
        limit=limit,
        input_path=input_path,
    )


def theme_map_status(path: Path | None = None, pending_path: Path | None = None) -> dict[str, Any]:  # type: ignore[override]
    status = _theme_map_status_v286(path or THEME_MAP_PATH)
    if pending_path is not None:
        rows = _read_theme_rows_v285(path or THEME_MAP_PATH)
        pending_rows = _read_theme_rows_v285(pending_path)
        stats = _theme_map_stats_v285(rows, pending_rows)
        status.update({
            "pending_exists": pending_path.exists(),
            "pending_path": str(pending_path),
            "pending_count": stats["pending_count"],
        })
    return status


def theme_map_preview(path: Path | None = None, limit: int = 20) -> dict[str, Any]:  # type: ignore[override]
    source = path or THEME_MAP_PATH
    rows = _theme_map_preview_v286(limit)
    if source != THEME_MAP_PATH:
        rows = _read_theme_rows_v285(source)[: max(0, int(limit or 20))]
    return {
        "ok": True,
        "exists": source.exists(),
        "path": str(source),
        "rows": rows,
        "limit": limit,
        "total_rows": len(_read_theme_rows_v285(source)),
        "message": "板块/题材映射预览只读，不会触发联网。",
    }
