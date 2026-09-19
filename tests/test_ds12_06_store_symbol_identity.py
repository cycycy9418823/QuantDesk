"""DS12-06 Section-C2: local daily-store symbol identity is unified.

Offline only: a throwaway SQLite file under ``tmp_path`` and throwaway CSVs.
The real database is never touched.

Contract under test:

* ``600000``, ``600000.SH``, ``600000.sh`` and ``sh600000`` are the same key;
* an alias query hits the same stored SQLite row in all three adjustment tables;
* a legacy/alias write for the same symbol+date collapses to one canonical row;
* CSV fallback never masks an SQLite alias hit (a non-empty CSV must not be
  returned when SQLite has the row);
* a genuinely empty SQLite store still falls back to CSV;
* a one-sided dual write is fail-closed.
"""

from __future__ import annotations

import pytest

from quantdesk.data_store.dual_writer import read_daily_prefer_sqlite, write_daily_dual
from quantdesk.data_store.repositories import (
    DailyBarRepository,
    daily_table_for_adjustment,
    normalize_symbol,
)
from quantdesk.data_store.sqlite_store import QuantDeskDB


def _db(tmp_path):
    db = QuantDeskDB(str(tmp_path / "quantdesk.db"))
    db.initialize()
    return db


def _bar(symbol: str, date: str, close: float = 10.0) -> dict:
    return {"symbol": symbol, "date": date, "open": close, "high": close, "low": close, "close": close, "volume": 100, "amount": 1000}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600000", "600000"),
        ("600000.SH", "600000"),
        ("600000.sh", "600000"),
        ("sh600000", "600000"),
        ("SH600000", "600000"),
        ("000001", "000001"),
        ("000001.SZ", "000001"),
        ("sz000001", "000001"),
        ("920010", "920010"),
        ("920010.BJ", "920010"),
        ("bj920010", "920010"),
        ("1", "000001"),
    ],
)
def test_plain_suffix_and_prefixed_are_unified(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize("adjustment", ["forward", "back", "raw"])
@pytest.mark.parametrize("alias", ["600000.SH", "sh600000", "600000.sh"])
def test_alias_query_hits_same_sqlite_row(tmp_path, adjustment: str, alias: str) -> None:
    db = _db(tmp_path)
    try:
        repo = DailyBarRepository(db)
        repo.upsert_daily_bars([_bar("600000", "2026-06-02", close=10.0)], adjustment=adjustment)

        assert len(repo.query_daily_bars("600000", adjustment=adjustment)) == 1
        rows = repo.query_daily_bars(alias, adjustment=adjustment)
        assert len(rows) == 1 and rows[0]["close"] == 10.0
    finally:
        db.close()


def test_legacy_alias_key_conflict_collapses_to_one_row(tmp_path) -> None:
    db = _db(tmp_path)
    try:
        repo = DailyBarRepository(db)
        repo.upsert_daily_bars([_bar("600000", "2026-06-02", close=10.0)])
        # A legacy writer using a suffixed key must not create a second row;
        # it updates the same canonical symbol+date.
        repo.upsert_daily_bars([_bar("600000.SH", "2026-06-02", close=99.0)])

        rows = repo.query_daily_bars("600000")
        assert len(rows) == 1
        assert rows[0]["close"] == 99.0
        assert rows[0]["symbol"] == "600000"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("adjustment", "table"),
    [
        ("forward", "daily_bars_forward_adj"),
        ("forward_adj", "daily_bars_forward_adj"),
        ("back", "daily_bars_back_adj"),
        ("back_adj", "daily_bars_back_adj"),
        ("raw", "daily_bars_raw"),
    ],
)
def test_three_adjustment_tables_are_separate(adjustment: str, table: str) -> None:
    assert daily_table_for_adjustment(adjustment) == table


def test_unknown_adjustment_is_rejected() -> None:
    with pytest.raises(ValueError):
        daily_table_for_adjustment("hfq")


def test_adjustment_tables_do_not_cross_read(tmp_path) -> None:
    db = _db(tmp_path)
    try:
        repo = DailyBarRepository(db)
        repo.upsert_daily_bars([_bar("600000", "2026-06-02", close=10.0)], adjustment="forward")
        repo.upsert_daily_bars([_bar("600000", "2026-06-02", close=20.0)], adjustment="raw")

        assert repo.query_daily_bars("600000", adjustment="forward")[0]["close"] == 10.0
        assert repo.query_daily_bars("600000", adjustment="raw")[0]["close"] == 20.0
    finally:
        db.close()


