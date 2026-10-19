"""The real snapshot worker must prefer dated HQ quotes and preserve unknowns."""

from __future__ import annotations

import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from quantdesk import snapshot_history as history


DAY = "2026-10-13"
NOW = datetime(2026, 10, 13, 10, 0, 5, tzinfo=history.SHANGHAI)
SYMBOLS = ["600000", "000001"]

# Explicitly loaded before the worker module in the actual spawned child. Only
# transport responses are faked; the worker entrypoint, HQ parser, provider
# selection, JSON pipe and archive normalization remain production code.
CHILD_FIXTURE = r'''
import builtins
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

events = Path(os.environ["QD_SNAPSHOT_TEST_EVENTS"])
mode = os.environ["QD_SNAPSHOT_TEST_MODE"]
day = os.environ["QD_SNAPSHOT_TEST_DAY"]

def record(event, **details):
    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(event=event, pid=os.getpid(), at=time.monotonic(), **details)) + "\n")

probing_guards = False

def forbidden_network(*args, **kwargs):
    if not probing_guards:
        record("unexpected_network")
    raise AssertionError("test child attempted non-fixture networking")

socket.create_connection = forbidden_network
socket.socket.connect = forbidden_network
socket.socket.connect_ex = forbidden_network
socket.getaddrinfo = forbidden_network
socket.gethostbyname = forbidden_network
socket.gethostbyname_ex = forbidden_network
socket.gethostbyaddr = forbidden_network
socket.getnameinfo = forbidden_network

import requests
requests.get = forbidden_network
requests.request = forbidden_network
requests.sessions.Session.request = forbidden_network
guarded_channels = []
probing_guards = True
for name, call in (
    ("connect", lambda: socket.socket.connect(None, ("fixture.invalid", 443))),
    ("connect_ex", lambda: socket.socket.connect_ex(None, ("fixture.invalid", 443))),
    ("create_connection", lambda: socket.create_connection(("fixture.invalid", 443))),
    ("getaddrinfo", lambda: socket.getaddrinfo("fixture.invalid", 443)),
    ("gethostbyname", lambda: socket.gethostbyname("fixture.invalid")),
    ("gethostbyname_ex", lambda: socket.gethostbyname_ex("fixture.invalid")),
    ("gethostbyaddr", lambda: socket.gethostbyaddr("192.0.2.1")),
    ("getnameinfo", lambda: socket.getnameinfo(("192.0.2.1", 443), 0)),
    ("requests.get", lambda: requests.get("https://fixture.invalid")),
    ("requests.request", lambda: requests.request("GET", "https://fixture.invalid")),
    ("requests.Session.request", lambda: requests.Session().request("GET", "https://fixture.invalid")),
):
    try:
        call()
    except AssertionError:
        guarded_channels.append(name)
    else:
        raise RuntimeError("network guard did not reject " + name)
probing_guards = False
project_loaded = any(name == "quantdesk" or name.startswith("quantdesk.") for name in sys.modules)
if project_loaded:
    raise RuntimeError("project imported before network guard verification")
fence_configs = getattr(sys, "_quantdesk_test_fence_registry", {}).get("configs", [])
if not fence_configs:
    raise RuntimeError("parent write fence was not restored before fixture")
record("fixture_ready", guarded_channels=guarded_channels, project_loaded=False, write_fence_restored=True)
if os.environ.get("QD_SNAPSHOT_TEST_GUARD_ONLY") == "1":
    raise SystemExit(0)

import urllib.request

def fake_hq(url, *, timeout):
    if urlsplit(url).netloc != "hq.sinajs.cn" or not urlsplit(url).path.startswith("/list="):
        return forbidden_network()
    record("hq", timeout=timeout)
    if mode == "hq_failure":
        raise TimeoutError("fixture_hq_read_timeout")
    if mode in {"hq_hang", "both_hang"}:
        time.sleep(30)
    if mode == "hq_empty":
        return ""
    keys = urlsplit(url).path.split("list=", 1)[1].split(",")
    if mode == "hq_partial":
        keys = keys[:1]
    result = []
    for key in keys:
        parts = ["0"] * 33
        parts[0:6] = ["fixture", "10", "9.9", "10.2", "10.3", "9.8"]
        parts[8:10] = ["12345", "125919"]
        parts[30:32] = ["2001-01-02" if mode == "hq_stale" else day, "10:00:01"]
        if mode == "hq_undated":
            parts[30] = ""
        result.append('var hq_str_' + key + '="' + ','.join(parts) + '";')
    return "\n".join(result)

def fake_list(url, *, params, headers, timeout):
    if url != "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData":
        return forbidden_network()
    record("list", timeout=timeout, page=params["page"])
    if mode == "both_hang":
        time.sleep(30)
    payload = [{"symbol": key, "code": key[2:], "name": "fixture",
                "trade": "10.2", "open": "10", "high": "10.3", "low": "9.8",
                "settlement": "9.9", "volume": 12345, "amount": 125919,
                "ticktime": "10:00:01"} for key in ("sh600000", "sz000001")]
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

def fake_urlopen(request, *, timeout):
    url = request.full_url
    if mode == "hq_slow_stream":
        if urlsplit(url).netloc != "hq.sinajs.cn":
            return forbidden_network()
        record("hq", timeout=timeout)
        body = b""
    else:
        body = fake_hq(url, timeout=timeout).encode("gb18030")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            if mode == "hq_slow_stream":
                # Chunks keep arriving sooner than the socket idle timeout,
                # but a complete response never arrives within the HQ window.
                for chunk in range(600):
                    time.sleep(0.05)
                    if chunk < 3:
                        record("slow_chunk")
            return body

    return Response()

urllib.request.urlopen = fake_urlopen
requests.get = fake_list

# Propagate the explicit offline bootstrap to a nested, real HQ worker. The
# guard still runs before importing any QuantDesk/provider module in that PID.
real_popen = subprocess.Popen

def offline_spawn(argv, *args, **kwargs):
    if argv[1:] == ["-B", "-m", "quantdesk.snapshot_history"]:
        environment = dict(kwargs.get("env") or os.environ)
        guard = environment["QUANTDESK_TEST_FENCE_BOOTSTRAP"]
        entries = [guard, *environment.get("PYTHONPATH", "").split(os.pathsep)]
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(value for value in entries if value))
        kwargs["env"] = environment
        bootstrap = (
            "import runpy, sys\n"
            + "runpy.run_path(" + repr(str(Path(guard) / "sitecustomize.py")) + ", run_name='_write_fence_startup')\n"
            + "sys.path.insert(0, " + repr(os.environ["QD_SNAPSHOT_TEST_SOURCE_ROOT"]) + ")\n"
            + "runpy.run_path(" + repr(__file__) + ", run_name='_offline_transport')\n"
            + "runpy.run_module('quantdesk.snapshot_history', run_name='__main__')\n"
        )
        argv = [argv[0], "-B", "-c", bootstrap]
    return real_popen(argv, *args, **kwargs)

subprocess.Popen = offline_spawn

real_import = builtins.__import__

def delayed_provider_import(name, *args, **kwargs):
    if name == "akshare" or name.startswith("akshare."):
        record("unexpected_adapter")
        raise ModuleNotFoundError("AkShare is absent from the lean collector environment")
    if name == "quantdesk.data_providers" and mode == "hq_import_hang":
        record("hq_import")
        time.sleep(30)
    return real_import(name, *args, **kwargs)

builtins.__import__ = delayed_provider_import
'''


