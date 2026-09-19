"""Normal writer inputs: scalable query groups and public alias equivalence."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest


def bar(symbol: str, day: str) -> dict:
    return {"symbol": symbol, "date": day, "open": 10.0, "high": 10.0,
            "low": 10.0, "close": 10.0, "volume": 100.0, "amount": 1000.0,
            "pct_chg": 0.0, "change": 0.0, "turnover": 1.0,
            "source": "deterministic_query_scale_fixture", "updated_at": "2026-06-02T16:00:00"}


def seed_with_normal_writer(path: Path, rows: list[dict]) -> list[dict]:
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB
    db = QuantDeskDB(path)
    try:
        db.initialize()
        outcome = DailyBarRepository(db).upsert_daily_bars(rows, adjustment="raw")
        assert outcome["ok"] is True
        actual = db.execute("SELECT symbol, date, close FROM daily_bars_raw ORDER BY date, symbol")
        assert len(actual) == len(rows)
        return actual
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["daily", "cross-section"])
@pytest.mark.parametrize("count", [10, 1100], ids=["small_positive", "ordinary_history_or_universe"])
def test_query_limit_works_with_many_normal_identity_date_groups(tmp_path: Path, kind: str, count: int) -> None:
    from quantdesk.data_store.query import run_safe_query

    if kind == "daily":
        rows = [bar("600000.SH", (date(2020, 1, 1) + timedelta(days=i)).isoformat()) for i in range(count)]
        kwargs = {"symbol": "600000.SH"}
    else:
        rows = [bar(str(600000 + i), "2026-06-02") for i in range(count)]
        kwargs = {"date": "2026-06-02"}
    path = tmp_path / "normal_bars.db"
    stored = seed_with_normal_writer(path, rows)
    expected = sorted((row["symbol"], row["date"], row["close"]) for row in stored[:100])
    failure = None
    actual = None
    try:
        actual = run_safe_query(kind, adjustment="raw", db_path=str(path), limit=100, **kwargs)
    except Exception as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)}
    observation = {"kind": kind, "stored_groups": count, "limit": 100,
                   "expected_row_count": min(count, 100), "exception": failure, "actual": actual}
    (tmp_path / "scale_observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    assert failure is None, observation
    assert actual["ok"] is True and actual["row_count"] == min(count, 100), observation
    assert sorted((r["symbol"], r["date"], r["close"]) for r in actual["rows"]) == expected, observation


@pytest.mark.parametrize("kind", ["daily", "cross-section"])
@pytest.mark.parametrize("selector", ["symbol_kwarg", "where_eq_suffix", "where_eq_prefix", "where_in_suffix"])
def test_where_alias_reads_the_same_canonical_row_as_symbol_argument(tmp_path: Path, kind: str, selector: str) -> None:
    from quantdesk.data_store.query import run_safe_query

    path = tmp_path / "canonical_only.db"
    stored = seed_with_normal_writer(path, [bar("600000.SH", "2026-06-02")])
    assert stored == [{"symbol": "600000", "date": "2026-06-02", "close": 10.0}]
    kwargs = {"date": "2026-06-02"} if kind == "cross-section" else {}
    where = None
    if selector == "symbol_kwarg":
        kwargs["symbol"] = "600000.SH"
    elif selector == "where_eq_suffix":
        where = "symbol = '600000.SH'"
    elif selector == "where_eq_prefix":
        where = "symbol = 'sh600000'"
    else:
        where = "symbol IN ('600000.SH')"
    actual = run_safe_query(kind, where=where, adjustment="raw", db_path=str(path), **kwargs)
    observation = {"kind": kind, "selector": selector, "where": where, "stored": stored, "actual": actual}
    (tmp_path / "alias_observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    assert actual.get("ok") is True and actual["row_count"] == 1, observation
    assert actual["rows"][0]["symbol"] == "600000" and actual["rows"][0]["close"] == 10.0, observation
