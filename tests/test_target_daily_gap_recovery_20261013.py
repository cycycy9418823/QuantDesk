"""Targeted daily recovery preserves history and respects source/write limits."""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quantdesk import daily_gap_recovery as gap
from quantdesk.daily_session_evidence import build_baostock_session_evidence
from quantdesk.process_deadline import ProcessDeadlineResult
from quantdesk.session_expectation import build_session_expectation
from scripts.run_daily_data_refresh import DAILY_HEADER


DAY = "2026-10-13"


def _bar(symbol: str = "000016", day: str = DAY, **changes) -> pd.DataFrame:
    row = {"date": day, "symbol": symbol, "open": 10.0, "high": 11.0, "low": 9.0,
           "close": 10.5, "volume": 1000.0, "amount": 10500.0, "adjust": "raw"}
    row.update(changes)
    return pd.DataFrame([row])


def _old_file(root: Path, symbol: str = "000016", day: str = "2026-10-12", **changes) -> tuple[Path, bytes]:
    path = root / "data" / "daily" / f"{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = _bar(symbol, day, **changes).iloc[0].to_dict()
    row.update(source="original", updated_at="2026-10-12T16:00:00+08:00")
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\r\n")
    writer.writerow(DAILY_HEADER.split(","))
    writer.writerow([row.get(name, "") for name in DAILY_HEADER.split(",")])
    before = b"\xef\xbb\xbf" + text.getvalue().encode("utf-8")
    path.write_bytes(before)
    return path, before


def _payload(latest: dict[str, str | None]) -> dict:
    missing = [symbol for symbol, value in latest.items() if not value]
    stale = [symbol for symbol, value in latest.items() if value and value < DAY]
    fresh = [symbol for symbol, value in latest.items() if value == DAY]
    return {"target_date": DAY, "processed_symbols": list(latest), "total_symbols": len(latest),
            "symbols_checked": len(latest), "latest_by_symbol_after": latest,
            "fresh_symbol_list_after": fresh, "stale_symbol_list_after": stale,
            "missing_symbol_list_after": missing, "failed_symbol_list": [],
            "invalid_price_basis_symbols": [], "provider_calls": 0, "network_effective": False,
            "raw_positive_volume_target_symbol_list": fresh, "raw_zero_volume_target_symbol_list": []}


@pytest.fixture
def fake_sources(monkeypatch):
    from quantdesk import daily_price_providers as providers

    calls = []
    responses = {}

    def fetch(provider, symbol, start, end, *, network, strict_input=False):
        assert strict_input is True
        calls.append((provider, symbol, start, end, network))
        response = responses.get((provider, symbol), pd.DataFrame())
        if isinstance(response, Exception):
            raise response
        return response, []

    def run(worker, tasks, *, context, max_workers, timeout_seconds):
        assert max_workers == 1
        assert 0 < timeout_seconds <= 120
        assert set(context) == {"target_date", "attempt_dir", "inter_call_seconds"}
        results, failures = {}, {}
        for index, task in enumerate(tasks):
            try:
                results[index] = worker(task, context)
            except Exception as exc:
                failures[index] = type(exc).__name__ + ":" + str(exc)
        return ProcessDeadlineResult(results, failures, [], False, [])

    monkeypatch.setattr(providers, "fetch_daily", fetch)
    monkeypatch.setattr(gap, "run_process_tasks_until_deadline", run)
    monkeypatch.setattr(gap.time, "sleep", lambda _seconds: None)
    return calls, responses


def _recover(root: Path, payload: dict, **kwargs) -> dict:
    return gap.recover_target_day_gaps(project_root=root, target_date=DAY, daily_payload=payload,
                                       output_dir=root / "run" / "gap", network=True, **kwargs)


def test_missing_today_uses_fallback_and_preserves_all_prior_bytes(tmp_path, fake_sources):
    calls, responses = fake_sources
    path, before = _old_file(tmp_path)
    existing, existing_before = _old_file(tmp_path, "000668", DAY)
    payload = _payload({"000016": "2026-10-12", "000668": DAY})
    # Historical continuity may be stale although today's row exists.
    payload["stale_symbol_list_after"].append("000668")
    responses[("tencent_daily", "000016")] = _bar()
    result = _recover(tmp_path, payload)
    assert result["appended_symbols"] == ["000016"]
    assert path.read_bytes().startswith(before)
    assert existing.read_bytes() == existing_before
    assert all(call[1:] == ("000016", DAY, DAY, True) for call in calls)
    assert [call[0] for call in calls] == ["baostock_daily", "tencent_daily"]
    assert pd.read_csv(path)["date"].tolist() == ["2026-10-12", DAY]
    assert result["provider_calls_started"] == 2
    assert result["status"] == "completed"
    # The stale summary cannot cause a duplicate target-day append on reuse.
    again = _recover(tmp_path, payload)
    assert again["appended_symbols"] == []
    assert len(calls) == 2


