"""S03 regressions: every attempt sidecar a capture writes must round-trip its own reader.

A partial capture can carry warnings without recording a reason code - a slot
with one usable row still flags its stale or missing rows - and the sidecar it
writes must stay readable by ``_read_attempts``.  The caller's own result payload
keeps exactly the reasons it always had, a genuinely malformed receipt is still
rejected (including an empty ``reason_codes``), and no reason or quote is
invented to make a slot look better.

No test contacts the network: providers and clocks are injected.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY = "2026-09-07"


@pytest.fixture(autouse=True)
def _legacy_hq_unavailable(monkeypatch):
    """These cases exercise the original providers with HQ unavailable."""
    from quantdesk import snapshot_history

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture_hq_unavailable")

    monkeypatch.setattr(snapshot_history, "_sina_hq_fetch", unavailable, raising=False)


def _history():
    return importlib.import_module("quantdesk.snapshot_history")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 7, hour, minute, 3, tzinfo=SHANGHAI)


def _calendar(root: Path) -> None:
    path = root / "data/calendar/a_share_trading_calendar.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"date,is_open,source\n{DAY},1,fixture\n", encoding="utf-8")


def _quote(symbol: str, price: object = 10.5, quote_time: str = "10:00:01", quote_date: str = DAY) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": "sample",
        "price": price,
        "quote_date": quote_date,
        "quote_time": quote_time,
    }


def _archive_one_slot(
    tmp_path: Path, symbols: list[str], payload: list[dict], hour: int, minute: int,
) -> dict:
    return _history().collect_market_snapshot(
        tmp_path,
        network=True,
        now=_at(hour, minute),
        clock=lambda: _at(hour, minute),
        symbols=symbols,
        fetcher=lambda: (payload, "fixture.sina"),
    )


def _summary(tmp_path: Path, symbols: list[str]) -> dict:
    return _history().snapshot_day_summary(tmp_path, DAY, symbols=symbols, as_of=_at(10, 0))


def _write_sidecar(root: Path, name: str, **overrides) -> None:
    directory = root / "data/snapshots" / DAY / "attempts"
    directory.mkdir(parents=True, exist_ok=True)
    item = {
        "schema_version": "a0_snapshot_attempt_v1",
        "trade_date": DAY,
        "slot_time": "1000",
        "status": "partial_archive",
        "reason_codes": ["snapshot_symbols_missing"],
        "warnings": ["snapshot_symbols_missing"],
    }
    item.update(overrides)
    (directory / name).write_text(json.dumps(item), encoding="utf-8")


# ---------------------------------------------------------------------------
# A partial archive's own receipt must survive its own reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbols", "payload", "expected_warning"),
    [
        (["600000", "920000"], [_quote("600000")], "snapshot_symbols_missing"),
        (
            ["600000", "000001"],
            [_quote("600000"), _quote("000001", quote_date="2026-09-04", quote_time="15:00:00")],
            "snapshot_quote_time_stale:1",
        ),
    ],
    ids=["roster_short", "stale_row"],
)
def test_partial_archive_sidecar_round_trips_its_own_reader(
    tmp_path: Path, symbols: list[str], payload: list[dict], expected_warning: str,
) -> None:
    """Warnings without a recorded reason must still leave a readable attempt."""

    _calendar(tmp_path)
    result = _archive_one_slot(tmp_path, symbols, payload, 10, 0)

    assert result["status"] == "partial"
    assert result["warnings"] == [expected_warning]
    # The caller's payload keeps exactly the reasons it always had - the
    # round-trip fix is scoped to the sidecar.
    assert result["reason_codes"] == []

    sidecar = json.loads((tmp_path / result["attempt_path"]).read_text(encoding="utf-8"))
    assert sidecar["status"] == "partial_archive"
    assert sidecar["reason_codes"] == [expected_warning]

    summary = _summary(tmp_path, symbols)
    assert summary["invalid_attempts"] == {}
    assert len(summary["slot_attempts"]["1000"]) == 1
    assert summary["slot_attempts"]["1000"][0]["reason_codes"] == [expected_warning]


# ---------------------------------------------------------------------------
# Malformed receipts are still rejected - including an empty reason list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        ("wrong-schema.json", {"schema_version": "a0_snapshot_attempt_v2"}),
        ("wrong-date.json", {"trade_date": "2026-09-04"}),
        ("unreadable-status.json", {"status": "attempt_unclassified"}),
        ("empty-reasons.json", {"reason_codes": []}),
        ("reasons-not-a-list.json", {"reason_codes": "snapshot_symbols_missing"}),
    ],
    ids=["wrong_schema", "wrong_date", "unreadable_status", "empty_reasons", "reasons_not_a_list"],
)
def test_malformed_attempt_receipts_are_still_rejected(
    tmp_path: Path, name: str, overrides: dict,
) -> None:
    _write_sidecar(tmp_path, name, **overrides)

    summary = _summary(tmp_path, ["600000"])

    assert summary["slot_attempts"] == {}
    assert len(summary["invalid_attempts"]) == 1
    (key, value), = summary["invalid_attempts"].items()
    assert key.endswith(name)
    assert "attempt_schema_invalid" in value


def test_valid_attempt_receipt_is_accepted(tmp_path: Path) -> None:
    """The malformed-receipt test above would pass even if all receipts were dropped."""

    _write_sidecar(tmp_path, "1000-000001.json")

    summary = _summary(tmp_path, ["600000"])

    assert summary["invalid_attempts"] == {}
    assert len(summary["slot_attempts"]["1000"]) == 1


# ---------------------------------------------------------------------------
# Failed and full captures keep their unchanged round trip
# ---------------------------------------------------------------------------


def test_failed_and_full_attempts_round_trip(tmp_path: Path) -> None:
    _calendar(tmp_path)

    failed = _archive_one_slot(tmp_path, ["600000"], [], 10, 0)
    assert failed["status"] == "provider_empty"
    sidecar = json.loads((tmp_path / failed["attempt_path"]).read_text(encoding="utf-8"))
    assert sidecar["status"] == "provider_empty"
    assert sidecar["reason_codes"] == ["provider_returned_no_usable_rows"]
    summary = _summary(tmp_path, ["600000"])
    assert summary["invalid_attempts"] == {}
    assert summary["slot_attempts"]["1000"][-1]["reason_codes"] == ["provider_returned_no_usable_rows"]

    written = _archive_one_slot(tmp_path, ["600000"], [_quote("600000")], 10, 0)
    assert written["status"] == "written"
    assert not written.get("attempt_written")
    recheck = _summary(tmp_path, ["600000"])
    assert recheck["invalid_attempts"] == {}
    assert recheck["completed_slots"] == ["1000"]
