"""Offline HTTP deadline, truncation and owned-child cleanup regressions."""

from contextlib import redirect_stdout
from datetime import datetime
import hashlib
from http.client import IncompleteRead, RemoteDisconnected
import io
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import pytest

from quantdesk.intraday_refresh import collector


NOW = datetime(2026, 10, 13, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
URL = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
       "?symbol=sh600000&scale=1&ma=no&datalen=240")


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def packet(*items):
    return b"".join(json.dumps(item).encode("ascii") + b"\n" for item in items)


def success(text="[]"):
    return packet({"event": "result", "ok": True, "text": text,
                   "http": {"phase": "decode", "received_bytes": len(text.encode("utf8"))}})


class FakeProcess:
    """A clock-driven process: no threads, sockets or subprocesses are created."""
    pid = 123456

    def __init__(self, clock, *, output=None, duration=0.1, returncode=0,
                 first_exception=None, kill_exception=None, cleanup_duration=0.02,
                 cleanup_stuck=False):
        self.clock = clock
        self.output = success() if output is None else output
        self.duration = duration
        self.exit_code = returncode
        self.first_exception = first_exception
        self.kill_exception = kill_exception
        self.cleanup_duration = cleanup_duration
        self.cleanup_stuck = cleanup_stuck
        self.returncode = None
        self.killed = False
        self.calls = []

    def communicate(self, *, timeout):
        self.calls.append(("communicate", timeout))
        if self.killed:
            if self.cleanup_stuck or self.cleanup_duration > timeout:
                self.clock.advance(timeout)
                raise subprocess.TimeoutExpired("offline_child", timeout, output=self.output)
            self.clock.advance(self.cleanup_duration)
            self.returncode = -9
            return self.output, None
        if self.first_exception is not None:
            failure, self.first_exception = self.first_exception, None
            raise failure
        if self.duration > timeout:
            self.clock.advance(timeout)
            raise subprocess.TimeoutExpired("offline_child", timeout, output=self.output)
        self.clock.advance(self.duration)
        self.returncode = self.exit_code
        return self.output, None

    def poll(self):
        return self.returncode

    def kill(self):
        self.calls.append(("kill", self.pid))
        if self.kill_exception is not None:
            raise self.kill_exception
        self.killed = True


@pytest.fixture(autouse=True)
def isolate_pending_fake(monkeypatch):
    # All retained handles in this file are FakeProcess instances.
    monkeypatch.setattr(collector, "_SINA_HTTP_UNREAPED", None)


def install_process(monkeypatch, clock, process, *, launch=0.0):
    launches = []

    def factory(argv, **kwargs):
        launches.append((argv, kwargs))
        clock.advance(launch)
        return process

    monkeypatch.setattr(collector.subprocess, "Popen", factory)
    return launches


def test_success_inherits_proxy_environment_and_hides_windows_worker(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock, output=success('[{"close":"9.4"}]'))
    launches = install_process(monkeypatch, clock, process, launch=0.05)
    text, evidence = collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert json.loads(text) == [{"close": "9.4"}]
    assert evidence["cleanup"] == "confirmed" and not evidence["deadline_exceeded"]
    argv, kwargs = launches[0]
    assert argv[0] == sys.executable and argv[1:5] == ["-I", "-S", "-B", "-c"]
    assert argv[6] == URL and argv[7].startswith("QuantDesk/")
    assert "env" not in kwargs and kwargs["close_fds"] is True
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert process.calls == [("communicate", pytest.approx(0.85))]


@pytest.mark.parametrize("phase", ["open", "read"])
def test_slow_dns_or_trickled_body_is_terminated_inside_total_budget(monkeypatch, phase):
    clock = Clock()
    progress = packet({"event": "stage", "http": {"phase": phase, "received_bytes": None}})
    process = FakeProcess(clock, output=progress, duration=3.6)
    install_process(monkeypatch, clock, process, launch=0.04)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    exc = caught.value
    assert exc.error_type == "TimeoutError" and clock() <= 1.0
    assert exc.http["phase"] == phase and exc.http["received_bytes"] is None
    assert exc.http["cleanup"] == "confirmed" and process.returncode == -9
    assert process.calls[0][1] == pytest.approx(0.86)
    assert process.calls[1] == ("kill", process.pid)
    assert process.calls[2][1] == pytest.approx(0.1)
    assert collector._SINA_HTTP_UNREAPED is None


