"""Shared local symbol-scope policy for daily QuantDesk workflows.

The policy is deliberately conservative: explicit symbols win, explicit scope
is respected, and the default beginner scope is current_analysis_pool.  The
implementation only reads local files and never falls back to full-A silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quantdesk.daily_price_update_engine import resolve_symbols
from quantdesk.shared import normalize_symbol
from quantdesk.symbol_display_utils import summarize_symbols

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCREEN_HANDOFF_JSON = Path("results") / "screen" / "screen_latest.json"
VALID_SCOPES = {
    "current_analysis_pool",
    "low_price_included",
    "specified_symbols",
    "local_daily_cache",
    "full_a",
}


def _split_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _load_low_price_symbols(*, max_symbols: int = 0, project_root: str | Path | None = None) -> list[str]:
    # resolve_symbols already knows the project-local low-price included pool.
    # Keep the full local pool here so callers can explain max-symbols truncation
    # without pretending the selected sample is the whole analysis pool.
    limit = max(0, int(max_symbols or 0))
    rows = resolve_symbols(only_low_price=True, limit=limit)
    return [normalize_symbol(row.get("symbol")) for row in rows if normalize_symbol(row.get("symbol"))]


def _load_local_daily_symbols(*, max_symbols: int = 0, project_root: str | Path | None = None) -> list[str]:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    limit = max(0, int(max_symbols or 0))
    daily_dir = root / "data" / "daily"
    if not daily_dir.exists():
        return []
    symbols: list[str] = []
    for path in sorted(daily_dir.glob("*.csv")):
        symbol = normalize_symbol(path.stem.split("_")[0])
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if limit and len(symbols) >= limit:
            break
    return symbols


def _dedupe_symbols(values: Any, *, limit: int = 0) -> list[str]:
    symbols: list[str] = []
    for item in _split_symbols(values):
        if item and item not in symbols:
            symbols.append(item)
        if limit and len(symbols) >= limit:
            break
    return symbols


def _screen_payload_symbols(payload: dict[str, Any]) -> list[str]:
    handoff = payload.get("candidate_handoff") if isinstance(payload.get("candidate_handoff"), dict) else {}
    symbols = _dedupe_symbols(handoff.get("symbols") if isinstance(handoff, dict) else [])
    if symbols:
        return symbols
    matched = payload.get("matched") if isinstance(payload.get("matched"), list) else []
    rows: list[str] = []
    for row in matched:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if symbol:
            rows.append(symbol)
    return _dedupe_symbols(rows)


def _load_screen_handoff_symbols(*, max_symbols: int = 0, project_root: str | Path | None = None) -> list[str]:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    path = root / SCREEN_HANDOFF_JSON
    if not path.exists() or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    return _dedupe_symbols(_screen_payload_symbols(payload), limit=max(0, int(max_symbols or 0)))


def resolve_scope(
    *,
    symbols: Any = None,
    scope: str | None = None,
    max_symbols: int = 80,
    project_root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    requested = _split_symbols(symbols)
    requested_scope = str(scope or "").strip()
    warnings: list[str] = []
    limit = max(1, int(max_symbols or 80))

    if requested:
        resolved_scope = "specified_symbols"
        available_symbols = requested
        selected = available_symbols[:limit]
        source = "explicit_symbols"
    else:
        resolved_scope = requested_scope or "current_analysis_pool"
        if resolved_scope not in VALID_SCOPES:
            warnings.append(f"未知 scope={resolved_scope}，已回退到 current_analysis_pool。")
            resolved_scope = "current_analysis_pool"
        if resolved_scope == "full_a" and not force:
            return {
                "data_scope": "full_a",
                "scope_label": "全 A",
                "scope_source": "explicit_full_a_blocked",
                "requested_count": 0,
                "analysis_pool_count": 0,
                "selected_count": 0,
                "max_symbols": limit,
                "truncated": False,
                "truncation_reason": "",
                "symbols": [],
                "symbols_preview": [],
                "symbols_omitted_count": 0,
                "warnings": ["scope=full_a 需要显式 --force true；QuantDesk 不默认扫描全 A。"],
                "blocked": True,
            }
        if resolved_scope == "current_analysis_pool":
            available_symbols = _load_screen_handoff_symbols(project_root=project_root)
            if available_symbols:
                selected = available_symbols[:limit]
                source = "screen_candidate_handoff"
            else:
                available_symbols = _load_low_price_symbols(project_root=project_root)
                selected = available_symbols[:limit]
                source = "fallback_low_price_included"
        elif resolved_scope == "low_price_included":
            available_symbols = _load_low_price_symbols(project_root=project_root)
            selected = available_symbols[:limit]
            source = "low_price_included"
        elif resolved_scope == "local_daily_cache":
            available_symbols = _load_local_daily_symbols(project_root=project_root)
            selected = available_symbols[:limit]
            source = "local_daily_cache"
        else:
            available_symbols = []
            selected = []
            source = "full_a" if resolved_scope == "full_a" else "unknown"

    if not selected and resolved_scope != "full_a":
        warnings.append("未找到可用股票范围；请先生成低价池，或显式传入 --symbols。")
    analysis_pool_count = len(available_symbols) if "available_symbols" in locals() else len(selected)
    summary = summarize_symbols(selected, max_visible=5)
    truncated = analysis_pool_count > len(selected) or bool(summary["is_truncated"])
    truncation_reason = ""
    if analysis_pool_count > len(selected):
        truncation_reason = f"max-symbols={limit} 生效，本次只检查前 {len(selected)} 只。"
    labels = {
        "specified_symbols": "指定股票",
        "current_analysis_pool": "当前分析池",
        "low_price_included": "低价学习池 included=true",
        "local_daily_cache": "本地日线缓存",
        "full_a": "全 A",
    }
    return {
        "data_scope": resolved_scope,
        "scope_label": labels.get(resolved_scope, resolved_scope),
        "scope_source": source,
        "requested_count": len(requested) if requested else analysis_pool_count,
        "analysis_pool_count": analysis_pool_count,
        "selected_count": len(selected),
        "max_symbols": limit,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "symbols": selected,
        "symbols_preview": summary["visible"],
        "symbols_omitted_count": max(0, len(selected) - len(summary["visible"])),
        "warnings": warnings,
        "blocked": False,
    }


def scope_fields(scope_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "data_scope": scope_summary.get("data_scope") or "unknown",
        "scope_label": scope_summary.get("scope_label") or "",
        "scope_source": scope_summary.get("scope_source") or "",
        "requested_count": int(scope_summary.get("requested_count") or 0),
        "analysis_pool_count": int(scope_summary.get("analysis_pool_count") or scope_summary.get("requested_count") or 0),
        "selected_count": int(scope_summary.get("selected_count") or 0),
        "max_symbols": int(scope_summary.get("max_symbols") or 0),
        "truncated": bool(scope_summary.get("truncated")),
        "truncation_reason": str(scope_summary.get("truncation_reason") or ""),
        "symbols_preview": list(scope_summary.get("symbols_preview") or []),
        "symbols_omitted_count": int(scope_summary.get("symbols_omitted_count") or 0),
    }