def test_primary_success_does_not_call_secondary(tmp_path, fake_sources):
    calls, responses = fake_sources
    _old_file(tmp_path)
    responses[("baostock_daily", "000016")] = _bar()
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert result["appended_symbols"] == ["000016"]
    assert len(calls) == 1


def test_provider_failure_does_not_prevent_independent_fallback(tmp_path, fake_sources):
    calls, responses = fake_sources
    _old_file(tmp_path)
    responses[("baostock_daily", "000016")] = RuntimeError("fixture_provider_failure")
    responses[("tencent_daily", "000016")] = _bar()
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert result["appended_symbols"] == ["000016"]
    assert result["attempts"][0]["status"] == "failed"
    assert len(calls) == 2


@pytest.mark.parametrize("change", [
    {"adjust": "qfq"}, {"adjust": "unknown"}, {"symbol": "000017"},
    {"date": "2026-10-12"}, {"date": "invalid"}, {"open": 0},
    {"volume": 0}, {"volume": float("inf")}, {"close": float("nan")},
    {"amount": -1}, {"high": 9},
])
def test_unusable_provider_result_never_becomes_daily_data(tmp_path, fake_sources, change):
    calls, responses = fake_sources
    path, before = _old_file(tmp_path)
    bad = _bar(**change)
    for provider in ("baostock_daily", "tencent_daily"):
        responses[(provider, "000016")] = bad
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert not result["appended_symbols"]
    assert path.read_bytes() == before
    assert result["remaining_symbols"] == ["000016"]
    assert len(calls) == 2


def test_duplicate_target_rows_are_not_deduplicated_into_evidence(tmp_path, fake_sources):
    _, responses = fake_sources
    path, before = _old_file(tmp_path)
    for provider in ("baostock_daily", "tencent_daily"):
        responses[(provider, "000016")] = pd.concat([_bar(), _bar(close=10.1)])
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert result["remaining_symbols"] == ["000016"]
    assert path.read_bytes() == before


def _halt(symbol: str = "000016") -> dict:
    return build_baostock_session_evidence(
        symbol=symbol, provider_code=f"sz.{symbol}", query_start_date=DAY, query_end_date=DAY,
        records=[{"trade_date": DAY, "tradestatus": "0"}],
    )


def test_verified_halt_is_saved_without_zero_bar_or_history_replacement(tmp_path, fake_sources):
    calls, responses = fake_sources
    path, before = _old_file(tmp_path)
    old_evidence = tmp_path / "data" / "metadata" / "provider_session_evidence" / "baostock" / "000016.json"
    old_evidence.parent.mkdir(parents=True)
    old_evidence.write_bytes(b"old evidence must remain unchanged")
    frame = pd.DataFrame()
    frame.attrs["suspension_evidence"] = _halt()
    responses[("baostock_daily", "000016")] = frame
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert result["verified_no_session_symbols"] == ["000016"]
    assert not result["appended_symbols"]
    assert path.read_bytes() == before
    assert old_evidence.read_bytes() == b"old evidence must remain unchanged"
    assert len(calls) == 1
    session = build_session_expectation(["000016"], DAY, project_root=tmp_path,
                                        target_gap_session_evidence=result["session_evidence"])
    assert session["legitimate_no_session_symbols"] == ["000016"]
    assert session["per_symbol"]["000016"]["reason"] == "target_gap_verified_tradestatus_zero"


def test_conflicting_halt_and_positive_bar_require_another_source(tmp_path, fake_sources):
    calls, responses = fake_sources
    _old_file(tmp_path)
    frame = _bar()
    frame.attrs["suspension_evidence"] = _halt()
    responses[("baostock_daily", "000016")] = frame
    responses[("tencent_daily", "000016")] = _bar()
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert result["attempts"][0]["reason"] == "provider_trade_and_halt_conflict"
    assert result["appended_symbols"] == ["000016"]
    assert not result["session_evidence"]
    assert len(calls) == 2


