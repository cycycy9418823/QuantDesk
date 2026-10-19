"""Isolated evidence, execution and recovery tests; all market rows are synthetic."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quantdesk.paper import auto_paper_portfolio as paper
from quantdesk.paper import snapshot_open_fill as opening
from quantdesk.paper import strategy_fill_journal as journal
from quantdesk.paper.trade_costs import buy_cash_outflow
from quantdesk.paper.trade_costs import compute_trade_costs, sell_cash_inflow
from tests.test_b5_formal_candidate_intake import dump, make_formal_batch, prepare_root
from tests.test_daily_candidate_refresh import _strategy_payload


def calendar(start: str = "2026-10-08", end: str = "2026-10-16") -> dict:
    day, last, flags = date.fromisoformat(start), date.fromisoformat(end), {}
    while day <= last:
        flags[day.isoformat()] = day.weekday() < 5
        day += timedelta(days=1)
    return {"is_open_by_date": flags}


def source_row(day: str = "2026-10-13", **changes) -> dict:
    return {"snapshot_date": day, "snapshot_time": "0930", "symbol": "600000", "name": "浦发银行",
            "price": "10.05", "open": "10.00", "pre_close": "10.00", "volume": "10000",
            "quote_date": day, "quote_time": "09:30:03", "fetched_at": f"{day}T09:30:09+08:00",
            "source": "synthetic_fixture", "price_basis": "raw", **changes}


def write_snapshot(root: Path, day: str = "2026-10-13", rows: list[dict] | None = None) -> Path:
    rows = rows if rows is not None else [source_row(day)]
    path = root / day / "0930.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def intent(side: str = "buy", signal: str = "2026-10-12", fill: str = "2026-10-13", **changes) -> dict:
    order = opening.make_pending_order(side, {"symbol": "600000", "name": "浦发银行", "strategy_type": "long"},
                                        signal_date=signal, fill_date=fill, quantity=100,
                                        reason="synthetic test", reference_price=10,
                                        recorded_at=f"{signal}T18:00:00+08:00")
    order["session_calendar_evidence"] = calendar(signal, fill)
    order.update(changes)
    return order


def evaluate(root: Path, row: dict | None = None, order: dict | None = None) -> dict:
    row = row or source_row()
    order = order or intent()
    write_snapshot(root, order["fill_date"], [row])
    snapshot = opening.load_open_snapshot(root, order["fill_date"])
    return opening.evaluate_open_fill(order, snapshot, order["fill_date"],
                                      calendar=calendar(order["signal_date"], order["fill_date"]),
                                      observed_at=f"{order['fill_date']}T18:00:00+08:00")


def test_open_fill_keeps_real_source_bytes_and_proxy_label(tmp_path: Path) -> None:
    result = evaluate(tmp_path)
    assert result["ok"] and result["price"] == 10
    assert result["fill_quality"] == "raw_open_paper_proxy" and result["liquidity_verified"] is False
    evidence = result["evidence"]
    assert evidence["quote_date"] == "2026-10-13"
    assert evidence["source_row"] == source_row()
    assert evidence["snapshot_sha256"] == hashlib.sha256(Path(evidence["snapshot_path"]).read_bytes()).hexdigest()
    assert evidence["source_row_sha256"] == journal.state_digest(source_row())


@pytest.mark.parametrize("change,reason", [
    ({"quote_date": ""}, "quote_date_missing_or_stale"),
    ({"quote_date": "2026-10-12"}, "quote_date_missing_or_stale"),
    ({"price_basis": ""}, "snapshot_price_basis_unverified"),
    ({"price_basis": "qfq"}, "snapshot_price_basis_unverified"),
    ({"quote_time": ""}, "quote_timestamp_unverified"),
    ({"quote_time": "09:29:59"}, "quote_or_fetch_outside_open_window"),
    ({"quote_time": "09:30:11"}, "quote_or_fetch_outside_open_window"),
    ({"fetched_at": "2026-10-13T09:35:00+08:00"}, "quote_or_fetch_outside_open_window"),
    ({"fetched_at": "2026-10-13T09:30:09"}, "quote_timestamp_unverified"),
    ({"source": ""}, "quote_source_missing"),
    ({"snapshot_time": "0935"}, "snapshot_slot_or_date_mismatch"),
    ({"snapshot_date": "2026-10-12"}, "snapshot_slot_or_date_mismatch"),
    ({"volume": "0"}, "suspended_or_no_volume"),
    ({"open": "nan"}, "open_price_invalid"),
    ({"pre_close": ""}, "limit_unverifiable"),
    ({"pre_close": "9.8"}, "opening_reference_changed_corporate_action_or_source_unresolved"),
])
def test_missing_stale_or_untrusted_fields_never_fill(tmp_path: Path, change: dict, reason: str) -> None:
    result = evaluate(tmp_path, source_row(**change))
    assert not result["ok"] and result["reason_code"] == reason
    assert result["price"] is None


@pytest.mark.parametrize("symbol,name,side,price,signal,fill,reason", [
    ("600000", "浦发银行", "buy", "11", "2026-10-12", "2026-10-13", "limit_up"),
    ("600000", "浦发银行", "sell", "9", "2026-10-12", "2026-10-13", "limit_down"),
    ("300001", "特锐德", "buy", "12", "2026-10-12", "2026-10-13", "limit_up"),
    ("688001", "华兴源创", "sell", "8", "2026-10-12", "2026-10-13", "limit_down"),
    ("920001", "合成北交样本", "buy", "13", "2026-10-12", "2026-10-13", "limit_up"),
    ("600001", "ST合成", "buy", "10.5", "2026-07-01", "2026-07-02", "limit_up"),
    ("600001", "ST合成", "buy", "11", "2026-10-12", "2026-10-13", "limit_up"),
])
def test_shared_board_and_date_limits_apply_to_open(
    tmp_path: Path, symbol: str, name: str, side: str, price: str, signal: str, fill: str, reason: str,
) -> None:
    result = evaluate(tmp_path, source_row(fill, symbol=symbol, name=name, open=price),
                      intent(side, signal, fill, symbol=symbol, name=name))
    assert not result["ok"] and result["reason_code"] == reason


@pytest.mark.parametrize("created,reason", [
    ("2026-10-13T09:30:00+08:00", "decision_not_recorded_before_open"),
    ("2026-10-12T14:59:59+08:00", "decision_not_recorded_before_open"),
    ("2026-10-12T18:00:00", "order_chronology_unverified"),
])
def test_decisions_cannot_be_backdated_from_an_observed_open(tmp_path: Path, created: str, reason: str) -> None:
    result = evaluate(tmp_path, order=intent(created_at=created))
    assert result["reason_code"] == reason and not result["ok"]


def test_duplicates_missing_files_and_calendar_gaps_fail_closed(tmp_path: Path) -> None:
    missing = opening.load_open_snapshot(tmp_path, "2026-10-13")
    assert opening.evaluate_open_fill(intent(), missing, "2026-10-13", calendar=calendar())["reason_code"] == "snapshot_missing"
    write_snapshot(tmp_path, rows=[source_row(), source_row(open="10.1")])
    loaded = opening.load_open_snapshot(tmp_path, "2026-10-13")
    assert loaded["duplicate_symbols"] == ["600000"]
    assert opening.evaluate_open_fill(intent(), loaded, "2026-10-13", calendar=calendar())["reason_code"] == "duplicate_snapshot_symbol"
    with pytest.raises(ValueError, match="next_session_unverified"):
        opening.next_trading_day("2026-10-09", {"is_open_by_date": {"2026-10-09": True, "2026-10-12": True}})
    assert opening.next_trading_day("2026-10-09", calendar()) == "2026-10-12"


def setup_book(tmp_path: Path, monkeypatch) -> None:
    prepare_root(tmp_path, monkeypatch)
    cal_path = tmp_path / "data/calendar/a_share_trading_calendar.csv"
    cal_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"date": day, "is_open": int(flag)} for day, flag in calendar().get("is_open_by_date", {}).items()]).to_csv(cal_path, index=False)


def daily(root: Path, symbol: str, prices: dict[str, float], *, basis_overrides: dict[str, str] | None = None) -> None:
    path = root / "data/daily" / f"{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"date": day, "open": close, "high": close + 0.2, "low": close - 0.2, "close": close,
                   "pre_close": 10, "volume": 100000, "amount": close * 100000,
                   "adjust": (basis_overrides or {}).get(day, "raw")} for day, close in prices.items()]).to_csv(path, index=False)


def run_day(root: Path, monkeypatch, day: str, *, close: float = 10.0, mode: str = "update",
            fill_mode: str = "next_open_snapshot", **kwargs) -> dict:
    monkeypatch.setattr(paper, "utc_now_iso", lambda: f"{day}T10:00:00+00:00")
    monkeypatch.setattr(paper, "now_text", lambda: f"{day}T18:00:00+08:00")
    candidate = _strategy_payload(day)["standard_candidates"][0]
    candidate.update(name="浦发银行", close=close, amount_ma20=50_000_000.0)
    batch = make_formal_batch(day, candidate=candidate)
    assert len(batch["candidates"]) == 1
    path = root / "results/candidate_refresh/daily" / f"{day}.json"
    dump(path, batch)
    return paper.run_auto_paper_portfolio(mode=mode, initial_cash=10_000, plan_date=day,
                                          formal_candidate_batch=batch, formal_candidate_batch_path=path,
                                          formal_candidate_status="passed", risk_gate_shadow_enabled=False,
                                          strategy_fill_mode=fill_mode, **kwargs)


def test_public_opt_in_queues_fills_at_next_open_marks_close_and_replays_once(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10.0, "2026-10-13": 10.3, "2026-10-14": 99.0})
    first = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")
    assert first["ok"], first
    assert first["summary"]["cash"] == 10_000 and first["positions"] == []
    report_path = tmp_path / first["report_path"]
    assert "研究输出·未验证" in report_path.read_text(encoding="utf-8")
    assert len(first["pending_orders"]) == 1 and first["pending_orders"][0]["fill_date"] == "2026-10-13"
    assert not any(row["action"] == "buy" for row in first["actions"])
    write_snapshot(tmp_path / "data/snapshots", rows=[source_row(open="10.10")])
    second = run_day(tmp_path, monkeypatch, "2026-10-13", close=10.3)
    assert second["ok"], second
    position = second["positions"][0]
    assert position["entry_date"] == "2026-10-13" and position["execution_price"] == 10.1
    assert position["latest_price"] == 10.3 and position["market_value"] == 1030
    cost = buy_cash_outflow(price=10.1, quantity=100)
    assert second["summary"]["cash"] == round(10_000 - cost["cash_out"], 2)
    state_path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON
    journal_path = Path(second["fill_journal_path"])
    saved_state, saved_journal = state_path.read_bytes(), journal_path.read_bytes()
    replay = journal.replay_journal(journal_path)
    assert replay["state"] == json.loads(saved_state)
    assert len(replay["records"]) == 3
    rerun = run_day(tmp_path, monkeypatch, "2026-10-13", close=10.3)
    assert rerun["ok"] and rerun["idempotent_replay"]
    assert state_path.read_bytes() == saved_state and journal_path.read_bytes() == saved_journal
    assert rerun["summary"] == second["summary"]
    history = json.loads(paper.AUTO_PAPER_PORTFOLIO_HISTORY_JSON.read_text(encoding="utf-8"))
    assert len(history["items"]) == 2


def test_inherited_policy_is_checkpointed_and_old_trades_stay_labelled(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10})
    legacy = paper.reconcile_paper_account(paper._empty_state(10_000))
    legacy["actions"] = [{"action": "buy", "fill_model": "daily_approx", "symbol": "600007"}]
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, legacy)
    result = run_day(tmp_path, monkeypatch, "2026-10-12")
    assert result["ok"], result
    replay = journal.replay_journal(Path(result["fill_journal_path"]))
    assert replay["records"][0]["state"] == legacy
    assert replay["records"][0]["inherited_history_status"] == "legacy_opening_balance_unverified"
    assert replay["state"]["fill_policy_transition"]["from"]["fill_policy"] == "daily_approx"
    assert replay["state"]["fill_policy"] == "next_open"
    assert replay["records"][1]["events"][0]["scope"] == "future_fills_only"
    old_bytes = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    refused = run_day(tmp_path, monkeypatch, "2026-10-12", fill_mode="same_day_close")
    assert refused["error"] == "snapshot_ledger_requires_explicit_snapshot_mode"
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == old_bytes


def test_open_cash_gate_uses_gap_price_and_fees(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10, "2026-10-13": 10.2})
    state = paper.reconcile_paper_account(paper._empty_state(10_000))
    state["cash"] = state["total_equity"] = 1010
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, state)
    assert run_day(tmp_path, monkeypatch, "2026-10-12")["ok"]
    write_snapshot(tmp_path / "data/snapshots", rows=[source_row(open="10.90")])
    result = run_day(tmp_path, monkeypatch, "2026-10-13", close=10.2)
    assert result["ok"], result
    assert result["summary"]["cash"] == 1010 and result["positions"] == []
    assert result["snapshot_outcomes"][0]["reason_code"] == "insufficient_open_cash"


def test_legacy_default_close_fill_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10})
    default = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init", fill_mode="same_day_close", dry_run=True)
    assert default["ok"] and default["positions"][0]["entry_date"] == "2026-10-12"
    assert default["positions"][0]["execution_price"] == 10
    assert "pending_orders" not in default


def test_limit_up_signal_is_queued_but_limit_up_next_open_does_not_fill(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 11, "2026-10-13": 11.2})
    first = run_day(tmp_path, monkeypatch, "2026-10-12", close=11, mode="init")
    assert first["ok"] and len(first["pending_orders"]) == 1
    write_snapshot(tmp_path / "data/snapshots", rows=[source_row(open="12.10", pre_close="11")])
    second = run_day(tmp_path, monkeypatch, "2026-10-13", close=11.2)
    assert second["ok"] and second["positions"] == []
    assert second["snapshot_outcomes"][0]["reason_code"] == "limit_up"
    assert second["pending_orders"][0]["signal_date"] == "2026-10-13"  # a new signal, no rollover


def test_missing_snapshot_is_rejected_and_missed_session_expires(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10, "2026-10-13": 10, "2026-10-15": 10})
    assert run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")["ok"]
    second = run_day(tmp_path, monkeypatch, "2026-10-13")
    assert second["ok"] and second["snapshot_outcomes"][0]["reason_code"] == "snapshot_missing"
    third = run_day(tmp_path, monkeypatch, "2026-10-15")
    assert third["ok"] and third["snapshot_outcomes"][0]["status"] == "expired"
    assert third["positions"] == [] and third["summary"]["cash"] == 10_000


def test_raw_contract_is_checked_before_selecting_as_of_day(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10, "2026-10-14": 999}, basis_overrides={"2026-10-14": "qfq"})
    result = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")
    assert result["ok"] and result["pending_orders"] == []
    assert "snapshot_signal_price_set_blocked" in result["warnings"]


@pytest.mark.parametrize("failure_point", ["state", "latest"])
def test_fsynced_transaction_recovers_after_interrupted_state_or_report_write(
    tmp_path: Path, monkeypatch, failure_point: str,
) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10, "2026-10-13": 10.2})
    assert run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")["ok"]
    write_snapshot(tmp_path / "data/snapshots", rows=[source_row(open="10.10")])
    if failure_point == "state":
        original = paper._auto_paper_state_store
        class FailingStore:
            def write(self, payload):
                raise OSError("synthetic interrupted ledger replace")
        monkeypatch.setattr(paper, "_auto_paper_state_store", lambda: FailingStore())
    else:
        original = paper._write_auto_paper_latest
        monkeypatch.setattr(paper, "_write_auto_paper_latest", lambda *_: (_ for _ in ()).throw(OSError("synthetic interrupted latest")))
    interrupted = run_day(tmp_path, monkeypatch, "2026-10-13", close=10.2)
    assert not interrupted["ok"] and interrupted["journal_may_require_recovery"]
    journal_path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.with_suffix(".fills.jsonl")
    records_before = journal_path.read_bytes()
    monkeypatch.setattr(paper, "_auto_paper_state_store" if failure_point == "state" else "_write_auto_paper_latest", original)
    recovered = run_day(tmp_path, monkeypatch, "2026-10-13", close=10.2)
    assert recovered["ok"] and recovered["idempotent_replay"]
    assert len(recovered["positions"]) == 1 and recovered["positions"][0]["quantity"] == 100
    assert journal_path.read_bytes() == records_before
    assert json.loads(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_text(encoding="utf-8")) == journal.replay_journal(journal_path)["state"]


def test_torn_journal_or_external_ledger_change_blocks_without_overwrite(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10})
    result = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")
    assert result["ok"]
    path = Path(result["fill_journal_path"])
    complete = path.read_bytes()
    path.write_bytes(complete + b'{"incomplete":')
    state_bytes, corrupt_journal = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes(), path.read_bytes()
    blocked = run_day(tmp_path, monkeypatch, "2026-10-12")
    assert not blocked["ok"] and "journal_incomplete_tail" in blocked["error"]
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == state_bytes and path.read_bytes() == corrupt_journal
    path.write_bytes(complete)  # repairing synthetic evidence only
    state = json.loads(state_bytes)
    state["cash"] += 1
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, state)
    mutated = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes()
    blocked = run_day(tmp_path, monkeypatch, "2026-10-12")
    assert not blocked["ok"] and "journal_ledger_conflict" in blocked["error"]
    assert paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_bytes() == mutated and path.read_bytes() == complete


def test_journal_rejects_unexplained_cash_and_position_changes(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10})
    result = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")
    path = Path(result["fill_journal_path"])
    before = journal.replay_journal(path)["state"]
    after = copy.deepcopy(before)
    after["cash"] += 100
    after["total_equity"] += 100
    previous_bytes = path.read_bytes()
    with pytest.raises(journal.JournalError, match="unexplained_cash_change"):
        journal.commit_transaction(path, before=before, after=after, events=[], transaction_id="tamper",
                                    recorded_at="2026-10-13T18:00:00+08:00", result={}, ledger_existed=True)
    assert path.read_bytes() == previous_bytes


def test_reset_unknown_mode_conditional_mode_and_unjournaled_orders_are_refused(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10})
    with pytest.raises(ValueError, match="unknown paper.strategy_fill_mode"):
        opening.resolve_strategy_fill_mode("next_open_snapsho")
    blocked = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init", enable_intraday_swing=True)
    assert not blocked["ok"] and "incompatible" in blocked["error"]
    state = paper.reconcile_paper_account(paper._empty_state(10_000))
    state["pending_orders"] = [intent()]
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, state)
    blocked = run_day(tmp_path, monkeypatch, "2026-10-12")
    assert blocked["error"] == "snapshot_pending_orders_have_no_journal_proof"
    blocked = run_day(tmp_path, monkeypatch, "2026-10-12", force_reset=True)
    assert blocked["error"] == "snapshot_reset_requires_new_ledger_and_journal"


def test_existing_stop_rule_sells_at_next_open_with_fee_net_replay(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10, "2026-10-13": 10})
    daily(tmp_path, "600007", {"2026-10-12": 9.1, "2026-10-13": 8.9})
    candidate = {"symbol": "600007", "name": "中国国贸", "strategy_type": "long"}
    bought = paper._make_position(candidate, 100, 10, "2026-10-08")
    legacy = paper._empty_state(10_000)
    legacy.update(positions=[bought], cash=10_000 - bought["entry_cash_out"])
    legacy = paper.reconcile_paper_account(legacy)
    dump(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON, legacy)
    first = run_day(tmp_path, monkeypatch, "2026-10-12")
    assert first["ok"], first
    assert {row["side"] for row in first["pending_orders"]} == {"sell", "buy"}
    assert len(first["positions"]) == 1 and first["closed_positions"] == []
    write_snapshot(tmp_path / "data/snapshots", rows=[source_row(),
                    source_row(symbol="600007", name="中国国贸", open="9.05", pre_close="9.1")])
    result = run_day(tmp_path, monkeypatch, "2026-10-13")
    assert result["ok"], result
    assert [row["side"] for row in result["snapshot_outcomes"]] == ["sell", "buy"]
    sold = result["closed_positions"][0]
    assert sold["exit_price"] == 9.05 and sold["exit_date"] == "2026-10-13"
    proceeds = sell_cash_inflow(price=9.05, quantity=100, avg_cost=bought["entry_price"])
    assert sold["realized_pnl"] == round(proceeds["realized_pnl"], 2)
    assert result["summary"]["cash"] == round(legacy["cash"] + proceeds["cash_in"] - buy_cash_outflow(price=10, quantity=100)["cash_out"], 2)
    replay = journal.replay_journal(Path(result["fill_journal_path"]))
    assert replay["state"] == json.loads(paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.read_text(encoding="utf-8"))
    # Reusing an already consumed intent is rejected even with internally valid prices.
    last = replay["records"][-1]
    outcomes = [event for event in last["events"] if event["event_type"] == "order_outcome"]
    with pytest.raises(journal.JournalError, match="outcome_without_prior_intent"):
        journal.validate_transaction(replay["previous_state"], replay["state"], [*last["events"], outcomes[0]])


@pytest.mark.parametrize("quantity,entry_date,reason", [
    (100, "2026-10-13", "t_plus_one_violation"),
    (99, "2026-10-08", "invalid_order_lot"),
])
def test_journal_independently_enforces_t_plus_one_and_board_lots(
    tmp_path: Path, quantity: int, entry_date: str, reason: str,
) -> None:
    order = intent("sell", quantity=quantity)
    checked = evaluate(tmp_path, order=order)
    assert checked["ok"]
    position = paper._make_position({"symbol": "600000", "strategy_type": "long"}, 100, 10, entry_date)
    before = paper.reconcile_paper_account({**paper._empty_state(10_000), "positions": [position],
                                          "cash": 8994.5, "strategy_fill_mode": "next_open_snapshot", "fill_policy": "next_open",
                                          "pending_orders": [order]})
    flow = sell_cash_inflow(price=10, quantity=quantity, avg_cost=position["entry_price"])
    event = opening.fill_journal_row(order, status="filled", decision_date="2026-10-13", price=10,
                                     quantity=quantity, evidence=checked["evidence"], limits=checked["limits"],
                                     amounts={**compute_trade_costs(side="sell", gross=quantity * 10),
                                              "gross": quantity * 10, "cash_delta": flow["cash_in"]},
                                     recorded_at="2026-10-13T18:00:00+08:00")
    after = paper.reconcile_paper_account({**before, "positions": [], "pending_orders": [],
                                         "cash": round(before["cash"] + flow["cash_in"], 2)})
    with pytest.raises(journal.JournalError, match=reason):
        journal.validate_transaction(before, after, [event])


def test_initialization_can_recover_without_an_existing_state_file(tmp_path: Path, monkeypatch) -> None:
    setup_book(tmp_path, monkeypatch)
    daily(tmp_path, "600000", {"2026-10-12": 10})
    original = paper._auto_paper_state_store
    class FailingStore:
        def write(self, payload):
            raise OSError("synthetic initial write interruption")
    monkeypatch.setattr(paper, "_auto_paper_state_store", lambda: FailingStore())
    failed = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")
    assert not failed["ok"] and not paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.exists()
    path = paper.AUTO_PAPER_PORTFOLIO_STATE_JSON.with_suffix(".fills.jsonl")
    recorded = path.read_bytes()
    monkeypatch.setattr(paper, "_auto_paper_state_store", original)
    restored = run_day(tmp_path, monkeypatch, "2026-10-12", mode="init")
    assert restored["ok"] and restored["idempotent_replay"]
    assert len(restored["pending_orders"]) == 1 and path.read_bytes() == recorded


def test_unknown_basis_in_one_requested_symbol_blocks_other_open_fill(tmp_path: Path) -> None:
    write_snapshot(tmp_path, rows=[source_row(), source_row(symbol="600007", price_basis="qfq")])
    loaded = opening.load_open_snapshot(tmp_path, "2026-10-13")
    gate = opening.snapshot_price_gate(loaded, ["600000", "600007"])
    assert not gate["eligible"] and gate["blocked_symbols"] == ["600007"]


@pytest.mark.parametrize("content", ["", "date,close,adjust\n2026-10-12,10,raw\n",
    "date,open,high,low,close,volume,adjust\n2026-10-12,10,inf,9,10,1000,raw\n"])
def test_malformed_or_nonfinite_daily_evidence_is_reported_without_a_signal(tmp_path: Path, monkeypatch, content: str) -> None:
    monkeypatch.setattr(paper, "DAILY_DIR", tmp_path)
    (tmp_path / "600000.csv").write_text(content, encoding="utf-8")
    quote = paper._snapshot_daily_quotes(["600000"], "2026-10-12")["600000"]
    assert not quote["valid"] and quote["warnings"]
