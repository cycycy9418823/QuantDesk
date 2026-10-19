"""Bounded roster retries and evidence-led discovery without cache relabelling."""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quantdesk import snapshot_history
from quantdesk.process_deadline import ProcessDeadlineResult
from quantdesk.universe_refresh import discover_daily_universe_symbols
from scripts import run_daily_data_refresh as daily
from scripts import update_a_share_universe as updater
from tests.test_daily_data_refresh import FakeLease


DAY = "2026-10-12"
NOW = datetime(2026, 10, 12, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def receipt(root, symbols, **overrides):
    codes = sorted(symbols)
    source = {
        "source": "baostock.query_stock_basic", "status": "partial",
        "observed_at": DAY + "T15:31:00+08:00", "symbols": codes,
        "row_count": len(codes),
        "symbols_sha256": hashlib.sha256(json.dumps(codes, separators=(",", ":")).encode()).hexdigest(),
        **overrides,
    }
    path = root / "data/cache/a_share_universe_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "universe_update.v2", "source_snapshots": [source]}), encoding="utf-8")
    return path


def test_each_stalled_roster_source_leaves_time_for_the_next():
    clock = [0.0]
    calls = []

    def run(worker, tasks, *, context, max_workers, timeout_seconds):
        calls.append((tasks[0], timeout_seconds))
        clock[0] += timeout_seconds
        return ProcessDeadlineResult({}, {}, [0], True, [])

    rows, source, errors = updater.fetch_universe_from_akshare(
        budget_seconds=35, providers=updater.UNIVERSE_SOURCE_PROVIDERS,
        runner=run, clock=lambda: clock[0],
    )
    assert not rows and not source and len(errors) == 4
    assert [name for name, _ in calls] == list(updater.UNIVERSE_SOURCE_PROVIDERS)
    assert all(0 < value <= 12 for _, value in calls) and clock[0] <= 35
    assert all(attempt["status"] == "failed" for attempt in rows.attempts)


def test_two_live_sources_keep_their_distinct_membership_evidence(monkeypatch):
    monkeypatch.setattr(updater, "now_text", lambda: DAY + "T15:31:00+08:00")
    observed = {"stock_info_a_code_name": ["600000"], "baostock.query_stock_basic": ["600000", "302132"]}

    def run(worker, tasks, **kwargs):
        source = tasks[0]
        rows = [{"symbol": symbol, "name": "fixture", "source": source} for symbol in observed[source]]
        return ProcessDeadlineResult({0: rows}, {}, [], False, [])

    rows, source, errors = updater.fetch_universe_from_akshare(
        providers=list(observed), runner=run, clock=lambda: 0, minimum_rows=1,
    )
    assert not errors and source == "baostock.query_stock_basic" and len(rows) == 2
    assert len(rows.source_snapshots) == 2
    assert rows.source_snapshots[0]["symbols"] == ["600000"]
    assert rows.source_snapshots[1]["symbols"] == ["302132", "600000"]


def test_roster_diagnostics_redact_proxy_passwords_and_tokens():
    def run(worker, tasks, **kwargs):
        return ProcessDeadlineResult({}, {0: "ProxyError:https://owner:privatepass@fixture?token=privatekey&symbol=600000"}, [], False, [])

    rows, _, errors = updater.fetch_universe_from_akshare(
        providers=["stock_zh_a_spot"], runner=run, clock=lambda: 0,
    )
    evidence = json.dumps({"errors": errors, "attempts": rows.attempts})
    assert "privatepass" not in evidence and "privatekey" not in evidence and "owner:" not in evidence
    assert "ProxyError" in evidence and "symbol=600000" in evidence


def test_failed_roster_refresh_preserves_exact_cache_bytes_and_old_date(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(updater, "load_config", lambda *_: {})
    monkeypatch.setattr(updater, "fetch_universe_from_akshare",
                        lambda **kw: (updater.UniverseSourceRows(), "", {"stock_zh_a_spot": "timeout"}))
    monkeypatch.setattr(updater, "load_local_fallback_universe", lambda *_: pytest.fail("cannot publish local cache as fresh"))
    output = tmp_path / "data/universe/a_share_symbols.csv"
    output.parent.mkdir(parents=True)
    original = b"symbol,name,source,updated_at\r\n600000,fixture,stock_info_a_code_name,2026-09-29T15:30:00\r\n"
    output.write_bytes(original)
    args = updater.parse_args(["--output", str(output), "--status-path", str(tmp_path / "status.json")])
    result = updater.run_update_universe(args)
    assert not result["ok"] and result["published"] is False
    assert result["status"] == "failed_preserved_existing"
    assert result["previous_roster_sha256"] == result["roster_sha256"]
    assert output.read_bytes() == original
    assert result["fetched_row_count"] == 0 and result["row_count"] == 1


def fresh_roster_fixture(tmp_path, monkeypatch, configured_path=None):
    config = {"data": {"a_share_universe_path": configured_path}} if configured_path else {}
    monkeypatch.setattr(updater, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(updater, "DEFAULT_UNIVERSE_PATH", tmp_path / "data/universe/a_share_symbols.csv")
    monkeypatch.setattr(updater, "DEFAULT_STATUS_PATH", tmp_path / "data/cache/a_share_universe_status.json")
    monkeypatch.setattr(updater, "load_config", lambda *_: config)
    monkeypatch.setattr(updater, "now_text", lambda: NOW.isoformat())
    fresh = updater.UniverseSourceRows([
        {"symbol": str(600000 + index), "name": "fixture", "source": "stock_info_a_code_name"}
        for index in range(5000)
    ], complete=True)
    monkeypatch.setattr(updater, "fetch_universe_from_akshare",
                        lambda **kw: (fresh, "stock_info_a_code_name", {}))
    output, status = updater.resolve_output_paths(config)
    assert tmp_path in output.parents and tmp_path in status.parents
    output.parent.mkdir(parents=True)
    original = b"symbol,name,source,updated_at\r\n600000,fixture,stock_info_a_code_name,2026-09-29T15:30:00\r\n"
    output.write_bytes(original)
    return output, status, original


@pytest.mark.parametrize("configured_path", [None, "fixtures/configured_roster.csv"])
@pytest.mark.parametrize("explicit_output", [False, True])
def test_limit_never_replaces_default_or_configured_roster(tmp_path, monkeypatch, configured_path, explicit_output):
    output, _, original = fresh_roster_fixture(tmp_path, monkeypatch, configured_path)
    argv = ["--limit", "10"]
    if explicit_output:
        argv += ["--output", str(output)]
    result = updater.run_update_universe(updater.parse_args(argv))
    assert result["fetched_row_count"] == 5000 and result["row_count"] == 1
    assert result["ok"] is False and result["published"] is False
    assert result["full_roster_published"] is False
    assert result["status"] == "limit_refused_preserved_existing"
    assert output.read_bytes() == original
    assert result["previous_roster_sha256"] == result["roster_sha256"]


def test_limit_writes_only_an_explicit_separate_preview_and_receipt(tmp_path, monkeypatch):
    output, default_status, original = fresh_roster_fixture(tmp_path, monkeypatch)
    preview = tmp_path / "preview.csv"
    result = updater.run_update_universe(updater.parse_args(["--limit", "10", "--output", str(preview)]))
    assert result["ok"] is True and result["published"] is True
    assert result["preview"] is True and result["full_roster_published"] is False
    assert result["status"] == "preview_written" and result["row_count"] == 10
    assert len(updater.read_csv_frame(preview)) == 10 and output.read_bytes() == original
    assert result["status_path"] == str(tmp_path / "preview.csv.status.json")
    assert not default_status.exists()


def test_limit_cannot_use_builtin_default_as_preview_under_custom_config(tmp_path, monkeypatch):
    configured, _, original = fresh_roster_fixture(tmp_path, monkeypatch, "fixtures/configured_roster.csv")
    default_output = updater.DEFAULT_UNIVERSE_PATH
    default_output.parent.mkdir(parents=True)
    default_output.write_bytes(original)
    result = updater.run_update_universe(updater.parse_args(["--limit", "10", "--output", str(default_output)]))
    assert result["fetched_row_count"] == 5000 and result["row_count"] == 1
    assert result["ok"] is False and result["published"] is False
    assert result["status"] == "limit_refused_preserved_existing"
    assert default_output.read_bytes() == configured.read_bytes() == original


def test_limit_dry_run_writes_neither_roster_nor_receipt(tmp_path, monkeypatch):
    output, status, original = fresh_roster_fixture(tmp_path, monkeypatch)
    result = updater.run_update_universe(updater.parse_args(["--limit", "10", "--dry-run"]))
    assert result["ok"] is True and result["published"] is False
    assert result["status"] == "preview_dry_run" and result["row_count"] == 10
    assert output.read_bytes() == original and not status.exists()


def test_complete_unlimited_refresh_still_publishes_the_full_roster(tmp_path, monkeypatch):
    output, _, _ = fresh_roster_fixture(tmp_path, monkeypatch)
    result = updater.run_update_universe(updater.parse_args([]))
    assert result["ok"] is True and result["published"] is True
    assert result["full_roster_published"] is True and result["preview"] is False
    assert result["status"] == "refreshed" and result["row_count"] == 5000
    assert len(updater.read_csv_frame(output)) == 5000


@pytest.mark.parametrize("fetched_count,accepted", [(5291, False), (5292, True)])
def test_roster_ninety_five_percent_boundary_preserves_old_bytes_until_sufficient(tmp_path, monkeypatch, fetched_count, accepted):
    output, _, _ = fresh_roster_fixture(tmp_path, monkeypatch)
    original = ("symbol,name,source,updated_at\n" + "".join(
        f"{600000 + index},fixture,stock_info_a_code_name,2026-09-29\n"
        for index in range(5570)
    )).encode()
    output.write_bytes(original)
    minimums = []

    def fetch(**kwargs):
        minimums.append(kwargs["minimum_rows"])
        return updater.UniverseSourceRows([
            {"symbol": str(600000 + index), "name": "fixture", "source": "stock_info_a_code_name"}
            for index in range(fetched_count)
        ], complete=True), "stock_info_a_code_name", {}

    monkeypatch.setattr(updater, "fetch_universe_from_akshare", fetch)
    result = updater.run_update_universe(updater.parse_args([]))
    assert minimums == [5292] and result["minimum_complete_rows"] == 5292
    assert result["ok"] is accepted and result["published"] is accepted
    assert result["full_roster_published"] is accepted
    assert result["fetched_row_count"] == fetched_count
    if accepted:
        assert result["status"] == "refreshed" and result["row_count"] == 5292
        assert len(updater.read_csv_frame(output)) == 5292
    else:
        assert result["status"] == "failed_preserved_existing" and result["row_count"] == 5570
        assert output.read_bytes() == original
        assert result["previous_roster_sha256"] == result["roster_sha256"] == hashlib.sha256(original).hexdigest()
        assert set(updater.read_csv_frame(output)["updated_at"]) == {"2026-09-29"}


def test_atomic_roster_publish_failure_cannot_truncate_original(tmp_path, monkeypatch):
    output = tmp_path / "roster.csv"
    output.write_bytes(b"original\r\n")
    monkeypatch.setattr(updater.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("fixture replace failure")))
    with pytest.raises(OSError, match="fixture replace failure"):
        updater.write_universe_csv(output, [{"symbol": "600000", "name": "fixture", "source": "stock_zh_a_spot"}])
    assert output.read_bytes() == b"original\r\n"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("override", [
    {"observed_at": "2026-09-29T15:31:00+08:00"},
    {"observed_at": DAY + "T15:31:00"},
    {"observed_at": DAY + "T17:31:00+08:00"},
    {"source": "local_daily_cache"}, {"symbols_sha256": "bad"}, {"status": "cached"},
])
def test_unverified_or_stale_discovery_never_expands_requests(tmp_path, override):
    receipt(tmp_path, ["689009"], **override)
    result = discover_daily_universe_symbols(tmp_path, DAY, now=NOW)
    assert result["symbols"] == [] and result["warnings"]
    assert result["network_attempted"] is False


@pytest.mark.parametrize("omitted", ["689009", "302132", "920998"])
def test_code_missing_from_both_roster_and_list_gets_dated_requery(tmp_path, monkeypatch, omitted):
    universe = tmp_path / "data/universe/a_share_symbols.csv"
    universe.parent.mkdir(parents=True)
    universe.write_text("symbol\n600000\n", encoding="utf-8")
    receipt(tmp_path, ["600000", omitted])
    for name, relative in (
        ("PROJECT_ROOT", "."), ("DAILY_DIR", "data/daily"), ("META_DIR", "data/metadata"),
        ("VAL_DIR", "data/metadata/valuation_snapshots"), ("UNIVERSE_FILE", "data/universe/a_share_symbols.csv"),
    ):
        monkeypatch.setattr(daily, name, tmp_path / relative)
    monkeypatch.setattr(daily, "_complete_live_session", lambda target: True)
    monkeypatch.setattr(daily, "_now_iso", lambda: NOW.isoformat())
    monkeypatch.setattr(daily, "fetch_market_snapshot", lambda session: [{
        "code": "600000", "name": "fixture", "open": 10, "high": 10.2, "low": 9.8,
        "trade": 10.1, "volume": 1000, "amount": 10100,
    }])
    requests = []

    def quotes(symbols):
        requests.append(list(symbols))
        return {code: {"date": DAY, "time": "15:00:03", "open": 10, "high": 10.2,
                       "low": 9.8, "price": 10.1, "volume": 1000, "amount": 10100}
                for code in symbols}

    monkeypatch.setattr(daily, "_dated_close_quotes", quotes)
    result = daily.family_bars_and_valuation(DAY, True, lease=FakeLease(DAY))
    assert result["universe_discovery"]["symbols"] == [omitted]
    assert result["universe_diff_requery"]["attempted"] == 1
    assert result["universe_diff_requery"]["appended"] == 1
    assert [omitted] in requests
    assert (tmp_path / f"data/daily/{omitted}.csv").is_file()
    assert universe.read_text(encoding="utf-8") == "symbol\n600000\n"


@pytest.mark.parametrize("quote_date,expected", [(DAY, ["302132"]), ("", [])])
def test_explicit_snapshot_root_requires_real_date_and_bound_archive(tmp_path, quote_date, expected):
    archive_root = tmp_path / "isolated_trial"
    calendar = archive_root / "data/calendar/a_share_trading_calendar.csv"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(f"date,is_open\n{DAY},true\n", encoding="utf-8")
    fetched = datetime(2026, 10, 12, 9, 30, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot_history.collect_market_snapshot(
        archive_root, network=True, now=fetched.replace(second=0), clock=lambda: fetched,
        symbols=["600000"],
        fetcher=lambda **kw: ([{"symbol": code, "price": 10, "quote_date": quote_date,
                               "quote_time": "09:30:10", "price_basis": "raw"}
                              for code in ("600000", "302132")], "fixture.live"),
    )
    assert discover_daily_universe_symbols(tmp_path, DAY, known_symbols=["600000"], now=NOW)["symbols"] == []
    result = discover_daily_universe_symbols(
        tmp_path, DAY, known_symbols=["600000"], snapshot_roots=[archive_root], now=NOW,
    )
    assert result["symbols"] == expected
    path = archive_root / "data/snapshots/2026-10-12/0930.csv"
    path.write_text(path.read_text(encoding="utf-8") + "bad\n", encoding="utf-8")
    result = discover_daily_universe_symbols(tmp_path, DAY, snapshot_roots=[archive_root], now=NOW)
    assert result["symbols"] == []
    assert any("archive_invalid" in warning for warning in result["warnings"])