def test_slow_launch_consumes_budget_before_waiting_for_http(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock)
    install_process(monkeypatch, clock, process, launch=0.92)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == "TimeoutError"
    assert process.calls == [("kill", process.pid), ("communicate", pytest.approx(0.08))]
    assert clock() == pytest.approx(0.94) and caught.value.http["cleanup"] == "confirmed"


def test_os_launch_overrun_is_visible_and_late_success_is_rejected(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock, cleanup_duration=0.0)
    install_process(monkeypatch, clock, process, launch=1.2)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == "TimeoutError"
    assert caught.value.http["deadline_exceeded"] is True
    assert caught.value.http["cleanup"] == "confirmed" and process.killed
    assert process.calls[-1] == ("communicate", 0.0)


@pytest.mark.parametrize("seconds", [0.0, -0.1, float("inf"), float("nan")])
def test_no_usable_budget_never_launches_process(monkeypatch, seconds):
    clock = Clock()
    launches = install_process(monkeypatch, clock, FakeProcess(clock))
    with pytest.raises(collector._SinaHTTPFailure):
        collector._sina_http_get_text(URL, timeout=seconds, monotonic=clock)
    assert launches == []


@pytest.mark.parametrize("code,expected", [(124, "TimeoutError"), (1, "HTTPWorkerError")])
def test_worker_exit_code_cannot_be_masked_by_json_success(monkeypatch, code, expected):
    clock = Clock()
    process = FakeProcess(clock, output=success(), returncode=code)
    install_process(monkeypatch, clock, process)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == expected and caught.value.http["cleanup"] == "confirmed"


@pytest.mark.parametrize("output", [
    b"[]", b'{"event":"result","ok":true,"text":"[]"}',
    success() + b"broken\n", success() + success(),
    packet({"event": "stage", "http": {"phase": "read"}}),
])
def test_incomplete_or_ambiguous_child_output_cannot_be_success(monkeypatch, output):
    clock = Clock()
    install_process(monkeypatch, clock, FakeProcess(clock, output=output))
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == "HTTPWorkerProtocolError"


def test_complete_payload_from_worker_that_did_not_exit_is_still_timeout(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock, output=success(), duration=2.0)
    install_process(monkeypatch, clock, process)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == "TimeoutError" and process.killed


def test_interrupt_reaps_only_owned_child_before_propagating(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock, first_exception=KeyboardInterrupt())
    install_process(monkeypatch, clock, process)
    with pytest.raises(KeyboardInterrupt):
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert process.killed and process.returncode == -9
    assert collector._SINA_HTTP_UNREAPED is None


@pytest.mark.parametrize("stage", ["kill", "reap", "poll"])
def test_interrupt_during_cleanup_retains_live_handle_and_failure_evidence(monkeypatch, stage):
    clock = Clock()
    process = FakeProcess(clock, duration=3.6)
    if stage == "kill":
        process.kill_exception = KeyboardInterrupt("fixture_cleanup_interrupted")
    elif stage == "reap":
        communicate = process.communicate

        def interrupted_reap(**kwargs):
            if process.killed:
                raise SystemExit("fixture_cleanup_interrupted")
            return communicate(**kwargs)

        process.communicate = interrupted_reap
    else:
        polls = []

        def interrupted_poll():
            polls.append(True)
            if len(polls) == 1:
                raise KeyboardInterrupt("fixture_cleanup_interrupted")
            return None

        process.poll = interrupted_poll
    install_process(monkeypatch, clock, process)
    with pytest.raises((KeyboardInterrupt, SystemExit)) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert collector._SINA_HTTP_UNREAPED.process is process
    assert caught.value.http_diagnostics["cleanup"] == "unconfirmed"
    assert caught.value.http_diagnostics["stop_collection"] is True
    assert collector._sina_http_pending_failure(deadline=clock() + 1.0, monotonic=clock) is not None


