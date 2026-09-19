"""A0 history snapshots keep provenance and never mutate a current quote cache."""

from __future__ import annotations

import csv
import importlib
import json
import math
import subprocess
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


def _history_module():
    return importlib.import_module("quantdesk.snapshot_history")


def _now(hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 9, 7, hour, minute, 3, tzinfo=SHANGHAI)


def _forbidden_fetch():
    raise AssertionError("the closed/offline branch must not contact a provider")


def _quote(
    symbol: str = "600000", *, quote_time: str = "10:00:01", quote_date: str = "2026-09-07",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": "sample",
        "price": 10.5,
        "quote_date": quote_date,
        "quote_time": quote_time,
    }


def _calendar(root: Path) -> None:
    path = root / "data/calendar/a_share_trading_calendar.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("date,is_open,source\n2026-09-07,1,fixture\n", encoding="utf-8")


def test_offline_collection_never_calls_provider_or_creates_files(tmp_path: Path) -> None:
    payload = _history_module().collect_market_snapshot(
        tmp_path, now=_now(), fetcher=_forbidden_fetch
    )

    assert payload["status"] == "offline"
    assert payload["network_attempted"] is False
    assert payload["fetched_at"] == ""
    assert payload["written"] is False
    assert "network_not_authorized" in payload["reason_codes"]
    assert list(tmp_path.iterdir()) == []


def test_snapshot_writes_a_dated_immutable_archive_with_fetch_evidence(tmp_path: Path) -> None:
    _calendar(tmp_path)
    latest = tmp_path / "data/cache/spot_snapshot_latest.csv"
    latest.parent.mkdir(parents=True)
    latest.write_text("current quote evidence\n", encoding="utf-8")
    latest_before = latest.read_bytes()
    fetched = _now().replace(second=18)

    payload = _history_module().collect_market_snapshot(
        tmp_path,
        network=True,
        now=_now(),
        clock=lambda: fetched,
        fetcher=lambda: ([_quote()], "fixture.sina"),
        symbols=["600000"],
    )

    target = tmp_path / "data/snapshots/2026-09-07/1000.csv"
    metadata_path = target.with_suffix(".json")
    assert payload["status"] == "written"
    assert payload["written"] is True
    assert payload["snapshot_path"] == "data/snapshots/2026-09-07/1000.csv"
    with target.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["symbol"] == "600000"
    assert rows[0]["price"] == "10.5"
    assert rows[0]["source"] == "fixture.sina"
    assert rows[0]["fetched_at"] == "2026-09-07T10:00:18+08:00"
    assert rows[0]["quote_time"] == "10:00:01"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["fetched_at"] == "2026-09-07T10:00:18+08:00"
    assert metadata["row_count"] == 1
    assert metadata["source"] == "fixture.sina"
    assert metadata["snapshot_path"] == payload["snapshot_path"]
    assert latest.read_bytes() == latest_before

    before = {path: path.read_bytes() for path in (target, metadata_path)}
    repeated = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), fetcher=_forbidden_fetch
    )
    assert repeated["status"] == "already_exists"
    assert repeated["written"] is False
    assert {path: path.read_bytes() for path in before} == before


def test_expected_slot_count_is_explicit_and_summary_marks_uncollected_slots(tmp_path: Path) -> None:
    _calendar(tmp_path)
    history = _history_module()
    assert len(history.expected_snapshot_slots()) == 48
    assert len(history.expected_snapshot_slots(include_close=True)) == 49

    payload = history.collect_market_snapshot(
        tmp_path,
        network=True,
        now=_now(9, 30),
        clock=lambda: _now(9, 30),
        symbols=["600000", "000001"],
        fetcher=lambda: ([_quote("600000", quote_time="09:30:01")], "fixture.sina"),
    )
    assert payload["missing_symbols"] == ["000001"]
    summary = history.snapshot_day_summary(tmp_path, "2026-09-07", symbols=["600000", "000001"])
    assert summary["expected_slot_count"] == 48
    assert payload["status"] == "partial"
    assert summary["completed_slots"] == []
    assert summary["partial_slots"] == ["0930"]
    assert len(summary["missing_slots"]) == 47


