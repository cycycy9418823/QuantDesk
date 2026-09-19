"""Data-plane tests: shared bar validity, suspension producer, intraday scope,
collector (sessions, locking, idempotency, fail-closed validation), manifest
freshness, alert gates, and the seven-ledger read-only guarantee.

No test touches the network: providers are injected, senders are fakes, and
all file fixtures live under pytest tmp_path.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk.data_truth.daily_bar_validity import (
    daily_bar_validity_from_row,
    filter_invalid_daily_bars,
    is_valid_daily_bar,
)
from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.intraday_refresh import collector
from quantdesk.intraday_refresh.alerts import dispatch_intraday_alerts
from quantdesk.intraday_refresh.collector import (
    _append_rows,
    collect_intraday_refresh,
    market_session_for,
    validate_minute_bar,
)
from quantdesk.intraday_refresh.scope import build_dynamic_scope
from quantdesk.intraday_refresh.snapshot import (
    select_mover_symbols,
    write_spot_snapshot,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
LEDGERS = [
    "state/auto_paper_portfolio.json",
    "state/ai_trader/ledger.json",
    "state/ai_trader/plan_latest.json",
    "state/ai_trader/journal.jsonl",
    "state/ai_trader/journal_latest.json",
    "state/ai_trader/watchlist.json",
    "state/ai_trader/philosophy.json",
]
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _skip_live_spot_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        collector,
        "refresh_full_market_snapshot",
        lambda *args, **kwargs: {
            "ok": False,
            "status": "skipped_in_test",
            "row_count": 0,
            "movers": [],
            "mover_count": 0,
            "reason_codes": [],
        },
    )


# ---------------------------------------------------------------------------
# Shared daily-bar validity (fast path and slow path share one implementation)
# ---------------------------------------------------------------------------


def test_valid_daily_bar_accepts_normal_row():
    ok, reason = is_valid_daily_bar(10.0, 10.5, 9.8, 10.2, 1_000_000)
    assert ok and reason == ""


@pytest.mark.parametrize(
    "open_,high,low,close,volume,code",
    [
        (0.0, 10.0, 9.0, 10.0, 100, "open_not_positive"),
        (10.0, 0.0, 9.0, 10.0, 100, "high_not_positive"),
        (10.0, 10.0, 0.0, 10.0, 100, "low_not_positive"),
        (10.0, 10.0, 9.0, 0.0, 100, "close_not_positive"),
        (10.0, 10.0, 9.0, 10.0, 0, "volume_not_positive"),
        (10.0, 9.5, 9.0, 10.0, 100, "high_below_body"),
        (10.0, 10.5, 10.2, 10.0, 100, "low_above_body"),
        (None, 10.0, 9.0, 10.0, 100, "open_not_numeric"),
    ],
)
def test_invalid_daily_bar_variants(open_, high, low, close, volume, code):
    ok, reason = is_valid_daily_bar(open_, high, low, close, volume)
    assert not ok and code in reason


def test_suspension_signatures_fail_validity():
    # close = 0, volume = 0 (zero_trade_with_estimates)
    ok, _ = is_valid_daily_bar(0.0, 0.0, 0.0, 0.0, 0)
    assert not ok
    # flat OHLC with last close and zero volume (zero_volume_with_last_close)
    ok, _ = is_valid_daily_bar(6.04, 6.04, 6.04, 6.04, 0)
    assert not ok


def test_filter_invalid_daily_bars_drops_flat_zero_volume_rows():
    import pandas as pd

    frame = pd.DataFrame(
        [
            {
                "date": "2026-08-30",
                "symbol": "600929",
                "open": 6.0,
                "high": 6.1,
                "low": 5.9,
                "close": 6.0,
                "volume": 1000.0,
            },
            {
                "date": "2026-08-31",
                "symbol": "600929",
                "open": 6.04,
                "high": 6.04,
                "low": 6.04,
                "close": 6.04,
                "volume": 0.0,
            },
        ]
    )
    frame.attrs = {"suspension_evidence": {"keep": True}}
    filtered, reasons = filter_invalid_daily_bars(frame, symbol="600929")
    assert len(filtered) == 1
    assert filtered.iloc[0]["date"] == "2026-08-30"
    assert reasons
    assert filtered.attrs.get("suspension_evidence") == {"keep": True}


def test_filter_preserves_empty_and_foreign_frames():
    import pandas as pd

    empty = pd.DataFrame()
    out, reasons = filter_invalid_daily_bars(empty)
    assert out is empty and not reasons
    foreign = pd.DataFrame({"x": [1]})
    out, reasons = filter_invalid_daily_bars(foreign)
    assert out is foreign and not reasons


def test_daily_bar_validity_from_row_mapping():
    ok, _ = daily_bar_validity_from_row(
        {"open": "9.01", "high": "9.18", "low": "9.0", "close": "9.16", "volume": "99682400"}
    )
    assert ok


# ---------------------------------------------------------------------------
# Intraday sessions
# ---------------------------------------------------------------------------


def test_parse_minute_datetime_accepts_tdx_without_seconds():
    parsed = collector.parse_minute_datetime("2026-09-07 14:41")
    assert parsed == datetime(2026, 9, 7, 14, 41, 0)
    assert collector.parse_minute_datetime("2026-09-07 14:41:00") == datetime(2026, 9, 7, 14, 41, 0)
    ok, reason = collector.validate_minute_bar(
        {
            "open": 9.24,
            "high": 9.25,
            "low": 9.24,
            "close": 9.24,
            "datetime": "2026-09-07 14:41",
        },
        now=datetime(2026, 9, 7, 15, 0, tzinfo=SHANGHAI),
        symbol="600000",
    )
    assert ok, reason


def test_append_accepts_legacy_price_basis_header_order(tmp_path):
    path = tmp_path / "600000.csv"
    path.write_text(
        "symbol,datetime,date,time,open,high,low,close,volume,amount,price_basis,source,updated_at\n"
        "600000,2026-09-04 15:00:00,2026-09-04,15:00:00,9.43,9.43,9.43,9.43,1.0,1.0,raw,mootdx_intraday,2026-09-04T16:03:23\n",
        encoding="utf-8",
    )
    accepted, reasons = collector._append_rows(
        path,
        [
            {
                "symbol": "600000",
                "datetime": "2026-09-07 14:41:00",
                "date": "2026-09-07",
                "time": "14:41:00",
                "open": 9.24,
                "high": 9.25,
                "low": 9.24,
                "close": 9.24,
                "volume": 111200.0,
                "amount": 1027935.0,
                "source": "mootdx_minute",
                "price_basis": "raw",
            }
        ],
        now_iso="2026-09-07T15:05:00",
    )
    assert accepted == 1, reasons
    text = path.read_text(encoding="utf-8")
    assert "2026-09-07 14:41:00" in text
    assert text.splitlines()[0].startswith("symbol,datetime")


def test_market_sessions():
    assert market_session_for(datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)) == "morning"
    assert market_session_for(datetime(2026, 9, 1, 12, 0, tzinfo=SHANGHAI)) == "lunch"
    assert market_session_for(datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI)) == "afternoon"
    assert market_session_for(datetime(2026, 9, 1, 16, 0, tzinfo=SHANGHAI)) == "closed"
    assert market_session_for(datetime(2026, 9, 1, 8, 0, tzinfo=SHANGHAI)) == "closed"


def test_non_trading_day_reports_closed_without_provider(tmp_path):
    calls = []

    def fake_fetch(symbol, offset):
        calls.append(symbol)
        return [], ""

    manifest = collect_intraday_refresh(
        tmp_path,
        network=True,
        now=datetime(2026, 9, 5, 10, 0, tzinfo=SHANGHAI),  # Saturday
        fetcher=fake_fetch,
        write_manifest=False,
    )
    assert manifest["status"] == "closed"
    assert manifest["market_session"] == "closed"
    assert "non_trading_day" in manifest["reason_codes"]
    assert calls == []  # zero provider requests


def test_lunch_break_does_not_flag_stale(tmp_path):
    def fake_fetch(symbol, offset):
        # last bar was 11:30 - during lunch that is the freshest possible
        return [
            {
                "symbol": symbol,
                "datetime": "2026-09-01 11:30:00",
                "date": "2026-09-01",
                "time": "11:30:00",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 100.0,
                "amount": 1000.0,
                "price_basis": "raw",
                "source": "fake",
            }
        ], ""

    root = _scope_root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=datetime(2026, 9, 1, 12, 30, tzinfo=SHANGHAI),
        fetcher=fake_fetch,
        write_manifest=False,
    )
    assert manifest["market_session"] == "lunch"
    assert manifest["stale_count"] == 0


def test_stale_detected_when_lag_exceeds_ten_minutes(tmp_path):
    def fake_fetch(symbol, offset):
        return [
            {
                "symbol": symbol,
                "datetime": "2026-09-01 09:40:00",
                "date": "2026-09-01",
                "time": "09:40:00",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 100.0,
                "amount": 1000.0,
                "price_basis": "raw",
                "source": "fake",
            }
        ], ""

    root = _scope_root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=datetime(2026, 9, 1, 10, 30, tzinfo=SHANGHAI),  # 50 min lag
        fetcher=fake_fetch,
        write_manifest=False,
    )
    assert manifest["stale_count"] == 1
    assert manifest["status"] in ("stale", "blocked")


def test_critical_scope_missing_blocks_ready(tmp_path):
    def fake_fetch(symbol, offset):
        return [], "provider_failed:mootdx:test"

    root = _scope_root(tmp_path, symbols=["600000", "000001"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
        fetcher=fake_fetch,
        write_manifest=False,
    )
    assert manifest["status"] == "blocked"
    assert any(rc.startswith("critical_scope_missing") for rc in manifest["reason_codes"])


# ---------------------------------------------------------------------------
# Collector mechanics: lock, idempotency, fail-closed rows
# ---------------------------------------------------------------------------


def _scope_root(tmp_path: Path, *, symbols: list[str]) -> Path:
    (tmp_path / "state" / "ai_trader").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "universe").mkdir(parents=True, exist_ok=True)
    (tmp_path / "results").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "ai_trader" / "ledger.json").write_text(
        json.dumps({"positions": {s: {"qty": 100} for s in symbols}}), encoding="utf-8"
    )
    (tmp_path / "state" / "ai_trader" / "plan_latest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "ai_trader" / "watchlist.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data" / "universe" / "a_share_symbols.csv").write_text(
        "symbol\n" + "\n".join(symbols) + "\n", encoding="utf-8"
    )
    return tmp_path


def _bar(symbol: str, dt: str, close: float = 10.0) -> dict:
    date, time_part = dt.split(" ")
    return {
        "symbol": symbol,
        "datetime": dt,
        "date": date,
        "time": time_part,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100.0,
        "amount": 1000.0,
        "price_basis": "raw",
        "source": "fake",
    }


def test_second_instance_returns_already_running(tmp_path):
    root = _scope_root(tmp_path, symbols=["600000"])
    lock_path = root / "data" / "intraday" / "1m" / ".locks" / "stage6_append_only_batch.lock"
    with interprocess_file_lock(lock_path, timeout_seconds=0):
        manifest = collect_intraday_refresh(
            root,
            network=True,
            now=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
            fetcher=lambda s, o: ([_bar(s, "2026-09-01 09:45:00")], ""),
            write_manifest=False,
        )
    assert manifest["status"] == "already_running"


def test_stale_json_marker_does_not_permanently_block_refresh(tmp_path):
    """A crashed old JSON marker is not an operating-system lock."""

    root = _scope_root(tmp_path, symbols=["600000"])
    marker = root / "data" / ".locks" / "intraday_refresh.lock.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"run_id": "crashed", "pid": 999999, "status": "running"}),
        encoding="utf-8",
    )

    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
        fetcher=lambda s, o: ([_bar(s, "2026-09-01 09:45:00")], ""),
        write_manifest=False,
    )

    assert manifest["status"] != "already_running"
    assert manifest["appended_rows"] == 1


def test_idempotent_append_never_duplicates_or_rewrites(tmp_path):
    path = tmp_path / "600000.csv"
    rows = [_bar("600000", "2026-09-01 09:45:00")]
    accepted, _ = _append_rows(path, rows, now_iso="2026-09-01T09:45:30")
    assert accepted == 1
    before = path.read_text(encoding="utf-8")
    accepted, rejects = _append_rows(path, rows, now_iso="2026-09-01T09:46:00")
    assert accepted == 0 and rejects
    assert path.read_text(encoding="utf-8") == before  # history untouched


def test_append_preserves_real_header_column_meaning(tmp_path):
    """Existing production files use source,updated_at,price_basis ordering."""

    path = tmp_path / "600000.csv"
    path.write_text(
        "symbol,datetime,date,time,open,high,low,close,volume,amount,source,updated_at,price_basis\n"
        "600000,2026-09-01 09:44:00,2026-09-01,09:44:00,10,10,10,10,100,1000,existing,2026-09-01T09:44:30,raw\n",
        encoding="utf-8",
    )

    accepted, reasons = _append_rows(
        path,
        [_bar("600000", "2026-09-01 09:45:00")],
        now_iso="2026-09-01T09:45:30",
    )

    assert accepted == 1 and reasons == []
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[-1]["source"] == "fake"
    assert rows[-1]["updated_at"] == "2026-09-01T09:45:30"
    assert rows[-1]["price_basis"] == "raw"


def test_existing_file_append_does_not_use_atomic_history_rewrite(tmp_path, monkeypatch):
    """Regression to read-merge-replace must fail even if bytes look identical."""

    path = tmp_path / "600000.csv"
    path.write_text(
        collector.MINUTE_HEADER + "\n"
        "600000,2026-09-01 09:44:00,2026-09-01,09:44:00,10,10,10,10,100,1000,existing,2026-09-01T09:44:30,raw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        collector,
        "_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("existing minute history was rewritten")),
        raising=False,
    )

    accepted, reasons = _append_rows(
        path,
        [_bar("600000", "2026-09-01 09:45:00")],
        now_iso="2026-09-01T09:45:30",
    )

    assert accepted == 1 and reasons == []


def test_unknown_existing_minute_header_fails_closed(tmp_path):
    path = tmp_path / "600000.csv"
    original = "symbol,datetime,unexpected\n600000,2026-09-01 09:44:00,x\n"
    path.write_text(original, encoding="utf-8")

    accepted, reasons = _append_rows(
        path,
        [_bar("600000", "2026-09-01 09:45:00")],
        now_iso="2026-09-01T09:45:30",
    )

    assert accepted == 0
    assert reasons == ["minute_header_mismatch:600000.csv"]
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "row,code",
    [
        (
            {"open": 0.0, "high": 1, "low": 1, "close": 1, "volume": 1, "datetime": "2026-09-01 09:45:00"},
            "open_not_positive",
        ),
        (
            {"open": 1, "high": 1, "low": 1, "close": -1, "volume": 1, "datetime": "2026-09-01 09:45:00"},
            "close_not_positive",
        ),
        (
            {"open": 1, "high": 0.5, "low": 1, "close": 1, "volume": 1, "datetime": "2026-09-01 09:45:00"},
            "high_below_body",
        ),
        (
            {"open": 1, "high": 1, "low": 1.5, "close": 1, "volume": 1, "datetime": "2026-09-01 09:45:00"},
            "low_above_body",
        ),
        ({"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "datetime": "2026-09-01 09:45:00"}, None),
    ],
)
def test_minute_bar_fail_closed(tmp_path, row, code):
    now = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)
    if "symbol" not in row:
        row["symbol"] = "600000"
    if "volume" in row and row["volume"] == 1:
        row["volume"] = 1.0
    ok, reason = validate_minute_bar(row, now=now, symbol="600000")
    if code is None:
        assert ok
    else:
        assert not ok and code in reason


def test_future_minute_bar_rejected():
    now = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)
    row = _bar("600000", "2026-09-01 10:30:00")
    ok, reason = validate_minute_bar(row, now=now, symbol="600000")
    assert not ok and "future_datetime" in reason


def test_provider_failure_not_reported_fresh(tmp_path):
    root = _scope_root(tmp_path, symbols=["600000"])
    manifest = collect_intraday_refresh(
        root,
        network=True,
        now=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
        fetcher=lambda s, o: ([], "provider_failed:mootxx:x"),
        write_manifest=False,
    )
    assert manifest["status"] == "blocked"
    assert manifest["fresh_count"] == 0


def test_offline_collection_is_planned_not_provider_failure(tmp_path):
    root = _scope_root(tmp_path, symbols=["600000"])
    calls = []

    manifest = collect_intraday_refresh(
        root,
        network=False,
        now=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
        fetcher=lambda symbol, offset: calls.append((symbol, offset)) or ([], ""),
        write_manifest=False,
    )

    assert manifest["status"] == "planned_offline"
    assert manifest["requested_count"] == 1
    assert manifest["provider_status"] == []
    assert "network_not_authorized" in manifest["reason_codes"]
    assert "provider_all_failed" not in manifest["reason_codes"]
    assert calls == []


@pytest.mark.parametrize("provider_raises", [False, True])
def test_mootdx_client_is_closed_on_success_and_failure(monkeypatch, provider_raises):
    import pandas as pd

    class FakeClient:
        def __init__(self):
            self.closed = False

        def bars(self, **_kwargs):
            if provider_raises:
                raise RuntimeError("provider boom")
            return pd.DataFrame(
                [
                    {
                        "datetime": "2026-09-01 09:45:00",
                        "open": 10,
                        "high": 10,
                        "low": 10,
                        "close": 10,
                        "volume": 100,
                        "amount": 1000,
                    }
                ]
            )

        def close(self):
            self.closed = True

    client = FakeClient()
    quotes_module = types.ModuleType("mootdx.quotes")

    class FakeQuotes:
        @staticmethod
        def factory(**_kwargs):
            return client

    quotes_module.Quotes = FakeQuotes
    mootdx_module = types.ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)

    collector._mootdx_minute_rows("600000", offset=60, now=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI))

    assert client.closed is True


# ---------------------------------------------------------------------------
# Dynamic scope
# ---------------------------------------------------------------------------


def test_dynamic_scope_dedup_and_priority(tmp_path):
    root = _scope_root(tmp_path, symbols=[])
    (root / "state" / "ai_trader" / "ledger.json").write_text(
        json.dumps({"positions": {"600000": {}, "000001": {}}}), encoding="utf-8"
    )
    (root / "state" / "ai_trader" / "plan_latest.json").write_text(
        json.dumps(
            {
                "committed": True,
                "trade_date": "2026-09-01",
                "intents": [{"action": "buy", "symbol": "000001"}, {"action": "buy", "symbol": "300750"}],
            }
        ),
        encoding="utf-8",
    )
    (root / "state" / "ai_trader" / "watchlist.json").write_text(
        json.dumps({"symbols": ["600000", "601398"]}), encoding="utf-8"
    )
    (root / "results" / "auto_long_candidates_latest.json").write_text(
        json.dumps({"candidates": [{"symbol": "601398"}, {"symbol": "600519"}]}), encoding="utf-8"
    )
    scope = build_dynamic_scope(root)
    by_symbol = {s["symbol"]: s for s in scope["symbols"]}
    assert by_symbol["600000"]["priority"] == "critical"  # position wins
    assert set(by_symbol["600000"]["reasons"]) == {"ai_position", "ai_watchlist"}
    assert by_symbol["000001"]["priority"] == "critical" and "plan" in by_symbol["000001"]["reasons"]
    assert by_symbol["300750"]["priority"] == "critical"
    assert by_symbol["601398"]["priority"] == "high"  # watchlist outranks candidate
    assert set(by_symbol["601398"]["reasons"]) == {"ai_watchlist", "research_candidate_long"}
    assert by_symbol["600519"]["priority"] == "normal"
    assert scope["counts"]["critical"] == 3


def test_dynamic_scope_reads_the_real_watchlist_items_shape(tmp_path):
    root = _scope_root(tmp_path, symbols=[])
    (root / "state" / "ai_trader" / "watchlist.json").write_text(
        json.dumps(
            {
                "schema_version": "ai_trader_watchlist_v1",
                "items": [
                    {"symbol": "600032", "last_seen": "2026-08-31"},
                    {"symbol": "bad"},
                ],
            }
        ),
        encoding="utf-8",
    )

    scope = build_dynamic_scope(root)

    by_symbol = {item["symbol"]: item for item in scope["symbols"]}
    assert by_symbol["600032"]["priority"] == "high"
    assert by_symbol["600032"]["reasons"] == ["ai_watchlist"]


def test_dynamic_scope_degrades_without_fabrication(tmp_path):
    scope = build_dynamic_scope(tmp_path)
    assert scope["symbols"] == []
    assert any("scope_source_missing" in w for w in scope["warnings"])


def test_select_mover_symbols_caps_and_prefers_limits():
    rows = (
        [{"symbol": f"{i:06d}", "pct_chg": 9.8, "amount": 1} for i in range(1, 5)]
        + [{"symbol": "600000", "pct_chg": 7.2, "amount": 10}]
        + [{"symbol": "601318", "pct_chg": 0.1, "amount": 9_999_999}]
        + [{"symbol": "688001", "pct_chg": -9.9, "amount": 1}]
    )
    movers = select_mover_symbols(rows, max_movers=8)
    reasons = {item["reason"] for item in movers}
    assert "snapshot_limit_up" in reasons
    assert "snapshot_limit_down" in reasons
    assert "snapshot_top_amount" in reasons
    assert len(movers) == 7


def test_snapshot_movers_join_minute_scope_without_replacing_positions(tmp_path):
    root = _scope_root(tmp_path, symbols=[])
    (root / "state" / "ai_trader" / "ledger.json").write_text(
        json.dumps({"positions": {"600000": {}}}), encoding="utf-8"
    )
    scope = build_dynamic_scope(
        root,
        extra_symbols=[
            {"symbol": "300750", "reason": "snapshot_limit_up"},
            {"symbol": "600000", "reason": "snapshot_mover"},
        ],
    )
    by_symbol = {item["symbol"]: item for item in scope["symbols"]}
    assert by_symbol["600000"]["priority"] == "critical"
    assert "snapshot_mover" in by_symbol["600000"]["reasons"]
    assert by_symbol["300750"]["priority"] == "high"
    assert by_symbol["300750"]["reasons"] == ["snapshot_limit_up"]


def test_write_spot_snapshot_overwrites_same_file(tmp_path):
    now = datetime(2026, 9, 4, 10, 0, tzinfo=SHANGHAI)
    first = [{"symbol": "600000", "name": "A", "price": 10, "pct_chg": 1, "amount": 1,
              "change": 0, "volume": 1, "open": 10, "pre_close": 10, "high": 10, "low": 10,
              "quote_time": "2026-09-04 10:00:00", "quote_date": "2026-09-04", "source": "test"}]
    path = write_spot_snapshot(tmp_path, first, source="test", now=now)
    second = list(first)
    second[0] = {**first[0], "price": 11, "pct_chg": 2}
    write_spot_snapshot(tmp_path, second, source="test", now=now)
    text = path.read_text(encoding="utf-8-sig")
    assert text.count("600000") == 1
    assert ",11," in text or "11.0" in text


# ---------------------------------------------------------------------------
# Alerts: three gates, dedupe by the deliverer, no real sends
# ---------------------------------------------------------------------------


def test_alerts_dry_run_preview_and_gates(tmp_path, monkeypatch):
    monkeypatch.delenv("QUANTDESK_DATA_HEALTH_ALERTS_PUSH", raising=False)
    root = _scope_root(tmp_path, symbols=["600000"])
    (root / "results" / "data_refresh").mkdir(parents=True, exist_ok=True)
    (root / "data_refresh" / "x").mkdir(parents=True, exist_ok=True) if False else None
    (root / "results" / "data_refresh" / "data_refresh_latest.json").write_text(
        json.dumps({"overall_ok": False, "target_date": "2026-09-01", "families": {"news": {"ok": False}}}),
        encoding="utf-8",
    )
    result = dispatch_intraday_alerts(
        root,
        network=False,
        push=False,
        dry_run=True,
        intraday_manifest={
            "status": "stale",
            "stale_count": 2,
            "provider_status": [{"symbol": "600000", "status": "ok"}],
        },
    )
    assert result["status"] == "preview_generated"
    assert result["event_count"] >= 1

    # Owner 2026-09-05: a missing push switch must not fail the data package, so
    # the deliverer previews instead of blocking.  The gate still exists - it is
    # covered by the network gate below.
    gated_off = dispatch_intraday_alerts(
        root,
        network=True,
        push=True,
        dry_run=False,
        intraday_manifest={"status": "stale", "stale_count": 2, "provider_status": []},
    )
    assert gated_off["status"] == "preview_generated"
    assert gated_off["reason_code"] == "data_health_alert_push_not_explicitly_enabled"

    monkeypatch.setenv("QUANTDESK_DATA_HEALTH_ALERTS_PUSH", "1")
    blocked = dispatch_intraday_alerts(
        root,
        network=False,
        push=True,
        dry_run=False,
        intraday_manifest={"status": "stale", "stale_count": 2, "provider_status": []},
    )
    assert blocked["status"] == "blocked"  # network gate missing
    assert blocked["reason_code"] == "network_not_authorized"

    sent = []

    def fake_sender(**kwargs):
        sent.append(kwargs)
        return {"channel": "server_chan", "status": "ok"}

    monkeypatch.setenv("QUANTDESK_DATA_HEALTH_ALERTS_PUSH", "1")
    monkeypatch.setenv("QUANTDESK_SERVERCHAN_SENDKEY", "test-key-not-real")
    ok_push = dispatch_intraday_alerts(
        root,
        network=True,
        push=True,
        dry_run=False,
        intraday_manifest={"status": "stale", "stale_count": 2, "provider_status": []},
        sender=fake_sender,
    )
    # sendkey comes from the env; the fake sender is invoked only after the
    # deliverer's own gates pass, so any of sent/ok proves the chain reached
    # the sender without a real network call.
    assert ok_push["status"] in ("sent", "ok", "failed")
    assert sent, "fake sender must be used instead of a real network call"


def test_alert_recovery_notification(tmp_path, monkeypatch):
    monkeypatch.delenv("QUANTDESK_DATA_HEALTH_ALERTS_PUSH", raising=False)
    root = _scope_root(tmp_path, symbols=["600000"])
    first = dispatch_intraday_alerts(
        root,
        network=False,
        push=False,
        dry_run=True,
        intraday_manifest={"status": "stale", "stale_count": 1, "provider_status": []},
    )
    second = dispatch_intraday_alerts(
        root,
        network=False,
        push=False,
        dry_run=True,
        intraday_manifest={"status": "ready", "provider_status": [{"symbol": "600000", "status": "ok"}]},
    )
    assert first["event_count"] >= 1
    assert second["event_count"] >= 1  # recovery event emitted


# ---------------------------------------------------------------------------
# Seven-ledger read-only guarantee
# ---------------------------------------------------------------------------


def test_scope_and_manifest_do_not_touch_ledgers():
    hashes_before = {
        rel: hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
        for rel in LEDGERS
        if (REPO_ROOT / rel).is_file()
    }
    if len(hashes_before) != len(LEDGERS):
        # The seven ledgers are runtime assets of the canonical checkout and are
        # deliberately not tracked, so an isolated worktree cannot hash them.
        # Skip loudly instead of asserting a guarantee that has nothing to guard.
        pytest.skip(f"runtime ledgers unavailable here: {len(hashes_before)}/{len(LEDGERS)} present")
    scope = build_dynamic_scope(REPO_ROOT)
    assert scope["read_only"] is True
    for rel, before in hashes_before.items():
        after = hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
        assert after == before, f"ledger changed: {rel}"


def test_suspension_evidence_contract_fields():
    path = REPO_ROOT / "data" / "metadata" / "suspension_evidence" / "20260831.json"
    if not path.is_file():
        pytest.skip("evidence fixture from 2026-08-31 not present")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["target_date"] == "2026-08-31"
    assert payload["contract"].startswith("suspended day = no bar")
    verified = payload.get("verified_exclusions") or {}
    for meta in verified.values():
        assert "signature" in meta
        assert meta.get("signature") in ("zero_trade_with_estimates", "zero_volume_with_last_close")
        for field in ("open", "close", "volume", "pe", "pb", "total_mv_raw"):
            assert field in meta