def test_only_true_missing_rows_are_candidates(tmp_path, fake_sources):
    calls, _ = fake_sources
    bad, bad_before = _old_file(tmp_path, "000016", adjust="qfq")
    present, present_before = _old_file(tmp_path, "000017", DAY)
    payload = _payload({"000016": "2026-10-12", "000017": "2026-10-12", "000018": "bad-date"})
    result = _recover(tmp_path, payload)
    assert not calls
    assert result["candidates"] == []
    assert len(result["rejected"]) == 3
    assert bad.read_bytes() == bad_before
    assert present.read_bytes() == present_before


def test_symbol_cap_defers_without_fetching_extra_securities(tmp_path, fake_sources):
    calls, responses = fake_sources
    latest = {f"0000{i:02d}": "2026-10-12" for i in range(1, 5)}
    for symbol in latest:
        _old_file(tmp_path, symbol)
        responses[("baostock_daily", symbol)] = _bar(symbol)
    result = _recover(tmp_path, _payload(latest), budget=gap.GapRecoveryBudget(max_symbols=2))
    assert result["appended_symbols"] == ["000001", "000002"]
    assert result["deferred_symbols"] == ["000003", "000004"]
    assert {call[1] for call in calls} == {"000001", "000002"}
    assert result["status"] == "partial"


def test_expired_fetch_result_cannot_write_a_late_bar(tmp_path, fake_sources, monkeypatch):
    _, responses = fake_sources
    path, before = _old_file(tmp_path)
    responses[("baostock_daily", "000016")] = _bar()
    clock = [1.0]
    original_runner = gap.run_process_tasks_until_deadline
    monkeypatch.setattr(gap.time, "monotonic", lambda: clock[0])

    def late_runner(*args, **kwargs):
        result = original_runner(*args, **kwargs)
        clock[0] = 122.0
        return result

    monkeypatch.setattr(gap, "run_process_tasks_until_deadline", late_runner)
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert result["deadline_exhausted"] is True
    assert not result["appended_symbols"]
    assert path.read_bytes() == before


def test_success_before_last_timeout_survives_worker_cleanup(tmp_path, fake_sources, monkeypatch):
    """The second source must leave time to save its already-finished rows."""
    _, responses = fake_sources
    recovered, before = _old_file(tmp_path, "000016")
    pending, pending_before = _old_file(tmp_path, "000017")
    responses[("tencent_daily", "000016")] = _bar()
    clock = [1.0]
    monkeypatch.setattr(gap.time, "monotonic", lambda: clock[0])

    def timed_runner(worker, tasks, *, context, max_workers, timeout_seconds):
        provider = tasks[0]["provider"]
        if provider == "baostock_daily":
            results = {index: worker(task, context) for index, task in enumerate(tasks)}
            clock[0] += 2.0
            return ProcessDeadlineResult(results, {}, [], False, [])
        result = worker(tasks[0], context)
        # The last task hangs; real process cleanup can take four more seconds.
        gap.atomic_write_json(gap._attempt_path(Path(context["attempt_dir"]), provider, tasks[1]["symbol"]), {"status": "provider_call_started"})
        clock[0] += timeout_seconds + 4.0
        return ProcessDeadlineResult({0: result}, {}, [1], True, [])

    monkeypatch.setattr(gap, "run_process_tasks_until_deadline", timed_runner)
    result = _recover(tmp_path, _payload({"000016": "2026-10-12", "000017": "2026-10-12"}))
    assert result["appended_symbols"] == ["000016"]
    assert recovered.read_bytes().startswith(before)
    assert pending.read_bytes() == pending_before
    assert result["remaining_symbols"] == ["000017"]
    assert clock[0] < 121.0
    assert result["deadline_exhausted"] is False


def test_changed_original_file_is_not_overwritten(tmp_path, fake_sources, monkeypatch):
    _, responses = fake_sources
    path, before = _old_file(tmp_path)
    responses[("baostock_daily", "000016")] = _bar()
    original_runner = gap.run_process_tasks_until_deadline

    def changed_runner(*args, **kwargs):
        result = original_runner(*args, **kwargs)
        path.write_bytes(before + b"\n")
        return result

    monkeypatch.setattr(gap, "run_process_tasks_until_deadline", changed_runner)
    result = _recover(tmp_path, _payload({"000016": "2026-10-12"}))
    assert not result["appended_symbols"]
    assert path.read_bytes() == before + b"\n"
    assert "changed_during_fetch" in result["attempts"][0]["reason"]


