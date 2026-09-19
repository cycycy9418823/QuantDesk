"""Public query compatibility: SQLite oracle, unchanged inputs, both revisions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def row(symbol: str, close: float, *, date: str = "2026-06-02", turnover: float | None = 0.0) -> dict:
    from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash
    data = {"symbol": symbol, "date": date, "open": close, "high": close, "low": close, "close": close,
            "volume": 100.0, "amount": 1000.0, "pct_chg": 0.0, "change": 0.0, "turnover": turnover,
            "source": "independent_query_compatibility", "updated_at": "2026-06-03T16:00:00"}
    data["row_hash"] = compute_row_hash(data, DAILY_HASH_FIELDS)
    return data


def identity(rows: list[dict]) -> list[tuple[str, str, float]]:
    from quantdesk.data_store.repositories import normalize_symbol
    return sorted((normalize_symbol(item["symbol"]), item["date"], item["close"]) for item in rows)


def seed(path: Path, rows: list[dict]) -> None:
    from quantdesk.data_store.sqlite_store import QuantDeskDB
    db = QuantDeskDB(path)
    try:
        db.initialize()
        db.upsert_many("daily_bars_raw", rows, ["symbol", "date"])
        actual = db.execute("SELECT * FROM daily_bars_raw")
        assert len(actual) == len(rows)
        assert {(r["symbol"], r["date"], r["row_hash"]) for r in actual} == {(r["symbol"], r["date"], r["row_hash"]) for r in rows}
    finally:
        db.close()


CASES = [
    pytest.param("date = '2026-06-02'", [0, 1], id="date_equal_positive"),
    pytest.param("date >= '2026-06-03'", [2], id="date_range_ge"),
    pytest.param("date < '2026-06-03'", [0, 1], id="date_range_lt"),
    pytest.param("symbol LIKE '60%'", [1, 2], id="symbol_like"),
    pytest.param("symbol != '600000.SH'", [0, 2], id="symbol_not_equal"),
    pytest.param("symbol IN ('600000.SH', '600001.SH')", [1, 2], id="symbol_in_positive"),
    pytest.param("close >= 20", [1, 2], id="numeric_range_positive"),
    pytest.param("close IN (10, 30)", [0, 2], id="numeric_in_positive"),
    pytest.param("turnover != 0", [2], id="nullable_not_equal"),
    pytest.param("turnover = 0", [1], id="nullable_equal_positive"),
]


@pytest.mark.parametrize("kind", ["daily", "cross-section"])
@pytest.mark.parametrize("where, selected", CASES)
def test_valid_public_query_preserves_sqlite_result(tmp_path: Path, kind: str, where: str, selected: list[int]) -> None:
    from quantdesk.data_store.query import run_safe_query
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    rows = [row("000001.SZ", 10.0, turnover=None), row("600000.SH", 20.0),
            row("600001.SH", 30.0, date="2026-06-03", turnover=2.0)]
    path = tmp_path / "normal_legacy.db"
    seed(path, rows)
    expected = identity([rows[i] for i in selected])
    db = QuantDeskDB(path)
    try:
        # All where strings are fixed test constants. The original SQL engine
        # independently confirms expected results, including SQL NULL behavior.
        sql_rows = db.execute("SELECT * FROM daily_bars_raw WHERE " + where)
    finally:
        db.close()
    assert identity(sql_rows) == expected
    actual = run_safe_query(kind, where=where, adjustment="raw", db_path=str(path))
    observed = {"kind": kind, "where": where, "expected": expected, "sqlite_oracle": identity(sql_rows), "actual": actual}
    (tmp_path / "query_observation.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8")
    assert actual.get("ok") is True and actual.get("status") == "passed", observed
    assert identity(actual["rows"]) == expected, observed


@pytest.mark.parametrize("conflict_date", ["2026-06-03", "2026-06-02"], ids=["outside_date_positive", "all_aliases_outside_business_filter"])
def test_unselected_identity_does_not_block_selected_stock(tmp_path: Path, conflict_date: str) -> None:
    from quantdesk.data_store.query import run_safe_query
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    path = tmp_path / "scope.db"
    rows = [row("600000", 30.0, date=conflict_date), row("600000.SH", 40.0, date=conflict_date), row("000001.SZ", 10.0)]
    seed(path, rows)
    db = QuantDeskDB(path)
    try:
        candidates = db.execute("SELECT * FROM daily_bars_raw WHERE date = ? AND close < ?", ["2026-06-02", 20])
        assert identity(candidates) == [("000001", "2026-06-02", 10.0)]
    finally:
        db.close()
    actual = run_safe_query("cross-section", date="2026-06-02", where="close < 20", adjustment="raw", db_path=str(path))
    observed = {"conflict_date": conflict_date, "expected": [("000001", "2026-06-02", 10.0)], "actual": actual}
    (tmp_path / "query_observation.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8")
    assert actual.get("ok") is True and identity(actual["rows"]) == [("000001", "2026-06-02", 10.0)], observed
