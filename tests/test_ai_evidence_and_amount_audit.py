"""Amounts stay unchanged; new price evidence is captured before settlement."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import pytest

from ai_trader.execution_evidence import (
    audit_trade_evidence, capture_execution_evidence, verify_execution_receipt,
)
from ai_trader.ledger import empty_ledger
from ai_trader.ledger_audit import audit_ledger
from ai_trader.market import load_ohlc_row
from ai_trader.overnight import settle_plan


def source_row(tmp_path):
    path = tmp_path / "600000.csv"
    pd.DataFrame([dict(date="2026-09-11", symbol="600000",
        open=10, high=10.1, low=9.9, close=10, volume=1000,
        pct_chg=0, adjust="raw", source="synthetic_immutable_fixture",
        fetched_at="2026-09-11T16:00:00+08:00")]).to_csv(path, index=False)
    return path, load_ohlc_row("600000", "2026-09-11", tmp_path)


def execution():
    return dict(symbol="600000", trade_date="2026-09-11", side="buy",
                price_field="open", price=10.0, price_basis="raw",
                source="synthetic_immutable_fixture", timestamp="2026-09-11T16:00:00+08:00")


def test_archive_survives_source_rewrite_and_refuses_tampering(tmp_path):
    path, row = source_row(tmp_path)
    captured = capture_execution_evidence(execution(), row, plan_id="p1", evidence_dir=tmp_path / "evidence")
    stored = Path(captured["archive_path"]).read_bytes()
    assert verify_execution_receipt(captured["receipt"])
    again = capture_execution_evidence(execution(), row, plan_id="p1", evidence_dir=tmp_path / "evidence")
    assert again == captured
    assert Path(captured["archive_path"]).read_bytes() == stored
    path.write_text("later,different,source\n", encoding="utf-8")
    assert verify_execution_receipt(captured["receipt"])
    changed = deepcopy(captured["receipt"])
    changed["content"]["execution"]["price"] = 99
    assert not verify_execution_receipt(changed)


def test_archive_never_overwrites_existing_conflicting_evidence(tmp_path):
    _, row = source_row(tmp_path)
    captured = capture_execution_evidence(execution(), row, plan_id="p1", evidence_dir=tmp_path / "evidence")
    target = Path(captured["archive_path"])
    target.write_text('{"corrupt":true}', encoding="utf-8")
    original = target.read_bytes()
    with pytest.raises(ValueError, match="archive_conflict"):
        capture_execution_evidence(execution(), row, plan_id="p1", evidence_dir=tmp_path / "evidence")
    assert target.read_bytes() == original


def test_new_fill_links_snapshot_and_historical_trade_is_not_backfilled(tmp_path):
    source_row(tmp_path)
    ledger = empty_ledger()
    legacy = {"symbol": "000001", "date": "2026-09-01", "side": "buy", "quantity": 100, "price": 10,
              "cash_delta": -1000, "fee": 0}
    ledger["trades"] = [deepcopy(legacy)]
    plan = {"plan_id": "p1", "trade_date": "2026-09-11", "status": "pending_open",
            "intents": [{"symbol": "600000", "side": "buy", "quantity": 100, "ref_close": 10}]}
    settled, after, actions = settle_plan(plan, ledger, as_of="2026-09-11", daily_dir=tmp_path,
                                        evidence_dir=tmp_path / "evidence")
    assert settled["status"] == "settled"
    assert after["trades"][0] == legacy
    assert any(action.get("status") == "filled" for action in actions)
    evidence = after["trades"][-1]["price_evidence"]
    assert Path(evidence["archive_path"]).is_file()
    assert evidence["receipt"]["content"]["original_row"]["open"] == "10"
    coverage = audit_trade_evidence(after["trades"])
    assert coverage["counts"] == {"captured": 1, "legacy_unverified": 1, "invalid": 0}
    modified = deepcopy(after["trades"])
    modified[-1]["price"] = 99
    assert audit_trade_evidence(modified)["counts"]["invalid"] == 1


def test_archive_failure_precedes_financial_mutation(tmp_path, monkeypatch):
    source_row(tmp_path)
    ledger = empty_ledger()
    original = deepcopy(ledger)
    plan = {"plan_id": "p1", "trade_date": "2026-09-11", "status": "pending_open",
            "intents": [{"symbol": "600000", "side": "buy", "quantity": 100, "ref_close": 10}]}
    original_open = Path.open
    def fail_archive(path, *args, **kwargs):
        if path.parent.name == "evidence" and args and args[0] == "x":
            raise OSError("injected archive failure")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_archive)
    with pytest.raises(OSError, match="injected"):
        settle_plan(plan, ledger, as_of="2026-09-11", daily_dir=tmp_path, evidence_dir=tmp_path / "evidence")
    assert ledger == original and plan["status"] == "pending_open"


def test_read_only_amount_audit_keeps_small_residual_visible():
    ledger = empty_ledger()
    ledger["trades"] = [{"symbol": "600000", "date": "2026-09-01", "side": "sell",
                         "quantity": 100, "price": 11, "fee": 6.1,
                         "cash_delta": 1093.9, "realized_pnl": 88.4} for _ in range(4)]
    ledger["cash"] = 104375.6
    ledger["equity"] = 104375.6
    ledger["realized_pnl"] = 353.6002
    original = deepcopy(ledger)
    result = audit_ledger(ledger)
    check = next(row for row in result["checks"] if row["name"] == "realized_pnl_from_recorded_exits")
    assert Decimal(check["delta"]) == Decimal("0.0002")
    assert check["status"] == "rounding_residual"
    assert result["rounding_policy"]["precision_changed"] is False
    assert result["execution_evidence"]["counts"]["legacy_unverified"] == 4
    assert ledger == original


def test_registered_default_and_glm_ledgers_keep_amount_checks():
    for ledger_id in ("ai_trader_100k", "ai_trader_100k_glm"):
        ledger = empty_ledger(ledger_id=ledger_id)
        original = deepcopy(ledger)
        result = audit_ledger(ledger)
        assert result["status"] == "exact"
        assert result["ledger_id"] == ledger_id
        assert result["checks"]
        assert result["historical_backfill"] is False
        assert result["read_only"] is True
        assert ledger == original
        ledger["cash"] -= 1
        changed = deepcopy(ledger)
        reviewed = audit_ledger(ledger)
        assert reviewed["status"] == "requires_review"
        assert reviewed["historical_backfill"] is False
        assert ledger == changed


@pytest.mark.parametrize(
    "ledger_id",
    [None, "", "ai_trader_100k_glm_extra", "ai_trader_100k_gl", "ai_trader_100k ", "strategy_book", [], {}, 42, True],
)
def test_missing_similar_and_unknown_ledger_ids_stay_foreign(ledger_id):
    ledger = empty_ledger()
    if ledger_id is None:
        ledger.pop("ledger_id", None)
    else:
        ledger["ledger_id"] = ledger_id
    original = deepcopy(ledger)
    result = audit_ledger(ledger)
    assert result["status"] == "foreign_ledger"
    assert result["checks"] == []
    assert result["historical_backfill"] is False
    assert ledger == original


def test_missing_nonfinite_foreign_and_material_amount_differences_remain_visible():
    ledger = empty_ledger()
    ledger["cash"] -= 1
    result = audit_ledger(ledger)
    assert result["status"] == "requires_review"
    ledger["cash"] = float("nan")
    assert any(row["status"] == "unavailable" for row in audit_ledger(ledger)["checks"])
    ledger["ledger_id"] = "strategy_book"
    assert audit_ledger(ledger)["status"] == "foreign_ledger"
    assert not verify_execution_receipt({"schema": "ai_execution_evidence_v1", "content": {"source": []}})

