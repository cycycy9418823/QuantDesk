"""Minute intake uses declared evidence and records incomplete snapshot fetches."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from quantdesk import snapshot_history
from quantdesk.intraday import minute_storage
from quantdesk.intraday.intraday_schema import read_intraday_csv
from quantdesk.intraday_refresh import collector, snapshot


DAY = "2026-10-09"
NOW = datetime(2026, 10, 9, 14, 59, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _bar(stamp, basis="raw"):
    return {"symbol": "600000", "datetime": stamp, "date": stamp[:10], "time": stamp[11:],
            "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "amount": 1000,
            "price_basis": basis, "source": "fixture", "updated_at": NOW.isoformat()}


def _write_minutes(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=collector.MINUTE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _archive(root, *, bad_rows=True):
    path = root / "data/snapshots" / DAY / "1455.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fetched = "2026-10-09T14:55:30+08:00"
    rows = [{name: "" for name in snapshot_history.SNAPSHOT_COLUMNS}]
    rows[0].update(snapshot_date=DAY, snapshot_time="1455", symbol="600000", price="10",
                   name="fixture", amount="1000", pct_chg="", quote_date="", quote_time="14:55:03",
                   fetched_at=fetched, source="fixture.sina")
    if bad_rows:
        rows += [{**rows[0], "symbol": "600001", "price": "0"},
                 {**rows[0], "symbol": "600002", "quote_time": ""}]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=snapshot_history.SNAPSHOT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    raw = stream.getvalue().encode("utf-8")
    path.write_bytes(raw)
    symbols = [row["symbol"] for row in rows]
    metadata = {
        "schema_version": "a0_snapshot_history_v1", "trade_date": DAY, "slot_time": "1455",
        "snapshot_path": path.relative_to(root).as_posix(), "row_count": len(rows),
        "expected_symbols": symbols, "expected_symbol_count": len(symbols),
        "universe_sha256": snapshot_history._universe_hash(symbols), "csv_sha256": hashlib.sha256(raw).hexdigest(),
        "source": "fixture.sina", "fetched_at": fetched, "missing_symbols": [],
        "coverage_count": len(rows), "coverage_ratio": 1.0, "unexpected_symbols": [],
        "status": "partial" if bad_rows else "written", "warnings": [],
    }
    path.with_suffix(".json").write_text(json.dumps(metadata), encoding="utf-8")
    assert snapshot_history._slot_evidence(root, DAY, "1455")["status"] in {"partial", "complete"}
    return path, metadata


def test_unknown_history_keeps_bytes_while_new_raw_is_saved_separately(tmp_path):
    path = tmp_path / "data/intraday/1m/600000.csv"
    _write_minutes(path, [_bar("2026-10-08 09:31:00", "unknown"), _bar("2026-10-08 09:32:00")])
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    count, reasons = collector._append_rows(path, [_bar(f"{DAY} 14:59:00")], now_iso=NOW.isoformat())
    assert count == 1, reasons
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    actual = minute_storage.resolve_minute_read_path(path)
    assert actual == path.parent / "declared_basis" / path.name
    payload = read_intraday_csv(path, default_symbol="600000", target_date=DAY)
    assert payload["price_basis"] == "raw" and len(payload["frame"]) == 1
    assert collector._append_rows(path, [_bar(f"{DAY} 14:59:00")], now_iso=NOW.isoformat())[0] == 0


def test_paginated_failure_is_partial_with_page_evidence(tmp_path, monkeypatch):
    calls = []
    page = [{"code": f"600{i:03d}", "trade": 10, "amount": 1000} for i in range(100)]

    def get(*args, **kwargs):
        number = kwargs["params"]["page"]
        calls.append(number)
        if number == 2:
            raise TimeoutError("fixture page timeout")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: page)

    import requests
    import time
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = snapshot.refresh_full_market_snapshot(tmp_path, network=True, now=NOW, budget_seconds=20, clock=lambda: 0)
    assert calls == [1, 2, 2]
    assert result["status"] == "partial" and result["ok"] is False
    assert result["row_count"] == 100 and result["published"] is True
    assert result["pagination"]["failed_page"] == 2
    assert result["pagination"]["pages_completed"] == 1
    assert any("TimeoutError" in reason for reason in result["reason_codes"])


def test_storage_receipt_uses_the_same_observation_clock_as_the_write(tmp_path):
    current = datetime(2099, 10, 9, 14, 59, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = tmp_path / "data/intraday/1m/600000.csv"
    _write_minutes(path, [_bar("2099-10-08 09:31:00", "unknown")])
    before = path.read_bytes()
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=current,
        snapshot_fetcher=lambda: ([], "fixture"),
        fetcher=lambda *a: ([_bar("2099-10-09 14:59:00")], ""),
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600000", "priority": "critical"}], "warnings": []},
    )
    receipt = result["provider_status"][0]
    expected = path.parent / "declared_basis" / path.name
    assert receipt["appended"] == 1 and expected.is_file()
    assert tmp_path / receipt["storage_path"] == expected
    assert path.read_bytes() == before


def test_verified_synced_snapshot_is_reused_without_changing_evidence(tmp_path, monkeypatch):
    path, metadata = _archive(tmp_path)
    cache = tmp_path / snapshot.SPOT_RELATIVE
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"existing unrelated cache\n")
    cache_before = cache.read_bytes(), cache.stat().st_mtime_ns
    originals = {p: p.read_bytes() for p in (path, path.with_suffix(".json"))}

    def forbidden(**kwargs):
        raise AssertionError("fresh verified snapshot must not be fetched again")

    monkeypatch.setattr(snapshot, "_default_network_fetch", forbidden)
    result = snapshot.refresh_full_market_snapshot(tmp_path, network=True, now=NOW, budget_seconds=60)
    assert result["reused"] is True and result["status"] == "partial"
    assert result["archive_sha256"] == metadata["csv_sha256"]
    assert result["archive_row_count"] == 3 and result["row_count"] == 1
    reused, _ = snapshot._synced_snapshot(tmp_path, NOW)
    rows = reused["rows"]
    assert rows[0]["quote_date"] == "" and rows[0]["quote_time"] == "14:55:03"
    assert rows[0]["pct_chg"] == "" and rows[0]["source"] == "fixture.sina"
    assert rows[0]["fetched_at"] == metadata["fetched_at"]
    assert result["updated_at"] == metadata["fetched_at"]
    assert result["reused_at"] == NOW.isoformat(timespec="seconds")
    assert result["published"] is False and tmp_path / result["path"] == path
    assert cache_before == (cache.read_bytes(), cache.stat().st_mtime_ns)
    assert not (tmp_path / snapshot.SPOT_META_RELATIVE).exists()
    assert originals == {p: p.read_bytes() for p in originals}


def test_minute_round_reserves_time_after_snapshot(tmp_path, monkeypatch):
    seen = []

    def spot(*args, budget_seconds, **kwargs):
        seen.append(budget_seconds)
        return {"movers": [], "reason_codes": []}

    monkeypatch.setattr(collector, "refresh_full_market_snapshot", spot)
    collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=NOW,
        fetcher=lambda *a: ([_bar(f"{DAY} 14:59:00")], ""),
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600000", "priority": "critical"}], "warnings": []},
    )
    assert len(seen) == 1 and 0 < seen[0] <= 60


@pytest.mark.parametrize("damage", ["negative_price", "symbol", "datetime", "date", "future", "qfq", "hfq", "malformed"])
def test_unknown_basis_does_not_hide_other_history_damage(tmp_path, damage):
    path = tmp_path / "600000.csv"
    rows = [_bar("2026-10-08 09:31:00"), _bar("2026-10-08 09:32:00", "unknown")]
    if damage == "negative_price":
        rows[0]["close"] = -10
    elif damage == "symbol":
        rows[0]["symbol"] = "600001"
    elif damage == "datetime":
        rows[0]["datetime"] = "not_a_date"
    elif damage == "date":
        rows[0]["date"] = "2026-99-99"
    elif damage == "future":
        rows[0] = _bar("2026-10-09 15:30:00")
    elif damage in {"qfq", "hfq"}:
        rows[0]["price_basis"] = damage
    _write_minutes(path, rows)
    if damage == "malformed":
        path.write_bytes(path.read_bytes() + b"600000,partial\n")
    before = path.read_bytes()
    count, _ = collector._append_rows(path, [_bar(f"{DAY} 14:59:00")], now_iso=NOW.isoformat())
    assert count == 0 and path.read_bytes() == before
    assert not (path.parent / "declared_basis" / path.name).exists()


@pytest.mark.parametrize("basis", ["unknown", "qfq", "hfq", ""])
def test_new_or_preexisting_segment_must_still_declare_raw(tmp_path, basis):
    path = tmp_path / "600000.csv"
    _write_minutes(path, [_bar("2026-10-08 09:31:00", "unknown")])
    before = path.read_bytes()
    assert collector._append_rows(path, [_bar(f"{DAY} 14:59:00", basis)], now_iso=NOW.isoformat())[0] == 0
    actual = path.parent / "declared_basis" / path.name
    _write_minutes(actual, [_bar(f"{DAY} 14:58:00", basis)])
    segment = actual.read_bytes()
    assert collector._append_rows(path, [_bar(f"{DAY} 14:59:00")], now_iso=NOW.isoformat())[0] == 0
    assert actual.read_bytes() == segment and path.read_bytes() == before
    assert not (actual.parent / "declared_basis").exists()


# 1C (owner plan 2026-10-11): raw rows get the same float-serialization
# tolerance as unknown rows; a real inversion (8.959) still fails for both.
@pytest.mark.parametrize(("basis", "high", "separate"), [
    ("unknown", "8.959999999999999", True),
    ("raw", "8.959999999999999", True),
    ("unknown", "8.959", False),
    ("raw", "8.959", False),
])
def test_unknown_float_serialization_never_relaxes_raw_validation(tmp_path, basis, high, separate):
    path = tmp_path / "600000.csv"
    old = _bar("2026-10-08 09:31:00", basis)
    old.update(open="8.96", high=high, low="8.94", close="8.95")
    _write_minutes(path, [old, _bar("2026-10-08 09:32:00", "unknown")])
    before = path.read_bytes()
    count, _ = collector._append_rows(path, [_bar(f"{DAY} 14:59:00")], now_iso=NOW.isoformat())
    assert count == int(separate)
    assert path.read_bytes() == before
    assert (path.parent / "declared_basis" / path.name).exists() is separate


@pytest.mark.parametrize("bad_page", [["provider_error"], [{}], [{"symbol": "bad", "trade": 10}]])
def test_bad_terminal_page_cannot_prove_snapshot_complete(tmp_path, monkeypatch, bad_page):
    import requests
    import time

    first = [{"code": f"600{i:03d}", "trade": 10, "amount": 1000} for i in range(100)]
    monkeypatch.setattr(requests, "get", lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: first if kw["params"]["page"] == 1 else bad_page))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = snapshot.refresh_full_market_snapshot(tmp_path, network=True, now=NOW, budget_seconds=20, clock=lambda: 0)
    assert result["ok"] is False and result["status"] == "partial"
    assert result["pagination"]["complete"] is False and result["pagination"]["failed_page"] == 2
    assert any("invalid_rows" in code for code in result["reason_codes"])
    meta = json.loads((tmp_path / snapshot.SPOT_META_RELATIVE).read_text())
    assert meta["pagination_complete"] is False and "coverage_complete" not in meta


@pytest.mark.parametrize("fault", ["old", "future_capture", "tampered", "date_conflict", "all_bad_prices"])
def test_unusable_synced_archive_falls_back_without_refreshing_its_age(tmp_path, monkeypatch, fault):
    path, meta = _archive(tmp_path, bad_rows=False)
    if fault == "tampered":
        path.write_bytes(path.read_bytes() + b"\n")
    elif fault in {"date_conflict", "all_bad_prices"}:
        raw = list(csv.DictReader(io.StringIO(path.read_text())))
        if fault == "date_conflict":
            raw[0].update(quote_date="2026-10-08", quote_time="2026-10-09 14:55:03")
        else:
            raw[0]["price"] = "0"
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=snapshot_history.SNAPSHOT_COLUMNS)
        writer.writeheader()
        writer.writerows(raw)
        path.write_text(stream.getvalue(), encoding="utf-8")
        meta["csv_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    current = NOW.replace(hour=15, minute=5) if fault == "old" else NOW.replace(minute=55, second=10) if fault == "future_capture" else NOW
    calls = []
    monkeypatch.setattr(snapshot, "_default_network_fetch", lambda **k: calls.append(k) or ([], "fixture"))
    result = snapshot.refresh_full_market_snapshot(tmp_path, network=True, now=current, budget_seconds=60)
    assert len(calls) == 1 and not result.get("reused") and result["ok"] is False
    assert not (tmp_path / snapshot.SPOT_RELATIVE).exists()


def test_synced_invalid_amount_uses_safe_ranking_and_keeps_original(tmp_path, monkeypatch):
    path, meta = _archive(tmp_path, bad_rows=False)
    raw = list(csv.DictReader(io.StringIO(path.read_text())))
    raw[0].update(amount="--", pct_chg="9.9")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=snapshot_history.SNAPSHOT_COLUMNS)
    writer.writeheader()
    writer.writerows(raw)
    path.write_text(stream.getvalue(), encoding="utf-8")
    meta["csv_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(snapshot, "_default_network_fetch", lambda **k: pytest.fail("unexpected network"))
    result = snapshot.refresh_full_market_snapshot(tmp_path, network=True, now=NOW, budget_seconds=60)
    assert result["reused"] and result["movers"] == [{"symbol": "600000", "reason": "snapshot_limit_up"}]
    saved = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert saved[0]["amount"] == "--"
    assert not (tmp_path / snapshot.SPOT_RELATIVE).exists()


def test_synced_evidence_reaches_round_manifest(tmp_path, monkeypatch):
    _archive(tmp_path, bad_rows=False)
    monkeypatch.setattr(snapshot, "_default_network_fetch", lambda **k: pytest.fail("unexpected network"))
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=NOW,
        fetcher=lambda *a: ([_bar(f"{DAY} 14:59:00")], ""),
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600000", "priority": "critical"}], "warnings": []},
    )
    assert result["snapshot"]["reused"] is True
    assert result["snapshot"]["archive_sha256"] and result["snapshot"]["source_fetched_at"] == "2026-10-09T14:55:30+08:00"
    assert result["fresh_count"] == 1 and result["appended_rows"] == 1