@pytest.mark.parametrize("timeout_kind", ["parent", "child"])
def test_timeout_preserves_already_reported_child_truncation(monkeypatch, timeout_kind):
    clock = Clock()
    raw = packet({"event": "result", "ok": False, "error_type": "IncompleteRead",
                  "message": "fixture_truncated_body", "original_exception": "OSError: fixture_reset",
                  "http": {"phase": "read", "received_bytes": 2, "incomplete_expected_bytes": 12}})
    process = FakeProcess(clock, output=raw, duration=3.6 if timeout_kind == "parent" else 0.2,
                          returncode=124 if timeout_kind == "child" else 0)
    install_process(monkeypatch, clock, process)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == "TimeoutError"
    child_error = caught.value.http["child_error"]
    assert child_error["error_type"] == "IncompleteRead"
    assert child_error["original_exception"] == "OSError: fixture_reset"
    assert caught.value.http["received_bytes"] == 2


@pytest.mark.parametrize("kind", ["kill_denied", "reap_timeout"])
def test_unconfirmed_cleanup_preserves_handle_and_blocks_next_network_call(monkeypatch, kind):
    clock = Clock()
    process = FakeProcess(clock, duration=3.6,
                          kill_exception=PermissionError("fixture_kill_denied") if kind == "kill_denied" else None,
                          cleanup_stuck=kind == "reap_timeout")
    launches = install_process(monkeypatch, clock, process)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == "HTTPWorkerCleanupError"
    assert caught.value.http["cleanup"] == "unconfirmed" and caught.value.http["stop_collection"]
    assert collector._SINA_HTTP_UNREAPED.process is process
    native_calls = []
    monkeypatch.setattr(collector, "_mootdx_minute_rows", lambda *a, **k: native_calls.append(a))
    rows, error, evidence = collector._live_minute_rows("600001", offset=240, now=NOW, monotonic=clock)
    assert not rows and error.endswith("HTTPWorkerCleanupError") and evidence["stop_collection"]
    assert len(launches) == 1 and native_calls == []
    process.returncode = -9
    process.exit_code = -9
    process.duration = process.cleanup_duration = 0.0
    process.cleanup_stuck = False
    assert collector._sina_http_pending_failure(deadline=clock() + 1.0, monotonic=clock) is None
    assert collector._SINA_HTTP_UNREAPED is None


class FakeTimer:
    instances = []

    def __init__(self, seconds, action):
        self.seconds, self.action = seconds, action
        self.daemon = False
        self.started = self.cancelled = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class FakeResponse:
    def __init__(self, body=b"[]", *, headers=None, read_error=None, close_error=None, clock=None, delay=0):
        self.body, self.headers = body, headers or {}
        self.read_error, self.close_error = read_error, close_error
        self.clock, self.delay = clock, delay
        self.status = 200
        self.closed = False
        self.read_sizes = []
        self.watchdog_active_on_close = None

    def read(self, size):
        self.read_sizes.append(size)
        if self.clock is not None:
            self.clock.advance(self.delay)
        if self.read_error is not None:
            raise self.read_error
        return self.body

    def close(self):
        self.closed = True
        self.watchdog_active_on_close = FakeTimer.instances[-1].started and not FakeTimer.instances[-1].cancelled
        if self.close_error is not None:
            raise self.close_error