@pytest.fixture
def fake_worker(tmp_path: Path, monkeypatch):
    injection = tmp_path / "worker_fixture"
    injection.mkdir()
    transport_fixture = injection / "offline_transport.py"
    transport_fixture.write_text(CHILD_FIXTURE, encoding="utf-8")
    events = tmp_path / "worker_events.jsonl"
    monkeypatch.setenv("QD_SNAPSHOT_TEST_EVENTS", str(events))
    monkeypatch.setenv("QD_SNAPSHOT_TEST_MODE", "hq_ok")
    monkeypatch.setenv("QD_SNAPSHOT_TEST_DAY", DAY)
    monkeypatch.setenv("QD_SNAPSHOT_TEST_SOURCE_ROOT", str(Path(history.__file__).resolve().parents[1]))
    monkeypatch.delenv("QD_SNAPSHOT_TEST_GUARD_ONLY", raising=False)
    original_as_shanghai = history._as_shanghai
    monkeypatch.setattr(history, "_as_shanghai", lambda value: NOW if value is None else original_as_shanghai(value))
    original_popen = subprocess.Popen

    def spawn(argv, *args, **kwargs):
        if argv[1:] == ["-B", "-m", "quantdesk.snapshot_history"]:
            environment = dict(kwargs.get("env") or os.environ)
            guard = environment["QUANTDESK_TEST_FENCE_BOOTSTRAP"]
            entries = [guard, *environment.get("PYTHONPATH", "").split(os.pathsep)]
            environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(value for value in entries if value))
            kwargs["env"] = environment
            # A same-named sitecustomize in the repository can hide PYTHONPATH
            # fixtures. Restore the unchanged parent write fence explicitly
            # before the network fixture or any project code is loaded.
            bootstrap = (
                "import runpy, sys\n"
                f"runpy.run_path({str(Path(guard) / 'sitecustomize.py')!r}, run_name='_write_fence_startup')\n"
                f"sys.path.insert(0, {str(Path(history.__file__).resolve().parents[1])!r})\n"
                f"runpy.run_path({str(transport_fixture)!r}, run_name='_offline_transport')\n"
                "runpy.run_module('quantdesk.snapshot_history', run_name='__main__')\n"
            )
            argv = [argv[0], "-B", "-c", bootstrap]
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield events
    assert events.is_file(), "the child's offline guard was never loaded"
    records = read_events(events)
    assert records[0]["event"] == "fixture_ready"
    guards = [row for row in records if row["event"] == "fixture_ready"]
    assert all(len(row["guarded_channels"]) == 11 and row["project_loaded"] is False
               and row["write_fence_restored"] is True for row in guards)
    assert {row["pid"] for row in records} <= {row["pid"] for row in guards}
    assert all(row["pid"] != os.getpid() for row in records)
    assert not any(row["event"].startswith("unexpected_") for row in records)
    reaping = [observe_process(pid) for pid in sorted({row["pid"] for row in records})]
    events.with_name("process_reaping.json").write_text(json.dumps(reaping), encoding="utf-8")
    assert not any(row["running"] for row in reaping), reaping


