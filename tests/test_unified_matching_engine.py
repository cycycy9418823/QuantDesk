"""AI next-open matching must fail closed on price evidence."""

from __future__ import annotations

import csv
import json
import sys
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_trader.ledger import empty_ledger
from ai_trader.market import load_ohlc_row
from ai_trader.overnight import settle_plan
from ai_trader.paths import CyclePaths
from quantdesk.paper.matching import evaluate_next_open_fill

TRADE_DATE = "2026-09-05"
FETCHED_AT = "2026-09-05T09:31:00+08:00"


def _plan(*, symbol: str = "600000", side: str = "buy", quantity: int = 1000) -> dict:
    return {
        "trade_date": TRADE_DATE,
        "status": "pending_open",
        "live_method": {"hypothesis_id": "evidence-test"},
        "intents": [
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "ref_close": 10.0,
                "reason": "test intent",
            }
        ],
    }


def _ledger_with_position(
    symbol: str = "600000",
    *,
    quantity: int = 1000,
    cash: float = 20_000.0,
) -> dict:
    ledger = empty_ledger(created_at="2026-09-04T20:20:00+08:00")
    ledger["cash"] = cash
    ledger["positions"] = [
        {
            "symbol": symbol,
            "quantity": quantity,
            "avg_cost": 10.0,
            "acquired_on": "2026-09-01",
            "market_price": 10.0,
        }
    ]
    return ledger