def run_child(monkeypatch, response=None, *, open_error=None, clock=None, cutoff=1.0, body_limit=1024):
    """Execute the exact child program in-process with fake opener and timer."""
    clock = clock or Clock()
    requests = []

    def open_fake(request, *, timeout):
        requests.append((request, timeout))
        if open_error is not None:
            raise open_error
        return response

    monkeypatch.setattr(urllib.request, "urlopen", open_fake)
    monkeypatch.setattr(threading, "Timer", FakeTimer)
    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(sys, "argv", ["-c", URL, "QuantDesk/fixture", str(cutoff), str(body_limit)])
    monkeypatch.setattr(FakeTimer, "instances", [])
    namespace = {"__name__": "offline_child_fixture"}
    exec(compile(collector._SINA_HTTP_PROGRAM, "<sina_http_worker>", "exec"), namespace)
    output = io.StringIO()
    with redirect_stdout(output):
        namespace["run"]()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    return frames[-1], requests, frames


def test_child_preserves_url_user_agent_utf8_and_cleanup_watchdog(monkeypatch):
    response = FakeResponse('[{"说明":"原始"}]'.encode("utf8"))
    result, requests, _ = run_child(monkeypatch, response)
    assert result["ok"] is True and json.loads(result["text"]) == [{"说明": "原始"}]
    assert requests[0][0].full_url == URL
    assert requests[0][0].get_header("User-agent") == "QuantDesk/fixture"
    assert requests[0][1] == pytest.approx(1.0)
    assert response.closed and response.watchdog_active_on_close
    assert FakeTimer.instances[0].daemon and FakeTimer.instances[0].cancelled


@pytest.mark.parametrize("expected", [12, None])
def test_incomplete_read_keeps_phase_byte_counts_and_hash_without_accepting_prefix(monkeypatch, expected):
    prefix = b'[{"close":"9.4"}]'
    response = FakeResponse(read_error=IncompleteRead(prefix, expected))
    result, requests, _ = run_child(monkeypatch, response)
    assert result["ok"] is False and result["error_type"] == "IncompleteRead"
    assert "text" not in result and response.closed and len(requests) == 1
    assert result["http"]["phase"] == "read"
    assert result["http"]["received_bytes"] == len(prefix)
    assert result["http"]["incomplete_expected_bytes"] == expected
    assert result["http"]["partial_sha256"] == hashlib.sha256(prefix).hexdigest()


def test_fixed_length_short_read_is_rejected_even_when_json_is_complete(monkeypatch):
    response = FakeResponse(b"[]", headers={"Content-Length": "14"})
    result, _, _ = run_child(monkeypatch, response)
    assert not result["ok"] and result["error_type"] == "IncompleteRead"
    assert result["http"]["content_length"] == 14
    assert result["http"]["received_bytes"] == 2
    assert result["http"]["incomplete_expected_bytes"] == 12


def test_chunked_truncation_keeps_nested_partial_evidence_without_inventing_total(monkeypatch):
    inner = IncompleteRead(b"incomplete last chunk", 8)
    outer = IncompleteRead(b"finished first chunk")
    outer.__cause__ = inner
    result, _, _ = run_child(monkeypatch, FakeResponse(read_error=outer, headers={"Transfer-Encoding": "chunked"}))
    assert not result["ok"] and result["error_type"] == "IncompleteRead"
    evidence = result["http"]
    assert evidence["received_bytes"] is None
    assert evidence["reported_partial_bytes"] == len(outer.partial)
    assert evidence["incomplete_read_chain"] == [
        {"partial_bytes": len(outer.partial), "expected_bytes": None,
         "partial_sha256": hashlib.sha256(outer.partial).hexdigest()},
        {"partial_bytes": len(inner.partial), "expected_bytes": 8,
         "partial_sha256": hashlib.sha256(inner.partial).hexdigest()},
    ]
    assert not evidence["incomplete_chain_truncated"]


def test_invalid_utf8_must_not_change_a_quote_by_dropping_a_byte(monkeypatch):
    response = FakeResponse(b'[{"close":"9.\xff40"}]')
    result, _, _ = run_child(monkeypatch, response)
    assert not result["ok"] and result["error_type"] == "UnicodeDecodeError"
    assert result["http"]["phase"] == "decode" and "text" not in result


