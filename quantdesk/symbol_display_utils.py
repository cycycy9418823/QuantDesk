"""Small display helpers for long symbol lists and CLI commands.

These helpers only format text for UI/CLI output. They never fetch data,
change strategy logic, or alter the JSON payloads that keep full details.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


def _as_symbol_list(symbols: Any) -> list[str]:
    if symbols is None:
        return []
    if isinstance(symbols, str):
        return [item.strip() for item in symbols.split(",") if item.strip()]
    if isinstance(symbols, Iterable):
        return [str(item).strip() for item in symbols if str(item).strip()]
    return []


def safe_symbol_count(symbols: Any) -> int:
    return len(_as_symbol_list(symbols))


def summarize_symbols(symbols: Any, max_visible: int = 5) -> dict[str, Any]:
    rows = _as_symbol_list(symbols)
    visible_limit = max(0, int(max_visible or 0))
    visible = rows[:visible_limit]
    omitted = max(0, len(rows) - len(visible))
    return {
        "count": len(rows),
        "visible": visible,
        "omitted_count": omitted,
        "is_truncated": omitted > 0,
    }


def format_symbol_preview(symbols: Any, max_visible: int = 5, *, empty: str = "-") -> str:
    summary = summarize_symbols(symbols, max_visible=max_visible)
    visible = summary["visible"]
    if not visible:
        return empty
    text = ", ".join(visible)
    if summary["omitted_count"]:
        marker = "等" if visible else ""
        text = f"{text} {marker}，另有 {summary['omitted_count']} 只".strip()
    return text


def build_symbols_omitted_note(symbols: Any, *, max_visible: int = 5, result_path: str = "") -> str:
    summary = summarize_symbols(symbols, max_visible=max_visible)
    if not summary["is_truncated"]:
        return ""
    target = result_path or "结果 JSON"
    return f"已省略长 symbols 列表，完整内容见 {target}。"


def truncate_command_symbols(command: str, max_symbols: int = 10) -> str:
    text = " ".join(str(command or "").strip().split())
    if not text:
        return ""

    def replace(match: re.Match[str]) -> str:
        quote = match.group("quote") or ""
        raw = match.group("symbols") or ""
        symbols = _as_symbol_list(raw)
        if len(symbols) <= max_symbols:
            return match.group(0)
        preview = ",".join(symbols[: max(1, min(3, max_symbols))])
        return f"--symbols {quote}{preview},...{quote}"

    return re.sub(r"--symbols\s+(?P<quote>[\"']?)(?P<symbols>[^\"'\s]+)(?P=quote)", replace, text)