def test_non_trading_day_is_closed_without_network_even_when_authorized(tmp_path: Path) -> None:
    payload = _history_module().collect_market_snapshot(
        tmp_path,
        network=True,
        now=datetime(2026, 9, 6, 10, 0, 3, tzinfo=SHANGHAI),
        fetcher=_forbidden_fetch,
    )
    assert payload["status"] == "closed"
    assert payload["network_attempted"] is False
    assert payload["reason_codes"] == ["non_trading_day"]
    assert not (tmp_path / "data").exists()


def test_empty_archive_pair_is_invalid_not_completed(tmp_path: Path) -> None:
    day = tmp_path / "data/snapshots/2026-09-07"
    day.mkdir(parents=True)
    (day / "1000.csv").write_text("", encoding="utf-8")
    (day / "1000.json").write_text("{}", encoding="utf-8")
    result = _history_module().snapshot_day_summary(tmp_path, "2026-09-07")
    assert result["completed_slots"] == []
    assert "1000" in result["invalid_slots"]


def test_orphan_archive_is_reported_and_preserved_without_fetch(tmp_path: Path) -> None:
    _calendar(tmp_path)
    target = tmp_path / "data/snapshots/2026-09-07/1000.csv"
    target.parent.mkdir(parents=True)
    target.write_text("original partial bytes", encoding="utf-8")
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), fetcher=_forbidden_fetch
    )
    assert result["status"] == "archive_incomplete"
    assert result["network_attempted"] is False
    assert target.read_text(encoding="utf-8") == "original partial bytes"


def test_full_market_rows_outside_stale_roster_are_preserved(tmp_path: Path) -> None:
    _calendar(tmp_path)
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now, symbols=["600000"],
        fetcher=lambda: ([_quote(), _quote("000001")], "fixture.sina"),
    )
    assert result["row_count"] == 2
    metadata = json.loads((tmp_path / result["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["coverage_ratio"] == 1.0
    assert metadata["unexpected_symbols"] == ["000001"]


def test_summary_keeps_the_archived_universe_after_roster_changes(tmp_path: Path) -> None:
    _calendar(tmp_path)
    _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now, symbols=["600000"],
        fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    summary = _history_module().snapshot_day_summary(
        tmp_path, "2026-09-07", symbols=["600000", "000001"]
    )
    assert summary["expected_symbol_count"] == 1
    assert summary["completed_slots"] == ["1000"]


def test_tampered_csv_is_not_counted_as_complete(tmp_path: Path) -> None:
    _calendar(tmp_path)
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now, symbols=["600000"],
        fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    target = tmp_path / result["snapshot_path"]
    target.write_text(target.read_text(encoding="utf-8").replace("10.5", "99.5"), encoding="utf-8")
    summary = _history_module().snapshot_day_summary(tmp_path, "2026-09-07")
    assert summary["completed_slots"] == []
    assert "1000" in summary["invalid_slots"]


def test_tampered_coverage_metadata_is_not_counted_as_complete(tmp_path: Path) -> None:
    _calendar(tmp_path)
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now, symbols=["600000"],
        fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    path = tmp_path / result["metadata_path"]
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["coverage_count"] = 9000
    path.write_text(json.dumps(metadata), encoding="utf-8")
    assert "1000" in _history_module().snapshot_day_summary(tmp_path, "2026-09-07")["invalid_slots"]


def test_missing_price_is_preserved_but_not_complete(tmp_path: Path) -> None:
    _calendar(tmp_path)
    quote = _quote()
    quote["price"] = None
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now, symbols=["600000"],
        fetcher=lambda: ([quote], "fixture.sina"),
    )
    assert result["status"] == "partial"
    assert "snapshot_price_missing" in result["warnings"]


def test_write_failure_preserves_orphan_and_returns_failure(tmp_path: Path, monkeypatch) -> None:
    history = _history_module()
    _calendar(tmp_path)
    monkeypatch.setattr(history, "_write_json", lambda *args: (_ for _ in ()).throw(OSError("fixture disk error")))
    result = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now, symbols=["600000"],
        fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    assert result["status"] == "archive_write_failed"
    assert result["written"] is False
    assert (tmp_path / result["snapshot_path"]).is_file()


def test_missing_provider_values_are_empty(tmp_path: Path) -> None:
    rows = _history_module().normalize_snapshot_rows(
        [{"symbol": "600000", "price": math.nan, "quote_time": math.nan}],
        source="fixture", fetched_at=_now(), trade_date="2026-09-07", slot_time="1000",
    )
    assert rows[0]["price"] == ""
    assert rows[0]["quote_time"] == ""


def test_local_legacy_calendar_holiday_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "config/trading_calendar_cn.yaml"
    path.parent.mkdir()
    path.write_text("holidays:\n  - '2026-09-07'\n", encoding="utf-8")
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), fetcher=_forbidden_fetch
    )
    assert result["status"] == "closed"
    assert result["calendar_source"] == "legacy_yaml"


