"""Shared deterministic fixtures for the DS12-02 recovery probes.

All prices are raw, dated and sourced so they pass the project's real fill gate
(``evaluate_next_open_fill``).  No real model, network, or canonical state.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from ai_trader import ledger as ledger_module
from ai_trader.paths import CyclePaths

DECISION_DATE = "2026-09-10"
TRADE_DATE = "2026-09-11"

# columns match the project's daily schema; ``adjust=raw`` -> price_basis raw
_HEADER = "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,fetched_at\n"


def _bar(date: str, symbol: str, o: float, h: float, l: float, c: float) -> str:
    return (
        f"{date},{symbol},{o:.2f},{h:.2f},{l:.2f},{c:.2f},"
        f"1000000,{o*1000000:.0f},0.0,raw,diagnostic_fixture,{date}T16:00:00+08:00\n"
    )


def write_daily(daily_dir: Path, symbol: str, *, prev_close: float, open_px: float,
                high: float | None = None, low: float | None = None,
                close: float | None = None) -> None:
    """Two dated raw bars for ``symbol`` (prev session + trade session)."""
    daily_dir.mkdir(parents=True, exist_ok=True)
    high = high if high is not None else max(open_px, prev_close) * 1.02
    low = low if low is not None else min(open_px, prev_close) * 0.98
    close = close if close is not None else open_px
    (daily_dir / f"{symbol}.csv").write_text(
        _HEADER
        + _bar(DECISION_DATE, symbol, prev_close, prev_close * 1.01, prev_close * 0.99, prev_close)
        + _bar(TRADE_DATE, symbol, open_px, high, low, close),
        encoding="utf-8",
    )


def empty_ledger(cash: float = 100_000.0) -> dict[str, Any]:
    led = ledger_module.empty_ledger(created_at="2026-09-10T20:20:00+08:00")
    led["as_of"] = DECISION_DATE
    led["cash"] = cash
    return led


def buy_intent(symbol: str, *, quantity: int, ref_close: float,
               preset_touch_price: float | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": "buy",
        "quantity": quantity,
        "ref_close": ref_close,
        "predicted_direction": "up",
        "preset_touch_price": preset_touch_price if preset_touch_price is not None else ref_close * 1.04,
    }


def plan(*, plan_id: str, intents: list[dict[str, Any]], status: str = "pending_open") -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "decision_date": DECISION_DATE,
        "trade_date": TRADE_DATE,
        "status": status,
        "committed": True,
        "intents": deepcopy(intents),
    }


def trades_for_plan(ledger: dict[str, Any], plan_id: str) -> list[dict[str, Any]]:
    return [t for t in (ledger.get("trades") or []) if str(t.get("plan_id") or "") == plan_id]


def seed_root(tmp_path: Path, *, plan_obj: dict[str, Any], ledger: dict[str, Any],
              symbols: dict[str, dict[str, float]]) -> Path:
    """Lay out a runnable state/ + data/daily tree for a real run_evening call."""
    root = tmp_path / "scenario"
    state = root / "state" / "ai_trader"
    results = root / "results" / "ai_trader"
    daily = root / "data" / "daily"
    minute = root / "data" / "intraday"
    for d in (state, results, daily, minute):
        d.mkdir(parents=True, exist_ok=True)
    for sym, spec in symbols.items():
        write_daily(daily, sym, prev_close=spec["prev_close"], open_px=spec["open"],
                    close=spec.get("close", spec["open"]))
    (state / "ledger.json").write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    (state / "plan_latest.json").write_text(json.dumps(plan_obj, ensure_ascii=False), encoding="utf-8")
    return root


def stub_evening(monkeypatch, overnight, settle_callable, *, stub_model: bool = True,
                 symbols: list[str] | None = None) -> None:
    """Replace ONLY market-data loaders and the LLM with deterministic stand-ins.

    Persistence and recovery (settle_plan, save_ledger, append_journal dedup,
    record_evening_and_refresh_page, _ledger_has_plan_fills) stay REAL -- the
    task forbids stubbing the logic under test.  ``settle_callable`` is passed in
    so a caller can wrap the real settle_plan in a Mock to count invocations
    without replacing its behaviour.
    """
    import pandas as pd

    syms = symbols or ["600500"]
    panel = pd.DataFrame({s: [5.0, 5.08] for s in syms}, index=[DECISION_DATE, TRADE_DATE])
    monkeypatch.setattr(overnight, "latest_as_of", lambda *_: {"as_of": TRADE_DATE})
    monkeypatch.setattr(overnight, "focus_universe", lambda *a, **k: list(syms))
    monkeypatch.setattr(overnight, "load_close_panel", lambda *a, **k: (panel.copy(), pd.DataFrame(), []))
    monkeypatch.setattr(overnight, "load_recommendations", lambda **k: {"symbols": [], "rows": [], "warnings": []})
    monkeypatch.setattr(overnight, "research_ideas", lambda p, b, **k: {"book": b, "evaluated": [], "live": None})
    monkeypatch.setattr(overnight, "pick_operating_method", lambda *_: (None, "idle"))
    monkeypatch.setattr(overnight, "next_trade_day", lambda *_: "2026-09-14")
    monkeypatch.setattr(overnight, "_build_llm_context", lambda **k: {"fixture_only": True})

    def no_model(context, diagnostics, **kwargs):
        diagnostics.update(reason="diagnostic_fixture_no_model")
        return None

    if stub_model:
        monkeypatch.setattr(overnight, "propose_picks", no_model)
    monkeypatch.setattr(overnight, "settle_plan", settle_callable)


def cycle_paths(root: Path) -> CyclePaths:
    state = root / "state" / "ai_trader"
    results = root / "results" / "ai_trader"
    return CyclePaths(
        daily_dir=root / "data/daily",
        minute_dir=root / "data/intraday",
        ledger=state / "ledger.json",
        hypotheses=state / "hypotheses.json",
        journal_latest=state / "journal_latest.json",
        journal_log=state / "journal.jsonl",
        daytime_notes=state / "daytime_notes.jsonl",
        daytime_session=state / "daytime_session.json",
        plan_latest=state / "plan_latest.json",
        review_latest=state / "review_latest.json",
        watchlist=state / "watchlist.json",
        philosophy=state / "philosophy.json",
        cycle_latest=results / "cycle_latest.json",
        snapshot=results / "snapshot_latest.json",
        decision_log=state / "decision_log.jsonl",
        decision_latest=state / "decision_latest.json",
        decision_page=results / "decision_page.html",
        decision_page_md=results / "decision_page.md",
    )
