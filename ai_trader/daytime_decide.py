"""Daytime decision loop (A1 · S2): one slot, one profile, at most two model calls.

Default off: nothing here calls a model or the network unless ``AI_TRADER_DAYTIME=1``
and an ``llm`` callable is provided.  Tests always inject a fake model.

Owner rules carried by this module:

* the 09:30 slot may only watch, never order;
* after 14:55 no new position may be opened (sells stay allowed);
* every order's ``reason`` describes **this slot's facts only** — last night's plan
  thesis is kept in its own dated field and is never copied into ``reason``/``thesis``
  (Codex review, 2026-09-23);
* every intent and every journal record carries ``model_used`` and ``elapsed_ms``.
"""

from __future__ import annotations

from quantdesk.market_clock import CN_TIMEZONE, to_shanghai_datetime

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ai_trader.paths import PROJECT_ROOT, CyclePaths, assert_not_quantdesk_ledger, profile_paths
from ai_trader.daytime_screen import build_screen, render_text
from quantdesk.paper.market_rules import order_quantity_rule

DAYTIME_ENV = "AI_TRADER_DAYTIME"
DRY_ENV = "AI_TRADER_DAYTIME_DRY"
DECIDE_SCHEMA = "ai_trader_daytime_intent_v1"
JOURNAL_SCHEMA = "ai_trader_daytime_journal_v1"
MAX_CALLS_PER_SLOT = 2
MAX_TOKENS = 2000
TIMEOUT_SECONDS = 120
MAX_WANT_MINUTES = 10
WATCH_ONLY_SLOT = "0930"
NO_NEW_OPEN_AFTER = "1455"
MAX_REASON_CHARS = 200
ORDER_SIDES = ("buy", "sell")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_MISSING = "missing"


def enabled() -> bool:
    return os.environ.get(DAYTIME_ENV, "").strip() == "1"


def dry_mode() -> bool:
    return os.environ.get(DRY_ENV, "").strip() == "1"


@dataclass
class ModelCall:
    """One model answer plus the numbers the health line will want later."""

    text: str = ""
    model: str = ""
    elapsed_ms: int = 0
    prompt_chars: int = 0
    ok: bool = False
    error: str = ""


@dataclass
class SlotDecision:
    slot: str = ""
    status: str = "skipped"
    calls: list[ModelCall] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    want_minutes: list[str] = field(default_factory=list)
    note: str = ""
    reason: str = ""

    @property
    def model_used(self) -> bool:
        return any(call.ok for call in self.calls)

    @property
    def elapsed_ms(self) -> int:
        return sum(call.elapsed_ms for call in self.calls)

    @property
    def prompt_chars(self) -> int:
        return sum(call.prompt_chars for call in self.calls)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Tolerate code fences and surrounding prose around one JSON object."""

    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.lstrip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        end = cleaned.rfind("```")
        if end != -1:
            cleaned = cleaned[:end]
    start, stop = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or stop <= start:
        return None
    try:
        payload = json.loads(cleaned[start:stop + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _clean_symbol(value: Any) -> str:
    symbol = str(value or "").strip()
    return symbol if len(symbol) == 6 and symbol.isdigit() else ""


def parse_want_minutes(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("want_minutes")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        symbol = _clean_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
        if len(out) >= MAX_WANT_MINUTES:
            break
    return out


def parse_orders(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate the model's orders; anything unusable is returned as a rejection."""

    raw = payload.get("orders")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if raw is None:
        return accepted, rejected
    if not isinstance(raw, list):
        return accepted, [{"reason": "orders_not_a_list", "raw": str(raw)[:120]}]
    for item in raw:
        if not isinstance(item, dict):
            rejected.append({"reason": "order_not_an_object", "raw": str(item)[:120]})
            continue
        symbol = _clean_symbol(item.get("symbol"))
        side = str(item.get("side") or "").strip().lower()
        reason = str(item.get("reason") or "").strip()[:MAX_REASON_CHARS]
        try:
            quantity = int(item.get("qty"))
        except (TypeError, ValueError):
            quantity = 0
        problems = []
        if not symbol:
            problems.append("bad_symbol")
        if side not in ORDER_SIDES:
            problems.append("bad_side")
        if quantity <= 0:
            problems.append("bad_qty")
        elif symbol and order_quantity_rule(symbol, quantity):
            # Same board rule as the fill (STAR 200 起 1 股递增, BSE 100 起).
            problems.append("qty_not_a_lot")
        if not reason:
            problems.append("reason_missing")
        if problems:
            rejected.append({"symbol": symbol, "side": side, "qty": quantity, "problems": problems})
            continue
        accepted.append({
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "reason": reason,
            "reason_scope": "slot_facts_only",
            "stop": item.get("stop") if item.get("stop") in (None, "") or _number(item.get("stop")) else None,
            "valid_until_slot": str(item.get("valid_until_slot") or "").strip()[:8],
        })
    return accepted, rejected


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


