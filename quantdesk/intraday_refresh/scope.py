"""Dynamic intraday scope: which symbols deserve minute data right now.

Sources (all read-only - this module never writes state):
  critical : AI trader positions        state/ai_trader/ledger.json
             tonight's committed plan   state/ai_trader/plan_latest.json
  high     : AI watchlist               state/ai_trader/watchlist.json
  normal   : research candidate pools   results/auto_long_candidates_latest.json
                                         results/auto_board_candidates_latest.json

Missing or unreadable sources degrade with explicit warnings; the scope never
invents symbols. Reading these files does not modify them (see the test that
asserts the seven protected ledgers keep their hashes).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2}
MAX_MINUTE_SYMBOLS = 100
REASON_LABELS = {
    "ai_position": "AI 持仓",
    "plan": "当晚已提交计划",
    "ai_watchlist": "AI 自选",
    "ai_minute_focus": "AI 当日分钟关注建议（已授权读取）",
    "research_candidate_long": "研究台多头候选",
    "research_candidate_board": "研究台打板候选",
}


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, f"scope_source_missing:{path.as_posix()}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"scope_source_unreadable:{path.as_posix()}:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return {}, f"scope_source_not_an_object:{path.as_posix()}"
    return payload, ""


def _symbols_from_positions(ledger: dict[str, Any]) -> list[str]:
    out: list[str] = []
    positions = ledger.get("positions")
    if isinstance(positions, dict):
        out.extend(str(sym) for sym in positions.keys() if str(sym).strip())
    elif isinstance(positions, list):
        for row in positions:
            if isinstance(row, dict):
                sym = str(row.get("symbol") or "").strip()
                if sym:
                    out.append(sym)
    return [s for s in (x.strip() for x in out) if s.isdigit() and len(s.strip()) == 6]


def _symbols_from_plan(plan: dict[str, Any]) -> list[str]:
    """Committed BUY intents for the upcoming trade date."""

    trade_date = str(plan.get("trade_date") or "")
    committed = plan.get("committed")
    if committed is not True:
        return []
    intents = plan.get("intents")
    if not isinstance(intents, list):
        return []
    out: list[str] = []
    for row in intents:
        if not isinstance(row, dict):
            continue
        if str(row.get("action") or row.get("side") or "buy").lower() not in ("buy", "b"):
            continue
        sym = str(row.get("symbol") or "").strip()
        if sym and len(sym) == 6 and sym.isdigit():
            out.append(sym)
    _ = trade_date
    return out


def _symbols_from_watchlist(watchlist: dict[str, Any]) -> list[str]:
    raw = watchlist.get("items") or watchlist.get("symbols") or watchlist.get("watchlist")
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            value = item.get("symbol") if isinstance(item, dict) else item
            symbol = str(value or "").strip()
            if symbol.isdigit() and len(symbol) == 6:
                out.append(symbol)
        return out
    if isinstance(raw, dict):
        return [str(s).strip() for s in raw.keys() if str(s).strip().isdigit()]
    return []


def _symbols_from_candidates(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        rows = payload.get("pool")
    out: list[str] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                sym = str(row.get("symbol") or row.get("code") or "").strip()
                if sym.isdigit() and len(sym) == 6:
                    out.append(sym)
    return out


def _read_ai_focus(root: Path, now: datetime | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Consumption needs its own opt-in and the existing level-3 authority."""
    try:
        from ai_trader.daytime_fill import daytime_trust
        from ai_trader.paths import PROFILE_NAMES, ledger_id_for_profile
    except ImportError:
        return [], ["ai_minute_focus_contract_unavailable"]
    shanghai = ZoneInfo("Asia/Shanghai")
    current = now or datetime.now(shanghai)
    current = current.replace(tzinfo=shanghai) if current.tzinfo is None else current.astimezone(shanghai)
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for profile in ("", *PROFILE_NAMES):
        name = profile or "default"
        folder = root / "state" / (f"ai_trader_{profile}" if profile else "ai_trader")
        if profile and not folder.is_dir():
            continue
        payload, error = _read_json(folder / "minute_focus.json")
        if error:
            warnings.append(f"ai_minute_focus:{name}:{error}")
            continue
        ledger, error = _read_json(folder / "ledger.json")
        trust = daytime_trust(ledger)
        if error or trust["level"] < 3 or not trust["authorized_by"] or not trust["authorized_at"]:
            warnings.append(f"ai_minute_focus:{name}:level_3_authorization_required")
            continue
        expected_id = ledger_id_for_profile(profile)
        if (payload.get("schema") != "ai_minute_focus.v1" or payload.get("profile") != name
                or payload.get("ledger_id") != expected_id or ledger.get("ledger_id") != expected_id):
            warnings.append(f"ai_minute_focus:{name}:identity_or_schema_mismatch")
            continue
        if (payload.get("source") != "model_want_minutes" or payload.get("model_used") is not True
                or not str(payload.get("model") or "").strip() or payload.get("status") != "active"):
            warnings.append(f"ai_minute_focus:{name}:active_model_request_required")
            continue
        try:
            generated = datetime.fromisoformat(str(payload.get("generated_at") or ""))
            expires = datetime.fromisoformat(str(payload.get("expires_at") or ""))
            if (generated.utcoffset() != timedelta(hours=8) or expires.utcoffset() != timedelta(hours=8)
                    or payload.get("as_of") != current.date().isoformat()
                    or generated.date() != current.date() or expires.date() != current.date()
                    or not generated <= current < expires
                    or not timedelta(0) < expires - generated <= timedelta(minutes=30)
                    or expires > current.replace(hour=15, minute=0, second=0, microsecond=0)):
                raise ValueError("date_or_expiry_invalid")
            slot = str(payload.get("slot") or "")
            if not re.fullmatch(r"[0-9]{4}", slot):
                raise ValueError("slot_invalid")
            slot_at = generated.replace(hour=int(slot[:2]), minute=int(slot[2:]), second=0, microsecond=0)
            # The slot labels the pre-model observation bucket, not completion.
            # Its five minutes plus the producer's <=300s response freshness
            # window can cross one bucket boundary without becoming stale.
            if not 0 <= (generated - slot_at).total_seconds() < 600:
                raise ValueError("slot_stale_or_future")
        except (TypeError, ValueError):
            warnings.append(f"ai_minute_focus:{name}:date_slot_or_expiry_unverified")
            continue
        symbols = payload.get("symbols")
        if (not isinstance(symbols, list) or not 0 < len(symbols) <= 10
                or any(not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol) for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            warnings.append(f"ai_minute_focus:{name}:symbols_invalid")
            continue
        entries.extend({"symbol": symbol, "profile": name, "model": payload["model"],
                        "ledger_id": expected_id, "source": "model_want_minutes",
                        "path": str(folder / "minute_focus.json"), "expires_at": payload["expires_at"]}
                       for symbol in symbols)
    return entries, warnings


def build_dynamic_scope(
    project_root: Path | None = None,
    *,
    now: datetime | None = None,
    extra_symbols: Sequence[Mapping[str, Any]] | None = None,
    max_symbols: int = MAX_MINUTE_SYMBOLS,
    read_ai_minute_focus: bool = False,
) -> dict[str, Any]:
    """Return the deduplicated minute-data scope with per-symbol provenance."""

    root = Path(project_root or PROJECT_ROOT)
    warnings: list[str] = []
    entries: dict[str, dict[str, Any]] = {}

    def _add(symbol: str, reason: str, priority: str) -> None:
        sym = symbol.strip()
        if not sym:
            return
        entry = entries.setdefault(sym, {"symbol": sym, "reasons": [], "priority": priority})
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)
        if PRIORITY_ORDER[priority] < PRIORITY_ORDER[entry["priority"]]:
            entry["priority"] = priority

    ledger, err = _read_json(root / "state" / "ai_trader" / "ledger.json")
    if err:
        warnings.append(err)
    for sym in _symbols_from_positions(ledger):
        _add(sym, "ai_position", "critical")

    plan, err = _read_json(root / "state" / "ai_trader" / "plan_latest.json")
    if err:
        warnings.append(err)
    for sym in _symbols_from_plan(plan):
        _add(sym, "plan", "critical")

    watchlist, err = _read_json(root / "state" / "ai_trader" / "watchlist.json")
    if err:
        warnings.append(err)
    for sym in _symbols_from_watchlist(watchlist):
        _add(sym, "ai_watchlist", "high")

    if read_ai_minute_focus is True:
        focus, focus_warnings = _read_ai_focus(root, now)
        warnings.extend(focus_warnings)
        for row in focus:
            _add(row["symbol"], "ai_minute_focus", "high")
            entries[row["symbol"]].setdefault("focus_provenance", []).append(row)

    long_payload, err = _read_json(root / "results" / "auto_long_candidates_latest.json")
    if err:
        warnings.append(err)
    for sym in _symbols_from_candidates(long_payload):
        _add(sym, "research_candidate_long", "normal")

    board_payload, err = _read_json(root / "results" / "auto_board_candidates_latest.json")
    if err:
        warnings.append(err)
    for sym in _symbols_from_candidates(board_payload):
        _add(sym, "research_candidate_board", "normal")

    for extra in extra_symbols or ():
        symbol = str(extra.get("symbol") or "").strip()
        reason = str(extra.get("reason") or "snapshot_mover")
        priority = "high" if "limit" in reason else "normal"
        _add(symbol, reason, priority)

    symbols = sorted(
        entries.values(),
        key=lambda e: (PRIORITY_ORDER[e["priority"]], e["symbol"]),
    )
    if len(symbols) > max_symbols:
        symbols = symbols[:max_symbols]
        warnings.append(f"minute_scope_capped:{max_symbols}")
    if not symbols:
        warnings.append("scope_empty")
    return {
        "symbols": symbols,
        "counts": {
            "total": len(symbols),
            "critical": sum(1 for s in symbols if s["priority"] == "critical"),
            "high": sum(1 for s in symbols if s["priority"] == "high"),
            "normal": sum(1 for s in symbols if s["priority"] == "normal"),
        },
        "warnings": warnings,
        "read_only": True,
        "ai_minute_focus_read_enabled": read_ai_minute_focus is True,
    }


