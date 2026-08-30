"""Validate board/theme mapping coverage for current candidates and holdings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quantdesk.candidate_contract import (
    attach_theme_map_to_candidates,
    candidate_rows_from_strategy_payload,
    summarize_candidate_theme_coverage,
)
from quantdesk.shared import normalize_symbol
from quantdesk.theme_sector_engine import THEME_MAP_PATH, load_theme_map
from quantdesk.utils import read_json_safe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTO_STRATEGY_LATEST_JSON = PROJECT_ROOT / "results" / "auto_strategy_latest.json"
AUTO_PAPER_PORTFOLIO_LATEST_JSON = PROJECT_ROOT / "results" / "auto_paper_portfolio_latest.json"
AUTO_PAPER_PORTFOLIO_STATE_JSON = PROJECT_ROOT / "state" / "auto_paper_portfolio.json"


def _add(
    rows: dict[str, dict[str, str]],
    symbol_value: Any,
    name_value: Any = "",
    *,
    sector_value: Any = "",
    industry_value: Any = "",
    theme_value: Any = "",
) -> None:
    symbol = normalize_symbol(symbol_value)
    if not symbol:
        return
    current = rows.setdefault(symbol, {"symbol": symbol, "name": ""})
    if not current.get("name") and str(name_value or "").strip():
        current["name"] = str(name_value or "").strip()
    for key, value in (
        ("sector", sector_value),
        ("industry", industry_value),
        ("primary_theme", theme_value),
        ("theme", theme_value),
    ):
        if not current.get(key) and str(value or "").strip():
            current[key] = str(value or "").strip()


def _iter_positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    positions = payload.get("positions")
    if isinstance(positions, dict):
        return [row for row in positions.values() if isinstance(row, dict)]
    if isinstance(positions, list):
        return [row for row in positions if isinstance(row, dict)]
    nested = payload.get("portfolio")
    if isinstance(nested, dict):
        return _iter_positions(nested)
    return []


def current_candidate_symbols() -> list[dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    strategy = read_json_safe(AUTO_STRATEGY_LATEST_JSON, {})
    for item in candidate_rows_from_strategy_payload(strategy if isinstance(strategy, dict) else {}, strategy="all"):
        _add(
            rows,
            item.get("symbol") or item.get("code"),
            item.get("name"),
            sector_value=item.get("sector"),
            industry_value=item.get("industry"),
            theme_value=item.get("primary_theme") or item.get("theme") or item.get("theme_tags"),
        )
    for path in (AUTO_PAPER_PORTFOLIO_LATEST_JSON, AUTO_PAPER_PORTFOLIO_STATE_JSON):
        payload = read_json_safe(path, {})
        for item in _iter_positions(payload):
            _add(
                rows,
                item.get("symbol") or item.get("code"),
                item.get("name"),
                sector_value=item.get("sector"),
                industry_value=item.get("industry"),
                theme_value=item.get("primary_theme") or item.get("theme") or item.get("theme_tags"),
            )
    return list(rows.values())


def validate_theme_map_for_current_candidates(path: Path | None = None) -> dict[str, Any]:
    source = path or THEME_MAP_PATH
    candidates = current_candidate_symbols()
    mapping = load_theme_map(source)
    enriched = attach_theme_map_to_candidates([dict(item) for item in candidates], mapping)
    coverage = summarize_candidate_theme_coverage(enriched)
    missing = coverage["missing_symbols"]
    mapped = int(coverage["mapped_count"])
    total = len(candidates)

    # Distinguish two very different reasons a candidate is "unmapped":
    #  - pending: the symbol IS present in stock_theme_map.csv, but only as a
    #    placeholder row (未知板块/source=pending) whose classification has not
    #    been filled yet (filling needs a network refresh). The symbol is known.
    #  - absent: the symbol is not in the map at all (genuine symbol mismatch or
    #    the map has never been generated for it).
    # Collapsing both into "symbol 不匹配" is misleading, so we surface them
    # separately and keep the degraded copy honest without inventing data.
    unmapped = set(missing)
    pending_symbols = [
        symbol for symbol in (normalize_symbol(item.get("symbol")) for item in candidates)
        if symbol and symbol in unmapped and symbol in mapping
    ]
    absent_symbols = [
        symbol for symbol in (normalize_symbol(item.get("symbol")) for item in candidates)
        if symbol and symbol in unmapped and symbol not in mapping
    ]

    # Message parts appended to the base "已映射 M / N" line so the daily
    # warning is self-explanatory: N pending (in map, classification 待补) vs
    # N absent (symbol not in map at all). No data is invented; this only
    # clarifies WHY a candidate is unmapped.
    message_suffix = ""
    if pending_symbols and absent_symbols:
        message_suffix = f"（待补 {len(pending_symbols)}，未匹配 {len(absent_symbols)}）"
    elif pending_symbols:
        message_suffix = f"（{len(pending_symbols)} 只待补，需联网刷新题材映射）"
    elif absent_symbols:
        message_suffix = f"（{len(absent_symbols)} 只 symbol 尚未进入映射）"

    if not total:
        reason = "当前没有可校验的候选或自动虚拟组合持仓。"
    elif mapped == total:
        reason = "当前候选/持仓已能匹配 stock_theme_map.csv。"
    elif not source.exists():
        reason = "stock_theme_map.csv 不存在，请先生成导入草稿并导入正式映射。"
    elif pending_symbols and not absent_symbols:
        reason = (
            "当前候选已在 stock_theme_map.csv 中，但板块/题材分类待补（pending 占位）；"
            "需联网刷新题材映射后才会显示具体板块。"
        )
    elif pending_symbols and absent_symbols:
        reason = (
            "部分候选板块分类待补（pending 占位），部分候选 symbol 尚未进入映射；"
            "待补部分需联网刷新，未匹配部分需先生成导入草稿。"
        )
    else:
        reason = "当前候选尚未导入正式映射、symbol 不匹配，或策略沙盒结果未刷新。"

    # Keep the one-line warning honest: separate "pending"（symbol 已在映射、
    # 分类待补）from "absent"（symbol 未进入映射）instead of lumping both into
    # a bare unmapped count.
    suffix_parts: list[str] = []
    if pending_symbols:
        suffix_parts.append(f"待补 {len(pending_symbols)} 只（已在映射，分类待联网刷新）")
    if absent_symbols:
        suffix_parts.append(f"未匹配 {len(absent_symbols)} 只（symbol 未进入映射）")
    message_suffix = ("；" + "，".join(suffix_parts) + "。") if suffix_parts else ""
    return {
        "ok": True,
        "candidate_count": total,
        "mapped_current_candidates": mapped,
        "unknown_current_candidates": max(0, total - mapped),
        "pending_current_candidates": len(pending_symbols),
        "absent_current_candidates": len(absent_symbols),
        "missing_symbols": missing,
        "pending_symbols": pending_symbols,
        "absent_symbols": absent_symbols,
        "path": str(source),
        "reason": reason,
        "message": f"当前候选板块映射：已映射 {mapped} / {total}。{message_suffix}",
    }
