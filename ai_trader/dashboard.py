"""Read-only dashboard adapter for the independent AI paper trader.

This is the shared data layer that the QuantDesk 8000 server
(``/api/ai-trader/dashboard``) uses to display the trader without running a
second server. It is deliberately dependency-free (stdlib only, no Flask, no
pandas) so both the 8000 route and tests can import it safely.

Hard contract:

- READ-ONLY. Nothing here writes any file. It does not call ``init_ledger``,
  ``save_*`` or ``load_philosophy`` (the latter *moves aside* a corrupt file,
  which is a write side effect a viewer must never trigger).
- No second field interpretation: account/position/journal values are passed
  through from the authoritative artifacts verbatim. This module classifies
  lifecycle stages only — it never recomputes equity, positions, returns or
  drawdown.
- Lifecycle stages are explicit and never conflated:
    * ``proposed``      — the LLM brain floated a symbol tonight.
    * ``committed``     — an intent that survived account constraints and was
      written into the next paper plan.
    * ``filled``        — executed against historical open prices.
    * ``rejected``      — blocked by constraints (limit/T+1/cash/holdings...).
  A proposed symbol is NEVER reported as committed; membership overlap is
  computed here so neither the API nor the JS has to guess.
- Missing or corrupt artifacts degrade to warnings plus ``degraded=True``;
  they never fabricate data and never raise past :func:`build_dashboard_payload`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_trader.llm import llm_runtime_descriptor
from ai_trader.mandate import BOUNDARY
from ai_trader.paths import CYCLE_LATEST_PATH, STATE_DIR

DASHBOARD_SCHEMA = "ai_trader_dashboard_view_v1"
CHAT_BOUNDARY_NOTE = "聊天只能解释，不能下单：仓位只在每晚纸面复盘流程中变化。"

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _shanghai_date(value: datetime):
    """Return the Asia/Shanghai calendar date for *value*.

    Naive datetimes are treated as Shanghai local time; aware datetimes are
    converted. This keeps journal-age comparisons correct right after Shanghai
    midnight and avoids mixing naive and aware datetimes in a subtraction.
    """

    if value.tzinfo is None:
        return value.date()
    return value.astimezone(_SHANGHAI).date()

# A journal/ledger older than this many days means at least one scheduled
# evening run was missed; the UI must say so instead of looking current.
STALE_AFTER_DAYS = 2

_POSITION_FIELDS = (
    "symbol",
    "name",
    "quantity",
    "avg_cost",
    "market_price",
    "market_value",
    "unrealized_pnl",
    "weight",
    "style",
    "planned_hold_days",
    "entry_date",
    "acquired_on",
    "thesis",
    "stale_mark",
)

_INTENT_FIELDS = (
    "symbol",
    "side",
    "quantity",
    "ref_close",
    "planned_hold_days",
    "style",
    "conviction",
    "thesis",
    "pick_source",
    "pick_source_zh",
)


def _read_json(path: Path) -> tuple[Any, str]:
    """Read one JSON artifact. Returns (payload, error). Missing => (None, "")."""

    if not path.is_file():
        return None, ""
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: row.get(key) for key in fields if key in row}


def classify_intents(plan: dict[str, Any]) -> dict[str, Any]:
    """Split one plan into committed intents and per-symbol proposal status.

    Fail-closed on the plan's ``committed`` flag: only a literal ``True`` makes
    intents count as committed. A deferred plan (``committed=false``, e.g. the
    previous plan is still awaiting settlement) keeps its raw intents visible
    for audit under ``uncommitted_intents`` but must never be reported through
    ``committed_intents`` / ``committed_buy_symbols`` — otherwise an intent
    that was explicitly NOT written into tomorrow's paper book would display
    as submitted.
    """

    # Strict identity check: missing, false, null or string values all
    # degrade to "not committed". Only JSON `true` counts.
    plan_committed = plan.get("committed") is True
    raw_intents = [
        _pick_fields(row, _INTENT_FIELDS)
        for row in _as_list(plan.get("intents"))
        if isinstance(row, dict) and row.get("symbol")
    ]
    committed_intents = list(raw_intents) if plan_committed else []
    uncommitted_intents = [] if plan_committed else list(raw_intents)
    buy_symbols = {
        str(row.get("symbol"))
        for row in committed_intents
        if str(row.get("side") or "") == "buy"
    }
    llm_block = _as_dict(plan.get("llm"))
    styles = _as_dict(llm_block.get("styles"))
    proposed: list[dict[str, Any]] = []
    for symbol in _as_list(llm_block.get("picked")):
        text = str(symbol or "")
        if not text:
            continue
        is_committed = plan_committed and text in buy_symbols
        proposed.append(
            {
                "symbol": text,
                "style": styles.get(text) or "",
                # The three-way truth the UI must keep visible:
                "stage": "committed" if is_committed else "proposed_only",
                "stage_zh": "已提交" if is_committed else "仅提议·未提交",
            }
        )
    proposed_not_committed = [
        row["symbol"] for row in proposed if row["stage"] == "proposed_only"
    ]
    return {
        "plan_committed": plan_committed,
        "raw_intents": raw_intents,
        "committed_intents": committed_intents,
        "uncommitted_intents": uncommitted_intents,
        "committed_buy_symbols": sorted(buy_symbols),
        "proposed": proposed,
        "proposed_symbols": [row["symbol"] for row in proposed],
        "proposed_not_committed_symbols": proposed_not_committed,
    }


def _collect_actions(review: dict[str, Any], journal: dict[str, Any]) -> list[dict[str, Any]]:
    """Settled actions from the authoritative review, falling back to journal."""

    rows = [row for row in _as_list(review.get("actions")) if isinstance(row, dict)]
    if rows:
        return rows
    return [row for row in _as_list(journal.get("actions")) if isinstance(row, dict)]


def _age_days(as_of: str) -> int | None:
    text = str(as_of or "")[:10]
    if not text:
        return None
    try:
        delta = datetime.now() - datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return max(int(delta.days), 0)


def build_dashboard_payload(
    *,
    state_dir: Path | None = None,
    results_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the read-only AI-trader dashboard payload.

    All inputs are optional so tests can point this at a temporary state dir.
    Never raises for missing/corrupt artifacts; degrades with explicit warnings.
    """

    root_state = Path(state_dir) if state_dir is not None else STATE_DIR
    root_results = Path(results_dir) if results_dir is not None else CYCLE_LATEST_PATH.parent
    warnings: list[str] = []
    degraded = False

    def _require(path: Path, label: str) -> Any:
        nonlocal degraded
        payload, error = _read_json(path)
        if payload is None:
            degraded = True
            if error:
                warnings.append(f"{label}_corrupt:{path.name}:{error}")
            else:
                warnings.append(f"{label}_missing:{path.name}")
            return {}
        if not isinstance(payload, dict):
            degraded = True
            warnings.append(f"{label}_invalid:{path.name}")
            return {}
        return payload

    ledger = _require(root_state / "ledger.json", "ledger")
    plan = _require(root_state / "plan_latest.json", "plan")
    journal = _require(root_state / "journal_latest.json", "journal")
    philosophy = _require(root_state / "philosophy.json", "philosophy")
    review = _read_json(root_state / "review_latest.json")[0]
    review = review if isinstance(review, dict) else {}
    cycle = _require(root_results / "cycle_latest.json", "cycle")

    positions = [
        _pick_fields(row, _POSITION_FIELDS)
        for row in _as_list(ledger.get("positions"))
        if isinstance(row, dict)
    ]
    trades = [row for row in _as_list(ledger.get("trades")) if isinstance(row, dict)]

    classified = classify_intents(plan)
    actions = _collect_actions(review, journal)
    filled = [row for row in actions if row.get("status") == "filled"]
    rejected = [
        row
        for row in actions
        if row.get("status") in {"rejected", "abandoned"}
    ]

    llm_plan = _as_dict(plan.get("llm"))
    llm_cycle = _as_dict(cycle.get("plan")).get("llm") if isinstance(cycle.get("plan"), dict) else {}
    llm_cycle = _as_dict(llm_cycle)
    llm_used = bool(llm_plan.get("used"))
    llm = {
        "used": llm_used,
        "model": str(llm_plan.get("model") or ""),
        "unavailable_reason": str(llm_plan.get("unavailable_reason") or ""),
        "market_note": str(llm_plan.get("market_note") or ""),
        "picked": list(_as_list(llm_plan.get("picked"))),
        "sells_requested": list(_as_list(llm_plan.get("sells_requested"))),
        "source": "plan_latest.json",
    }
    # Cross-check against the cycle snapshot when present: disagreement is a
    # warning, never silently rewritten to look consistent.
    if cycle and llm_cycle and bool(llm_cycle.get("used")) != llm_used:
        warnings.append("llm_state_mismatch_between_plan_and_cycle")

    stance = _as_dict(philosophy.get("stance"))
    style_ledger = _as_dict(philosophy.get("style_ledger"))

    recommendation_view = _as_dict(plan.get("recommendation_view"))
    source_breakdown: dict[str, int] = {}
    for intent in classified["committed_intents"]:
        key = str(intent.get("pick_source") or "unknown")
        source_breakdown[key] = source_breakdown.get(key, 0) + 1

    last_journal_date = str(journal.get("as_of") or ledger.get("as_of") or "")
    age_days: int | None = None
    stale = False
    if last_journal_date:
        if now is None:
            now = datetime.now(_SHANGHAI)
        try:
            reference = _shanghai_date(now) if isinstance(now, datetime) else now
            delta = reference - datetime.strptime(last_journal_date[:10], "%Y-%m-%d").date()
            age_days = max(int(delta.days), 0)
            stale = age_days >= STALE_AFTER_DAYS
        except (ValueError, TypeError):
            warnings.append(f"unreadable_journal_date:{last_journal_date}")

    if stale:
        warnings.append(f"ai_trader_data_stale:{last_journal_date}:{age_days}d")
    if classified["proposed_not_committed_symbols"]:
        warnings.append(
            "llm_proposed_not_committed:" + ",".join(classified["proposed_not_committed_symbols"])
        )

    boundary = {
        **{key: value for key, value in BOUNDARY.items()},
        "chat_can_not_trade": True,
        "chat_boundary_zh": CHAT_BOUNDARY_NOTE,
        "is_real_order": False,
    }

    payload: dict[str, Any] = {
        "schema_version": DASHBOARD_SCHEMA,
        "ok": True,
        "degraded": degraded,
        "warnings": warnings,
        "boundary": boundary,
        "account": {
            "equity": ledger.get("equity"),
            "cash": ledger.get("cash"),
            "market_value": ledger.get("market_value"),
            "unrealized_pnl": ledger.get("unrealized_pnl"),
            "realized_pnl": ledger.get("realized_pnl"),
            "initial_cash": ledger.get("initial_cash"),
            "as_of": ledger.get("as_of"),
            "updated_at": ledger.get("updated_at"),
            "positions_count": len(positions),
        },
        "positions": positions,
        "latest_journal": {
            "as_of": journal.get("as_of"),
            "trade_date": journal.get("trade_date"),
            "headline": journal.get("headline"),
            "thought": journal.get("thought"),
            "lessons": _as_list(journal.get("lessons"))[:5],
            "filled_count": journal.get("filled_count"),
            "rejected_count": journal.get("rejected_count"),
            "boundary": journal.get("boundary"),
        },
        "latest_plan": {
            "plan_id": plan.get("plan_id"),
            "decision_date": plan.get("decision_date"),
            "trade_date": plan.get("trade_date"),
            "status": plan.get("status"),
            "committed": classified["plan_committed"],
            "not_committed_reason": plan.get("not_committed_reason") or "",
            "conviction": plan.get("conviction"),
            "intents_count": len(classified["committed_intents"]),
        },
        "stance": stance,
        "revisions_count": len(_as_list(philosophy.get("revisions"))),
        "notes": _as_list(philosophy.get("notes"))[-3:],
        "philosophy_error": philosophy.get("error") or "",
        "style_ledger": style_ledger,
        "recommendation_view": {
            "symbols": list(_as_list(recommendation_view.get("symbols"))),
            "count": recommendation_view.get("count"),
            "warnings": list(_as_list(recommendation_view.get("warnings"))),
            "advisory_only": bool(recommendation_view.get("advisory_only", True)),
            "committed_source_breakdown": source_breakdown,
        },
        "proposed_symbols": classified["proposed"],
        "proposed_not_committed_symbols": classified["proposed_not_committed_symbols"],
        "committed_intents": classified["committed_intents"],
        "uncommitted_intents": classified["uncommitted_intents"],
        "committed_buy_symbols": classified["committed_buy_symbols"],
        "filled_trades": filled,
        "rejected_or_abandoned": rejected,
        "recent_trades": trades[-10:],
        "review_summary": _as_dict(review.get("score_summary")),
        "llm": llm,
        # Secret-free runtime disclosure: which LLM provider is configured,
        # whether data leaves this device, and EXACTLY what context would be
        # sent. The token is never included.
        "llm_runtime": llm_runtime_descriptor(),
        "schedule_status": {
            "last_journal_date": last_journal_date,
            "plan_status": plan.get("status") or "",
            "plan_committed": classified["plan_committed"],
            "trade_date": plan.get("trade_date") or "",
            "llm_used": llm_used,
            "model": llm["model"],
            "unavailable_reason": llm["unavailable_reason"],
            "age_days": age_days,
            "stale": stale,
            "stale_after_days": STALE_AFTER_DAYS,
            "scheduler_note": (
                "唯一正常自动决策入口是 Windows 计划任务（晚间 20:20）；"
                "本页面只读展示，不提供启动/停止/重跑。"
            ),
        },
        "source_paths": {
            "ledger": str(root_state / "ledger.json"),
            "plan_latest": str(root_state / "plan_latest.json"),
            "journal_latest": str(root_state / "journal_latest.json"),
            "philosophy": str(root_state / "philosophy.json"),
            "review_latest": str(root_state / "review_latest.json"),
            "cycle_latest": str(root_results / "cycle_latest.json"),
        },
        "updated_at": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
    }
    return payload