def test_unknown_calendar_and_roster_cannot_claim_complete(tmp_path: Path) -> None:
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    assert result["status"] == "partial"
    assert "calendar_unavailable" in result["warnings"]
    assert "universe_unavailable" in result["warnings"]


def test_default_fetched_at_is_completion_clock_not_injected_start(tmp_path: Path, monkeypatch) -> None:
    history = _history_module()
    _calendar(tmp_path)
    original = history._as_shanghai
    monkeypatch.setattr(history, "_as_shanghai", lambda value: _now().replace(second=40) if value is None else original(value))
    result = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), symbols=["600000"],
        fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    assert result["fetched_at"] == "2026-09-07T10:00:40+08:00"


def test_empty_fallback_provider_continues_to_next_provider(monkeypatch) -> None:
    history = _history_module()
    from quantdesk import data_providers
    monkeypatch.setattr(history, "_sina_direct_fetch", lambda: (_ for _ in ()).throw(RuntimeError("fixture")))
    monkeypatch.setattr(data_providers, "fetch_spot_eastmoney", lambda: [])
    monkeypatch.setattr(data_providers, "fetch_spot_sina", lambda: [_quote()])
    # An explicitly unbounded round keeps the full fallback chain.  Under the
    # default finite budget the AkShare adapters are deliberately skipped
    # instead of started unbounded (DSR-06); see the skip test below.
    rows, source = history.default_network_fetch(budget_seconds=None)
    assert source == "sina.spot_ak"
    assert len(rows) == 1


def test_finite_budget_skips_unbounded_fallback_with_evidence(monkeypatch) -> None:
    history = _history_module()
    from quantdesk import data_providers
    calls: list[str] = []
    monkeypatch.setattr(
        history, "_sina_direct_fetch",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
    )

    def _unbounded():
        calls.append("unbounded")
        return [_quote()]

    monkeypatch.setattr(data_providers, "fetch_spot_eastmoney", _unbounded)
    monkeypatch.setattr(data_providers, "fetch_spot_sina", _unbounded)
    rows, source = history.default_network_fetch(budget_seconds=2.0, clock=lambda: 0.0)
    assert calls == []
    assert source == history.PROVIDER_BUDGET_TOO_SMALL_SOURCE
    assert len(rows) == 0
    skipped = [
        error for error in rows.provider_errors
        if error.get("error_type") == "UnboundedProvider"
    ]
    assert {"eastmoney.spot_ak", "sina.spot_ak"} <= {error["source"] for error in skipped}
    assert "eastmoney.spot_ak:unbounded_source_skipped" in rows.warnings
    assert "sina.spot_ak:unbounded_source_skipped" in rows.warnings