@pytest.mark.parametrize("dry_run,network", [(True, True), (False, False)])
def test_disabled_or_dry_run_does_not_create_output(tmp_path, fake_sources, dry_run, network):
    calls, _ = fake_sources
    result = gap.recover_target_day_gaps(project_root=tmp_path, target_date=DAY,
        daily_payload=_payload({"000016": None}), output_dir=tmp_path / "run", network=network, dry_run=dry_run)
    assert result["status"] == "skipped"
    assert not calls
    assert not (tmp_path / "run").exists()


def test_writer_authority_is_checked_before_any_provider(tmp_path, fake_sources, monkeypatch):
    calls, _ = fake_sources

    def denied(_path):
        raise PermissionError("fixture_no_daily_authority")

    monkeypatch.setattr(gap, "assert_canonical_daily_write", denied)
    with pytest.raises(PermissionError):
        _recover(tmp_path, _payload({"000016": None}))
    assert not calls
    assert not (tmp_path / "run").exists()


def test_subset_merge_preserves_universe_and_other_historical_gaps():
    old = _payload({"000016": "2026-10-12", "000668": DAY, "000017": DAY})
    old["stale_symbol_list_after"].append("000668")
    refreshed = _payload({"000016": DAY})
    result = gap.merge_recovered_daily_status(old, refreshed, ["000016"], {"provider_calls_started": 2})
    assert result["total_symbols"] == 3
    assert result["processed_symbols"] == old["processed_symbols"]
    assert result["stale_symbol_list_after"] == ["000668"]
    assert result["latest_by_symbol_after"]["000016"] == DAY
    assert result["provider_calls"] == 2
    assert result["network_effective"] is True
    assert result["status"] == "partial"


def _bao_transport(monkeypatch, tmp_path, rows):
    from quantdesk import daily_price_providers as providers

    class Query:
        error_code = "0"
        fields = "date,code,open,high,low,close,volume,amount,tradestatus,isST".split(",")

        def __init__(self):
            self.rows = iter(rows)

        def next(self):
            self.current = next(self.rows, None)
            return self.current is not None

        def get_row_data(self):
            return self.current

    fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"),
                           logout=lambda: None, query_history_k_data_plus=lambda *a, **kw: Query())
    monkeypatch.setitem(sys.modules, "baostock", fake)
    monkeypatch.setattr(providers, "_BAOSTOCK_SESSION_LOCK_PATH", tmp_path / "bao.lock")
    return providers


def _bao_row():
    return [DAY, "sz.000016", "10", "11", "9", "10.5", "1000", "10500", "1", "0"]


@pytest.mark.parametrize("kind", ["duplicate", "date_suffix", "bad_date", "blank_code", "wrong_code",
    "malformed", "bad_status", "nan_volume", "infinite_close", "negative_volume", "bad_ohlc", "out_of_range"])
def test_baostock_original_input_rejected_before_normalization(tmp_path, monkeypatch, kind):
    row = _bao_row()
    rows = [row]
    if kind == "duplicate":
        rows.append([*row[:5], "10.25", *row[6:]])
    elif kind == "malformed":
        rows.append([DAY, "sz.000016"])
    else:
        column, value = {"date_suffix": (0, DAY + "T00:00:00"), "bad_date": (0, "2026-02-30"),
            "blank_code": (1, ""), "wrong_code": (1, "sz.000017"), "bad_status": (8, "unknown"),
            "nan_volume": (6, "nan"), "infinite_close": (5, "inf"), "negative_volume": (6, "-1"),
            "bad_ohlc": (3, "9"), "out_of_range": (0, "2026-10-12")}[kind]
        row[column] = value
    providers = _bao_transport(monkeypatch, tmp_path, rows)
    with pytest.raises(RuntimeError, match="strict_input"):
        providers.fetch_daily("baostock_daily", "000016", DAY, DAY, network=True, strict_input=True)


def test_baostock_original_input_valid_trade_and_halt(tmp_path, monkeypatch):
    providers = _bao_transport(monkeypatch, tmp_path, [_bao_row()])
    frame, _ = providers.fetch_daily("baostock_daily", "000016", DAY, DAY, network=True, strict_input=True)
    assert len(frame) == 1
    assert frame.iloc[0]["adjust"] == "raw"
    halted = _bao_row()
    halted[6:9] = ["0", "0", "0"]
    _bao_transport(monkeypatch, tmp_path, [halted])
    frame, _ = providers.fetch_daily("baostock_daily", "000016", DAY, DAY, network=True, strict_input=True)
    assert frame.empty
    assert frame.attrs["suspension_evidence"]["records"] == [{"trade_date": DAY, "tradestatus": "0"}]


