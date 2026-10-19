"""A model's bounded minute-data suggestion, separate from execution authority."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Mapping, Sequence

from ai_trader.daytime_fill import daytime_trust
from ai_trader.ledger import LedgerMissingError, load_ledger
from ai_trader.paths import CyclePaths, active_profile, ledger_id_for_path
from quantdesk.utils import atomic_write_json

SCHEMA = "ai_minute_focus.v1"
MAX_SYMBOLS = 10
TTL_MINUTES = 30
MAX_QUOTE_AGE_SECONDS = 300
TZ_SHANGHAI = timezone(timedelta(hours=8))


def focus_path(runtime: CyclePaths) -> Path:
    return runtime.ledger.parent / "minute_focus.json"


def _aware(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value or ""))
    except (ValueError, TypeError):
        return None
    return stamp.astimezone(TZ_SHANGHAI) if stamp.tzinfo is not None else None


def build_minute_focus(*, runtime: CyclePaths, profile: str | None,
                       decision: Mapping[str, Any], quote_rows: Sequence[Mapping[str, Any]],
                       visible_symbols: Sequence[str], now: datetime) -> dict[str, Any]:
    """Keep only fresh, observed, explicitly model-requested symbols.

    A suggestion may be reviewed below trust level 3. Consumption requires a
    separate opt-in reader and the existing ledger-header authorisation.
    """
    if now.tzinfo is None:
        raise ValueError("minute focus requires an explicit timezone")
    current = now.astimezone(TZ_SHANGHAI)
    day = current.date().isoformat()
    close = current.replace(hour=15, minute=0, second=0, microsecond=0)
    expires = min(current + timedelta(minutes=TTL_MINUTES), close)
    try:
        ledger = load_ledger(runtime.ledger, missing_ok=False)
    except (LedgerMissingError, OSError, ValueError, RuntimeError):
        ledger = {}
    expected_id = ledger_id_for_path(runtime.ledger)
    actual_id = str(ledger.get("ledger_id") or "")
    trust = daytime_trust(ledger)
    model = str(decision.get("want_minutes_model") or "").strip()
    result: dict[str, Any] = {
        "schema": SCHEMA, "source": "model_want_minutes",
        "profile": str((active_profile() if profile is None else profile) or "default"),
        "ledger_id": actual_id, "as_of": day, "slot": str(decision.get("slot") or ""),
        "generated_at": current.isoformat(), "expires_at": expires.isoformat(),
        "model_used": decision.get("model_used") is True, "model": model,
        "status": "unavailable", "symbols": [], "rejected_symbols": [],
        "consumer_eligible": bool(trust["level"] >= 3 and trust["authorized_by"] and trust["authorized_at"]),
        "reason": "model_decision_unavailable",
        "note": "仅为模型数据关注建议；采集器须显式开启读取并核实账本第3级授权，不含买卖指令。",
    }
    if not actual_id or actual_id != expected_id:
        result["reason"] = "ledger_identity_unverified"
        return result
    if decision.get("status") != "decided" or decision.get("model_used") is not True or not model:
        return result
    if str(decision.get("as_of") or "") != day or current >= close:
        result["reason"] = "outside_current_observation_day"
        return result
    completed = _aware(decision.get("model_completed_at"))
    if completed is None or completed > current or (current - completed).total_seconds() > MAX_QUOTE_AGE_SECONDS:
        result["reason"] = "model_observation_stale_or_unknown"
        return result
    visible = set(visible_symbols)
    fresh: set[str] = set()
    for quote in quote_rows:
        symbol = str(quote.get("symbol") or "")
        if symbol not in visible or not re.fullmatch(r"[0-9]{6}", symbol):
            continue
        stamp = _aware(quote.get("quote_time") or quote.get("timestamp") or quote.get("fetched_at"))
        try:
            price = float(quote.get("price"))
        except (TypeError, ValueError):
            continue
        if (stamp is not None and stamp.date().isoformat() == day and stamp <= current
                and (current - stamp).total_seconds() <= MAX_QUOTE_AGE_SECONDS and isfinite(price) and price > 0):
            fresh.add(symbol)
    requested = decision.get("want_minutes")
    if not isinstance(requested, list):
        result["reason"] = "model_focus_shape_invalid"
        return result
    for symbol in requested:
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
            result["rejected_symbols"].append({"symbol": str(symbol), "reason": "invalid_symbol"})
        elif symbol not in fresh:
            result["rejected_symbols"].append({"symbol": symbol, "reason": "not_in_fresh_observed_universe"})
        elif symbol not in result["symbols"] and len(result["symbols"]) < MAX_SYMBOLS:
            result["symbols"].append(symbol)
    result["status"] = "active" if result["symbols"] else "empty"
    result["reason"] = "model_focus_requested" if result["symbols"] else "no_valid_model_focus"
    return result


def write_minute_focus(runtime: CyclePaths, payload: dict[str, Any]) -> None:
    """Replace only this profile's suggestion; no market data or ledger writes."""
    atomic_write_json(focus_path(runtime), payload)


def read_minute_focus(runtime: CyclePaths, *, now: datetime | None = None) -> dict[str, Any]:
    """A read-only display that labels stale and malformed artifacts explicitly."""
    try:
        payload = json.loads(focus_path(runtime).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": True, "status": "missing", "symbols": [], "reason": "尚无模型分钟关注建议"}
    except (OSError, UnicodeError, ValueError):
        return {"ok": False, "status": "unreadable", "symbols": [], "reason": "分钟关注建议不可读，文件未改动"}
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return {"ok": False, "status": "invalid", "symbols": [], "reason": "分钟关注建议格式不受支持"}
    if payload.get("ledger_id") != ledger_id_for_path(runtime.ledger):
        return {"ok": False, "status": "invalid", "symbols": [], "reason": "分钟关注建议与账本身份不符"}
    current = now or datetime.now(TZ_SHANGHAI)
    if current.tzinfo is None:
        raise ValueError("minute focus requires an explicit timezone")
    expires = _aware(payload.get("expires_at"))
    output = {**payload, "ok": True, "display_only": True}
    if expires is None or expires <= current:
        output["status"] = "expired"
        output["reason"] = "关注建议已到期；仅保留历史展示，不应继续采集"
    return output