def _write_bars(
    daily_dir: Path,
    symbol: str,
    *,
    open_price: object = 10.0,
    close_price: object = 10.0,
    volume: object = 1000,
    adjust: str | None = "raw",
    source: str | None = "unit_daily",
    fetched_at: str | None = FETCHED_AT,
    suspended: object | None = None,
    earlier_adjust: str | None = None,
) -> None:
    fields = [
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pct_chg",
    ]
    values: list[object] = [
        TRADE_DATE,
        symbol,
        open_price,
        open_price,
        open_price,
        close_price,
        volume,
        10000,
        0,
    ]
    optional = {
        "adjust": adjust,
        "source": source,
        "fetched_at": fetched_at,
        "is_suspended": suspended,
    }
    for name, value in optional.items():
        if value is not None:
            fields.append(name)
            values.append(value)
    rows = [",".join(fields)]
    if earlier_adjust is not None:
        earlier = dict(zip(fields, values, strict=True))
        earlier["date"] = "2026-09-04"
        earlier["adjust"] = earlier_adjust
        rows.append(",".join(str(earlier[name]) for name in fields))
    rows.append(",".join(str(value) for value in values))
    (daily_dir / f"{symbol}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _settle(daily_dir: Path, *, plan: dict | None = None, ledger: dict | None = None):
    return settle_plan(
        deepcopy(plan or _plan()),
        deepcopy(ledger or empty_ledger(created_at="2026-09-04T20:20:00+08:00")),
        as_of=TRADE_DATE,
        daily_dir=daily_dir,
    )


def _only_action(actions: list[dict]) -> dict:
    matching = [row for row in actions if row.get("symbol") == "600000"]
    assert len(matching) == 1
    return matching[0]


def test_valid_raw_open_fill_preserves_source_time_and_bar_evidence(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_bars(daily, "600000")

    _settled, ledger, actions = _settle(daily)

    action = _only_action(actions)
    assert action["status"] == "filled"
    assert action["price"] == 10.0
    assert action["price_basis"] == "raw"
    assert action["evidence_source"] == "unit_daily"
    assert action["evidence_timestamp"] == FETCHED_AT
    evidence = action["price_evidence"]
    assert {key: evidence[key] for key in (
        "symbol",
        "trade_date",
        "side",
        "price_field",
        "price",
        "price_basis",
        "source",
        "timestamp",
        "timestamp_field",
        "fetched_at",
        "volume",
    )} == {
        "symbol": "600000",
        "trade_date": TRADE_DATE,
        "side": "buy",
        "price_field": "open",
        "price": 10.0,
        "price_basis": "raw",
        "source": "unit_daily",
        "timestamp": FETCHED_AT,
        "timestamp_field": "fetched_at",
        "fetched_at": FETCHED_AT,
        "volume": 1000.0,
    }
    # B3 adds content-addressed evidence for the accepted raw bar, captured
    # before the financial mutation. It proves evidence, never a committed fill.
    from ai_trader.execution_evidence import verify_execution_receipt

    assert evidence["capture_status"] == "embedded"
    assert isinstance(evidence["evidence_id"], str) and len(evidence["evidence_id"]) == 64
    assert verify_execution_receipt(evidence["receipt"]) is True
    assert evidence["receipt"]["proves_committed_fill"] is False
    assert ledger["trades"][-1]["price_evidence"] == evidence


@pytest.mark.parametrize(
    ("adjust", "earlier_adjust", "reason"),
    [
        (None, None, "unknown_price_basis"),
        ("unknown", None, "unknown_price_basis"),
        ("raw", "qfq", "mixed_price_basis"),
        ("qfq", None, "consumer_price_basis_not_allowed"),
    ],
)
def test_untrusted_or_adjusted_basis_cannot_fill_as_raw(
    tmp_path: Path,
    adjust: str | None,
    earlier_adjust: str | None,
    reason: str,
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_bars(daily, "600000", adjust=adjust, earlier_adjust=earlier_adjust)

    _settled, ledger, actions = _settle(daily)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == reason
    assert ledger["trades"] == []


@pytest.mark.parametrize(
    ("open_price", "reason"),
    [
        ("", "missing_open_price"),
        ("nan", "nonfinite_open_price"),
        ("inf", "nonfinite_open_price"),
        (0, "invalid_open_price"),
        (-1, "invalid_open_price"),
    ],
)
def test_missing_nonfinite_or_nonpositive_open_is_rejected(
    tmp_path: Path,
    open_price: object,
    reason: str,
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_bars(daily, "600000", open_price=open_price)

    _settled, ledger, actions = _settle(daily)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == reason
    assert ledger["trades"] == []


@pytest.mark.parametrize(
    ("volume", "reason"),
    [
        ("", "missing_volume"),
        ("nan", "nonfinite_volume"),
        (0, "zero_volume"),
        (-1, "invalid_volume"),
    ],
)
def test_missing_zero_or_invalid_volume_is_rejected(
    tmp_path: Path,
    volume: object,
    reason: str,
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_bars(daily, "600000", volume=volume)

    _settled, ledger, actions = _settle(daily)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == reason
    assert ledger["trades"] == []


def test_known_suspension_is_rejected_even_with_positive_volume(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_bars(daily, "600000", suspended=True)

    _settled, ledger, actions = _settle(daily)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == "suspended"
    assert ledger["trades"] == []


@pytest.mark.parametrize(
    ("side", "open_price", "ref_close", "reason_prefix"),
    [
        ("buy", 10.0, 9.0, "limit_up:"),
        ("sell", 10.0, 11.2, "limit_down:"),
    ],
)
def test_price_limit_block_applies_to_both_sides(
    tmp_path: Path,
    side: str,
    open_price: float,
    ref_close: float,
    reason_prefix: str,
) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    _write_bars(daily, "600000", open_price=open_price)
    plan = _plan(side=side)
    plan["intents"][0]["ref_close"] = ref_close
    ledger = _ledger_with_position() if side == "sell" else None

    _settled, updated, actions = _settle(daily, plan=plan, ledger=ledger)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert str(action["reason"]).startswith(reason_prefix)
    assert updated["trades"] == []


def test_next_open_result_and_quantity_do_not_depend_on_trade_day_close(tmp_path: Path) -> None:
    plan = _plan(quantity=1000)
    ledger = _ledger_with_position("600001")
    outcomes = []
    equities = []
    for label, held_close in (("low", 10.0), ("high", 100.0)):
        daily = tmp_path / label
        daily.mkdir()
        _write_bars(daily, "600000", close_price=10.0)
        _write_bars(daily, "600001", close_price=held_close)
        _settled, updated, actions = _settle(daily, plan=plan, ledger=ledger)
        action = _only_action(actions)
        outcomes.append((action["status"], action.get("quantity"), action.get("price")))
        equities.append(updated["equity"])

    assert outcomes == [("filled", 1000, 10.0), ("filled", 1000, 10.0)]
    assert equities[0] != equities[1]


def _records(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_records(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("source", None, "missing_evidence_source"),
        ("source", "", "missing_evidence_source"),
        ("source", "nan", "missing_evidence_source"),
        ("source", "  ", "missing_evidence_source"),
        ("fetched_at", None, "missing_evidence_timestamp"),
        ("fetched_at", "", "missing_evidence_timestamp"),
        ("fetched_at", "nan", "missing_evidence_timestamp"),
        ("fetched_at", "not-a-time", "invalid_evidence_timestamp"),
        ("fetched_at", TRADE_DATE, "invalid_evidence_timestamp"),
    ],
)
def test_missing_or_invalid_provenance_cannot_create_a_trade(
    tmp_path: Path, field: str, value: str | None, reason: str
) -> None:
    _write_bars(tmp_path, "600000", **{field: value})

    _settled, ledger, actions = _settle(tmp_path)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == reason
    assert ledger["trades"] == []


@pytest.mark.parametrize("bad_field", ["date", "close"])
def test_invalid_history_row_cannot_hide_a_mixed_price_basis(
    tmp_path: Path, bad_field: str
) -> None:
    _write_bars(tmp_path, "600000", earlier_adjust="qfq")
    path = tmp_path / "600000.csv"
    records = _records(path)
    records[0][bad_field] = ""
    _write_records(path, records)

    _settled, ledger, actions = _settle(tmp_path)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == "mixed_price_basis"
    assert ledger["trades"] == []


@pytest.mark.parametrize("close_price", ["", "nan", "inf"])
def test_missing_trade_day_close_does_not_cancel_a_valid_open_fill(
    tmp_path: Path, close_price: str
) -> None:
    _write_bars(tmp_path, "600000", close_price=close_price)

    settled, ledger, actions = _settle(tmp_path)

    action = _only_action(actions)
    assert action["status"] == "filled"
    assert (action["quantity"], action["price"]) == (1000, 10.0)
    # 10,000 principal, 5 commission, 0.10 transfer fee, 5 slippage.
    assert ledger["equity"] == 99_989.9
    assert "missing_close_price:600000:using_open_mark" in ledger["warnings"]
    assert "missing_close_price:600000:using_open_mark" in settled["warnings"]


def test_duplicate_date_price_and_provenance_come_from_the_same_record(tmp_path: Path) -> None:
    _write_bars(tmp_path, "600000")
    path = tmp_path / "600000.csv"
    records = _records(path)
    records.append({**records[0], "open": "10.2", "source": "corrected_daily", "fetched_at": "2026-09-05T15:01:00+08:00"})
    _write_records(path, records)

    _settled, ledger, actions = _settle(tmp_path)

    action = _only_action(actions)
    assert action["status"] == "filled"
    assert action["price"] == 10.2
    assert action["evidence_source"] == "corrected_daily"
    assert action["evidence_timestamp"] == "2026-09-05T15:01:00+08:00"
    assert ledger["trades"][-1]["price_evidence"]["price"] == 10.2


def test_duplicate_date_cannot_borrow_volume_from_an_older_record(tmp_path: Path) -> None:
    _write_bars(tmp_path, "600000")
    path = tmp_path / "600000.csv"
    records = _records(path)
    records.append({**records[0], "volume": "0"})
    _write_records(path, records)

    _settled, ledger, actions = _settle(tmp_path)

    assert _only_action(actions)["reason"] == "zero_volume"
    assert ledger["trades"] == []


@pytest.mark.parametrize("date", ["", "2026-09-04"])
def test_matching_gate_rejects_missing_or_different_bar_date(tmp_path: Path, date: str) -> None:
    _write_bars(tmp_path, "600000")
    bar = load_ohlc_row("600000", TRADE_DATE, tmp_path)
    bar["date"] = date

    decision = evaluate_next_open_fill(
        bar, symbol="600000", side="buy", prev_close=10.0, trade_date=TRADE_DATE
    )

    assert decision["ok"] is False
    assert decision["reason"] == "price_evidence_date_mismatch"


def test_settlement_does_not_rewrite_existing_same_day_trade_evidence(tmp_path: Path) -> None:
    _write_bars(tmp_path, "600000")
    ledger = empty_ledger()
    historical = {
        "symbol": "600000", "date": TRADE_DATE, "side": "sell", "price": 9.5,
        "price_evidence": {"source": "original_receipt", "price": 9.5},
    }
    ledger["trades"] = [deepcopy(historical)]

    _settled, updated, actions = _settle(tmp_path, ledger=ledger)

    assert _only_action(actions)["status"] == "filled"
    assert updated["trades"][0] == historical
    assert updated["trades"][1]["price_evidence"]["side"] == "buy"


def test_sell_then_buy_evidence_keeps_each_trade_side(tmp_path: Path) -> None:
    _write_bars(tmp_path, "600000")
    plan = _plan(side="sell")
    plan["intents"].append(_plan(side="buy")["intents"][0])

    _settled, updated, actions = _settle(tmp_path, plan=plan, ledger=_ledger_with_position())

    fills = [row for row in actions if row.get("status") == "filled"]
    assert [(row["side"], row["price_evidence"]["side"]) for row in fills] == [
        ("sell", "sell"), ("buy", "buy")
    ]
    assert [(row["side"], row["price_evidence"]["side"]) for row in updated["trades"]] == [
        ("sell", "sell"), ("buy", "buy")
    ]


@pytest.mark.parametrize("ref_close", [None, "nan", "inf", 0, -1, "bad-number"])
def test_missing_or_invalid_limit_reference_cannot_bypass_limit_check(
    tmp_path: Path, ref_close: object
) -> None:
    _write_bars(tmp_path, "600000")
    plan = _plan()
    plan["intents"][0]["ref_close"] = ref_close

    _settled, ledger, actions = _settle(tmp_path, plan=plan)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == "missing_or_invalid_previous_close"
    assert ledger["trades"] == []


@pytest.mark.parametrize("suspended", [" True ", "TRUE", "1.0", " yes "])
def test_known_suspension_tokens_are_not_lost_by_csv_typing(
    tmp_path: Path, suspended: str
) -> None:
    _write_bars(tmp_path, "600000", suspended=suspended)

    _settled, ledger, actions = _settle(tmp_path)

    action = _only_action(actions)
    assert action["status"] == "rejected"
    assert action["reason"] == "suspended"
    assert ledger["trades"] == []


@pytest.mark.parametrize("held_adjust", ["raw", "qfq"])
def test_evening_entry_uses_only_open_known_raw_marks_for_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_adjust: str
) -> None:
    from ai_trader import overnight, paths

    monkeypatch.setattr(overnight, "latest_as_of", lambda _daily: {"as_of": TRADE_DATE})
    monkeypatch.setattr(overnight, "load_recommendations", lambda **_kwargs: {"symbols": [], "warnings": []})
    monkeypatch.setattr(overnight, "propose_picks", lambda *_args: None)
    # The shared base lacks daytime's snapshot import; notes are unrelated to
    # settlement. All execution and ledger calculations below remain real.
    monkeypatch.setitem(sys.modules, "ai_trader.daytime", SimpleNamespace(load_daytime_notes_for=lambda *_args, **_kwargs: []))
    outcomes = []
    closing_equities = []
    for label, held_close in (("low", 10.0), ("high", 100.0)):
        daily = tmp_path / label / "daily"
        daily.mkdir(parents=True)
        _write_bars(daily, "600000")
        _write_bars(daily, "600001", open_price="", close_price=held_close, adjust=held_adjust)
        runtime_root = tmp_path / label / "runtime"
        runtime_root.mkdir()
        runtime_paths = {
            field.name: runtime_root / Path(field.default).name for field in fields(CyclePaths)
        }
        runtime_paths.update(daily_dir=daily, minute_dir=tmp_path / label / "minutes")
        runtime = CyclePaths(**runtime_paths)
        monkeypatch.setattr(paths, "STATE_DIR", runtime_root)
        monkeypatch.setattr(paths, "RESULTS_DIR", runtime_root)
        ledger = _ledger_with_position("600001")
        ledger["as_of"] = "2026-09-04"
        runtime.ledger.write_text(json.dumps(ledger), encoding="utf-8")
        runtime.plan_latest.write_text(json.dumps(_plan()), encoding="utf-8")

        snapshot = overnight.run_evening(paths=runtime, network_news=False)

        action = _only_action(snapshot["actions"])
        outcomes.append((action["status"], action.get("quantity"), action.get("price")))
        closing_equities.append(snapshot["ledger"]["equity"])
    assert outcomes == [("filled", 1000, 10.0), ("filled", 1000, 10.0)]
    if held_adjust == "raw":
        assert closing_equities[0] != closing_equities[1]
    else:
        assert closing_equities == [29_989.9, 29_989.9]


def test_actual_daily_schema_preserves_updated_at_without_inventing_fetched_at(tmp_path: Path) -> None:
    _write_bars(tmp_path, "600000")
    path = tmp_path / "600000.csv"
    records = _records(path)
    records[0].pop("fetched_at")
    records[0]["source"] = "local_existing"
    records[0]["updated_at"] = "2026-09-06T15:42:11"
    records[0]["change"] = "0.0"
    _write_records(path, records)

    _settled, ledger, actions = _settle(tmp_path)

    action = _only_action(actions)
    assert action["status"] == "filled"
    assert action["evidence_source"] == "local_existing"
    assert action["evidence_timestamp"] == "2026-09-06T15:42:11"
    evidence = ledger["trades"][-1]["price_evidence"]
    assert evidence["timestamp_field"] == "updated_at"
    assert evidence["updated_at"] == "2026-09-06T15:42:11"
    assert "fetched_at" not in evidence


@pytest.mark.parametrize(
    ("side", "open_price", "status", "reason"),
    # TRADE_DATE is 2026-09-05: from 2026-07-06 the main-board ST band is 10%
    # (HKEX circular 055/2026), so +/-5% opens fill and +/-10% opens are blocked.
    [
        # 10.48 (+4.8%): blocked under the old 5% band, under the 5% chase limit.
        ("buy", 10.48, "filled", ""),
        ("buy", 11.0, "rejected", "limit_up:"),
        ("sell", 9.5, "filled", ""),
        ("sell", 9.0, "rejected", "limit_down:"),
    ],
)
def test_explicit_st_evidence_uses_the_existing_st_limit_band(
    tmp_path: Path, side: str, open_price: float, status: str, reason: str
) -> None:
    _write_bars(tmp_path, "600000", open_price=open_price)
    path = tmp_path / "600000.csv"
    records = _records(path)
    records[0]["is_st"] = "True"
    _write_records(path, records)

    _settled, ledger, actions = _settle(
        tmp_path, plan=_plan(side=side), ledger=_ledger_with_position() if side == "sell" else None
    )

    action = _only_action(actions)
    assert action["status"] == status
    if status == "rejected":
        assert action["reason"].startswith(reason)
        assert ledger["trades"] == []


def test_non_raw_holding_prices_cannot_change_open_buy_acceptance_or_raw_valuation(tmp_path: Path) -> None:
    _write_bars(tmp_path, "600000")
    _write_bars(tmp_path, "600001", open_price=100.0, close_price=100.0, adjust="qfq")
    ledger = _ledger_with_position("600001")
    ledger["as_of"] = "2026-09-04"

    _settled, updated, actions = _settle(tmp_path, ledger=ledger)

    action = _only_action(actions)
    assert (action["status"], action.get("quantity")) == ("filled", 1000)
    held = next(row for row in updated["positions"] if row["symbol"] == "600001")
    assert held["market_price"] == 10.0
    assert held["stale_mark"] is True
    assert "untrusted_mark_price_basis:600001:forward_adjusted" in updated["warnings"]


@pytest.mark.parametrize("ledger_date", [TRADE_DATE, "2026-09-08", None])
def test_missing_held_open_cannot_use_an_undated_or_future_saved_mark(
    tmp_path: Path, ledger_date: str | None
) -> None:
    _write_bars(tmp_path, "600000")
    _write_bars(tmp_path, "600001", open_price="")
    for carried_price in (10.0, 100.0):
        ledger = _ledger_with_position("600001")
        ledger["as_of"] = ledger_date
        ledger["positions"][0]["market_price"] = carried_price

        _settled, updated, actions = _settle(tmp_path, ledger=ledger)

        action = _only_action(actions)
        assert action["status"] == "rejected"
        assert action["reason"] == "missing_open_valuation:600001"
        assert updated["trades"] == []


def test_missing_held_open_valuation_does_not_prevent_an_evidenced_sell(tmp_path: Path) -> None:
    _write_bars(tmp_path, "600000")
    _write_bars(tmp_path, "600001", open_price="")
    ledger = _ledger_with_position()
    ledger["positions"].extend(_ledger_with_position("600001")["positions"])
    ledger["as_of"] = TRADE_DATE

    _settled, updated, actions = _settle(tmp_path, plan=_plan(side="sell"), ledger=ledger)

    action = _only_action(actions)
    assert action["status"] == "filled"
    assert action["side"] == "sell"
    assert updated["trades"][-1]["quantity"] == 1000
