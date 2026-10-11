"""T07: reject impossible minute prices and report empty usable responses honestly.

Real collector/storage/manifest paths run only under tmp_path; providers are
injected and sockets are denied. Existing archive bytes must remain untouched.
"""

from __future__ import annotations

import csv
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from quantdesk.intraday_refresh import collector


SYMBOL = "600000"
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=collector.SHANGHAI)


def _bar(stamp: str = "2026-09-28 09:59:00", **changes: Any) -> dict[str, Any]:
    date, time = stamp.split(" ")
    return {
        "symbol": SYMBOL, "datetime": stamp, "date": date, "time": time,
        "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
        "volume": 100.0, "amount": float("nan"),
        "price_basis": "raw", "source": "injected_minute_fixture", **changes,
    }


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("T07 tests must never contact the network")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(
        collector, "refresh_full_market_snapshot",
        lambda *_a, **_k: {"ok": False, "status": "fixture_skipped", "movers": [], "reason_codes": []},
    )


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_prices_must_be_finite(field: str, value: float) -> None:
    valid, reason = collector.validate_minute_bar(_bar(**{field: value}), now=NOW, symbol=SYMBOL)
    assert not valid
    assert reason == f"invalid_minute_bar:{SYMBOL}:{field}_not_positive"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"low": 10.5}, "low_above_body"),
        ({"open": 11.0, "close": 10.0, "low": 10.5}, "low_above_body"),
        ({"high": 10.5}, "high_below_body"),
        ({"open": 11.0, "close": 10.0, "high": 10.5}, "high_below_body"),
    ],
)
def test_extremes_must_cover_both_open_and_close(changes: dict[str, float], code: str) -> None:
    valid, reason = collector.validate_minute_bar(_bar(**changes), now=NOW, symbol=SYMBOL)
    assert not valid
    assert reason == f"invalid_minute_bar:{SYMBOL}:{code}"


@pytest.mark.parametrize(
    "changes",
    [{"low": 10.0, "high": 11.0}, {"open": 11.0, "close": 10.0, "low": 10.0, "high": 11.0},
     {"open": 10.0, "close": 10.0, "low": 10.0, "high": 10.0}],
)
def test_body_boundaries_and_missing_amount_remain_valid(changes: dict[str, float]) -> None:
    assert collector.validate_minute_bar(_bar(**changes), now=NOW, symbol=SYMBOL) == (True, "")


@pytest.mark.parametrize("offset,expected", [(-86400, True), (120, True), (121, False)])
def test_datetime_tolerance_and_late_history_remain_unchanged(offset: int, expected: bool) -> None:
    stamp = (NOW + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
    valid, reason = collector.validate_minute_bar(_bar(stamp), now=NOW, symbol=SYMBOL)
    assert valid is expected
    assert reason == ("" if expected else f"invalid_minute_bar:{SYMBOL}:future_datetime")


def _collect(root: Path, now: datetime, rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = collector.collect_intraday_refresh(
        root, network=True, trading_day=True, now=now,
        fetcher=lambda *_a: (rows, ""),
        scope_builder=lambda *_a, **_k: {
            "symbols": [{"symbol": SYMBOL, "priority": "critical"}], "warnings": [],
        },
    )
    latest = json.loads((root / "results/intraday/intraday_refresh_latest.json").read_text(encoding="utf-8"))
    assert latest == result
    assert result["run_state"] == "completed"
    return result


@pytest.mark.parametrize("hour,minute", [(10, 0), (15, 5)])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("changes", [{"open": 0.0}, {"high": float("inf")}, {"low": 10.5}])
def test_all_rejected_rows_never_claim_fresh_or_write_history(
    tmp_path: Path, hour: int, minute: int, existing: bool, changes: dict[str, float],
) -> None:
    now = NOW.replace(hour=hour, minute=minute)
    stamp = now.replace(minute=max(0, minute - 1)).strftime("%Y-%m-%d %H:%M:%S")
    path = tmp_path / f"data/intraday/1m/{SYMBOL}.csv"
    if existing:
        collector._append_rows(path, [_bar(stamp)], now_iso="2026-09-28T09:59:00")
    before = path.read_bytes() if path.exists() else None

    result = _collect(tmp_path, now, [_bar(stamp, **changes)])

    assert result["fresh_count"] == 0
    assert result["missing_count"] == 1
    assert result["appended_rows"] == 0
    assert result["status"] != "ready"
    receipt = result["provider_status"][0]
    assert receipt["status"] == "validation_failed"
    assert receipt["error_stage"] == "validation"
    assert receipt["reason"].startswith(f"invalid_minute_bar:{SYMBOL}:")
    assert receipt["reason"] in result["reason_codes"]
    assert result["provider_failed_count"] == 0  # Transport succeeded; the data did not.
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("hour,minute", [(10, 0), (15, 5)])
def test_empty_response_without_provider_error_is_missing(tmp_path: Path, hour: int, minute: int) -> None:
    result = _collect(tmp_path, NOW.replace(hour=hour, minute=minute), [])
    assert result["fresh_count"] == 0 and result["missing_count"] == 1
    assert result["status"] != "ready"
    assert result["provider_status"][0]["status"] != "ok"
    assert not (tmp_path / f"data/intraday/1m/{SYMBOL}.csv").exists()


@pytest.mark.parametrize("hour,minute", [(10, 0), (15, 5)])
def test_valid_rows_survive_mixed_response_and_repeat_without_rewrite(
    tmp_path: Path, hour: int, minute: int,
) -> None:
    now = NOW.replace(hour=hour, minute=minute)
    stamp = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    invalid_stamp = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [_bar(stamp), _bar(invalid_stamp, high=10.5)]
    result = _collect(tmp_path, now, rows)
    assert result["fresh_count"] == 1 and result["missing_count"] == 0
    assert result["appended_rows"] == 1
    assert result["provider_status"][0]["status"] == "ok"
    assert f"invalid_minute_bar:{SYMBOL}:high_below_body" in result["reason_codes"]
    path = tmp_path / f"data/intraday/1m/{SYMBOL}.csv"
    before = path.read_bytes()
    with path.open(encoding="utf-8", newline="") as stream:
        archived = list(csv.DictReader(stream))
    assert len(archived) == 1 and archived[0]["datetime"] == stamp
    assert archived[0]["amount"] == "nan"  # Missing amount was not invented.
    repeated = _collect(tmp_path, now, rows)
    assert repeated["fresh_count"] == 1 and repeated["appended_rows"] == 0
    assert path.read_bytes() == before