def _tencent_transport(monkeypatch, rows, symbol="sz000016"):
    from quantdesk import daily_price_providers as providers
    from quantdesk.providers import tencent_provider

    payload = {"data": {symbol: {"day": rows}}}
    monkeypatch.setattr(tencent_provider, "_http_get_bytes", lambda *a, **kw: json.dumps(payload).encode())
    return providers


@pytest.mark.parametrize("kind", ["duplicate", "date_suffix", "bad_date", "malformed", "nan_volume",
    "infinite_close", "negative_volume", "bad_ohlc", "wrong_symbol", "bad_older_row"])
def test_tencent_original_input_rejected_before_normalization(monkeypatch, kind):
    row = [DAY, "10", "10.5", "11", "9", "10", "10500"]
    rows = [row]
    if kind == "duplicate":
        rows.append([*row[:2], "10.25", *row[3:]])
    elif kind == "malformed":
        rows.append([DAY, "10"])
    elif kind == "bad_older_row":
        rows.insert(0, ["2026-10-12 invalid", *row[1:]])
    elif kind != "wrong_symbol":
        column, value = {"date_suffix": (0, DAY + "T00:00:00"), "bad_date": (0, "2026-02-30"),
            "nan_volume": (5, "nan"), "infinite_close": (2, "inf"),
            "negative_volume": (5, "-1"), "bad_ohlc": (3, "9")}[kind]
        row[column] = value
    providers = _tencent_transport(monkeypatch, rows, "sz000017" if kind == "wrong_symbol" else "sz000016")
    with pytest.raises(RuntimeError, match="strict_input"):
        providers.fetch_daily("tencent_daily", "000016", DAY, DAY, network=True, strict_input=True)


def test_tencent_original_input_valid_preserves_raw_volume_and_amount(monkeypatch):
    providers = _tencent_transport(monkeypatch, [[DAY, "10", "10.5", "11", "9", "10", "10500"]])
    frame, _ = providers.fetch_daily("tencent_daily", "000016", DAY, DAY, network=True, strict_input=True)
    row = frame.iloc[0]
    assert row["adjust"] == "raw"
    assert row["volume"] == 1000
    assert row["amount"] == 10500


@pytest.mark.parametrize("provider", ["baostock_daily", "tencent_daily"])
def test_strict_input_is_opt_in_and_preserves_existing_default(tmp_path, monkeypatch, provider):
    if provider == "baostock_daily":
        row = _bao_row()
        providers = _bao_transport(monkeypatch, tmp_path, [row, [*row[:5], "10.25", *row[6:]]])
    else:
        row = [DAY, "10", "10.5", "11", "9", "10", "10500"]
        providers = _tencent_transport(monkeypatch, [row, [*row[:2], "10.25", *row[3:]]])
    frame, _ = providers.fetch_daily(provider, "000016", DAY, DAY, network=True)
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 10.25


@pytest.mark.parametrize("explicit_subset", [False, True])
def test_real_chain_recovers_only_verified_full_a_upstream_gaps(tmp_path, fake_sources, monkeypatch, explicit_subset):
    from tests.test_daily_data_chain_universe_refresh import _install_minimal_chain_fakes
    from tests.test_daily_data_chain_upstream_reuse_preflight import _valid_upstream_reuse

    calls, responses = fake_sources
    symbol = "600000"
    path, before = _old_file(tmp_path, symbol)
    reuse = _valid_upstream_reuse(tmp_path, target=DAY)
    chain = _install_minimal_chain_fakes(monkeypatch, [symbol])
    monkeypatch.setattr(chain, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(chain, "update_benchmark_files", lambda *a, **kw: {"status": "completed"})
    responses[("baostock_daily", symbol)] = _bar(symbol)
    result = chain.run_daily_data_chain(
        project_root=tmp_path, target_date=DAY, network=True, dry_run=False,
        rebuild_features=False, upstream_reuse=reuse, run_id="gap-entrypoint",
        symbols=[symbol] if explicit_subset else None,
    )
    recovery = result["components"]["target_day_gap_recovery"]
    if explicit_subset:
        assert recovery["status"] == "skipped"
        assert calls == []
        assert path.read_bytes() == before
    else:
        assert recovery["appended_symbols"] == [symbol]
        assert path.read_bytes().startswith(before)
        assert len(calls) == 1
        daily = result["components"]["daily_update"]
        assert daily["total_symbols"] == 1
        assert daily["processed_symbols"] == [symbol]
        assert daily["latest_by_symbol_after"][symbol] == DAY
        assert daily["provider_calls"] == 1
        assert daily["network_effective"] is True