@pytest.mark.parametrize("csv_present", [False, True])
def test_csv_does_not_mask_sqlite_alias_hit(tmp_path, csv_present: bool) -> None:
    db = _db(tmp_path)
    try:
        DailyBarRepository(db).upsert_daily_bars([_bar("600000", "2026-06-02", close=10.0)], adjustment="raw")
    finally:
        db.close()
    csv_path = tmp_path / "600000.csv" if csv_present else None
    if csv_path is not None:
        csv_path.write_text("date,close\n2026-06-02,20\n", encoding="utf-8")

    rows, meta = read_daily_prefer_sqlite(
        "600000.SH",
        adjustment="raw",
        csv_path=csv_path,
        config={"read_prefer_sqlite": True, "db_path": str(tmp_path / "quantdesk.db")},
    )

    assert meta["read_source"] == "sqlite"
    assert meta["fallback_used"] is False
    assert len(rows) == 1 and rows[0]["close"] == 10.0


def test_empty_sqlite_still_falls_back_to_csv(tmp_path) -> None:
    db = _db(tmp_path)
    db.close()
    csv_path = tmp_path / "600000.csv"
    csv_path.write_text("date,close\n2026-06-02,20\n", encoding="utf-8")

    rows, meta = read_daily_prefer_sqlite(
        "600000",
        adjustment="raw",
        csv_path=csv_path,
        config={"read_prefer_sqlite": True, "db_path": str(tmp_path / "quantdesk.db")},
    )

    assert meta["read_source"] == "csv"
    assert meta["fallback_used"] is True
    assert rows and rows[0]["close"] == 20.0


def test_dual_write_one_sided_failure_is_fail_closed(tmp_path, monkeypatch) -> None:
    import quantdesk.data_store.dual_writer as dual_writer

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(dual_writer, "atomic_write_csv", _boom)

    result = write_daily_dual(
        [_bar("600000", "2026-06-02")],
        csv_path=tmp_path / "600000.csv",
        adjustment="forward",
        db_path=str(tmp_path / "quantdesk.db"),
    )

    assert result["ok"] is False
    assert result["status"] == "failed_partial"
    assert result["csv_written"] is False
    assert result["sqlite_written"] is True
    assert result["retry_recommended"] is True
    assert "dual_write_partial_failure_fail_closed" in result["errors"]


# --- legacy on-disk keys (written before the canonical normalizer) ---------


def _legacy_row(symbol: str, close: float) -> dict:
    from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash

    row = {
        "symbol": symbol, "date": "2026-06-02", "open": close, "high": close,
        "low": close, "close": close, "volume": 100.0, "amount": 1000.0,
        "pct_chg": 0.0, "change": 0.0, "turnover": 1.0,
        "source": "deterministic_legacy_fixture", "updated_at": "2026-06-02T16:00:00",
    }
    row["row_hash"] = compute_row_hash(row, DAILY_HASH_FIELDS)
    return row


@pytest.mark.parametrize("adjustment", ["raw", "forward", "back"])
@pytest.mark.parametrize("stored_alias", ["600000", "600000.SH", "sh600000"])
def test_preexisting_legacy_key_is_readable(tmp_path, adjustment: str, stored_alias: str) -> None:
    from quantdesk.data_store.repositories import DailyBarRepository, daily_table_for_adjustment

    db = _db(tmp_path)
    try:
        table = daily_table_for_adjustment(adjustment)
        # Bypass the normalizer to simulate a row already on disk before upgrade.
        db.upsert_many(table, [_legacy_row(stored_alias, 10.0)], ["symbol", "date"])
        rows = DailyBarRepository(db).query_daily_bars(stored_alias, adjustment=adjustment)
        assert len(rows) == 1 and rows[0]["close"] == 10.0
    finally:
        db.close()


def test_conflicting_legacy_aliases_fail_visible(tmp_path) -> None:
    from quantdesk.data_store.repositories import (
        DailyBarRepository,
        SymbolIdentityConflict,
        daily_table_for_adjustment,
    )

    db = _db(tmp_path)
    try:
        table = daily_table_for_adjustment("raw")
        db.upsert_many(table, [_legacy_row("600000", 10.0), _legacy_row("600000.SH", 99.0)], ["symbol", "date"])
        with pytest.raises(SymbolIdentityConflict):
            DailyBarRepository(db).query_daily_bars("600000.SH", adjustment="raw")
    finally:
        db.close()
