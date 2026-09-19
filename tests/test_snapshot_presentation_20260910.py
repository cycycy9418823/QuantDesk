"""A0 presentation and provider-budget regressions from the 2026-09-10 run.

The live day showed a single ``missing`` total that folded together slots that
had genuinely failed, slots still in the future, and slots whose whole roster
was present but priced at zero.  It also lost the reason a provider chain
failed.  These tests pin the separated rates and the bounded provider chain.

No test contacts the network: providers and clocks are injected.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture(autouse=True)
def _legacy_hq_unavailable(monkeypatch):
    """These existing cases exercise the original providers with HQ unavailable."""
    from quantdesk import snapshot_history

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture_hq_unavailable")

    monkeypatch.setattr(snapshot_history, "_sina_hq_fetch", unavailable)

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY = "2026-09-07"


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


# ---------------------------------------------------------------------------
# Separated day rates
# ---------------------------------------------------------------------------


def test_day_summary_separates_elapsed_pending_and_three_rates(tmp_path: Path) -> None:
    """Collected / roster-coverage / valid-quote must be three distinct numbers."""

    _calendar(tmp_path)
    result = _archive_one_slot(tmp_path, ["600000", "000001"], [_quote("600000", quote_time="09:30:01")], 9, 30)
    assert result["status"] == "partial"

    summary = _history().snapshot_day_summary(
        tmp_path, DAY, symbols=["600000", "000001"], as_of=_at(10, 0),
    )

    assert summary["expected_slot_count"] == 48
    # 09:30 .. 10:00 inclusive are already due; everything after is pending.
    assert summary["elapsed_slot_count"] == 7
    assert summary["pending_slot_count"] == 41
    assert summary["elapsed_slots"][0] == "0930"
    assert summary["elapsed_slots"][-1] == "1000"
    assert summary["pending_slots"][0] == "1005"

    # Only 09:30 was archived, and only for one of the two roster symbols.
    assert summary["collected_slot_count"] == 1
    assert summary["collected_slot_ratio"] == pytest.approx(1 / 7)
    assert summary["roster_coverage_ratio"] == pytest.approx(0.5)
    assert summary["valid_quote_ratio"] == pytest.approx(1.0)
    assert summary["archived_slot_count"] == 1

    # Field compatibility: the old total and the old ratio are unchanged.
    assert len(summary["missing_slots"]) == 47
    assert summary["missing_slot_count"] == 47
    assert summary["complete_ratio"] == pytest.approx(0.0)

    # The new split is what tells failure from "not yet due".
    assert summary["missing_elapsed_slot_count"] == 6
    assert summary["missing_pending_slot_count"] == 41
    assert summary["missing_slots_include_pending"] is True
    assert summary["as_of"] == _at(10, 0).isoformat(timespec="seconds")


def test_pending_slots_are_not_counted_as_missing_elapsed(tmp_path: Path) -> None:
    _calendar(tmp_path)
    summary = _history().snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(9, 30))
    assert summary["elapsed_slot_count"] == 1
    assert summary["missing_elapsed_slots"] == ["0930"]
    assert "1005" in summary["pending_slots"]
    assert summary["missing_slot_reasons"]["1005"] == ["not_collected"]
    assert summary["missing_elapsed_slot_count"] == 1
    assert summary["pending_slot_count"] == 47


def test_valid_quote_ratio_is_not_roster_coverage(tmp_path: Path) -> None:
    """A zero price fills the roster but is not a usable quote."""

    _calendar(tmp_path)
    result = _archive_one_slot(tmp_path, ["600000"], [_quote("600000", price=0, quote_time="09:30:01")], 9, 30)
    assert "snapshot_price_missing" in result["warnings"]

    summary = _history().snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(9, 30))
    assert summary["roster_coverage_ratio"] == pytest.approx(1.0)
    assert summary["valid_quote_ratio"] == pytest.approx(0.0)
    assert summary["valid_quote_count"] == 0
    assert summary["quoted_row_count"] == 1
    quality = summary["slot_quality"]["0930"]
    assert quality["invalid_quote_count"] == 1
    assert quality["roster_coverage_count"] == 1


def test_historical_day_reports_every_slot_as_elapsed(tmp_path: Path) -> None:
    _calendar(tmp_path)
    summary = _history().snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(23, 59))
    assert summary["pending_slot_count"] == 0
    assert summary["elapsed_slot_count"] == 48
    assert summary["missing_elapsed_slot_count"] == len(summary["missing_slots"])


# ---------------------------------------------------------------------------
# Bounded provider chain with stage / source / duration evidence
# ---------------------------------------------------------------------------


def _step_clock(values: list[float]) -> object:
    """A monotonic clock that returns the given values, repeating the last."""

    state = {"index": 0}

    def clock() -> float:
        index = state["index"]
        state["index"] = index + 1
        return values[min(index, len(values) - 1)]

    return clock


def test_provider_chain_records_stage_source_and_duration(monkeypatch) -> None:
    history = _history()
    from requests.exceptions import ConnectionError as RequestsConnectionError, ReadTimeout

    def boom() -> object:
        raise ReadTimeout("read timed out")

    def boom_connection() -> object:
        raise RequestsConnectionError("connection aborted")

    monkeypatch.setattr(history, "_sina_direct_fetch", boom)
    monkeypatch.setattr(history, "_akshare_provider", lambda name: boom_connection())

    # The unbounded AkShare adapters are never started under a finite budget
    # (DSR-06); the chain reports the refused/skipped chain with evidence.
    rows, source = history.default_network_fetch(
        symbols=["600000"], clock=_step_clock([0.0, 0.25]),
    )
    assert source == history.PROVIDER_BUDGET_TOO_SMALL_SOURCE
    assert rows == []
    evidence = {item["source"]: item for item in rows.provider_errors}
    assert set(evidence) == {"sina.spot_direct", "sina.hq_batch", "eastmoney.spot_ak", "sina.spot_ak"}
    assert evidence["sina.spot_direct"]["stage"] == "timeout"
    assert evidence["sina.spot_direct"]["error_type"] == "ReadTimeout"
    assert evidence["sina.spot_direct"]["outcome"] == "failed"
    # The unbounded members were skipped, never started.
    for name in ("eastmoney.spot_ak", "sina.spot_ak"):
        assert evidence[name]["outcome"] == "budget_exhausted"
        assert evidence[name]["error_type"] == "UnboundedProvider"
    for item in rows.provider_errors:
        assert isinstance(item["duration_ms"], int)
        assert item["duration_ms"] >= 0
        assert item["message"]
    assert "eastmoney.spot_ak:unbounded_source_skipped" in rows.warnings
    assert "sina.spot_direct:ReadTimeout" in rows.warnings


def test_provider_chain_marks_unattempted_sources_when_budget_is_spent(monkeypatch) -> None:
    history = _history()
    from requests.exceptions import ReadTimeout

    monkeypatch.setattr(history, "_sina_direct_fetch", lambda: (_ for _ in ()).throw(ReadTimeout("slow")))
    monkeypatch.setattr(
        history, "_akshare_provider",
        lambda name: (_ for _ in ()).throw(AssertionError("budget must stop the chain")),
    )

    with pytest.raises(history.SnapshotProviderError) as captured:
        history.default_network_fetch(
            symbols=["600000"], clock=_step_clock([0.0, 1.0, 30.0]), budget_seconds=20.0,
        )

    error = captured.value
    budget_items = [item for item in error.provider_errors if item["outcome"] == "budget_exhausted"]
    assert {item["source"] for item in budget_items} == {"sina.hq_batch", "eastmoney.spot_ak", "sina.spot_ak"}
    assert "budget_exhausted" in str(error)
    # The real failure is kept, not replaced by the budget message.
    assert any(item["error_type"] == "ReadTimeout" for item in error.provider_errors)


def test_chain_forwards_its_leftover_window_and_skips_what_cannot_fit(monkeypatch) -> None:
    """The shared window is handed to budget-aware sources, never a fixed one."""

    history = _history()
    seen: list[float] = []

    def sina(*, budget_seconds: float):
        seen.append(budget_seconds)
        return [], history.SINA_DIRECT_BUDGET_REFUSED_SOURCE

    monkeypatch.setattr(history, "_sina_direct_fetch", sina)
    monkeypatch.setattr(
        history, "_akshare_provider",
        lambda name: (_ for _ in ()).throw(AssertionError("started without its minimum budget")),
    )

    rows, source = history.default_network_fetch(
        symbols=["600000"], clock=lambda: 0.0, budget_seconds=5.0,
    )

    assert seen == [5.0]
    assert source == history.PROVIDER_BUDGET_TOO_SMALL_SOURCE
    assert rows == []
    evidence = {item["source"]: item for item in rows.provider_errors}
    assert {name for name, item in evidence.items() if item["outcome"] == "budget_exhausted"} == {
        "sina.spot_direct", "eastmoney.spot_ak", "sina.spot_ak",
    }
    assert "budget_below_minimum" in evidence["sina.spot_direct"]["message"]
    assert any("snapshot_provider_budget_too_small" in warning for warning in rows.warnings)


def test_partial_provider_response_is_kept_with_coverage_warning(monkeypatch) -> None:
    history = _history()
    from requests.exceptions import ConnectionError as RequestsConnectionError

    monkeypatch.setattr(history, "_sina_direct_fetch", lambda: ([{"code": "600000"}], "sina.spot_direct"))
    monkeypatch.setattr(
        history, "_akshare_provider",
        lambda name: (_ for _ in ()).throw(RequestsConnectionError("down")),
    )
    result = history.default_network_fetch(symbols=["600000", "920000"])
    rows, source = result
    assert source == "sina.spot_direct"
    assert len(rows) == 1
    assert rows.source == "sina.spot_direct"
    assert "snapshot_provider_coverage_incomplete" in rows.warnings
    assert "sina.spot_direct:incomplete:1/2" in rows.warnings


def test_sina_direct_pagination_stops_on_its_own_budget(monkeypatch) -> None:
    history = _history()
    import requests

    state = {"now": 0.0}
    pages: list[int] = []
    timeouts: list[float] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, object]]:
            return [{"code": f"{600000 + index}", "trade": 10} for index in range(100)]

    def fake_get(url, params=None, **kwargs):  # noqa: ANN001, ANN003
        pages.append(int((params or {}).get("page") or 0))
        timeouts.append(float(kwargs["timeout"]))
        # Every request costs six seconds of the twelve-second window.
        state["now"] += 6.0
        return _Response()

    monkeypatch.setattr(requests, "get", fake_get)

    with pytest.raises(TimeoutError) as captured:
        history._sina_direct_fetch(clock=lambda: state["now"], budget_seconds=12.0)

    assert "page_budget_exhausted" in str(captured.value)
    assert pages == [1, 2]
    assert len(pages) < history.SINA_DIRECT_PAGE_LIMIT
    # The allowance shrinks with the remaining budget instead of staying at the
    # fixed 8-second request timeout the live run kept using.
    assert timeouts == [8.0, 6.0]
    assert max(timeouts) <= history.SINA_DIRECT_REQUEST_TIMEOUT_SECONDS


def test_sina_direct_refuses_zero_budget(monkeypatch) -> None:
    """An empty window is refused; positive short budgets are used by the provider."""

    history = _history()
    import requests

    monkeypatch.setattr(
        requests, "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no request may be sent")),
    )

    rows, source = history._sina_direct_fetch(clock=lambda: 0.0, budget_seconds=0.0)

    assert rows == []
    assert source == history.SINA_DIRECT_BUDGET_REFUSED_SOURCE


# ---------------------------------------------------------------------------
# Failure evidence reaches the immutable attempt archive
# ---------------------------------------------------------------------------


def test_failure_detail_is_persisted_with_the_attempt(tmp_path: Path, monkeypatch) -> None:
    _calendar(tmp_path)
    history = _history()
    from requests.exceptions import ReadTimeout

    monkeypatch.setattr(history, "_sina_direct_fetch", lambda: (_ for _ in ()).throw(ReadTimeout("t")))
    monkeypatch.setattr(
        history, "_akshare_provider",
        lambda name: (_ for _ in ()).throw(ConnectionError("c")),
    )

    # Under a finite budget the unbounded AkShare adapters are skipped (DSR-06):
    # the attempt is still failed and the evidence still persisted.
    result = history.collect_market_snapshot(
        tmp_path, network=True, now=_at(10), clock=lambda: _at(10), symbols=["600000"],
    )

    assert result["status"] == "provider_empty"
    assert "provider_returned_no_usable_rows" in result["reason_codes"]
    assert result["attempt_written"] is True

    attempt = json.loads((tmp_path / result["attempt_path"]).read_text(encoding="utf-8"))
    detail = attempt["failure_detail"]
    assert detail["stage"] == "provider_fetch"
    assert "sina.spot_direct" in detail["sources_failed"]
    assert any(item["error_type"] == "ReadTimeout" for item in detail["provider_errors"])
    assert set(detail["sources_not_attempted"]) == {"eastmoney.spot_ak", "sina.spot_ak"}
    assert {item["source"] for item in detail["provider_errors"]} == {
        "sina.spot_direct", "sina.hq_batch", "eastmoney.spot_ak", "sina.spot_ak",
    }
    assert attempt["stage_timings"][0]["stage"] == "provider_fetch"
    # The chain returned evidence instead of raising, so the fetch itself was ok.
    assert attempt["stage_timings"][0]["outcome"] == "ok"
    # The day manifest still routes the failure through the old reason channel.
    summary = history.snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(10))
    assert summary["missing_slot_reasons"]["1000"] == ["provider_returned_no_usable_rows"]


def test_error_text_is_sanitized_before_persisting() -> None:
    history = _history()
    message = history._sanitize_error_text("GET /x?sendkey=SCT123456&to=1&key=abcdef failed")
    assert "SCT123456" not in message
    assert "abcdef" not in message
    assert "sendkey=<redacted>" in message
    assert "key=<redacted>" in message


def test_provider_chain_success_keeps_the_two_tuple_contract(monkeypatch) -> None:
    """Callers unpack ``(rows, source)``; that contract must not drift."""

    history = _history()
    monkeypatch.setattr(history, "_sina_direct_fetch", lambda: ([_quote("600000")], "sina.spot_direct"))
    monkeypatch.setattr(
        history, "_akshare_provider",
        lambda name: (_ for _ in ()).throw(AssertionError("must not reach the fallback")),
    )
    rows, source = history.default_network_fetch()
    assert source == "sina.spot_direct"
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Provider quote-time contract (DSR-04) and late slots (DSR-05)
# ---------------------------------------------------------------------------


def test_previous_day_quote_can_never_be_read_as_this_slot(tmp_path: Path) -> None:
    """A quote dated days earlier is kept as evidence and never read as this slot."""

    _calendar(tmp_path)
    history = _history()
    result = _archive_one_slot(
        tmp_path,
        ["600000"],
        [_quote("600000", quote_date="2026-09-04", quote_time="15:00:00")],
        10,
        0,
    )

    assert result["status"] == "partial"
    assert "snapshot_quote_time_stale:1" in result["warnings"]
    assert "provider_quotes_not_usable_for_slot" in result["reason_codes"]
    attempt = json.loads((tmp_path / result["attempt_path"]).read_text(encoding="utf-8"))
    assert attempt["status"] == "partial_archive"
    assert attempt["quote_time_states"]["stale"] == 1

    summary = history.snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(10, 0))
    assert summary["completed_slots"] == []
    assert summary["collected_slots"] == []
    assert summary["late_slots"] == ["1000"]
    assert summary["valid_quote_ratio"] == pytest.approx(0.0)
    assert summary["slot_quality"]["1000"]["quote_time_states"]["stale"] == 1


def test_future_provider_quote_can_never_be_read_as_this_slot(tmp_path: Path) -> None:
    """A 10:00 slot may not be filled by a 14:55 quote, nor reported complete."""

    _calendar(tmp_path)
    history = _history()
    result = _archive_one_slot(
        tmp_path,
        ["600000"],
        [_quote("600000", quote_time="14:55:00")],
        10,
        0,
    )

    assert result["status"] == "partial"
    assert "snapshot_quote_time_future:1" in result["warnings"]

    summary = history.snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(10, 0))
    assert summary["completed_slots"] == []
    assert summary["collected_slots"] == []
    assert summary["late_slots"] == ["1000"]
    assert summary["valid_quote_ratio"] == pytest.approx(0.0)
    assert summary["slot_quality"]["1000"]["quote_time_states"]["future"] == 1


def test_clock_skew_allowance_is_exactly_five_seconds(tmp_path: Path) -> None:
    """+5s is skew and stays usable; +6s is a quote this capture could not have seen."""

    history = _history()
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    for root in (inside, outside):
        _calendar(root)

    _archive_one_slot(inside, ["600000"], [_quote("600000", quote_time="10:00:08")], 10, 0)
    _archive_one_slot(outside, ["600000"], [_quote("600000", quote_time="10:00:09")], 10, 0)

    within = history.snapshot_day_summary(inside, DAY, symbols=["600000"], as_of=_at(10, 0))
    beyond = history.snapshot_day_summary(outside, DAY, symbols=["600000"], as_of=_at(10, 0))
    assert within["slot_quality"]["1000"]["quote_time_states"]["usable"] == 1
    assert within["valid_quote_ratio"] == pytest.approx(1.0)
    assert beyond["slot_quality"]["1000"]["quote_time_states"]["future"] == 1
    assert beyond["valid_quote_ratio"] == pytest.approx(0.0)


def test_partial_archive_keeps_out_of_slot_rows_visible(tmp_path: Path) -> None:
    """One usable quote is enough to collect the slot - and the bad row is still flagged."""

    _calendar(tmp_path)
    history = _history()
    result = _archive_one_slot(
        tmp_path,
        ["600000", "000001"],
        [
            _quote("600000", quote_time="10:00:01"),
            _quote("000001", quote_date="2026-09-04", quote_time="15:00:00"),
        ],
        10,
        0,
    )

    assert result["status"] == "partial"
    assert "snapshot_quote_time_stale:1" in result["warnings"]
    assert "provider_quotes_not_usable_for_slot" not in result["reason_codes"]

    summary = history.snapshot_day_summary(tmp_path, DAY, symbols=["600000", "000001"], as_of=_at(10, 0))
    quality = summary["slot_quality"]["1000"]
    assert quality["quote_time_states"] == {
        "usable": 1, "stale": 1, "future": 0, "missing": 0, "unparseable": 0,
    }
    assert quality["valid_quote_count"] == 1
    assert summary["valid_quote_ratio"] == pytest.approx(0.5)
    assert summary["completed_slots"] == []


def test_a_local_clock_quote_without_a_date_is_accepted_when_observable(tmp_path: Path) -> None:
    """Sina sends only a clock time; it is usable when it could have been observed."""

    _calendar(tmp_path)
    history = _history()
    result = _archive_one_slot(
        tmp_path, ["600000"], [_quote("600000", quote_date="", quote_time="10:00:01")], 10, 0,
    )

    assert result["status"] in {"written", "partial"}
    summary = history.snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(10, 0))
    assert summary["slot_quality"]["1000"]["quote_time_states"]["usable"] == 1
    assert summary["valid_quote_ratio"] == pytest.approx(1.0)


def test_quote_matching_its_slot_stays_a_valid_quote(tmp_path: Path) -> None:
    _calendar(tmp_path)
    result = _archive_one_slot(tmp_path, ["600000"], [_quote("600000")], 10, 0)

    assert result["status"] in {"written", "partial"}
    summary = _history().snapshot_day_summary(
        tmp_path, DAY, symbols=["600000"], as_of=_at(10, 0),
    )
    assert summary["valid_quote_ratio"] == pytest.approx(1.0)
    assert summary["slot_quality"]["1000"]["quote_time_states"]["usable"] == 1


def test_late_capture_is_evidence_but_never_a_collected_slot(tmp_path: Path) -> None:
    """A fetch that finishes after its slot must not count as that slot's collection."""

    _calendar(tmp_path)
    history = _history()
    result = history.collect_market_snapshot(
        tmp_path,
        network=True,
        now=_at(10, 0),
        clock=lambda: _at(10, 5).replace(second=2),
        symbols=["600000"],
        fetcher=lambda: ([_quote("600000", quote_time="10:05:02")], "fixture.sina"),
    )

    # The capture reports what really happened and keeps its own evidence...
    assert result["status"] == "partial"
    assert "fetch_completed_outside_requested_slot" in result["warnings"]
    assert (tmp_path / result["snapshot_path"]).exists()

    # ...and the day rates never treat it as a collection of the 10:00 slot.
    summary = history.snapshot_day_summary(tmp_path, DAY, symbols=["600000"], as_of=_at(10, 10))
    assert summary["completed_slots"] == []
    assert summary["collected_slots"] == []
    assert summary["collected_slot_count"] == 0
    assert summary["late_slots"] == ["1000"]
    assert summary["late_slot_count"] == 1
    assert summary["slot_quality"]["1000"]["fetch_outside_slot"] is True
