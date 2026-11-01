"""Retained active contracts; original mixed tests remain in the retirement evidence archive."""


from __future__ import annotations


import json


import warnings


from pathlib import Path


import pytest


def legacy_row(symbol: str, close: float) -> dict:
    from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash
    row = {"symbol": symbol, "date": "2026-06-02", "open": close, "high": close,
           "low": close, "close": close, "volume": 100.0, "amount": 1000.0,
           "pct_chg": 0.0, "change": 0.0, "turnover": 1.0,
           "source": "independent_filtered_fixture", "updated_at": "2026-06-02T16:00:00"}
    row["row_hash"] = compute_row_hash(row, DAILY_HASH_FIELDS)
    return row


@pytest.mark.parametrize("kind", ["daily", "cross-section"])
@pytest.mark.parametrize("where", ["close < 20", "symbol = '600000.SH'"], ids=["business_filter", "legacy_symbol_filter"])
@pytest.mark.parametrize("conflicting", [False, True], ids=["equivalent_positive", "conflicting_aliases"])
def test_filtered_query_cannot_hide_conflict_in_selected_identity(tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str, where: str, conflicting: bool) -> None:
    from quantdesk.data_store.query import run_safe_query
    from quantdesk.data_store.repositories import daily_table_for_adjustment
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    path = tmp_path / "legacy.db"
    rows = [legacy_row("600000", 10.0), legacy_row("600000.SH", 99.0 if conflicting else 10.0)]
    db = QuantDeskDB(path)
    try:
        db.initialize()
        table = daily_table_for_adjustment("raw")
        db.upsert_many(table, rows, ["symbol", "date"])
        stored = db.execute(f"SELECT * FROM {table}")
        assert len(stored) == 2 and {r["close"] for r in stored} == ({10.0, 99.0} if conflicting else {10.0})
    finally:
        db.close()
    kwargs = {"symbol": "600000.SH"} if kind == "daily" else {"date": "2026-06-02"}
    caplog.clear()
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter("always")
        try:
            actual = run_safe_query(kind, where=where, adjustment="raw", db_path=str(path), **kwargs)
        except (ValueError, RuntimeError) as exc:
            assert conflicting and str(exc)
            return
    observation = {"kind": kind, "where": where, "conflicting": conflicting, "actual": actual,
                   "warnings": [str(n.message) for n in notices], "logs": [r.getMessage() for r in caplog.records]}
    (tmp_path / "filter_observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    if not conflicting:
        assert actual.get("ok") is True and actual["row_count"] == 1 and actual["rows"][0]["close"] == 10.0, observation
        return
    diagnostic = (actual.get("ok") is False or actual.get("identity_conflict") or actual.get("warnings_top")
                  or actual.get("warnings") or actual.get("warning_count") or notices
                  or any(r.levelno >= 30 for r in caplog.records)
                  or any(r.get("identity_conflict") or r.get("warnings") for r in actual.get("rows", [])))
    assert diagnostic, observation
