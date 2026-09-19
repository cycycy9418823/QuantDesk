"""DS12-02: AI settlement/recovery exactly-once contracts.

Offline and synthetic: fixed daily bars with raw prices and ``fetched_at``;
no real model, network, or real ledger.  Covers the zero-intent no-op, a
legitimate synthetic fill, the original interruption failure, the reverse
interruption (ledger written, marker missing), and refusal of an
unevidenced settled plan.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from ai_trader import ledger as ledger_module, overnight, recovery
from ai_trader.paths import CyclePaths


def _daily(tmp_path: Path) -> Path:
    daily = tmp_path / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "600500.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,fetched_at\n"
        "2026-09-10,600500,5.00,5.10,4.90,5.00,1000000,5000000,0.0,raw,diagnostic_fixture,2026-09-10T16:00:00+08:00\n"
        "2026-09-11,600500,5.02,5.20,4.99,5.08,1000000,5020000,1.6,raw,diagnostic_fixture,2026-09-11T16:00:00+08:00\n",
        encoding="utf-8",
    )
    return daily


def _plan(*, status: str = "pending_open") -> dict:
    return {
        "plan_id": "plan_20260910_for_20260911",
        "decision_date": "2026-09-10",
        "trade_date": "2026-09-11",
        "status": status,
        "committed": True,
        "intents": [
            {
                "symbol": "600500",
                "side": "buy",
                "quantity": 2500,
                "ref_close": 5.0,
                "predicted_direction": "up",
                "preset_touch_price": 5.2,
            }
        ],
    }


def _ledger() -> dict:
    led = ledger_module.empty_ledger(created_at="2026-09-10T20:20:00+08:00")
    led["as_of"] = "2026-09-10"
    return led


def test_zero_intent_settled_is_noop(tmp_path: Path) -> None:
    """Positive control: an already-settled zero-intent plan changes nothing."""

    daily = _daily(tmp_path)
    plan = {
        "plan_id": "plan_zero",
        "trade_date": "2026-09-11",
        "status": "settled",
        "settled_on": "2026-09-11",
        "intents": [],
        "actions": [{"action": "hold_cash", "status": "skipped"}],
    }
    ledger = _ledger()
    before = deepcopy(ledger)
    for _ in range(2):
        plan, ledger, actions = overnight.settle_plan(
            plan, ledger, as_of="2026-09-11", daily_dir=daily
        )
        assert actions == []
        assert ledger == before


def test_positive_trade_then_repeat_does_not_charge_twice(tmp_path: Path) -> None:
    """Positive control + idempotence: a legal buy fills once."""

    daily = _daily(tmp_path)
    settled, after, actions = overnight.settle_plan(
        _plan(), _ledger(), as_of="2026-09-11", daily_dir=daily
    )
    assert any(row.get("status") == "filled" for row in actions), actions
    assert len(after["trades"]) == 1
    assert after["trades"][0].get("plan_id") == "plan_20260910_for_20260911"

    _, repeated, second_actions = overnight.settle_plan(
        settled, after, as_of="2026-09-11", daily_dir=daily
    )
    assert second_actions == []
    assert repeated == after


def test_interruption_after_settled_save_recovers_once(tmp_path: Path) -> None:
    """Original failure: settled saved, ledger not -> recover exactly once."""

    daily = _daily(tmp_path)
    settled, after, actions = overnight.settle_plan(
        _plan(), _ledger(), as_of="2026-09-11", daily_dir=daily
    )
    assert len(after["trades"]) == 1 and any(a.get("status") == "filled" for a in actions)

    old_ledger = _ledger()  # persisted ledger lacks the committed fill
    try:
        _, recovered, _ = overnight.settle_plan(
            settled, old_ledger, as_of="2026-09-11", daily_dir=daily
        )
    except (ValueError, RuntimeError) as exc:
        # Explicit fail-closed is acceptable; silent loss is not.
        assert "unverified" in str(exc)
        return
    assert len(recovered["trades"]) == 1, "settled marker silently skipped a missing trade"


@pytest.mark.parametrize("persisted", ["none", "plan_only", "ledger_only", "both"])
def test_boundary_interrupt_recovery_exactly_once(tmp_path: Path, persisted: str) -> None:
    """S3: every persistence boundary recovers to exactly one effect (or fails closed)."""

    daily = _daily(tmp_path)
    settled, after, _ = overnight.settle_plan(
        _plan(), _ledger(), as_of="2026-09-11", daily_dir=daily
    )
    old = _ledger()
    if persisted == "none":
        plan, ledger = _plan(), old
    elif persisted == "plan_only":
        plan, ledger = settled, old
    elif persisted == "ledger_only":
        plan, ledger = _plan(status="pending_open"), after
    else:
        plan, ledger = settled, after
    try:
        _, recovered, _ = overnight.settle_plan(plan, ledger, as_of="2026-09-11", daily_dir=daily)
    except (ValueError, RuntimeError):
        return  # explicit fail-closed is acceptable; silent loss/double is not
    assert len(recovered["trades"]) == 1, persisted


def test_reverse_interruption_no_double_debit(tmp_path: Path) -> None:
    """Addendum: ledger written, settled marker missing -> no second debit."""

    daily = _daily(tmp_path)
    _, after, _ = overnight.settle_plan(
        _plan(), _ledger(), as_of="2026-09-11", daily_dir=daily
    )
    # The plan marker was never persisted; the ledger already has the tagged fill.
    _, again, actions = overnight.settle_plan(
        _plan(status="pending_open"), after, as_of="2026-09-11", daily_dir=daily
    )
    assert actions == []
    assert len(again["trades"]) == 1


def test_journal_same_as_of_is_not_duplicated(tmp_path: Path) -> None:
    """Same-cycle retry must not append a second journal row (S5)."""

    from ai_trader import journal

    latest = tmp_path / "journal_latest.json"
    log = tmp_path / "journal.jsonl"
    note = {"as_of": "2026-09-11", "headline": "fixture"}
    journal.append_journal(note, latest_path=latest, log_path=log)
    journal.append_journal(note, latest_path=latest, log_path=log)
    lines = [row for row in log.read_text(encoding="utf-8").splitlines() if row.strip()]
    assert len(lines) == 1


def test_settled_without_evidence_and_without_ledger_refuses(tmp_path: Path) -> None:
    """Corrupt/unevidenced material is refused, not silently re-applied."""

    daily = _daily(tmp_path)
    legacy = {
        "plan_id": "plan_20260910_for_20260911",
        "trade_date": "2026-09-11",
        "status": "settled",
        "settled_on": "2026-09-11",
        "intents": [],
        "actions": [{"action": "buy", "symbol": "600500", "status": "filled"}],
    }
    with pytest.raises(RuntimeError) as captured:
        overnight.settle_plan(legacy, _ledger(), as_of="2026-09-11", daily_dir=daily)
    assert "settled_plan_effect_unverified" in str(captured.value)


# ---------------------------------------------------------------------------
# A2: frozen input but no reliably persisted original model output
# ---------------------------------------------------------------------------


def test_missing_raw_model_output_stays_interrupted_not_fabricated() -> None:
    called: list[int] = []
    result = recovery.plan_cycle_recovery(
        input_snapshot={"as_of": "2026-09-11", "candidates": ["600500"]},
        persisted_model_output=None,
        model_caller=lambda: called.append(1) or {"buys": [{"symbol": "600500"}]},
    )
    assert result["status"] == recovery.MISSING_ORIGINAL_MODEL_OUTPUT
    assert result["model_called"] is False
    assert called == [], "an interrupted cycle must never re-call the model"


def test_verifiable_persisted_model_output_is_reused_without_new_call() -> None:
    called: list[int] = []
    result = recovery.plan_cycle_recovery(
        input_snapshot={"as_of": "2026-09-11"},
        persisted_model_output={
            "model": "fixture-model",
            "created_at": "2026-09-11T20:20:00+08:00",
            "buys": [],
        },
        model_caller=lambda: called.append(1),
    )
    assert result["status"] == recovery.REUSABLE_ORIGINAL_OUTPUT
    assert result["model_called"] is False
    assert called == []


def test_no_input_has_nothing_to_recover() -> None:
    assert recovery.plan_cycle_recovery(input_snapshot=None)["status"] == recovery.NO_INPUT


# ---------------------------------------------------------------------------
# S5: full run_evening re-entry must be idempotent (cash/trades/journal/plan)
# ---------------------------------------------------------------------------


_SETTLED_PLAN = {
    "plan_id": "plan_20260910_for_20260911",
    "decision_date": "2026-09-10",
    "trade_date": "2026-09-11",
    "status": "settled",
    "settled_on": "2026-09-11",
    "intents": [],
    "actions": [{"action": "hold_cash", "status": "skipped"}],
    "committed": True,
}

_PENDING_BUY_PLAN = {
    "plan_id": "plan_20260910_for_20260911",
    "decision_date": "2026-09-10",
    "trade_date": "2026-09-11",
    "status": "pending_open",
    "committed": True,
    "intents": [
        {
            "symbol": "600500",
            "side": "buy",
            "quantity": 2500,
            "ref_close": 5.0,
            "predicted_direction": "up",
            "preset_touch_price": 5.2,
        }
    ],
}


def _seed_root(
    tmp_path: Path,
    *,
    plan: dict | None = None,
    cash: float = 5000.0,
    position: bool = True,
) -> Path:
    root = tmp_path / "scenario"
    state = root / "state" / "ai_trader"
    results = root / "results" / "ai_trader"
    daily = root / "data" / "daily"
    minute = root / "data" / "intraday"
    for directory in (state, results, daily, minute):
        directory.mkdir(parents=True, exist_ok=True)
    (daily / "600500.csv").write_text(
        "date,symbol,open,high,low,close,volume,amount,pct_chg,adjust,source,fetched_at\n"
        "2026-09-10,600500,5.00,5.10,4.90,5.00,1000000,5000000,0.0,raw,diagnostic_fixture,2026-09-10T16:00:00+08:00\n"
        "2026-09-11,600500,5.02,5.20,4.99,5.08,1000000,5020000,1.6,raw,diagnostic_fixture,2026-09-11T16:00:00+08:00\n",
        encoding="utf-8",
    )
    ledger = ledger_module.empty_ledger(created_at="2026-09-10T20:20:00+08:00")
    ledger["as_of"] = "2026-09-10"
    ledger["cash"] = cash
    ledger["positions"] = (
        [{"symbol": "600500", "quantity": 1000, "avg_cost": 5.0, "market_price": 5.0, "acquired_on": "2026-09-01"}]
        if position
        else []
    )
    (state / "ledger.json").write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    (state / "plan_latest.json").write_text(
        json.dumps(plan or _SETTLED_PLAN, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _cycle_paths(root: Path) -> CyclePaths:
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


def _stub_evening(monkeypatch, settle_mock, *, stub_model: bool = True) -> None:
    panel = pd.DataFrame({"600500": [5.0, 5.08]}, index=["2026-09-10", "2026-09-11"])
    monkeypatch.setattr(overnight, "latest_as_of", lambda *_: {"as_of": "2026-09-11"})
    monkeypatch.setattr(overnight, "focus_universe", lambda *a, **k: ["600500"])
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
    monkeypatch.setattr(overnight, "settle_plan", settle_mock)


def test_full_evening_reentry_preserves_settlement_and_unique_journal(
    tmp_path: Path, monkeypatch
) -> None:
    root = _seed_root(tmp_path)
    runtime = _cycle_paths(root)
    original = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    settle = Mock(wraps=overnight.settle_plan)
    _stub_evening(monkeypatch, settle)

    def _decision_rows() -> list[dict]:
        if not runtime.decision_log.is_file():
            return []
        return [
            json.loads(line)
            for line in runtime.decision_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    first = overnight.run_evening(paths=runtime, network_news=False)
    first_ledger = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    first_decisions = len(_decision_rows())
    second = overnight.run_evening(paths=runtime, network_news=False)
    second_ledger = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    second_decisions = len(_decision_rows())

    assert second_decisions == first_decisions, "same-cycle recovery duplicated decision records"
    assert settle.call_count == 0, "an already-settled historical plan must not settle again"
    assert first_ledger["trades"] == second_ledger["trades"] == original["trades"]
    assert first_ledger["cash"] == second_ledger["cash"] == original["cash"]
    for payload in (first, second):
        assert payload["as_of"] == "2026-09-11"
        assert payload["plan"]["trade_date"] == "2026-09-14"
    notes = [
        json.loads(line)
        for line in runtime.journal_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    current = [row for row in notes if row.get("as_of") == "2026-09-11"]
    assert len(current) == 1, "same-cycle recovery appended another journal entry"


# ---------------------------------------------------------------------------
# S3 (full): crash AFTER each real write boundary of run_evening, then re-enter
# ---------------------------------------------------------------------------

_WRITE_BOUNDARIES = [
    "plan_settle",
    "ledger_settle",
    "review",
    "hypotheses",
    "watchlist",
    "plan_tonight",
    "ledger_final",
    "journal",
    "decision",
    "cycle",
    "snapshot",
]


def _install_crash_after_write(mp, boundary: str) -> None:
    """Wrap every run_evening writer so it commits, then raises for ``boundary``."""
    real_atomic = overnight.atomic_write_json
    real_save_ledger = overnight.save_ledger
    real_save_plan = overnight.save_plan
    real_save_hypotheses = overnight.save_hypotheses
    real_save_watchlist = overnight.save_watchlist
    real_save_philosophy = overnight.save_philosophy
    real_append = overnight.append_journal
    real_record = overnight.record_evening_and_refresh_page
    counts = {"ledger": 0, "plan": 0}

    def crash(label: str) -> None:
        if boundary == label:
            raise RuntimeError("injected_crash:" + label)

    def atomic(path, payload):
        real_atomic(path, payload)
        crash(
            {
                "review_latest.json": "review",
                "cycle_latest.json": "cycle",
                "snapshot_latest.json": "snapshot",
            }.get(Path(path).name, "")
        )

    def save_ledger(ledger, path=None):
        result = real_save_ledger(ledger, path)
        counts["ledger"] += 1
        crash("ledger_settle" if counts["ledger"] == 1 else "ledger_final")
        return result

    def save_plan(plan, path=None):
        result = real_save_plan(plan, path)
        counts["plan"] += 1
        crash("plan_settle" if counts["plan"] == 1 else "plan_tonight")
        return result

    def save_hypotheses(*args, **kwargs):
        result = real_save_hypotheses(*args, **kwargs)
        crash("hypotheses")
        return result

    def save_watchlist(*args, **kwargs):
        result = real_save_watchlist(*args, **kwargs)
        crash("watchlist")
        return result

    def save_philosophy(*args, **kwargs):
        result = real_save_philosophy(*args, **kwargs)
        crash("philosophy")
        return result

    def append_journal(*args, **kwargs):
        real_append(*args, **kwargs)
        crash("journal")

    def record(*args, **kwargs):
        real_record(*args, **kwargs)
        crash("decision")

    mp.setattr(overnight, "atomic_write_json", atomic)
    mp.setattr(overnight, "save_ledger", save_ledger)
    mp.setattr(overnight, "save_plan", save_plan)
    mp.setattr(overnight, "save_hypotheses", save_hypotheses)
    mp.setattr(overnight, "save_watchlist", save_watchlist)
    mp.setattr(overnight, "save_philosophy", save_philosophy)
    mp.setattr(overnight, "append_journal", append_journal)
    mp.setattr(overnight, "record_evening_and_refresh_page", record)


def _decision_identities(runtime: CyclePaths) -> list[str]:
    if not runtime.decision_log.is_file():
        return []
    rows = [
        json.loads(line)
        for line in runtime.decision_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # ``recorded_at`` is a legitimate write timestamp and may refresh; identity is
    # the decision_id, and no two rows may share it.
    return [str(row.get("decision_id") or "") for row in rows]


def _same_cycle_rows(runtime: CyclePaths) -> tuple[int, int, list[str]]:
    ledger = json.loads(runtime.ledger.read_text(encoding="utf-8"))
    notes = [
        json.loads(line)
        for line in runtime.journal_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    journal_rows = len([n for n in notes if n.get("as_of") == "2026-09-11"])
    return len(ledger["trades"]), journal_rows, _decision_identities(runtime)


@pytest.mark.parametrize("boundary", _WRITE_BOUNDARIES)
def test_run_evening_write_boundary_crash_recovers_exactly_once(
    tmp_path: Path, monkeypatch, boundary: str
) -> None:
    """S3 full: a real fill must land exactly once across every run_evening write boundary.

    Each boundary is recorded with its actual hook and injection point (see
    ``_install_crash_after_write``); the crash happens *after* that writer has
    committed, so the re-entry must recognise the committed effect.
    """

    root = _seed_root(
        tmp_path, plan=deepcopy(_PENDING_BUY_PLAN), cash=100000.0, position=False
    )
    runtime = _cycle_paths(root)
    _stub_evening(monkeypatch, overnight.settle_plan)

    with pytest.MonkeyPatch.context() as mp:
        _install_crash_after_write(mp, boundary)
        with pytest.raises(RuntimeError, match="injected_crash"):
            overnight.run_evening(paths=runtime, network_news=False)

    # Re-enter twice without the fault: financial effect exactly once, records unique.
    overnight.run_evening(paths=runtime, network_news=False)
    trades_1, journal_1, decisions_1 = _same_cycle_rows(runtime)
    overnight.run_evening(paths=runtime, network_news=False)
    trades_2, journal_2, decisions_2 = _same_cycle_rows(runtime)

    assert trades_1 == trades_2 == 1, f"{boundary}: expected one fill, got {trades_1}/{trades_2}"
    assert journal_1 == journal_2 == 1, f"{boundary}: expected one journal row, got {journal_1}/{journal_2}"
    assert decisions_1 == decisions_2, f"{boundary}: re-entry duplicated decision records"
    assert len(decisions_2) == len(set(decisions_2)), f"{boundary}: duplicate decision rows"


# ---------------------------------------------------------------------------
# A2 (real flow): run_evening must honour the recovery verdict for its own cycle
# ---------------------------------------------------------------------------


def _prior_cycle_plan(llm: dict) -> dict:
    """A plan already committed for the same as_of (an interrupted tonight)."""
    plan = deepcopy(_SETTLED_PLAN)
    plan.update(
        {
            "decision_date": "2026-09-11",
            "trade_date": "2026-09-14",
            "status": "committed",
            "settled_on": "",
            "llm": llm,
        }
    )
    return plan


def test_run_evening_interrupted_cycle_never_recalls_model(tmp_path, monkeypatch) -> None:
    prior = _prior_cycle_plan({"used": False, "unavailable_reason": "not_enabled_or_unconfigured"})
    root = _seed_root(tmp_path, plan=prior, cash=100000.0, position=False)
    runtime = _cycle_paths(root)
    _stub_evening(monkeypatch, overnight.settle_plan)

    def must_not_call(*args, **kwargs):
        raise AssertionError("the model must not be recalled to recover an interrupted cycle")

    monkeypatch.setattr(overnight, "propose_picks", must_not_call)

    snapshot = overnight.run_evening(paths=runtime, network_news=False)

    assert snapshot["plan"]["llm"]["recovery_status"] == "missing_original_model_output"
    assert snapshot["plan"]["llm"]["used"] is False
    assert "llm_recovery_missing_original_model_output" in snapshot["warnings"]


def test_run_evening_recovery_replays_persisted_output_without_network(tmp_path, monkeypatch) -> None:
    import ai_trader.llm as llm_module

    raw_text = json.dumps({"buys": [], "sells": [], "stance": {}, "lesson": "", "market_note": ""})
    prior = _prior_cycle_plan(
        {
            "used": True,
            "model": "fixture-model",
            "raw_response": {
                "text": raw_text,
                "model": "fixture-model",
                "source": "llm",
                "created_at": "2026-09-11T20:20:00+08:00",
            },
        }
    )
    root = _seed_root(tmp_path, plan=prior, cash=100000.0, position=False)
    runtime = _cycle_paths(root)
    # Keep the real propose_picks so the persisted-reply path is exercised.
    _stub_evening(monkeypatch, overnight.settle_plan, stub_model=False)

    def no_network(*args, **kwargs):
        raise AssertionError("network must not be used when replaying persisted output")

    monkeypatch.setattr(llm_module, "_call", no_network)

    snapshot = overnight.run_evening(paths=runtime, network_news=False)

    assert snapshot["plan"]["llm"]["recovery_status"] == "reusable_original_output"
    assert snapshot["plan"]["llm"]["used"] is True
    assert snapshot["plan"]["llm"]["raw_response"]["reused"] is True