@pytest.mark.parametrize("status", ["partial", "archive_incomplete", "archive_invalid", "archive_write_failed"])
def test_cli_rejects_incomplete_success_labels(status: str) -> None:
    from scripts.collect_market_snapshots import exit_code
    assert exit_code(status) != 0


def test_provider_failure_and_retry_keep_attempt_evidence(tmp_path: Path) -> None:
    history = _history_module()
    _calendar(tmp_path)

    def fail():
        raise ConnectionError("fixture provider unavailable")

    first = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=fail,
    )
    assert first["status"] == "provider_failed"
    attempt_path = tmp_path / first["attempt_path"]
    original = attempt_path.read_bytes()
    attempt = json.loads(original)
    assert attempt["status"] == "provider_failed"
    assert attempt["slot_time"] == "1000"
    assert attempt["reason_codes"] == ["provider_failed:ConnectionError"]
    assert attempt["attempt_started_at"] == _now().isoformat(timespec="seconds")
    assert attempt["attempt_completed_at"] == _now().isoformat(timespec="seconds")
    assert attempt["fetched_at"] == ""
    assert attempt["network_attempted"] is True
    assert "code_version" in attempt
    assert not (tmp_path / first["snapshot_path"]).exists()
    assert not (tmp_path / first["metadata_path"]).exists()
    manifest = json.loads((tmp_path / first["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["completed_slot_count"] == 0
    assert manifest["missing_slot_reasons"]["1000"] == ["provider_failed:ConnectionError"]
    assert manifest["slot_attempts"]["1000"][0]["attempt_path"] == first["attempt_path"]

    second = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=fail,
    )
    assert second["attempt_path"] != first["attempt_path"]
    assert attempt_path.read_bytes() == original
    summary = history.snapshot_day_summary(tmp_path, "2026-09-07")
    assert len(summary["slot_attempts"]["1000"]) == 2
    assert summary["missing_slot_count"] == 48

    recovered = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    assert recovered["status"] == "written"
    assert attempt_path.read_bytes() == original
    summary = history.snapshot_day_summary(tmp_path, "2026-09-07")
    assert summary["completed_slots"] == ["1000"]
    assert "1000" not in summary["missing_slot_reasons"]
    assert len(summary["slot_attempts"]["1000"]) == 2


def test_empty_provider_persists_missing_slot_reason(tmp_path: Path) -> None:
    _calendar(tmp_path)
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=lambda: ([], "fixture.sina"),
    )
    assert result["status"] == "provider_empty"
    attempt = json.loads((tmp_path / result["attempt_path"]).read_text(encoding="utf-8"))
    assert attempt["source"] == "fixture.sina"
    assert attempt["reason_codes"] == ["provider_returned_no_usable_rows"]
    assert attempt["row_count"] == 0
    summary = _history_module().snapshot_day_summary(tmp_path, "2026-09-07")
    assert summary["missing_slot_reasons"]["1000"] == ["provider_returned_no_usable_rows"]
    assert summary["completed_slot_count"] == 0


def test_code_version_is_frozen_before_fetch_from_running_code(tmp_path: Path, monkeypatch) -> None:
    history = _history_module()
    _calendar(tmp_path)
    state = {"head": "a" * 40, "status": " M one.py\0R  renamed.py\0original.py\0?? new.py\0"}
    calls = []

    def git_run(command, **kwargs):
        calls.append((command, kwargs))
        output = state["head"] + "\n" if "rev-parse" in command else state["status"]
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def fetch():
        state.update(head="b" * 40, status="")
        return [_quote()], "fixture.sina"

    monkeypatch.setattr(subprocess, "run", git_run)
    result = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=fetch,
    )
    metadata = json.loads((tmp_path / result["metadata_path"]).read_text(encoding="utf-8"))
    version = metadata["code_version"]
    assert version["git_head"] == "a" * 40
    assert version["dirty_count"] == 3
    assert version["status"] == "recorded"
    assert version["captured_at"] == _now().isoformat(timespec="seconds")
    assert result["code_version"] == version
    assert len(calls) == 2
    assert all(Path(options["cwd"]) == Path(history.__file__).resolve().parents[1] for _, options in calls)
    manifest = json.loads((tmp_path / result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["slot_code_versions"]["1000"] == version
    summary = history.snapshot_day_summary(tmp_path, "2026-09-07")
    assert summary["slot_code_versions"]["1000"] == version
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [FileNotFoundError("fixture no git"), subprocess.TimeoutExpired("git", 5)])
def test_git_unavailable_is_unknown_without_blocking_snapshot(tmp_path: Path, monkeypatch, failure) -> None:
    _calendar(tmp_path)

    def fail_git(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail_git)
    result = _history_module().collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    assert result["status"] == "written"
    metadata = json.loads((tmp_path / result["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["code_version"]["status"] == "unknown"
    assert metadata["code_version"]["git_head"] == "unknown"
    assert metadata["code_version"]["dirty_count"] == "unknown"
    assert metadata["code_version"]["reason_code"].startswith("git_unavailable:")
    assert _history_module().snapshot_day_summary(tmp_path, "2026-09-07")["completed_slots"] == ["1000"]


def test_legacy_snapshot_version_stays_unknown_without_backfill(tmp_path: Path, monkeypatch) -> None:
    history = _history_module()
    _calendar(tmp_path)
    result = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=lambda: ([_quote()], "fixture.sina"),
    )
    metadata_path = tmp_path / result["metadata_path"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("code_version", None)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    before = metadata_path.read_bytes()

    def forbidden_git(*args, **kwargs):
        raise AssertionError("reading historical evidence must not stamp current code")

    monkeypatch.setattr(subprocess, "run", forbidden_git)
    summary = history.snapshot_day_summary(tmp_path, "2026-09-07")
    assert summary["slot_code_versions"]["1000"]["git_head"] == "unknown"
    assert summary["slot_code_versions"]["1000"]["captured_at"] == ""
    repeated = history.collect_market_snapshot(tmp_path, network=True, now=_now(), fetcher=_forbidden_fetch)
    assert repeated["status"] == "already_exists"
    assert repeated["code_version"]["git_head"] == "unknown"
    assert metadata_path.read_bytes() == before


def test_failure_evidence_write_error_is_reported(tmp_path: Path, monkeypatch) -> None:
    history = _history_module()
    _calendar(tmp_path)

    def fail():
        raise ConnectionError("fixture provider unavailable")

    monkeypatch.setattr(history, "_write_json", lambda *args: (_ for _ in ()).throw(OSError("fixture disk error")))
    result = history.collect_market_snapshot(
        tmp_path, network=True, now=_now(), clock=_now,
        symbols=["600000"], fetcher=fail,
    )
    assert result["status"] == "archive_write_failed"
    assert result["written"] is False
    assert "provider_failed:ConnectionError" in result["reason_codes"]
    assert any(reason.startswith("attempt_evidence_write_failed:") for reason in result["reason_codes"])


def test_busy_collector_lock_does_not_fetch_or_write_attempts(tmp_path: Path) -> None:
    history = _history_module()
    _calendar(tmp_path)
    with history.interprocess_file_lock(tmp_path / history.LOCK_RELATIVE_PATH, timeout_seconds=0):
        result = history.collect_market_snapshot(
            tmp_path, network=True, now=_now(), clock=_now,
            symbols=["600000"], fetcher=_forbidden_fetch,
        )
    assert result["status"] == "already_running"
    assert result["network_attempted"] is False
    assert not (tmp_path / "data/snapshots/2026-09-07").exists()
