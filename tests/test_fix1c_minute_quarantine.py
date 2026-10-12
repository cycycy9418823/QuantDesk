"""1C-D: one bad stored minute row is isolated, not a whole-file refusal.

2026-10-09: 73 symbols were storage_blocked all day. For the all-raw archives
the first "invalid" row was float serialization noise, e.g. 600036 line 48927
open=39.690000000000005 > high=39.69.  These tests fail on the old collector.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday import minute_storage
from quantdesk.intraday_refresh import collector

DAY = "2026-10-09"
NOW = datetime(2026, 10, 9, 14, 59, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _bar(stamp, *, symbol="600036", **prices):
    row = {"symbol": symbol, "datetime": stamp, "date": stamp[:10], "time": stamp[11:],
           "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.2, "volume": 100, "amount": 1000,
           "source": "fixture", "updated_at": stamp, "price_basis": "raw"}
    row.update(prices)
    return row


def _write(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=collector.MINUTE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# Real stored values, data/intraday/1m/600036.csv line 48927.
NOISE = {"open": "39.690000000000005", "high": "39.69", "low": "39.50999999999999", "close": "39.52000000000001"}


def test_float_noise_is_not_an_inverted_bar_but_a_real_inversion_is():
    assert collector.validate_minute_bar(_bar("2026-04-24 09:32:00", **NOISE), now=NOW, symbol="600036")[0]
    real = {**NOISE, "high": "39.68"}
    ok, reason = collector.validate_minute_bar(_bar("2026-04-24 09:32:00", **real), now=NOW, symbol="600036")
    assert not ok and reason.endswith("high_below_body")
    assert minute_storage.envelope_violation(10.0, 9.99, 9.0, 9.5) == "high_below_body"
    assert minute_storage.envelope_violation(10.0, 11.0, 10.01, 10.5) == "low_above_body"


def test_noise_row_in_raw_history_no_longer_blocks_the_archive(tmp_path):
    path = tmp_path / "600036.csv"
    _write(path, [_bar("2026-04-24 09:31:00"), _bar("2026-04-24 09:32:00", **NOISE)])
    before = path.read_bytes()
    count, reasons = collector._append_rows(path, [_bar(f"{DAY} 14:59:00")], now_iso=NOW.isoformat())
    assert count == 1, reasons
    assert path.read_bytes().startswith(before)
    assert not (tmp_path / minute_storage.QUARANTINE_DIR).exists()


def test_torn_row_is_quarantined_and_cannot_deduplicate_or_claim_fresh(tmp_path):
    root = tmp_path
    path = root / "data/intraday/1m/600036.csv"
    stamp = f"{DAY} 14:55:00"
    _write(path, [_bar(f"{DAY} 14:50:00")])
    path.write_bytes(path.read_bytes() + f"600036,{stamp}\n".encode("utf-8"))
    before = path.read_bytes()
    result = collector.collect_intraday_refresh(
        root, network=True, trading_day=True, now=NOW.replace(minute=56),
        snapshot_fetcher=lambda: ([], "fixture"),
        fetcher=lambda *_a: ([_bar(stamp)], ""),
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600036", "priority": "critical"}], "warnings": []},
    )
    receipt = result["provider_status"][0]
    assert receipt["status"] == "ok" and receipt["appended"] == 1
    assert receipt["quarantined_rows"] == 1
    # Freshness comes from the bar that was actually stored, not the torn row.
    assert receipt["latest_bar_time"] == stamp
    assert path.read_bytes().startswith(before)
    record = json.loads((path.parent / minute_storage.QUARANTINE_DIR / "600036.json").read_text(encoding="utf-8"))
    assert record["quarantined_count"] == 1 and record["reasons"] == {"column_count": 1}
    assert record["rows"][0]["line"] == 3
    assert any(code.startswith("minute_stored_rows_quarantined:600036.csv:1") for code in result["reason_codes"])


def test_bad_price_row_is_isolated_and_good_rows_still_deduplicate(tmp_path):
    path = tmp_path / "600036.csv"
    good = _bar(f"{DAY} 14:50:00")
    _write(path, [good, _bar(f"{DAY} 14:51:00", close=-1)])
    before = path.read_bytes()
    count, reasons = collector._append_rows(path, [good], now_iso=NOW.isoformat())
    assert count == 0 and any(r.startswith("duplicate_minute_bar:") for r in reasons)
    assert any(r.startswith("minute_stored_rows_quarantined:600036.csv:1") for r in reasons)
    assert path.read_bytes() == before
    count, _ = collector._append_rows(path, [_bar(f"{DAY} 14:51:00")], now_iso=NOW.isoformat())
    assert count == 1


def test_non_raw_history_still_blocks_the_whole_file(tmp_path):
    path = tmp_path / "600036.csv"
    _write(path, [_bar(f"{DAY} 14:50:00", price_basis="qfq")])
    before = path.read_bytes()
    count, reasons = collector._append_rows(path, [_bar(f"{DAY} 14:59:00")], now_iso=NOW.isoformat())
    assert count == 0 and reasons[0].startswith("minute_stored_row_invalid:")
    assert path.read_bytes() == before


def test_failed_replace_leaves_the_archive_unchanged_and_the_round_alive(tmp_path, monkeypatch):
    root = tmp_path
    first = root / "data/intraday/1m/600036.csv"
    second = root / "data/intraday/1m/600000.csv"
    _write(first, [_bar(f"{DAY} 14:50:00")])
    before = first.read_bytes()
    real_replace = os.replace

    def replace(src, dst):
        if Path(dst).name == "600036.csv":
            raise PermissionError("held by a reader")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(collector.time, "sleep", lambda seconds: None)
    result = collector.collect_intraday_refresh(
        root, network=True, trading_day=True, now=NOW,
        snapshot_fetcher=lambda: ([], "fixture"),
        fetcher=lambda symbol, _o: ([_bar(f"{DAY} 14:59:00", symbol=symbol)], ""),
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": "600036", "priority": "critical"},
                                                    {"symbol": "600000", "priority": "critical"}], "warnings": []},
    )
    by_symbol = {item["symbol"]: item for item in result["provider_status"]}
    assert by_symbol["600036"]["status"] == "storage_blocked"
    assert by_symbol["600036"]["reason"].startswith("minute_append_failed:600036.csv")
    assert first.read_bytes() == before
    assert not list(first.parent.glob(".*.tmp"))
    assert by_symbol["600000"]["status"] == "ok" and second.is_file()


def test_archive_changed_after_validation_is_not_overwritten(tmp_path):
    path = tmp_path / "600036.csv"
    _write(path, [_bar(f"{DAY} 14:50:00")])
    scanned = path.stat()
    path.write_bytes(path.read_bytes() + b"600036,changed-by-someone-else\n")
    changed = path.read_bytes()
    with pytest.raises(OSError):
        collector._atomic_append_bytes(path, b"new\n", scanned=scanned)
    assert path.read_bytes() == changed


def test_round_budget_fits_inside_the_five_minute_trigger():
    assert 0 < collector.DEFAULT_MAX_ROUND_SECONDS < 300
