"""DS12-03 fixed 17-case matrix: real update and evidence/valuation boundaries.

The initial 72k creation fixture was invalid and is preserved separately. This
version uses the supported 100k initial profile. Missing source/time CSV columns
alone are not a mandatory rejection criterion: credible formal-candidate evidence
may supply provenance. Those exploratory cases are retained, not counted as bugs.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from ai_trader import ledger as ai_ledger
from quantdesk.paper import auto_paper_portfolio as app

DAY = "2026-09-11"


def observe(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def environment(tmp_path, monkeypatch, *, cash=100000.0, positions=(), candidates=()):
    root = tmp_path / "scenario"
    for relative in ("state", "results", "reports", "data/daily", "data/metadata", "data/intraday"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    mapping = {
        "PROJECT_ROOT": root, "STATE_DIR": root / "state", "RESULTS_DIR": root / "results",
        "REPORTS_DIR": root / "reports", "DAILY_DIR": root / "data/daily",
        "THEME_MAP_PATH": root / "data/metadata/themes.csv",
        "AUTO_PAPER_PORTFOLIO_STATE_JSON": root / "state/auto_paper_portfolio.json",
        "AUTO_PAPER_PORTFOLIO_LATEST_JSON": root / "results/auto_paper_portfolio_latest.json",
        "AUTO_PAPER_PORTFOLIO_HISTORY_JSON": root / "results/auto_paper_portfolio_history.json",
    }
    for name, path in mapping.items():
        monkeypatch.setattr(app, name, path)
    capital = cash + sum(float(p["entry_price"]) * int(p["quantity"]) for p in positions)
    state = app._empty_state(capital)
    state.update(cash=cash, positions=deepcopy(list(positions)), closed_positions=[], daily_actions=[])
    observe(mapping["AUTO_PAPER_PORTFOLIO_STATE_JSON"], state)
    # Deterministic upstream intake only. Keep the public lock, current CSV
    # reader, all trade gates, valuation, fees, state/latest/history writers real.
    monkeypatch.setattr(app, "load_formal_candidate_intake", lambda *a, **k: {
        "input_source": "independent_deterministic_intake", "status": "passed", "path": "", "reason_codes": []})
    monkeypatch.setattr(app, "formal_candidate_strategy_payload", lambda *a, **k: {"generated_at": DAY + "T16:00:00+08:00"})
    monkeypatch.setattr(app, "candidate_rows_from_strategy_payload", lambda *a, **k: deepcopy(list(candidates)))
    return root


def write_bars(root, symbol, *, close=10.2, previous=10.0, variant="valid_raw"):
    fields = ["date", "symbol", "open", "high", "low", "close", "volume", "amount", "pct_chg", "adjust", "source", "fetched_at"]
    rows = []
    for day, price in (("2026-09-10", previous), (DAY, close)):
        rows.append(dict(date=day, symbol=symbol, open=price, high=price * 1.01, low=price * .99,
            close=price, volume=1000000, amount=price * 1000000, pct_chg=0, adjust="raw",
            source="independent_fixture", fetched_at=day + "T16:00:00+08:00"))
    if variant == "unknown":
        fields.remove("adjust")
    elif variant == "mixed":
        rows[0]["adjust"] = "qfq"
    elif variant == "zero_volume":
        rows[-1].update(open=close, high=close, low=close, volume=0, amount=0)
    elif variant == "missing_source":
        fields.remove("source")
    elif variant == "missing_time":
        fields.remove("fetched_at")
    elif variant in {"limit_up", "limit_down"}:
        rows[-1].update(open=close, high=close, low=close, pct_chg=10 if variant == "limit_up" else -10)
    path = root / "data/daily" / f"{symbol}.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def update(root):
    payload = app.run_auto_paper_portfolio(mode="update", plan_date=DAY, review_target_date=DAY,
        enable_intraday_swing=False, allow_daily_approx=False, risk_gate_shadow_enabled=False, dry_run=False)
    state = json.loads((root / "state/auto_paper_portfolio.json").read_text(encoding="utf-8"))
    observe(root.parent / "public_update_observation.json", {"payload": payload, "state": state,
        "entry": "run_auto_paper_portfolio(mode=update, enable_intraday_swing=False, dry_run=False)",
        "upstream_intake": "deterministic fixture; no claim of observed scheduled-task reachability"})
    assert payload.get("ok") is True, payload
    return payload, state


def position(symbol, price):
    return dict(symbol=symbol, name="fixture", strategy_type="long", quantity=1000,
        entry_date="2026-09-09", entry_price=price, latest_price=price, market_value=price * 1000)


@pytest.mark.parametrize("missing", [False, True], ids=["all_quotes", "one_quote_missing"])
def test_ai_valuation_preserves_cash_and_unquoted_position(tmp_path, missing):
    original = ai_ledger.empty_ledger(created_at="2026-09-09T20:20:00+08:00")
    original.update(cash=20000.0, positions=[
        dict(symbol="600500", quantity=1000, avg_cost=10.0, market_price=10.0),
        dict(symbol="600501", quantity=1000, avg_cost=42.0, market_price=42.0)])
    before = deepcopy(original)
    prices = {"600500": 11.0}
    if not missing:
        prices["600501"] = 42.0
    marked = ai_ledger.mark_to_market(original, prices, as_of=DAY)
    observe(tmp_path / "ai_valuation_observation.json", marked)
    assert original == before
    assert marked["cash"] == 20000.0 and marked["equity"] == 73000.0
    assert marked["positions"][1]["market_value"] == 42000.0
    if missing:
        assert marked["positions"][1]["stale_mark"] is True


@pytest.mark.parametrize("missing", [False, True], ids=["all_quotes", "one_quote_missing"])
def test_strategy_public_update_preserves_unquoted_holding(tmp_path, monkeypatch, missing):
    root = environment(tmp_path, monkeypatch, cash=48000.0, positions=[position("600500", 10), position("600501", 42)])
    write_bars(root, "600500", close=11.0)
    if not missing:
        write_bars(root, "600501", close=42, previous=42)
    payload, state = update(root)
    kept = next(p for p in state["positions"] if p["symbol"] == "600501")
    assert state["cash"] == 48000.0
    assert kept["market_value"] == 42000.0
    assert state["total_equity"] == 101000.0
    if missing:
        assert "missing_daily_quote:600501" in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("missing", [False, True], ids=["all_quotes", "one_quote_missing"])
def test_strategy_public_snapshot_money_agrees_with_persisted_ledger(tmp_path, monkeypatch, missing):
    root = environment(tmp_path, monkeypatch, cash=48000.0, positions=[position("600500", 10), position("600501", 42)])
    write_bars(root, "600500", close=11.0)
    if not missing:
        write_bars(root, "600501", close=42, previous=42)
    payload, state = update(root)
    assert payload["cash"] == state["cash"] == payload["summary"]["cash"]
    assert payload["total_equity"] == state["total_equity"] == payload["summary"]["total_equity"]


@pytest.mark.parametrize("variant", ["valid_raw", "unknown", "mixed", "zero_volume", "limit_up"])
def test_strategy_public_buy_obeys_current_daily_evidence(tmp_path, monkeypatch, variant):
    price = 11.0 if variant == "limit_up" else 10.2
    candidate = dict(symbol="000001", name="fixture", strategy_type="long", close=price, quantity=100)
    root = environment(tmp_path, monkeypatch, candidates=[candidate])
    write_bars(root, "000001", close=price, variant=variant)
    payload, state = update(root)
    buys = [a for a in payload.get("actions", []) if a.get("action") == "buy"]
    if variant == "valid_raw":
        assert len(buys) == 1 and state["cash"] < 100000 and len(state["positions"]) == 1
    else:
        assert buys == [], {"variant": variant, "buys": buys, "warnings": payload.get("warnings")}
        assert state["cash"] == 100000 and state["positions"] == []


@pytest.mark.parametrize("variant", ["valid_raw", "unknown", "mixed", "zero_volume", "missing_file", "limit_down"])
def test_strategy_public_sell_obeys_current_daily_evidence(tmp_path, monkeypatch, variant):
    held = position("600500", 10.0)
    held.update(latest_price=9.1, market_value=9100.0)
    root = environment(tmp_path, monkeypatch, cash=90000.0, positions=[held])
    if variant != "missing_file":
        write_bars(root, "600500", close=9.0 if variant == "limit_down" else 9.1,
            previous=9.1 if variant == "zero_volume" else 10.0, variant=variant)
    payload, state = update(root)
    sells = [a for a in payload.get("actions", []) if a.get("action") == "sell"]
    if variant == "valid_raw":
        assert len(sells) == 1 and len(state["closed_positions"]) == 1 and state["positions"] == []
    else:
        assert sells == [], {"variant": variant, "sells": sells, "warnings": payload.get("warnings")}
        assert state["cash"] == 90000 and len(state["positions"]) == 1
