"""Retained active contracts; original mixed tests remain in the retirement evidence archive."""


from __future__ import annotations


from pathlib import Path


import warnings


import pytest


def legacy_row(symbol: str, close: float) -> dict:
    from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash
    row = {"symbol": symbol, "date": "2026-06-02", "open": close, "high": close,
           "low": close, "close": close, "volume": 100.0, "amount": 1000.0,
           "pct_chg": 0.0, "change": 0.0, "turnover": 1.0,
           "source": "deterministic_legacy_fixture", "updated_at": "2026-06-02T16:00:00"}
    row["row_hash"] = compute_row_hash(row, DAILY_HASH_FIELDS)
    return row


@pytest.mark.parametrize("adjustment", ["raw", "forward", "back"])
@pytest.mark.parametrize("stored_alias", ["600000", "600000.SH", "sh600000"])
def test_preexisting_key_is_readable_by_its_original_identity(tmp_path: Path, adjustment: str, stored_alias: str) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository, daily_table_for_adjustment
    from quantdesk.data_store.sqlite_store import QuantDeskDB
    db = QuantDeskDB(tmp_path / "legacy.db")
    db.initialize()
    table = daily_table_for_adjustment(adjustment)
    try:
        # Deliberately bypass the NEW normalizer: these are records already on
        # disk before the code upgrade, with valid hashes for the stored key.
        db.upsert_many(table, [legacy_row(stored_alias, 10.0)], ["symbol", "date"])
        raw = db.execute(f"SELECT symbol, close FROM {table} WHERE symbol = ?", [stored_alias])
        assert len(raw) == 1 and raw[0]["close"] == 10.0
        rows = DailyBarRepository(db).query_daily_bars(stored_alias, adjustment=adjustment)
    finally:
        db.close()
    assert len(rows) == 1 and rows[0]["close"] == 10.0, {"stored_alias": stored_alias, "adjustment": adjustment, "rows": rows}


def test_conflicting_preexisting_aliases_are_not_silently_selected(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository, daily_table_for_adjustment
    from quantdesk.data_store.sqlite_store import QuantDeskDB
    db = QuantDeskDB(tmp_path / "legacy_conflict.db")
    db.initialize()
    try:
        table = daily_table_for_adjustment("raw")
        db.upsert_many(table, [legacy_row("600000", 10.0), legacy_row("600000.SH", 99.0)], ["symbol", "date"])
        raw = db.execute(f"SELECT symbol, close FROM {table} ORDER BY symbol")
        assert len(raw) == 2 and {row["close"] for row in raw} == {10.0, 99.0}
        # The original task requires a visible conflict/reconciliation result,
        # not one particular exception class or English reason string.
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always")
            try:
                result = DailyBarRepository(db).query_daily_bars("600000.SH", adjustment="raw")
            except (ValueError, RuntimeError) as exc:
                assert str(exc), "an explicit refusal needs a visible reason"
                return
        row_diagnostics = any(row.get("warnings") or row.get("identity_conflict") or row.get("conflict") for row in result)
        log_diagnostics = any(record.levelno >= 30 for record in caplog.records)
        assert notices or row_diagnostics or log_diagnostics, {"stored_rows": raw, "returned_rows": result, "warnings": list(notices)}
    finally:
        db.close()
