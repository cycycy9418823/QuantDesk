"""Canonical candidate contract shared by sandbox, plans, and workflow."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from quantdesk.shared import ensure_list, normalize_symbol, safe_float, safe_int

STRATEGY_LABELS = {
    "long": "长线",
    "board": "打板",
    "long_swing": "长线波段",
    "board_momentum": "打板动量",
}

UNKNOWN_SECTOR = "未知板块"
UNKNOWN_INDUSTRY = "未知行业"
UNKNOWN_THEME = "未知题材"
UNKNOWN_CONCEPT = "未知概念"

INTERNAL_UNKNOWN = {
    "",
    "-",
    "nan",
    "none",
    "null",
    "<na>",
    "unknown",
    "pending",
    "pending_manual",
    "待补",
    "未知",
    "未知板块",
    "未知行业",
    "未知题材",
    "未知概念",
    "鏈煡",
    "鏈煡鏉垮潡",
    "鏈煡琛屼笟",
    "鏈煡棰樻潗",
    "鏈煡姒傚康",
}


def clean_display_text(value: Any, unknown: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in INTERNAL_UNKNOWN or text in INTERNAL_UNKNOWN:
        return unknown
    return text


def first_clean_display_text(*values: Any, unknown: str = "") -> str:
    """Return the first non-placeholder display value.

    Theme-map rows may contain pending/unknown placeholders while the source
    candidate or paper-position row already has a useful local sector/industry.
    A plain ``mapped.get("sector") or item.get("sector")`` keeps the truthy
    placeholder and hides the better local value, so use this helper whenever a
    mapped value should be allowed to degrade to a local fallback.
    """

    for value in values:
        cleaned = clean_display_text(value, "")
        if cleaned:
            return cleaned
    return unknown


def normalize_strategy_type(value: Any, default: str | None = None) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "long_virtual_entry_plan": "long",
        "long_candidate": "long",
        "longswing": "long_swing",
        "swing": "long_swing",
        "board_virtual_entry_plan": "board",
        "board_candidate": "board",
        "打板": "board",
        "长线": "long",
        "長線": "long",
    }
    text = aliases.get(text, text)
    if text in STRATEGY_LABELS:
        return text
    return default or "long"


def strategy_label(strategy_type: Any) -> str:
    return STRATEGY_LABELS.get(normalize_strategy_type(strategy_type), STRATEGY_LABELS["long"])


def _score(row: dict[str, Any]) -> float | None:
    for key in ("score", "long_score", "board_score", "rank_score", "composite_score"):
        number = safe_float(row.get(key), None)
        if number is not None:
            return number
    return None


def normalize_candidate_row(row: dict[str, Any], *, default_strategy_type: str | None = None) -> dict[str, Any]:
    raw = dict(row or {})
    strategy_type = normalize_strategy_type(raw.get("strategy_type") or raw.get("strategy"), default_strategy_type)
    symbol = normalize_symbol(raw.get("symbol") or raw.get("code") or raw.get("代码"))
    name = clean_display_text(raw.get("name") or raw.get("名称"), symbol)
    theme = clean_display_text(raw.get("theme") or raw.get("primary_theme") or raw.get("theme_tags"), UNKNOWN_THEME)
    item = {
        "symbol": symbol,
        "name": name,
        "strategy_type": strategy_type,
        "strategy_label": STRATEGY_LABELS.get(strategy_type, strategy_type),
        "candidate_source": raw.get("candidate_source") or raw.get("source") or "auto_strategy_sandbox",
        "rank": safe_int(raw.get("rank"), None),
        "score": _score(raw),
        "reason": str(raw.get("reason") or raw.get("signal_reason") or ""),
        "sector": clean_display_text(raw.get("sector"), UNKNOWN_SECTOR),
        "industry": clean_display_text(raw.get("industry"), UNKNOWN_INDUSTRY),
        "theme": theme,
        "primary_theme": clean_display_text(raw.get("primary_theme") or theme, UNKNOWN_THEME),
        "data_latest_date": str(raw.get("data_latest_date") or raw.get("latest_date") or "")[:10],
        "is_target_date_covered": bool(raw.get("is_target_date_covered", False)),
        "data_freshness_label": str(raw.get("data_freshness_label") or ""),
        "planned_action": str(raw.get("planned_action") or ""),
        "raw": raw,
    }
    for key, value in raw.items():
        item.setdefault(key, value)
    item["symbol"] = symbol
    item["strategy_type"] = strategy_type
    item["strategy_label"] = STRATEGY_LABELS.get(strategy_type, strategy_type)
    return item


def candidate_rows_from_strategy_payload(payload: dict[str, Any] | None, strategy: str = "all") -> list[dict[str, Any]]:
    payload = payload or {}
    rows: list[dict[str, Any]] = []
    if strategy in {"all", "long", "long_swing"}:
        rows.extend(
            normalize_candidate_row(row, default_strategy_type="long")
            for row in ensure_list(payload.get("long_candidates"))
            if isinstance(row, dict)
        )
    if strategy in {"all", "board", "board_momentum"}:
        rows.extend(
            normalize_candidate_row(row, default_strategy_type="board")
            for row in ensure_list(payload.get("board_candidates"))
            if isinstance(row, dict)
        )
    for key in ("candidates", "strategy_candidates", "signals_today", "items"):
        for row in ensure_list(payload.get(key)):
            if isinstance(row, dict):
                normalized = normalize_candidate_row(row)
                if strategy == "all" or normalized["strategy_type"] == strategy:
                    rows.append(normalized)
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.get("symbol", ""), row.get("strategy_type", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def split_candidates_by_strategy(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result = {key: [] for key in STRATEGY_LABELS}
    for row in candidates:
        normalized = normalize_candidate_row(row)
        result.setdefault(normalized["strategy_type"], []).append(normalized)
    return result


def _theme_lookup(theme_map: Any) -> dict[str, dict[str, Any]]:
    if isinstance(theme_map, dict):
        values = theme_map.values() if all(isinstance(v, dict) for v in theme_map.values()) else theme_map
        if isinstance(values, dict):
            iterable = values.values()
        else:
            iterable = values
    else:
        iterable = theme_map or []
    lookup: dict[str, dict[str, Any]] = {}
    for row in iterable:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("代码"))
        if symbol:
            lookup[symbol] = row
    return lookup


def attach_theme_map_to_candidates(candidates: list[dict[str, Any]], theme_map: Any) -> list[dict[str, Any]]:
    lookup = _theme_lookup(theme_map)
    output: list[dict[str, Any]] = []
    for row in candidates:
        item = normalize_candidate_row(row)
        mapped = lookup.get(item["symbol"], {})
        sector = first_clean_display_text(mapped.get("sector"), item.get("sector"), unknown=UNKNOWN_SECTOR)
        industry = first_clean_display_text(mapped.get("industry"), item.get("industry"), unknown=UNKNOWN_INDUSTRY)
        primary_theme = first_clean_display_text(mapped.get("primary_theme"), item.get("primary_theme"), unknown=UNKNOWN_THEME)
        theme_tags = first_clean_display_text(
            mapped.get("theme_tags"),
            mapped.get("concept_tags"),
            item.get("theme_tags"),
            primary_theme,
            unknown=UNKNOWN_THEME,
        )
        item.update({
            "sector": sector,
            "industry": industry,
            "theme": primary_theme if primary_theme != UNKNOWN_THEME else theme_tags,
            "primary_theme": primary_theme,
            "theme_tags": theme_tags,
            "concept_tags": first_clean_display_text(mapped.get("concept_tags"), item.get("concept_tags"), unknown=UNKNOWN_CONCEPT),
            "theme_source": clean_display_text(mapped.get("source"), ""),
        })
        output.append(item)
    return output


def attach_freshness_to_candidates(candidates: list[dict[str, Any]], freshness_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    freshness_payload = freshness_payload or {}
    target = str(freshness_payload.get("target_complete_trade_date") or freshness_payload.get("target_date") or "")[:10]
    latest = str(
        freshness_payload.get("daily_feature_date")
        or freshness_payload.get("local_daily_latest_date")
        or freshness_payload.get("latest_local_date")
        or ""
    )[:10]
    covered = bool(target and latest and latest >= target and not freshness_payload.get("is_daily_stale"))
    label = "目标日期候选" if covered else "滞后数据候选"
    rows: list[dict[str, Any]] = []
    for row in candidates:
        item = normalize_candidate_row(row)
        item["data_latest_date"] = latest or item.get("data_latest_date") or ""
        item["is_target_date_covered"] = covered
        item["data_freshness_label"] = label
        rows.append(item)
    return rows


def summarize_candidate_theme_coverage(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    missing: list[str] = []
    mapped = 0
    for row in candidates:
        item = normalize_candidate_row(row)
        has_mapping = any(
            clean_display_text(item.get(key), "") not in {"", UNKNOWN_SECTOR, UNKNOWN_INDUSTRY, UNKNOWN_THEME, UNKNOWN_CONCEPT}
            for key in ("sector", "industry", "primary_theme", "theme", "theme_tags", "concept_tags")
        )
        if has_mapping:
            mapped += 1
        else:
            missing.append(item.get("symbol", ""))
    return {
        "candidate_count": len(candidates),
        "mapped_count": mapped,
        "unknown_count": len(candidates) - mapped,
        "missing_symbols": [symbol for symbol in missing if symbol],
    }


def summarize_candidates_by_strategy(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in candidates:
        counts[normalize_strategy_type(row.get("strategy_type"))] += 1
    return {key: counts.get(key, 0) for key in STRATEGY_LABELS}
