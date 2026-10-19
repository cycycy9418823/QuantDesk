"""Cross-account rejection and corrupted-value visibility, all in tmp fixtures."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from ai_trader import app as viewer
from ai_trader import ledger as books
from ai_trader import overnight
from ai_trader.paths import CyclePaths, ledger_id_for_path
from tests import _ai_recovery_fixtures as fx


def files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def book_path(root: Path, profile: str = "") -> Path:
    return root / "state" / ("ai_trader_" + profile if profile else "ai_trader") / "ledger.json"


@pytest.mark.parametrize("profile", ["", "glm"])
def test_foreign_save_is_refused_before_touching_account(tmp_path, profile):
    target = book_path(tmp_path, profile)
    own = books.empty_ledger(ledger_id=ledger_id_for_path(target))
    books.save_ledger(own, target)
    before = files(tmp_path)
    foreign_path = book_path(tmp_path, "" if profile else "glm")
    foreign = books.empty_ledger(ledger_id=ledger_id_for_path(foreign_path))
    foreign["cash"] = 1.0
    with pytest.raises(ValueError, match="ledger_id mismatch"):
        books.save_ledger(foreign, target)
    assert files(tmp_path) == before
    assert books.load_ledger(target, missing_ok=False)["cash"] == 100_000


def test_invalid_first_save_creates_no_directory(tmp_path):
    target = book_path(tmp_path, "glm")
    with pytest.raises(ValueError, match="ledger_id mismatch"):
        books.save_ledger(books.empty_ledger(ledger_id="ai_trader_100k"), target)
    assert not target.parent.exists()


@pytest.mark.parametrize("profile,environment_profile", [("glm", ""), ("", "glm")])
def test_first_run_identity_follows_explicit_path(tmp_path, monkeypatch, profile, environment_profile):
    monkeypatch.setenv("AI_TRADER_PROFILE", environment_profile)
    target = book_path(tmp_path, profile)
    created = books.init_ledger(path=target)
    assert created["ledger_id"] == ledger_id_for_path(target)
    assert books.load_ledger(target, missing_ok=False)["ledger_id"] == created["ledger_id"]
    assert created["cash"] == 100_000 and created["trades"] == []


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_nonfinite_saved_amounts_are_unreadable_and_get_does_not_repair(tmp_path, monkeypatch, token):
    target = book_path(tmp_path)
    target.parent.mkdir(parents=True)
    raw = json.dumps(books.empty_ledger(ledger_id=ledger_id_for_path(target))).replace('"equity": 100000.0', '"equity": ' + token)
    assert token in raw
    target.write_text(raw, encoding="utf8")
    paths = CyclePaths(ledger=target, snapshot=tmp_path / "results/ai_trader/snapshot_latest.json")
    monkeypatch.setattr(viewer, "profile_paths", lambda: paths)
    before = files(tmp_path)
    with pytest.raises(ValueError, match="nonfinite"):
        books.load_ledger(target, missing_ok=False)
    client = viewer.create_app().test_client()
    response = client.get("/api/ledger")
    assert response.status_code == 500 and response.get_json()["status"] == "ledger_unreadable"
    snapshot = client.get("/api/snapshot").get_json()
    assert snapshot["ledger_status"] == "unreadable"
    assert snapshot["journal"]["account"]["equity"] is None
    assert snapshot["journal"]["account"]["pnl"] is None
    assert files(tmp_path) == before


def produce(tmp_path: Path):
    daily = tmp_path / "daily"
    fx.write_daily(daily, "600500", prev_close=5.0, open_px=5.08, close=5.10)
    original = fx.plan(plan_id="fixture_plan", intents=[fx.buy_intent("600500", quantity=100, ref_close=5.0)])
    original["ledger_id"] = "ai_trader_100k"
    book = books.empty_ledger(ledger_id=original["ledger_id"])
    settled, after, actions = overnight.settle_plan(deepcopy(original), deepcopy(book), as_of=fx.TRADE_DATE,
                                                   daily_dir=daily, evidence_dir=tmp_path / "evidence")
    assert len(after["trades"]) == 1
    assert after["cash"] == pytest.approx(book["cash"] + sum(t["cash_delta"] for t in after["trades"]))
    assert after["equity"] == pytest.approx(after["cash"] + after["market_value"])
    return original, book, settled, after, actions, daily


@pytest.mark.parametrize("status", ["pending_open", "settled"])
def test_explicit_foreign_plan_cannot_settle_or_impersonate_recovery(tmp_path, status):
    original, before_book, settled, after, _, daily = produce(tmp_path)
    plan = deepcopy(original if status == "pending_open" else settled)
    plan["ledger_id"] = "ai_trader_100k_glm"
    supplied = deepcopy(before_book if status == "pending_open" else after)
    before = deepcopy(supplied)
    disk_before = files(tmp_path)
    with pytest.raises(RuntimeError, match="plan_ledger_id_mismatch"):
        overnight.settle_plan(plan, supplied, as_of=fx.TRADE_DATE, daily_dir=daily,
                              evidence_dir=tmp_path / "reentry_evidence")
    assert supplied == before and files(tmp_path) == disk_before


@pytest.mark.parametrize("field,value", [("symbol", "600501"), ("side", "sell"), ("date", fx.DECISION_DATE)])
def test_pending_plan_id_does_not_accept_foreign_trade_identity(tmp_path, field, value):
    original, _, _, after, _, daily = produce(tmp_path)
    wrong = deepcopy(after)
    wrong["trades"][0][field] = value
    before = deepcopy(wrong)
    disk_before = files(tmp_path)
    with pytest.raises(RuntimeError, match="plan_recovery_effect_unverified"):
        overnight.settle_plan(deepcopy(original), wrong, as_of=fx.TRADE_DATE, daily_dir=daily,
                              evidence_dir=tmp_path / "reentry_evidence")
    assert wrong == before and files(tmp_path) == disk_before


def test_matching_pending_recovery_preserves_money_and_original_prices(tmp_path, monkeypatch):
    original, before_book, settled, after, actions, daily = produce(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("A committed fill must not be repriced from today's mutable CSV")

    monkeypatch.setattr(overnight, "load_ohlc_row", forbidden)
    recovered, final, new_actions = overnight.settle_plan(deepcopy(original), deepcopy(after), as_of=fx.TRADE_DATE,
                                                         daily_dir=daily, evidence_dir=tmp_path / "reentry_evidence")
    assert recovered["status"] == "settled" and new_actions == []
    assert final == after and not (tmp_path / "reentry_evidence").exists()
    evidence = {"synthetic": True, "ledger_id": final["ledger_id"], "plan_id": original["plan_id"],
                "before": {k: before_book[k] for k in ["cash", "equity", "positions", "trades"]},
                "after": {k: final[k] for k in ["cash", "equity", "positions", "trades"]},
                "actions": actions, "repeat_unchanged": True}
    (tmp_path / "money_reconciliation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf8")


@pytest.mark.parametrize("identity", ["ai_trader_100k_glm", ""])
def test_financial_snapshot_cannot_show_an_unbound_account(tmp_path, monkeypatch, identity):
    target = book_path(tmp_path)
    own = books.empty_ledger(ledger_id=ledger_id_for_path(target))
    books.save_ledger(own, target)
    paths = CyclePaths(ledger=target, snapshot=tmp_path / "results/ai_trader/snapshot_latest.json")
    paths.snapshot.parent.mkdir(parents=True)
    payload = {"ok": True, "as_of": fx.TRADE_DATE, "ledger": {"ledger_id": identity, "cash": 1, "equity": 1, "positions": []},
               "journal": {"account": {"cash": 1, "equity": 1, "pnl": -99999}}}
    paths.snapshot.write_text(json.dumps(payload), encoding="utf8")
    monkeypatch.setattr(viewer, "profile_paths", lambda: paths)
    before = files(tmp_path)
    result = viewer.create_app().test_client().get("/api/snapshot").get_json()
    assert result["ledger"]["ledger_id"] == own["ledger_id"]
    assert result["ledger"]["cash"] == own["cash"]
    assert result["journal"]["account"]["pnl"] == 0
    assert result["snapshot_status"] == ("ledger_identity_mismatch" if identity else "ledger_identity_unverified")
    assert result["degraded"] is True and "未改动文件" in result["reason"]
    assert files(tmp_path) == before


def test_bound_snapshot_is_served_without_changes(tmp_path, monkeypatch):
    target = book_path(tmp_path, "glm")
    paths = CyclePaths(ledger=target, snapshot=tmp_path / "results/ai_trader_glm/snapshot_latest.json")
    paths.snapshot.parent.mkdir(parents=True)
    payload = {"ok": True, "as_of": fx.TRADE_DATE,
               "ledger": {"ledger_id": ledger_id_for_path(target), "cash": 12345, "equity": 12345, "positions": []},
               "plan": {"ledger_id": ledger_id_for_path(target)}}
    paths.snapshot.write_text(json.dumps(payload), encoding="utf8")
    monkeypatch.setattr(viewer, "profile_paths", lambda: paths)
    before = files(tmp_path)
    assert viewer.create_app().test_client().get("/api/snapshot").get_json() == payload
    assert files(tmp_path) == before and not target.exists()


@pytest.mark.parametrize("nested", [False, True])
def test_nonfinite_input_is_not_silently_saved_as_null(tmp_path, nested):
    target = book_path(tmp_path)
    book = books.empty_ledger(ledger_id=ledger_id_for_path(target))
    if nested:
        book["positions"] = [{"symbol": "600500", "quantity": 100, "market_price": float("nan")}]
    else:
        book["cash"] = float("inf")
    with pytest.raises(ValueError, match="nonfinite"):
        books.save_ledger(book, target)
    assert not target.parent.exists()


def test_full_evening_foreign_plan_is_refused_before_model_or_runtime_writes(tmp_path, monkeypatch):
    foreign = fx.plan(plan_id="foreign_plan", intents=[fx.buy_intent("600500", quantity=100, ref_close=5.0)])
    foreign["ledger_id"] = "ai_trader_100k_glm"
    root = fx.seed_root(tmp_path, plan_obj=foreign, ledger=books.empty_ledger(ledger_id="ai_trader_100k"),
                        symbols={"600500": {"prev_close": 5.0, "open": 5.08, "close": 5.10}})
    runtime = fx.cycle_paths(root)
    fx.stub_evening(monkeypatch, overnight, overnight.settle_plan)

    def forbidden(*args, **kwargs):
        pytest.fail("Foreign plan must be refused before any model call")

    monkeypatch.setattr(overnight, "propose_picks", forbidden)
    before = files(root)
    with pytest.raises(RuntimeError, match="plan_ledger_id_mismatch"):
        overnight.run_evening(paths=runtime, network_news=False)
    assert files(root) == before


def test_legal_partial_pending_recovery_does_not_require_every_intent_to_fill(tmp_path, monkeypatch):
    daily = tmp_path / "daily"
    fx.write_daily(daily, "600500", prev_close=5.0, open_px=5.08, close=5.10)
    fx.write_daily(daily, "600501", prev_close=5.0, open_px=5.50, close=5.50)
    original = fx.plan(plan_id="legal_partial", intents=[fx.buy_intent("600500", quantity=100, ref_close=5.0),
                                                       fx.buy_intent("600501", quantity=100, ref_close=5.0)])
    original["ledger_id"] = "ai_trader_100k"
    _, after, actions = overnight.settle_plan(deepcopy(original), books.empty_ledger(ledger_id="ai_trader_100k"),
                                             as_of=fx.TRADE_DATE, daily_dir=daily, evidence_dir=tmp_path / "evidence")
    assert len(after["trades"]) == 1 and any(a["status"] == "rejected" for a in actions)
    monkeypatch.setattr(overnight, "load_ohlc_row", lambda *args, **kwargs: pytest.fail("Committed effects cannot be repriced"))
    recovered, final, repeated = overnight.settle_plan(original, deepcopy(after), as_of=fx.TRADE_DATE, daily_dir=daily)
    assert recovered["status"] == "settled" and final == after and repeated == []


def test_duplicate_tagged_trade_is_not_a_pending_recovery(tmp_path):
    original, _, _, after, _, daily = produce(tmp_path)
    wrong = deepcopy(after)
    wrong["trades"].append(deepcopy(wrong["trades"][0]))
    before = deepcopy(wrong)
    with pytest.raises(RuntimeError, match="plan_recovery_effect_unverified"):
        overnight.settle_plan(original, wrong, as_of=fx.TRADE_DATE, daily_dir=daily)
    assert wrong == before


def test_pending_recovery_with_frozen_actions_keeps_exact_money_checks(tmp_path):
    original, _, settled, after, _, daily = produce(tmp_path)
    original["actions"] = deepcopy(settled["actions"])
    original["actions"][0]["cash_delta"] += 1
    before = deepcopy(after)
    with pytest.raises(RuntimeError, match="plan_recovery_effect_unverified.*cash_delta_mismatch"):
        overnight.settle_plan(original, after, as_of=fx.TRADE_DATE, daily_dir=daily)
    assert after == before


def test_nonfinite_snapshot_is_rejected_without_revaluing_own_saved_book(tmp_path, monkeypatch):
    target = book_path(tmp_path)
    book = books.empty_ledger(ledger_id=ledger_id_for_path(target))
    books.save_ledger(book, target)
    paths = CyclePaths(ledger=target, snapshot=tmp_path / "results/ai_trader/snapshot_latest.json")
    paths.snapshot.parent.mkdir(parents=True)
    paths.snapshot.write_text(json.dumps({"ok": True, "ledger": {"ledger_id": book["ledger_id"], "cash": float("nan")}}), encoding="utf8")
    monkeypatch.setattr(viewer, "profile_paths", lambda: paths)
    before = files(tmp_path)
    snapshot = viewer.create_app().test_client().get("/api/snapshot").get_json()
    assert snapshot["snapshot_status"] == "unreadable"
    assert snapshot["valuation_status"] == "saved_book_only"
    assert snapshot["ledger"]["cash"] == book["cash"] and files(tmp_path) == before


def test_correct_snapshot_book_with_foreign_plan_is_not_displayed(tmp_path, monkeypatch):
    target = book_path(tmp_path)
    book = books.empty_ledger(ledger_id=ledger_id_for_path(target))
    books.save_ledger(book, target)
    paths = CyclePaths(ledger=target, snapshot=tmp_path / "results/ai_trader/snapshot_latest.json")
    paths.snapshot.parent.mkdir(parents=True)
    paths.snapshot.write_text(json.dumps({"ok": True, "ledger": {"ledger_id": book["ledger_id"], "cash": 1},
                                          "plan": {"ledger_id": "ai_trader_100k_glm"}}), encoding="utf8")
    monkeypatch.setattr(viewer, "profile_paths", lambda: paths)
    before = files(tmp_path)
    snapshot = viewer.create_app().test_client().get("/api/snapshot").get_json()
    assert snapshot["snapshot_status"] == "ledger_identity_mismatch"
    assert snapshot["ledger"]["cash"] == book["cash"] and files(tmp_path) == before