def build_degraded_payload(reason: str) -> dict[str, Any]:
    """Structured fallback when assembly itself fails. Never a 500 body."""

    return {
        "schema_version": DASHBOARD_SCHEMA,
        "ok": False,
        "degraded": True,
        "warnings": [f"dashboard_assembly_failed:{reason}"],
        "boundary": {
            **dict(BOUNDARY),
            "chat_can_not_trade": True,
            "chat_boundary_zh": CHAT_BOUNDARY_NOTE,
            "is_real_order": False,
        },
        "account": {},
        "positions": [],
        "latest_journal": {},
        "latest_plan": {},
        "stance": {},
        "style_ledger": {},
        "recommendation_view": {},
        "proposed_symbols": [],
        "proposed_not_committed_symbols": [],
        "committed_intents": [],
        "uncommitted_intents": [],
        "committed_buy_symbols": [],
        "filled_trades": [],
        "rejected_or_abandoned": [],
        "recent_trades": [],
        "llm": {"used": False, "model": "", "unavailable_reason": "", "source": "unavailable"},
        "llm_runtime": llm_runtime_descriptor(),
        "schedule_status": {"stale": True},
        "source_paths": {},
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


__all__ = [
    "CHAT_BOUNDARY_NOTE",
    "DASHBOARD_SCHEMA",
    "STALE_AFTER_DAYS",
    "build_dashboard_payload",
    "build_degraded_payload",
    "classify_intents",
]