@pytest.mark.parametrize("with_length", [False, True])
def test_oversized_body_is_bounded_and_not_used(monkeypatch, with_length):
    response = FakeResponse(b"x" * 33, headers={"Content-Length": "33"} if with_length else {})
    result, _, _ = run_child(monkeypatch, response, body_limit=32)
    assert not result["ok"] and result["error_type"] == "ValueError"
    assert response.read_sizes == ([] if with_length else [33]) and response.closed


def test_disconnect_does_not_turn_into_empty_success_or_a_retry(monkeypatch):
    result, requests, _ = run_child(monkeypatch, open_error=RemoteDisconnected("fixture_closed_before_headers"))
    assert not result["ok"] and result["error_type"] == "RemoteDisconnected"
    assert result["http"]["phase"] == "open" and result["http"]["received_bytes"] is None
    assert len(requests) == 1


def test_wrapped_socket_failure_retains_underlying_reason(monkeypatch):
    result, _, _ = run_child(monkeypatch, open_error=urllib.error.URLError(TimeoutError("fixture_socket_timeout")))
    assert not result["ok"] and result["error_type"] == "URLError"
    assert result["original_exception"] == "TimeoutError: fixture_socket_timeout"


@pytest.mark.parametrize("truncated", [False, True])
def test_response_close_failure_is_visible_without_replacing_prior_truncation(monkeypatch, truncated):
    response = FakeResponse(read_error=IncompleteRead(b"[]", 10) if truncated else None,
                            close_error=OSError("fixture_close_failed"))
    result, _, _ = run_child(monkeypatch, response)
    assert not result["ok"]
    assert result["error_type"] == ("IncompleteRead" if truncated else "OSError")
    assert result["http"]["response_close_error"] == "OSError"


def test_child_that_started_after_absolute_cutoff_never_opens_http(monkeypatch):
    result, requests, _ = run_child(monkeypatch, FakeResponse(), cutoff=0.0)
    assert not result["ok"] and result["error_type"] == "TimeoutError"
    assert requests == [] and FakeTimer.instances == []


def test_child_rejects_read_that_arrived_after_absolute_cutoff(monkeypatch):
    clock = Clock()
    result, _, _ = run_child(monkeypatch, FakeResponse(clock=clock, delay=1.01), clock=clock)
    assert not result["ok"] and result["error_type"] == "TimeoutError" and "text" not in result


def test_incomplete_child_result_reaches_collector_without_losing_http_evidence(monkeypatch):
    clock = Clock()
    raw = packet({"event": "result", "ok": False, "error_type": "IncompleteRead",
                  "message": "IncompleteRead(2 bytes read, 12 more expected)",
                  "http": {"phase": "read", "received_bytes": 2, "incomplete_expected_bytes": 12}})
    launches = install_process(monkeypatch, clock, FakeProcess(clock, output=raw))
    rows, error, evidence = collector._sina_minute_rows("600000", offset=240, now=NOW, monotonic=clock)
    assert rows == [] and error == "provider_failed:sina_intraday:IncompleteRead"
    assert evidence["http"]["phase"] == "read" and evidence["http"]["received_bytes"] == 2
    assert evidence["http"]["incomplete_expected_bytes"] == 12
    assert evidence["http"]["cleanup"] == "confirmed" and len(launches) == 1


@pytest.mark.parametrize("sina_first", [False, True])
def test_cleanup_failure_stops_both_fallback_routes_without_new_socket(monkeypatch, sina_first):
    calls = []

    def native(*args, **kwargs):
        calls.append("native")
        return [], "provider_failed:mootdx:ConnectionError", {}

    def failed_http(*args, **kwargs):
        calls.append("sina")
        return [], "provider_failed:sina_intraday:HTTPWorkerCleanupError", {
            "http": {"cleanup": "unconfirmed"}, "stop_collection": True,
        }

    monkeypatch.setattr(collector, "_mootdx_minute_rows", native)
    monkeypatch.setattr(collector, "_sina_minute_rows", failed_http)
    rows, error, evidence = collector._live_minute_rows(
        "600000", offset=240, now=NOW, monotonic=lambda: 0,
        preferred_provider="sina_intraday" if sina_first else "mootdx")
    assert not rows and error.endswith("HTTPWorkerCleanupError") and evidence["stop_collection"]
    assert calls == (["sina"] if sina_first else ["native", "sina"])


