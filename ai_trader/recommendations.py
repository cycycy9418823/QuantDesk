"""Read QuantDesk's daily recommendation pool — read-only, never authoritative.

The AI trader may *consider* the 8000 research desk's candidates next to its own
watchlist, but it still decides for itself: a recommendation is an input, never
an order. This module only reads; it never writes to ``results/`` and never
touches the QuantDesk paper ledger.

Missing, stale, or malformed files degrade to an empty list plus an explicit
warning. A recommendation is never fabricated, and a pick is never labelled as
coming from the research desk unless it actually did.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RESULTS_DIR = Path("results")
LONG_POOL = "auto_long_candidates_latest.json"
BOARD_POOL = "auto_board_candidates_latest.json"
SCHEMA = "ai_trader_recommendation_view_v1"


def _coerce_symbol(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = text.split(".")[0].strip()
    return digits.zfill(6) if digits.isdigit() and len(digits) <= 6 else ""


def _read_pool(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], [f"recommendation_pool_missing:{path.name}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], [f"recommendation_pool_unreadable:{path.name}:{type(exc).__name__}"]
    if not isinstance(payload, dict):
        return [], [f"recommendation_pool_invalid:{path.name}"]
    items = payload.get("items")
    if not isinstance(items, list):
        return [], [f"recommendation_pool_no_items:{path.name}"]
    rows: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        symbol = _coerce_symbol(row.get("symbol"))
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": str(row.get("name") or ""),
                "rank": row.get("rank"),
                "score": row.get("score"),
                "strategy_label": str(row.get("strategy_label") or ""),
                "reason": str(row.get("reason") or ""),
                "sector": str(row.get("sector") or row.get("industry") or ""),
                "theme": str(row.get("primary_theme") or row.get("theme") or ""),
                "data_latest_date": str(row.get("data_latest_date") or ""),
                "is_target_date_covered": row.get("is_target_date_covered"),
                "pool": path.name,
            }
        )
    return rows, []


def _rank_key(row: dict[str, Any]) -> tuple[float, float]:
    try:
        rank = float(row.get("rank")) if row.get("rank") is not None else 9e9
    except (TypeError, ValueError):
        rank = 9e9
    try:
        score = -float(row.get("score")) if row.get("score") is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    return (rank, score)


def load_recommendations(
    results_dir: Path | str | None = None,
    *,
    pools: Iterable[str] = (LONG_POOL, BOARD_POOL),
    limit: int = 12,
    require_fresh_for: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, de-duplicated view of the research desk's picks.

    ``require_fresh_for`` is an optional ``YYYY-MM-DD``; rows whose
    ``data_latest_date`` is older are kept but flagged ``stale``, so the trader
    can see them and decide, instead of silently trusting an old pick.
    """

    root = Path(results_dir) if results_dir is not None else DEFAULT_RESULTS_DIR
    warnings: list[str] = []
    merged: dict[str, dict[str, Any]] = {}
    for pool in pools:
        rows, pool_warnings = _read_pool(root / pool)
        warnings.extend(pool_warnings)
        for row in rows:
            existing = merged.get(row["symbol"])
            if existing is None or _rank_key(row) < _rank_key(existing):
                merged[row["symbol"]] = row
    ordered = sorted(merged.values(), key=_rank_key)
    if require_fresh_for:
        cutoff = str(require_fresh_for)[:10]
        for row in ordered:
            latest = str(row.get("data_latest_date") or "")[:10]
            row["stale"] = bool(latest and latest < cutoff)
    bounded = ordered[: max(int(limit), 0)]
    if not bounded and not warnings:
        warnings.append("recommendation_pool_empty")
    return {
        "schema_version": SCHEMA,
        "symbols": [row["symbol"] for row in bounded],
        "rows": bounded,
        "warnings": warnings,
        "boundary": {"read_only": True, "advisory_only": True, "is_order": False},
    }


def recommendation_note(rows: Iterable[dict[str, Any]], symbol: str) -> str:
    """One short human line explaining a research-desk pick, for the journal."""

    code = _coerce_symbol(symbol)
    for row in rows:
        if row.get("symbol") != code:
            continue
        bits = [f"研究台推荐（{row.get('strategy_label') or '推荐'}"]
        if row.get("rank") is not None:
            bits.append(f"第{row['rank']}名")
        bits.append("）")
        head = "".join(bits[:1] + ([bits[1]] if len(bits) > 2 else []) + [bits[-1]])
        extra = str(row.get("reason") or "").strip()
        theme = str(row.get("theme") or "").strip()
        if theme:
            head += f" 题材：{theme}。"
        if extra:
            head += f" 理由：{extra}"
        if row.get("stale"):
            head += "（该推荐的数据日期偏旧，仅作线索。）"
        return head.strip()
    return ""


__all__ = [
    "BOARD_POOL",
    "LONG_POOL",
    "SCHEMA",
    "load_recommendations",
    "recommendation_note",
]