SYSTEM_PROMPT = (
    "你是纸面研究交易员，只输出 JSON，不解释。字段："
    '{"want_minutes":[6位代码，最多10个],"orders":[{"symbol":"6位代码","side":"buy|sell",'
    '"qty":整数(100的倍数),"reason":"只写本槽事实(≤200字)","stop":数字或null,'
    '"valid_until_slot":"HHMM"}],"note":"一句话"}'
)


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    target = assert_not_quantdesk_ledger(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _default_llm(prompt: str, *, max_tokens: int = MAX_TOKENS,
                 timeout: float = TIMEOUT_SECONDS) -> ModelCall:
    """Adapter over ai_trader.llm. Returns a failed call instead of raising."""

    started = time.monotonic()
    try:
        from ai_trader import llm  # local import: the default path may stay unused
        cfg = llm.load_llm_config(explicit_optin=True)
    except Exception as exc:  # pragma: no cover - defensive
        return ModelCall(ok=False, error=f"llm_unavailable:{type(exc).__name__}", prompt_chars=len(prompt))
    if not cfg:
        return ModelCall(ok=False, error="llm_disabled_or_unconfigured", prompt_chars=len(prompt))
    cfg = dict(cfg)
    cfg.setdefault("max_tokens", str(max_tokens))
    cfg.setdefault("timeout", str(timeout))
    diagnostics: dict[str, Any] = {}
    try:
        text = llm._call(SYSTEM_PROMPT, prompt, cfg, diagnostics)
    except Exception as exc:  # pragma: no cover - provider side
        text = None
        diagnostics.setdefault("error_type", type(exc).__name__)
    elapsed = int((time.monotonic() - started) * 1000)
    return ModelCall(text=text or "", model=str(cfg.get("model") or ""), elapsed_ms=elapsed,
                     prompt_chars=len(prompt), ok=bool(text),
                     error="" if text else str(diagnostics.get("error_type") or "empty_response"))


def _bar_label(bar: dict[str, Any]) -> str:
    """HH:MM for either a bare time or a full datetime."""

    raw = str(bar.get("time") or bar.get("datetime") or "")
    match = _TIME_RE.search(raw)
    return match.group(0) if match else raw[-5:]


def _bars_block(minute_bars: dict[str, list[dict[str, Any]]] | None, limit: int = 60) -> str:
    if not minute_bars:
        return ""
    lines = [f"【按需分钟线（每只最近 {limit} 根）】"]
    for symbol, bars in sorted(minute_bars.items()):
        tail = [bar for bar in bars if isinstance(bar, dict)][-limit:]
        rendered = " ".join(
            f"{_bar_label(bar)}={bar.get('close') or bar.get('price')}" for bar in tail
        )
        lines.append(f"{symbol} ({len(tail)}根): {rendered}")
    return "\n".join(lines)


def _overnight_thesis(plan: dict[str, Any]) -> dict[str, Any]:
    """Last night's thesis as its own dated field — never merged into an intent."""

    decision_date = str(plan.get("decision_date") or "")[:10]
    texts = []
    for item in plan.get("intents") or []:
        if isinstance(item, dict) and item.get("thesis"):
            texts.append({"symbol": item.get("symbol"), "date": decision_date,
                          "thesis": str(item.get("thesis"))[:200]})
    return {"decision_date": decision_date, "items": texts}


def _read_json(path: Path) -> dict[str, Any]:
    target = assert_not_quantdesk_ledger(path)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def decide_slot(
    project_root: Path | None = None,
    *,
    profile: str | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    paths: CyclePaths | None = None,
    llm: Callable[..., ModelCall] | None = None,
    screen: dict[str, Any] | None = None,
    screen_text: str = "",
    minute_bars: dict[str, list[dict[str, Any]]] | None = None,
    minute_fetcher: Callable[[list[str]], dict[str, list[dict[str, Any]]]] | None = None,
    plan_override: dict[str, Any] | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """One slot's decision for one profile. Returns the journal record it wrote."""

    root = Path(project_root or PROJECT_ROOT).resolve()
    runtime = paths or profile_paths(profile)
    read_clock = clock or (lambda: datetime.now(CN_TIMEZONE))
    current = to_shanghai_datetime(now or read_clock())
    if not enabled():
        return {"status": "skipped", "reason": "daytime_disabled", "model_used": False,
                "model_calls": 0, "elapsed_ms": 0, "orders": [], "rejected": [],
                "intents": [], "written": False}

    decision = SlotDecision(slot=str((screen or {}).get("slot") or ""))
    if screen is None:
        screen = build_screen(root, profile=profile, now=current, paths=runtime)
        decision.slot = str(screen.get("slot") or "")
    if not screen_text:
        screen_text = render_text(screen)
    call_model = llm or _default_llm
    payload: dict[str, Any] | None = None

    if not decision.slot:
        decision.status, decision.reason = "skipped", "slot_unknown"
    else:
        call = call_model(screen_text,
                          max_tokens=MAX_TOKENS, timeout=TIMEOUT_SECONDS)
        decision.calls.append(call)
        if not call.ok:
            decision.status, decision.reason = "skipped", call.error or "call_failed"
        else:
            payload = _extract_json(call.text)
            if payload is None:
                decision.status, decision.reason = "skipped", "bad_json"
            else:
                decision.want_minutes = parse_want_minutes(payload)
                decision.note = str(payload.get("note") or "")[:200]
                if decision.want_minutes and minute_fetcher is not None and minute_bars is None:
                    try:
                        minute_bars = minute_fetcher(decision.want_minutes) or {}
                    except Exception as exc:  # pragma: no cover - provider side
                        minute_bars = {}
                        decision.rejected.append(
                            {"symbol": "", "problems": [f"minute_fetch_failed:{type(exc).__name__}"]})
                if minute_bars:
                    second = call_model(
                        screen_text + "\n\n" + _bars_block(minute_bars),
                        max_tokens=MAX_TOKENS, timeout=TIMEOUT_SECONDS)
                    decision.calls.append(second)
                    second_payload = _extract_json(second.text) if second.ok else None
                    if second_payload is None:
                        decision.status = "skipped"
                        decision.reason = second.error if not second.ok else "bad_json_after_minutes"
                        payload = None
                    else:
                        payload = second_payload
                if payload is not None:
                    orders, parse_rejections = parse_orders(payload)
                    decision.orders = orders
                    decision.rejected.extend(parse_rejections)
                    decision.status = "decided"

    completed = to_shanghai_datetime(read_clock()) if decision.calls else None
    kept: list[dict[str, Any]] = []
    for order in decision.orders:
        if decision.slot == WATCH_ONLY_SLOT and order["side"] == "buy":
            decision.rejected.append({**order, "problems": ["watch_only_slot_no_buy"]})
        elif decision.slot >= NO_NEW_OPEN_AFTER and order["side"] == "buy":
            # 14:55 counts as "after 14:55" on purpose: an intent written in the 14:55
            # slot would be filled off the 15:00 close snapshot, which is exactly what
            # the rule forbids. Sells stay allowed.
            decision.rejected.append({**order, "problems": ["no_new_open_after_1455"]})
        else:
            kept.append(order)
    decision.orders = kept

    plan = plan_override if plan_override is not None else _read_json(runtime.plan_latest)
    overnight = _overnight_thesis(plan)
    model = next((call.model for call in decision.calls if call.model), "")
    as_of = current.strftime("%Y-%m-%d")
    recorded = to_shanghai_datetime(read_clock())
    intents = [{
        "schema_version": DECIDE_SCHEMA, "as_of": as_of, "slot": decision.slot,
        "created_at": recorded.isoformat(),
        "decision_started_at": current.isoformat(),
        "model_completed_at": completed.isoformat() if completed else "",
        "symbol": order["symbol"], "side": order["side"], "quantity": order["quantity"],
        "reason": order["reason"], "reason_scope": order["reason_scope"], "thesis": "",
        "overnight_plan_thesis": overnight, "stop": order["stop"],
        "valid_until_slot": order["valid_until_slot"], "status": "pending",
        "fill_policy_intraday": "snapshot", "model_used": decision.model_used, "model": model,
        "elapsed_ms": decision.elapsed_ms, "prompt_chars": decision.prompt_chars,
        "note": decision.note,
    } for order in decision.orders]
    journal = {
        "schema_version": JOURNAL_SCHEMA, "as_of": as_of, "slot": decision.slot,
        "at": recorded.isoformat(), "status": decision.status,
        "decision_started_at": current.isoformat(), "model_completed_at": completed.isoformat() if completed else "",
        "reason": decision.reason, "model_used": decision.model_used, "model": model,
        "elapsed_ms": decision.elapsed_ms, "prompt_chars": decision.prompt_chars,
        "model_calls": len(decision.calls),
        "calls": [{"ok": call.ok, "elapsed_ms": call.elapsed_ms, "prompt_chars": call.prompt_chars,
                   "model": call.model, "error": call.error} for call in decision.calls],
        "want_minutes": decision.want_minutes, "orders_accepted": len(decision.orders),
        "orders_rejected": len(decision.rejected), "rejected": decision.rejected,
        "note": decision.note,
    }
    if write:
        _append_jsonl(runtime.daytime_intents, intents)
        _append_jsonl(runtime.daytime_notes.parent / "daytime_journal.jsonl", [journal])
    return {**journal, "intents": intents, "screen_chars": len(screen_text),
            "orders": decision.orders, "written": bool(write)}
