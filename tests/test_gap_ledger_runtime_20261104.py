"""Keep the full history audit while bounding repeated per-row work."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from quantdesk import daily_gap_recovery as gap


SYMBOL = "000001"
TARGET = "2026-11-03"
FIELDS = ("date", "symbol", "open", "high", "low", "close", "volume", "adjust")


def _bar(day: str, **changes: str) -> dict[str, str]:
    return {
        "date": day, "symbol": SYMBOL, "open": "10", "high": "11",
        "low": "9", "close": "10", "volume": "1000", "adjust": "raw",
        **changes,
    }


def _write(root: Path, rows: list[dict[str, str]]) -> Path:
    path = root / "data" / "daily" / f"{SYMBOL}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _audit(root: Path, days: list[str] | None = None) -> dict:
    return gap.build_symbol_date_gap_ledger(
        project_root=root, symbols=[SYMBOL], trading_days=[TARGET] if days is None else days
    )


def test_repeated_symbol_spellings_do_not_repeat_normalization_per_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A large local history still has only three distinct identity spellings.
    days = pd.bdate_range(end=TARGET, periods=1000).strftime("%Y-%m-%d").tolist()
    spellings = ("000001", "1", "SZ.000001")
    path = _write(tmp_path, [_bar(day, symbol=spellings[i % 3]) for i, day in enumerate(days)])
    original_bytes = path.read_bytes()
    original_normalize = gap.normalize_symbol
    calls = 0

    def counted(value: object) -> str:
        nonlocal calls
        calls += 1
        return original_normalize(value)

    monkeypatch.setattr(gap, "normalize_symbol", counted)
    ledger = _audit(tmp_path, days[-250:])

    assert ledger["totals"] == {"recovered": 250, "verified_no_session": 0, "pending": 0}
    assert ledger["rows"][0]["bar_count"] == 1000
    assert ledger["invalid_symbol_count"] == 0
    assert path.read_bytes() == original_bytes
    assert calls <= 8, f"repeated identity conversion still scales with history rows: {calls}"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"date": "not-a-date"}, "original_frame_invalid_date"),
        ({"symbol": "000002"}, "original_frame_symbol_mismatch"),
        ({"symbol": ""}, "original_frame_symbol_mismatch"),
        ({"adjust": "qfq"}, "original_frame_not_verified_raw"),
        ({"adjust": ""}, "original_frame_not_verified_raw"),
    ],
)
def test_original_history_is_validated_before_selecting_requested_dates(
    tmp_path: Path, changes: dict[str, str], reason: str
) -> None:
    old = _bar("2020-01-02")
    old.update(changes)
    _write(tmp_path, [old, _bar(TARGET)])

    row = _audit(tmp_path)["rows"][0]

    assert not row["usable"]
    assert reason in row["invalid_evidence"]
    assert row["recovered_dates"] == row["verified_no_session_dates"] == row["pending_dates"] == []


def test_duplicate_dates_outside_the_requested_window_remain_invalid(tmp_path: Path) -> None:
    _write(tmp_path, [_bar("2020-01-02"), _bar("2020-01-02"), _bar(TARGET)])

    row = _audit(tmp_path)["rows"][0]

    assert not row["usable"]
    assert row["bar_count"] == 2
    assert row["invalid_evidence"] == ["duplicate_date:2020-01-02"]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"volume": "0"}, "invalid_daily_bar:volume_not_positive"),
        ({"close": "inf"}, "invalid_daily_bar:close_not_numeric"),
        ({"open": "nan"}, "invalid_daily_bar:open_not_numeric"),
        ({"high": "9"}, "invalid_daily_bar:high_below_body"),
        ({"low": "11"}, "invalid_daily_bar:low_above_body"),
        ({"close": "-1"}, "invalid_daily_bar:close_not_positive"),
    ],
)
def test_each_requested_bar_keeps_existing_price_validation(
    tmp_path: Path, changes: dict[str, str], reason: str
) -> None:
    _write(tmp_path, [_bar(TARGET, **changes)])

    row = _audit(tmp_path)["rows"][0]

    assert not row["usable"]
    assert row["invalid_evidence"] == [reason]
    assert row["price_validation_status"] == "invalid"


def test_price_validation_scope_and_real_missing_dates_do_not_change(tmp_path: Path) -> None:
    # Old prices outside the requested window were never certified by this audit.
    _write(tmp_path, [_bar("2020-01-02", close=""), _bar(TARGET)])

    ledger = _audit(tmp_path, ["2026-11-02", TARGET])
    row = ledger["rows"][0]

    assert row["usable"]
    assert row["price_validation_scope"] == "requested_trading_dates_only"
    assert row["recovered_dates"] == [TARGET]
    assert row["pending_dates"] == ["2026-11-02"]
    assert row["verified_no_session_dates"] == []
    assert ledger["totals"] == {"recovered": 1, "verified_no_session": 0, "pending": 1}