def observe_process(pid: int) -> dict:
    """Check the real child PID independently of its returned evidence."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, read only
        if not handle:
            error = ctypes.get_last_error()
            assert error == 87, (pid, error)  # ERROR_INVALID_PARAMETER: absent PID
            return {"pid": pid, "running": False, "evidence": "pid_absent"}
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            assert status in {0, 258}, (pid, status)
            return {"pid": pid, "running": status == 258, "evidence": "process_handle_wait"}
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"pid": pid, "running": False, "evidence": "pid_absent"}
    return {"pid": pid, "running": True, "evidence": "pid_exists"}


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def provider_events(path: Path) -> list[str]:
    return [row["event"] for row in read_events(path) if row["event"] != "fixture_ready"]


def test_offline_bootstrap_blocks_socket_dns_and_requests_before_project_imports(fake_worker, monkeypatch):
    monkeypatch.setenv("QD_SNAPSHOT_TEST_GUARD_ONLY", "1")
    fixture = fake_worker.parent / "worker_fixture/offline_transport.py"
    result = subprocess.run(
        [sys.executable, "-B", str(fixture)], cwd=str(fixture.parent),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert [row["event"] for row in read_events(fake_worker)] == ["fixture_ready"]


def test_actual_spawned_worker_prefers_hq_dates_and_explicit_raw_basis(fake_worker):
    rows = history.fetch_snapshot_in_process(SYMBOLS, network=True, budget_seconds=15)
    assert rows.source == "sina.hq_batch", rows.provider_errors
    normalized = history.normalize_snapshot_rows(
        rows, source=rows.source, fetched_at=NOW, trade_date=DAY, slot_time="1000",
    )
    assert len(normalized) == 2
    assert all(row["quote_date"] == DAY and row["quote_time"] == "10:00:01" for row in normalized)
    assert all(row["price_basis"] == "raw" for row in normalized)
    assert all(row["volume"] == 12345 and row["amount"] == 125919 for row in normalized)
    assert provider_events(fake_worker) == ["hq"]
    assert history._quote_field_quality(normalized, DAY)["quality_status"] == "complete"


@pytest.mark.parametrize("mode", ["hq_failure", "hq_empty", "hq_partial", "hq_undated", "hq_stale"])
def test_actual_worker_fallback_preserves_unknown_quote_dates(fake_worker, monkeypatch, mode):
    monkeypatch.setenv("QD_SNAPSHOT_TEST_MODE", mode)
    rows = history.fetch_snapshot_in_process(SYMBOLS, network=True, budget_seconds=15)
    assert rows.source == ("sina.hq_batch" if mode == "hq_partial" else "sina.spot_direct")
    assert rows.provider_errors[0]["source"] == "sina.hq_batch"
    assert rows.provider_errors[0]["outcome"] != "ok"
    normalized = history.normalize_snapshot_rows(
        rows, source=rows.source, fetched_at=NOW, trade_date=DAY, slot_time="1000",
    )
    assert len(normalized) == (1 if mode == "hq_partial" else 2)
    assert all(row["quote_date"] == (DAY if mode == "hq_partial" else "")
               and row["quote_time"] == "10:00:01" for row in normalized)
    assert all(row["price_basis"] == "raw" for row in normalized)
    quality = history._quote_field_quality(normalized, DAY)
    assert quality["quote_date_missing_count"] == (0 if mode == "hq_partial" else 2)
    if mode == "hq_partial":
        # A dated one-symbol HQ capture is more usable than two undated list
        # rows. Keep both attempts' evidence without inventing list dates.
        list_stage = next(item for item in rows.provider_errors
                          if item.get("source") == "sina.spot_direct" and item.get("row_count"))
        assert list_stage["quote_date_missing_count"] == 2
        assert list_stage["usable_quote_count"] == 0 and list_stage["outcome"] == "unusable"
    else:
        assert quality["quality_status"] == "partial"
    events = provider_events(fake_worker)
    # Provider start order is timing-dependent under the shared 15s budget;
    # the contract is that both HQ and list were attempted, and the quote-date
    # preservation assertions above pin the actual result.
    assert "hq" in events and "list" in events
    assert all(0 < row["timeout"] <= 15 for row in read_events(fake_worker) if "timeout" in row)


def test_requested_session_date_reaches_actual_worker_without_rewriting_provider_date(fake_worker, monkeypatch):
    monkeypatch.setenv("QD_SNAPSHOT_TEST_DAY", "2031-01-02")
    rows = history.fetch_snapshot_in_process(
        SYMBOLS, network=True, budget_seconds=15, trade_date="2031-01-02",
    )
    assert rows.source == "sina.hq_batch"
    assert {row["date"] for row in rows} == {"2031-01-02"}
    assert provider_events(fake_worker) == ["hq"]


def test_collect_market_snapshots_command_uses_spawned_hq_path_and_archives_fields(
    tmp_path: Path, fake_worker, monkeypatch, capsys,
):
    from scripts import collect_market_snapshots as command

    root = tmp_path / "archive"
    for relative, content in (
        ("data/calendar/a_share_trading_calendar.csv", f"date,is_open,source\n{DAY},1,fixture\n"),
        ("data/universe/a_share_symbols.csv", "symbol\n600000\n000001\n"),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    original_collect = command.collect_market_snapshot
    monkeypatch.setattr(command, "collect_market_snapshot", lambda root, **kwargs: original_collect(
        root, now=NOW, clock=lambda: NOW, **kwargs,
    ))
    code = command.main(["--root", str(root), "--enable-network", "--slot-time", "1000",
                         "--provider-budget-seconds", "15"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["written"] and result["source"] == "sina.hq_batch"
    archive = root / "data/snapshots" / DAY / "1000.csv"
    with archive.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert all(row["quote_date"] == DAY and row["price_basis"] == "raw" for row in rows)
    metadata = json.loads(archive.with_suffix(".json").read_bytes())
    assert metadata["quote_date_missing_count"] == 0
    assert metadata["price_basis_unverified_count"] == 0
    assert metadata["quality_status"] == "complete"
    assert provider_events(fake_worker) == ["hq"]


def test_parent_passes_original_deadline_and_session_date_after_spawn_cost(monkeypatch):
    clock = [100.0]
    requests = []
    monkeypatch.setattr(history.time_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(history, "_as_shanghai", lambda value: NOW)

    class Process:
        pid = 123
        returncode = 0

        def communicate(self, value=None, timeout=None):
            requests.append((json.loads(value), timeout))
            return json.dumps({"ok": True, "source": "fixture", "rows": []}), ""

    def spawn(*args, **kwargs):
        clock[0] += 2
        return Process()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    history.fetch_snapshot_in_process(SYMBOLS, network=True, budget_seconds=10)
    request, timeout = requests[0]
    assert request["deadline_monotonic"] == 110
    assert request["fetch_deadline_monotonic"] == 109
    assert request["worker_mode"] == "chain"
    assert request["trade_date"] == DAY and request["symbols"] == SYMBOLS
    assert timeout == 7  # startup and the one-second cleanup reserve are included


def test_worker_deducts_import_cost_before_starting_hq_chain(monkeypatch, capsys):
    requests = []
    monkeypatch.setattr(history.time_module, "monotonic", lambda: 106.0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "network": True, "symbols": SYMBOLS, "deadline_monotonic": 110.0, "trade_date": DAY,
    })))

    def fetch(symbols, **kwargs):
        requests.append((symbols, kwargs))
        return [], "fixture"

    monkeypatch.setattr(history, "resilient_network_fetch", fetch)
    monkeypatch.setattr(history, "default_network_fetch", lambda *args, **kwargs: pytest.fail("legacy worker route"))
    assert history._snapshot_worker_main() == 0
    assert len(requests) == 1 and requests[0][0] == SYMBOLS
    options = dict(requests[0][1])
    assert callable(options.pop("primary"))
    assert options == {"budget_seconds": 4.0, "trade_date": DAY}
    assert json.loads(capsys.readouterr().out)["source"] == "fixture"


@pytest.mark.parametrize("mode", ["hq_hang", "hq_slow_stream", "hq_import_hang"])
def test_stalled_hq_yields_to_list_within_original_process_budget(fake_worker, monkeypatch, mode):
    monkeypatch.setenv("QD_SNAPSHOT_TEST_MODE", mode)
    started = time.monotonic()
    rows = history.fetch_snapshot_in_process(SYMBOLS, network=True, budget_seconds=15)
    assert time.monotonic() - started < 15
    assert rows.source == "sina.spot_direct" and len(rows) == 2
    events = provider_events(fake_worker)
    # Start order is timing-dependent under the shared budget; the contract is
    # that the stalled HQ phase and the list fallback were both attempted.
    assert ("hq_import" if mode == "hq_import_hang" else "hq") in events and "list" in events
    if mode == "hq_slow_stream":
        assert events.count("slow_chunk") >= 2
    assert rows.provider_errors[0]["source"] == "sina.hq_batch"
    assert rows.provider_errors[0]["outcome"] != "ok"
    phase = next(row for row in rows.provider_errors if row.get("worker_mode") == "hq")
    assert phase["source"] == "sina.hq_batch" and phase["error_type"] == "TimeoutError"
    assert phase["process_reaped"] is True and phase["return_code"] is not None
    assert 0 < phase["duration_ms"] / 1000 < phase["budget_seconds"] <= 5
    provider_pid = next(row["pid"] for row in read_events(fake_worker) if row["event"] == events[0])
    # On Windows a venv redirector may have a different PID than the actual
    # interpreter. Verify both instead of treating the launcher's exit as proof.
    proof = {"spawned_process": observe_process(phase["runtime_pid"]), "provider_interpreter": observe_process(provider_pid)}
    fake_worker.with_name("hq_phase_reaping.json").write_text(json.dumps(proof), encoding="utf-8")
    assert not any(row["running"] for row in proof.values()), proof


def test_total_budget_still_terminates_a_stalled_fallback(fake_worker, monkeypatch):
    monkeypatch.setenv("QD_SNAPSHOT_TEST_MODE", "both_hang")
    started = time.monotonic()
    with pytest.raises(history.SnapshotProviderError, match="snapshot_worker_deadline_exhausted") as caught:
        history.fetch_snapshot_in_process(SYMBOLS, network=True, budget_seconds=10)
    assert time.monotonic() - started < 10
    assert caught.value.provider_errors[-1]["error_type"] == "TimeoutError"
    assert caught.value.provider_errors[-1]["return_code"] is not None
    assert "list" in provider_events(fake_worker)


def test_hq_watchdog_exits_during_provider_import_without_parent_timeout(fake_worker, monkeypatch):
    monkeypatch.setenv("QD_SNAPSHOT_TEST_MODE", "hq_import_hang")
    child = subprocess.Popen(
        [sys.executable, "-B", "-m", "quantdesk.snapshot_history"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    started = time.monotonic()
    try:
        child.communicate(json.dumps({
            "network": True, "worker_mode": "hq", "symbols": SYMBOLS,
            "trade_date": DAY, "deadline_monotonic": started + 3,
        }), timeout=8)
        assert child.returncode == 124
        assert time.monotonic() - started < 5
        assert provider_events(fake_worker) == ["hq_import"]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)


def test_hq_mode_starts_process_exit_watchdog_before_fetch_and_keeps_it_through_output(monkeypatch, capsys):
    import threading

    events = []
    timers = []
    monkeypatch.setattr(history.time_module, "monotonic", lambda: 106.0)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "network": True, "worker_mode": "hq", "symbols": SYMBOLS,
        "deadline_monotonic": 110.0, "trade_date": DAY,
    })))

    class Timer:
        def __init__(self, interval, function, args):
            self.interval, self.function, self.args = interval, function, args
            self.daemon = False
            timers.append(self)

        def start(self):
            events.append("watchdog_started")

        def cancel(self):
            events.append("watchdog_cancelled")

    def fetch(symbols, **kwargs):
        assert events == ["watchdog_started"]
        assert symbols == SYMBOLS and kwargs == {"budget_seconds": 4.0, "batch_size": 500}
        events.append("fetch")
        return [], "sina.hq_batch"

    original_dumps = json.dumps

    def serialize(*args, **kwargs):
        assert events == ["watchdog_started", "fetch"]
        events.append("serialize")
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(threading, "Timer", Timer)
    monkeypatch.setattr(history, "_sina_hq_fetch", fetch)
    monkeypatch.setattr(history, "resilient_network_fetch", lambda *a, **k: pytest.fail("HQ mode recursed into chain"))
    monkeypatch.setattr(history, "_fetch_snapshot_worker", lambda *a, **k: pytest.fail("HQ mode spawned another worker"))
    monkeypatch.setattr(json, "dumps", serialize)
    assert history._snapshot_worker_main() == 0
    assert timers[0].function is os._exit and timers[0].args == (124,)
    assert timers[0].interval == 4.0 and timers[0].daemon is True
    assert events == ["watchdog_started", "fetch", "serialize", "watchdog_cancelled"]
    assert json.loads(capsys.readouterr().out)["source"] == "sina.hq_batch"


def test_hq_phase_includes_spawn_and_cleanup_without_extending_outer_deadline(monkeypatch):
    clock = [100.0]
    requests = []
    monkeypatch.setattr(history.time_module, "monotonic", lambda: clock[0])

    class Process:
        pid = 123
        returncode = None

        def communicate(self, value=None, timeout=None):
            if value is not None:
                requests.append((json.loads(value), timeout))
                clock[0] += timeout
                raise subprocess.TimeoutExpired("fixture", timeout)
            requests.append((None, timeout))
            clock[0] += 0.25
            self.returncode = -9
            return "", ""

        def kill(self):
            clock[0] += 0.25

    def spawn(*args, **kwargs):
        clock[0] += 2
        return Process()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(history.SnapshotProviderError) as caught:
        history._fetch_snapshot_worker(SYMBOLS, budget_seconds=10, trade_date=DAY, mode="hq", outer_deadline=108)
    request, timeout = requests[0]
    assert request["deadline_monotonic"] == 108 and request["fetch_deadline_monotonic"] == 107
    assert request["worker_mode"] == "hq" and timeout == 5
    assert requests[1] == (None, 0.75)
    phase = caught.value.provider_errors[0]
    assert phase["budget_seconds"] == 8 and phase["duration_ms"] == 7500
    assert phase["process_reaped"] is True and phase["error_type"] == "TimeoutError"


def test_unreaped_hq_child_does_not_start_the_fallback(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(history.time_module, "monotonic", lambda: clock[0])

    class Process:
        pid = 123
        returncode = None

        def communicate(self, value=None, timeout=None):
            clock[0] += timeout
            raise subprocess.TimeoutExpired("fixture", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Process())

    def primary(*, budget_seconds):
        return history._fetch_snapshot_worker(SYMBOLS, budget_seconds=budget_seconds, trade_date=DAY, mode="hq")

    with pytest.raises(history.SnapshotProviderError, match="snapshot_worker_cleanup_deadline_exhausted") as caught:
        history.resilient_network_fetch(
            SYMBOLS, budget_seconds=15, clock=lambda: clock[0], primary=primary,
            secondary=lambda **kwargs: pytest.fail("list must not start before HQ reaping"),
        )
    assert caught.value.provider_errors[0]["process_reaped"] is False
    assert caught.value.provider_errors[0]["error_type"] == "ProcessCleanupTimeout"
    assert clock[0] == 105


@pytest.mark.parametrize("network,budget", [(False, 15), (True, 0)])
def test_no_worker_starts_without_network_permission_or_budget(monkeypatch, network, budget):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("worker must not start"))
    if network:
        with pytest.raises(history.SnapshotProviderError, match="snapshot_worker_budget_exhausted"):
            history.fetch_snapshot_in_process(SYMBOLS, network=network, budget_seconds=budget)
    else:
        rows = history.fetch_snapshot_in_process(SYMBOLS, network=network, budget_seconds=budget)
        assert rows == [] and rows.source == "offline"