def test_round_keeps_remaining_symbols_missing_when_worker_exit_is_unconfirmed(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {
        "ok": False, "status": "skipped_in_test", "row_count": 0,
        "movers": [], "mover_count": 0, "reason_codes": [],
    })
    calls = []

    def fetch(symbol, offset, **kwargs):
        calls.append(symbol)
        return [], "provider_failed:sina_intraday:HTTPWorkerCleanupError", {
            "error_type": "HTTPWorkerCleanupError", "stop_collection": True,
            "http": {"phase": "cleanup", "cleanup": "unconfirmed"},
        }

    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=NOW, fetcher=fetch,
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": str(600000 + i)} for i in range(3)],
                                     "reason_codes": []})
    assert calls == ["600000"] and result["appended_rows"] == 0
    assert result["not_attempted_symbols"] == ["600001", "600002"]
    assert set(result["not_attempted_reasons"].values()) == {"http_worker_cleanup_unconfirmed"}
    assert "minute_http_worker_cleanup_unconfirmed" in result["reason_codes"]
    assert len(result["provider_status"]) == 1 and result["provider_status"][0]["status"] == "failed"


def test_already_exited_child_is_still_drained_within_remaining_budget(monkeypatch):
    clock = Clock()
    progress = packet({"event": "stage", "http": {"phase": "read"}})
    result = packet({"event": "result", "ok": False, "error_type": "IncompleteRead",
                     "message": "fixture_final_error", "http": {"received_bytes": 2}})
    process = FakeProcess(clock)
    calls = []

    def communicate(*, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            clock.advance(timeout)
            process.returncode = 0  # Exit races the parent's timed read.
            raise subprocess.TimeoutExpired("offline_child", timeout, output=progress)
        return progress + result, None

    process.communicate = communicate
    install_process(monkeypatch, clock, process)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert caught.value.error_type == "TimeoutError" and not process.killed
    assert calls == [pytest.approx(0.9), pytest.approx(0.1)]
    assert caught.value.http["output_drain"] == "confirmed"
    assert caught.value.http["child_error"]["error_type"] == "IncompleteRead"


def test_exited_child_with_unfinished_pipe_is_not_reported_fully_cleaned(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock)
    calls = []

    def communicate(*, timeout):
        calls.append(timeout)
        clock.advance(timeout)
        process.returncode = 0
        raise subprocess.TimeoutExpired("offline_child", timeout)

    process.communicate = communicate
    install_process(monkeypatch, clock, process)
    with pytest.raises(collector._SinaHTTPFailure) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    evidence = caught.value.http
    assert evidence["process_exit"] == "confirmed" and evidence["output_drain"] == "unconfirmed"
    assert evidence["cleanup"] == "unconfirmed" and evidence["stop_collection"]
    assert clock() <= 1.0 and collector._SINA_HTTP_UNREAPED.process is process
    assert collector._sina_http_pending_failure(deadline=clock() + 1.0, monotonic=clock) is not None
    assert 0 < calls[-1] <= 0.05
    process.communicate = lambda **kwargs: (success(), None)
    assert collector._sina_http_pending_failure(deadline=clock() + 1.0, monotonic=clock) is None


def test_round_abort_receipt_keeps_http_cleanup_diagnostics():
    failure = KeyboardInterrupt("fixture_interrupted")
    failure.http_diagnostics = {"cleanup": "unconfirmed", "process_exit": "unconfirmed", "stop_collection": True}
    manifest = {"stage_timings": [], "reason_codes": []}
    collector._record_stage_failure(manifest, failure, current_stage={"name": "provider", "symbol": "600000"},
                                    monotonic=lambda: 0)
    assert manifest["stage_timings"][0]["http"] == failure.http_diagnostics


def test_interrupt_while_checking_cleanup_error_is_not_swallowed(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock, duration=3.6, kill_exception=OSError("fixture_kill_failed"))
    polls = []

    def interrupted_poll():
        polls.append(True)
        if len(polls) == 2:
            raise KeyboardInterrupt("fixture_second_interrupt")
        return None

    process.poll = interrupted_poll
    install_process(monkeypatch, clock, process)
    with pytest.raises(KeyboardInterrupt) as caught:
        collector._sina_http_get_text(URL, timeout=1.0, monotonic=clock)
    assert collector._SINA_HTTP_UNREAPED.process is process
    assert caught.value.http_diagnostics["cleanup_poll_error_type"] == "KeyboardInterrupt"
    assert caught.value.http_diagnostics["stop_collection"] is True


@pytest.mark.parametrize("sina_first", [False, True])
def test_pending_pipe_cleanup_has_positive_budget_and_reduces_next_request(monkeypatch, sina_first):
    clock = Clock()
    process = FakeProcess(clock)
    process.returncode = 0
    drain_budgets = []

    def posix_drain(*, timeout):
        drain_budgets.append(timeout)
        # Like POSIX _communicate, an expired timeout makes no read progress.
        if timeout <= 0:
            raise subprocess.TimeoutExpired("offline_posix_pipe", timeout)
        clock.advance(0.04)
        return success(), None

    process.communicate = posix_drain
    collector._SINA_HTTP_UNREAPED = collector._SinaHTTPPending(process)
    provider_budgets = []

    def provider(*args, **kwargs):
        provider_budgets.append(kwargs["timeout_seconds"])
        return [{"symbol": "600000", "price_basis": "raw"}], "", {}

    monkeypatch.setattr(collector, "_mootdx_minute_rows", provider)
    monkeypatch.setattr(collector, "_sina_minute_rows", provider)
    rows, error, _ = collector._live_minute_rows(
        "600000", offset=240, now=NOW, timeout_seconds=1.0, monotonic=clock,
        preferred_provider="sina_intraday" if sina_first else "mootdx")
    assert rows and not error and drain_budgets == [0.05]
    assert provider_budgets == [pytest.approx(0.96 * 0.8 if sina_first else 0.96 / 3)]
    assert collector._SINA_HTTP_UNREAPED is None


@pytest.mark.parametrize("stage", ["poll", "drain"])
def test_pending_cleanup_interrupt_keeps_stage_diagnostics(monkeypatch, stage):
    clock = Clock()
    process = FakeProcess(clock)
    process.returncode = 0

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("fixture_pending_cleanup_interrupted")

    if stage == "poll":
        process.poll = interrupted
    else:
        process.communicate = interrupted
    collector._SINA_HTTP_UNREAPED = collector._SinaHTTPPending(process)
    with pytest.raises(KeyboardInterrupt) as caught:
        collector._sina_http_pending_failure(deadline=1.0, monotonic=clock)
    assert collector._SINA_HTTP_UNREAPED.process is process
    evidence = caught.value.http_diagnostics
    assert evidence["cleanup"] == "unconfirmed" and evidence["stop_collection"]
    assert evidence["process_exit"] == ("confirmed" if stage == "drain" else "unconfirmed")


def test_pending_drain_with_no_budget_does_not_reset_deadline(monkeypatch):
    clock = Clock()
    process = FakeProcess(clock)
    process.returncode = 0
    collector._SINA_HTTP_UNREAPED = collector._SinaHTTPPending(process)
    failure = collector._sina_http_pending_failure(deadline=0.0, monotonic=clock)
    assert failure.http["stop_collection"] and process.calls == []
    assert "budget_exhausted" in failure.http["cleanup_error"]
