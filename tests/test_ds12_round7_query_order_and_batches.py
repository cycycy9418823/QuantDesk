"""Public query contracts: alias-independent order and complete late conflict groups."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest


def legacy_bar(symbol: str, day: str = "2026-06-02", close: float = 10.0) -> dict:
    from quantdesk.data_store.hash_utils import DAILY_HASH_FIELDS, compute_row_hash

    row = {
        "symbol": symbol, "date": day, "open": close, "high": close,
        "low": close, "close": close, "volume": 100.0, "amount": 1000.0,
        "pct_chg": 0.0, "change": 0.0, "turnover": 1.0,
        "source": "independent_order_batch_fixture", "updated_at": "2026-06-02T16:00:00",
    }
    # Pre-upgrade hashes include the exact stored symbol.
    row["row_hash"] = compute_row_hash(row, DAILY_HASH_FIELDS)
    return row


def seed_legacy_database(path: Path, rows: list[dict]) -> None:
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    db = QuantDeskDB(path)
    try:
        db.initialize()
        db.upsert_many("daily_bars_raw", rows, ["symbol", "date"])
        actual = db.execute("SELECT symbol, date, close, row_hash FROM daily_bars_raw")
        expected = {(r["symbol"], r["date"], r["close"], r["row_hash"]) for r in rows}
        assert {(r["symbol"], r["date"], r["close"], r["row_hash"]) for r in actual} == expected
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["daily", "cross-section"])
@pytest.mark.parametrize("limit", [1, 100], ids=["first_identity", "full_order"])
@pytest.mark.parametrize(
    "stored_symbols",
    [
        ("000001", "600000"),
        ("000001.SZ", "600000.SH"),
        ("sz000001", "sh600000"),
        ("sz000001", "600000"),
    ],
    ids=["canonical_positive", "suffix_positive", "legacy_prefixes", "legacy_and_canonical"],
)
def test_same_date_query_order_and_limit_follow_identity(
    tmp_path: Path, kind: str, limit: int, stored_symbols: tuple[str, str],
) -> None:
    from quantdesk.data_store.query import run_safe_query

    path = tmp_path / "preexisting_keys.db"
    seed_legacy_database(path, [legacy_bar(symbol) for symbol in stored_symbols])
    kwargs = {"date": "2026-06-02"} if kind == "cross-section" else {}
    actual = run_safe_query(kind, limit=limit, adjustment="raw", db_path=str(path), **kwargs)
    expected = list(stored_symbols[:limit])  # Canonical identities are 000001 then 600000.
    observed = [r["symbol"] for r in actual.get("rows", [])]
    observation = {
        "kind": kind, "limit": limit, "stored_symbols": stored_symbols,
        "expected_canonical_order": ["000001", "600000"][:limit],
        "expected_stored_symbols": expected, "actual_stored_symbols": observed,
        "actual": actual,
    }
    (tmp_path / "order_observation.json").write_text(
        json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    assert actual["ok"] is True and actual["row_count"] == len(expected), observation
    assert observed == expected, observation


@pytest.mark.parametrize("kind", ["daily", "cross-section"])
@pytest.mark.parametrize("conflict_is_matched", [True, False], ids=["late_conflict_visible", "unmatched_conflict_positive"])
def test_late_identity_group_is_reconciled_before_result_limit(
    tmp_path: Path, kind: str, conflict_is_matched: bool,
) -> None:
    from quantdesk.data_store.query import run_safe_query
    from quantdesk.data_store.repositories import DailyBarRepository
    from quantdesk.data_store.sqlite_store import QuantDeskDB

    if kind == "daily":
        rows = [legacy_bar("600000", (date(2020, 1, 1) + timedelta(days=i)).isoformat()) for i in range(120)]
        kwargs = {"symbol": "600000.SH"}
    else:
        rows = [legacy_bar(str(600000 + i)) for i in range(120)]
        kwargs = {"date": "2026-06-02"}
    if not conflict_is_matched:
        rows[-1] = legacy_bar(rows[-1]["symbol"], rows[-1]["date"], 99.0)
    path = tmp_path / "late_group.db"
    db = QuantDeskDB(path)
    try:
        db.initialize()
        assert DailyBarRepository(db).upsert_daily_bars(rows, adjustment="raw")["ok"] is True
        target = rows[-1]
        alias = legacy_bar("sh" + target["symbol"], target["date"], 99.0 if conflict_is_matched else 98.0)
        db.upsert_many("daily_bars_raw", [alias], ["symbol", "date"])
        persisted = db.execute("SELECT symbol, date, close FROM daily_bars_raw")
        assert len(persisted) == 121
        assert sum(r["close"] < 20 for r in persisted) == (120 if conflict_is_matched else 119)
    finally:
        db.close()
    actual = run_safe_query(
        kind, where="close < 20", limit=100, adjustment="raw", db_path=str(path), **kwargs,
    )
    observation = {
        "kind": kind, "matched_groups": 120 if conflict_is_matched else 119,
        "conflict_group": [target["symbol"], target["date"]],
        "conflict_is_matched": conflict_is_matched, "limit": 100, "actual": actual,
    }
    (tmp_path / "batch_observation.json").write_text(
        json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    if conflict_is_matched:
        assert actual["ok"] is False and actual["status"] == "identity_conflict", observation
        assert actual["row_count"] == 0 and actual["identity_conflict"], observation
    else:
        assert actual["ok"] is True and actual["row_count"] == 100, observation
        assert [(r["symbol"], r["date"]) for r in actual["rows"]] == [
            (r["symbol"], r["date"]) for r in rows[:100]
        ], observation
