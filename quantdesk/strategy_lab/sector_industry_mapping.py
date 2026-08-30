from __future__ import annotations

from typing import Any

UNKNOWN_MARKERS = {"", "nan", "none", "null", "unknown", "未知", "未知行业", "未知板块", "未知题材", "未知概念", "待分类"}


def _meaningful_sector(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.lower() not in UNKNOWN_MARKERS and text not in UNKNOWN_MARKERS


def sector_mapping_quality(stock_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    symbols = sorted({row.get("symbol") or row.get("stock_symbol") for row in stock_rows if row.get("symbol") or row.get("stock_symbol")})
    mapped_symbols = {
        row.get("symbol") or row.get("stock_symbol")
        for row in stock_rows
        if row.get("sector_mapping_source") == "stock_theme_map" and _meaningful_sector(row.get("sector_name"))
        or (
            _meaningful_sector(row.get("sector_name"))
            and "proxy" not in str(row.get("risk_flags") or "").lower()
            and "proxy" not in str(row.get("sector_name") or "").lower()
        )
    }
    unmapped = [
        {"symbol": s, "reason": "formal sector map missing; proxy sector may be Unknown"}
        for s in symbols
        if s not in mapped_symbols
    ]
    mapped_count = sum(1 for row in stock_rows if (row.get("symbol") or row.get("stock_symbol")) in mapped_symbols)
    total = len(stock_rows)
    payload = {
        "task_id": "v421_sector_mapping_quality",
        "status": "completed" if total and mapped_count == total else "completed_with_mapping_gaps",
        "total_stock_rows": total,
        "mapped_rows": mapped_count,
        "mapping_coverage": mapped_count / total if total else 0.0,
        "unmapped_symbol_count": len(unmapped),
        "limitation_note": "No formal industry map is fabricated; Unknown/proxy mapping is recorded.",
    }
    return payload, unmapped[:500]
