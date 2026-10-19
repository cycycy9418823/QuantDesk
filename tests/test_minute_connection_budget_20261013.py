"""Native setup must share one real deadline and leave time for fallback."""

import json
from importlib.machinery import ModuleSpec
import sys
import threading
import types
from datetime import datetime, timezone

import pytest

from quantdesk.intraday_refresh import collector


NOW = datetime(2026, 10, 13, 10, 0, tzinfo=collector.SHANGHAI)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Frame:
    def __len__(self):
        return 1

    def to_dict(self, orient):
        assert orient == "records"
        return [{"datetime": "2026-10-13 10:00:00", "open": 10, "high": 10,
                 "low": 10, "close": 10, "volume": 100, "amount": 1000}]


def fake_native(monkeypatch, clock, *, import_seconds=0, connect_seconds=0,
                send_seconds=0, recv_seconds=0, setup_count=3, request_reads=1,
                failure=None, fail_close=False):
    """Mirror Quotes -> API.connect -> socket -> repeated setup I/O offline."""
    sockets, events, factory_kwargs = [], [], []
    transport = types.ModuleType("tdxpy.base_socket_client")

    class Socket:
        def __init__(self, *args, **kwargs):
            self.timeout = None
            self._closed = False
            self.close_count = 0
            sockets.append(self)

        def settimeout(self, value):
            self.timeout = value
            events.append(("settimeout", clock(), value))

        def _io(self, name, seconds):
            assert not self._closed
            events.append((name, clock(), self.timeout))
            spent = seconds if self.timeout is None else min(seconds, self.timeout)
            clock.advance(spent)
            if self.timeout is not None and seconds > self.timeout:
                raise TimeoutError("fixture_socket_timeout")

        def connect(self, address):
            self._io("connect", connect_seconds)

        def send(self, payload, *args):
            self._io("send", send_seconds)
            return len(payload)

        def recv(self, size, *args):
            self._io("recv", recv_seconds)
            return b"x" * size

        def close(self):
            if fail_close:
                raise OSError("fixture_close_failed")
            self._closed = True
            self.close_count += 1
            events.append(("close", clock(), None))

    transport.TrafficStatSocket = Socket

    class Api:
        def connect(self, *, time_out):
            self.client = transport.TrafficStatSocket(2, 1)
            self.client.settimeout(time_out)
            self.client.connect(("127.0.0.1", 7709))
            for _ in range(setup_count):
                self.client.send(b"setup")
                self.client.recv(1)
                if failure == "setup":
                    raise ConnectionError("fixture_setup_failed")

    Api.connect.__module__ = transport.__name__

    class Client:
        def __init__(self, api):
            self.client = api

        def bars(self, **kwargs):
            self.client.client.send(b"bars")
            for _ in range(request_reads):
                self.client.client.recv(1)
            if failure == "request":
                raise ConnectionError("fixture_request_failed")
            if failure == "interrupt":
                raise KeyboardInterrupt("fixture_interrupt")
            return Frame()

        def close(self):
            self.client.client.close()

    class Quotes:
        @staticmethod
        def factory(**kwargs):
            factory_kwargs.append(kwargs)
            api = Api()
            api.connect(time_out=kwargs["timeout"])
            if failure == "factory":
                raise ConnectionError("fixture_factory_failed")
            return Client(api)

    class QuotesModule(types.ModuleType):
        def __getattribute__(self, name):
            if name == "Quotes" and self.import_seconds:
                clock.advance(self.import_seconds)
                self.import_seconds = 0
            return super().__getattribute__(name)

    quotes = QuotesModule("mootdx.quotes")
    quotes.import_seconds = import_seconds
    quotes.Quotes = Quotes
    quotes.TdxHq_API = Api
    parent = types.ModuleType("mootdx")
    parent.quotes = quotes
    monkeypatch.setitem(sys.modules, "mootdx", parent)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes)
    monkeypatch.setitem(sys.modules, transport.__name__, transport)
    monkeypatch.setattr(collector.time, "sleep", clock.advance)
    return types.SimpleNamespace(sockets=sockets, events=events,
                                 factory_kwargs=factory_kwargs, transport=transport,
                                 original_socket=Socket, quotes=quotes)


def test_import_time_is_deducted_before_factory_timeout(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock, import_seconds=0.25)
    rows, error, _ = collector._mootdx_minute_rows_once(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert not error and rows
    assert native.factory_kwargs[0]["timeout"] == pytest.approx(0.75)


@pytest.mark.parametrize("connect_seconds", [0.4, 0.7])
def test_three_setup_reads_cannot_spend_fallback_budget(monkeypatch, connect_seconds):
    clock = Clock()
    native = fake_native(monkeypatch, clock, connect_seconds=connect_seconds,
                         send_seconds=0.04, recv_seconds=0.66)
    fallback = []

    def sina(symbol, **kwargs):
        fallback.append((clock(), kwargs["timeout_seconds"]))
        clock.advance(0.05)
        return [{"symbol": symbol, "price_basis": "raw"}], "", {
            "provider": "sina_intraday", "stages": [],
        }

    monkeypatch.setattr(collector, "_sina_minute_rows", sina)
    rows, error, evidence = collector._live_minute_rows(
        "600000", offset=1, now=NOW, timeout_seconds=2.608, monotonic=clock,
    )
    assert fallback, "native setup consumed the entire symbol allowance"
    assert fallback[0][0] <= 2.608 / 3 + 1e-9
    assert fallback[0][1] == pytest.approx(2.608 * 2 / 3)
    assert not error and rows and evidence["fallback_from"] == "mootdx"
    assert all(sock._closed for sock in native.sockets)
    assert native.transport.TrafficStatSocket is native.original_socket


def test_request_chunks_share_remaining_connection_deadline(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock, connect_seconds=0.1, send_seconds=0.01,
                         recv_seconds=0.4, setup_count=0, request_reads=3)
    rows, error, evidence = collector._mootdx_minute_rows_once(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert rows == [] and error == "provider_failed:mootdx:TimeoutError"
    assert clock() == pytest.approx(1)
    assert evidence["native_deadline_operation"] == "recv"
    reads = [event for event in native.events if event[0] == "recv"]
    assert [event[2] for event in reads] == pytest.approx([0.89, 0.49, 0.09])
    assert evidence["native_sockets_closed"] and native.sockets[0].close_count == 1


@pytest.mark.parametrize("failure", ["factory", "setup", "request"])
def test_factory_and_protocol_failures_restore_symbol_and_close_socket(monkeypatch, failure):
    clock = Clock()
    native = fake_native(monkeypatch, clock, failure=failure)
    rows, error, evidence = collector._mootdx_minute_rows_once(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert rows == [] and "ConnectionError" in error
    assert f"fixture_{failure}_failed" in evidence["message"]
    assert native.sockets[0].close_count == 1
    assert evidence["native_sockets_closed"] and evidence["socket_patch_restored"]
    assert native.transport.TrafficStatSocket is native.original_socket


def test_interrupt_during_request_still_closes_native_transport(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock, failure="interrupt")
    with pytest.raises(KeyboardInterrupt, match="fixture_interrupt"):
        collector._mootdx_minute_rows_once(
            "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
        )
    assert native.sockets[0].close_count == 1
    assert native.transport.TrafficStatSocket is native.original_socket


def test_close_failure_is_visible_and_discards_rows(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock, fail_close=True)
    rows, error, evidence = collector._mootdx_minute_rows_once(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert rows == [] and error == "provider_close_failed:mootdx:OSError"
    assert evidence["socket_close_errors"] == ["fixture_close_failed"]
    assert evidence["native_sockets_closed"] is False
    assert native.transport.TrafficStatSocket is native.original_socket


@pytest.mark.parametrize("wait,acquired", [(0.4, True), (1.0, False), (1.0, True)])
def test_adapter_lock_wait_is_inside_the_native_deadline(monkeypatch, wait, acquired):
    clock = Clock()
    native = fake_native(monkeypatch, clock)
    waits, releases = [], []

    class Lock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            clock.advance(wait)
            return acquired

        def release(self):
            releases.append(True)

    monkeypatch.setattr(collector, "_NATIVE_SOCKET_PATCH_LOCK", Lock())
    rows, error, evidence = collector._mootdx_minute_rows_once(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert waits == [1] and len(releases) == int(acquired)
    assert evidence["socket_patch_lock_wait_ms"] == round(wait * 1000)
    if wait < 1:
        assert rows and not error
        assert native.factory_kwargs[0]["timeout"] == pytest.approx(1 - wait)
    else:
        assert not rows and "TimeoutError" in error and not native.factory_kwargs
    assert native.transport.TrafficStatSocket is native.original_socket


def test_socket_interception_does_not_apply_to_another_thread(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock)
    owned, foreign, evidence = [], [], {}
    with collector._mootdx_deadline_sockets(
        native.quotes, deadline=1, monotonic=clock, sockets=owned, diagnostics=evidence,
    ):
        ours = native.transport.TrafficStatSocket(2, 1)
        worker = threading.Thread(target=lambda: foreign.append(native.transport.TrafficStatSocket(2, 1)))
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert isinstance(ours, collector._DeadlineSocket)
        assert len(foreign) == 1 and isinstance(foreign[0], native.original_socket)
        assert owned == [ours]
    ours.close()
    foreign[0].close()
    assert native.transport.TrafficStatSocket is native.original_socket


def test_factory_does_not_start_worker_or_heartbeat_threads(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock)

    def forbidden(*args, **kwargs):
        raise AssertionError("native fetch must stay synchronous")

    monkeypatch.setattr(collector.threading, "Thread", forbidden)
    rows, error, _ = collector._mootdx_minute_rows_once(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert rows and not error
    assert native.factory_kwargs[0]["heartbeat"] is False
    assert native.factory_kwargs[0]["auto_retry"] is False


def test_restore_conflict_is_visible_and_keeps_foreign_replacement(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock)
    original_factory = native.quotes.Quotes.factory
    foreign_factory = lambda *args, **kwargs: None

    def conflicting(**kwargs):
        client = original_factory(**kwargs)
        native.transport.TrafficStatSocket = foreign_factory
        return client

    monkeypatch.setattr(native.quotes.Quotes, "factory", conflicting)
    rows, error, evidence = collector._mootdx_minute_rows(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert not rows and "DeadlineRestoreError" in error
    assert evidence["attempt_count"] == 1
    assert evidence["socket_patch_restored"] is False
    assert evidence["socket_patch_restore_error"] == "native_socket_factory_changed_during_call"
    assert native.transport.TrafficStatSocket is foreign_factory
    assert native.sockets[0].close_count == 1


def test_failed_symbol_restoration_leaves_wrapper_inert_and_releases_lock(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock)

    class RestoreDenied(types.ModuleType):
        def __setattr__(self, name, value):
            if name == "TrafficStatSocket" and value is native.original_socket:
                raise RuntimeError("fixture_restore_denied")
            super().__setattr__(name, value)

    native.transport.__class__ = RestoreDenied
    rows, error, evidence = collector._mootdx_minute_rows(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert not rows and "DeadlineRestoreError" in error
    assert evidence["attempt_count"] == 1 and evidence["socket_patch_restored"] is False
    assert evidence["socket_patch_restore_error"] == "fixture_restore_denied"
    assert native.sockets[0].close_count == 1
    # The retained callable cannot keep attaching an expired deadline.
    subsequent = native.transport.TrafficStatSocket(2, 1)
    assert isinstance(subsequent, native.original_socket)
    subsequent.close()
    assert collector._NATIVE_SOCKET_PATCH_LOCK.acquire(blocking=False)
    collector._NATIVE_SOCKET_PATCH_LOCK.release()


@pytest.mark.parametrize("config_case", ["missing", "bad_json", "removed_during_factory", "valid"])
def test_config_setup_never_launches_implicit_server_discovery(tmp_path, monkeypatch, config_case):
    clock = Clock()
    native = fake_native(monkeypatch, clock)
    config = types.ModuleType("mootdx.config")
    config.CONF = tmp_path / "config.json"
    config.settings = {}
    probes, fallback = [], []
    if config_case != "missing":
        config.CONF.write_text("{" if config_case == "bad_json" else '{"fixture": true}', encoding="utf8")
    before = config.CONF.read_bytes() if config.CONF.exists() else None

    def bestip(**kwargs):
        probes.append(kwargs)
        raise AssertionError("implicit discovery would start unbounded workers")

    # This is the inspected library setup control flow, without its globals or
    # real profile. The finally read must not hide a blocked discovery cause.
    def setup():
        def load_config():
            config.settings.update(json.loads(config.CONF.read_text(encoding="utf8")))
        try:
            load_config()
        except (json.JSONDecodeError, FileNotFoundError):
            config.bestip(console=False, limit=5, sync=False)
        finally:
            load_config()

    config.bestip = bestip
    config.setup = setup
    native.quotes.config = config
    original_factory = native.quotes.Quotes.factory

    def configured_factory(**kwargs):
        if config_case == "removed_during_factory":
            config.CONF.unlink()
        config.setup()
        return original_factory(**kwargs)

    def sina(symbol, **kwargs):
        fallback.append(kwargs["timeout_seconds"])
        return [{"symbol": symbol, "price_basis": "raw"}], "", {"provider": "sina_intraday"}

    monkeypatch.setattr(native.quotes.Quotes, "factory", configured_factory)
    monkeypatch.setattr(collector, "_sina_minute_rows", sina)
    rows, error, evidence = collector._live_minute_rows(
        "600000", offset=1, now=NOW, timeout_seconds=2.608, monotonic=clock,
    )
    assert rows and not error and probes == []
    assert config.bestip is bestip
    assert native.transport.TrafficStatSocket is native.original_socket
    if config_case == "valid":
        assert fallback == [] and native.factory_kwargs[0]["bestip"] is False
        assert config.CONF.read_bytes() == before
    else:
        assert len(fallback) == 1 and not native.factory_kwargs and not native.sockets
        primary = evidence["attempts"][0]
        assert primary["native_discovery_blocked"] is True and primary["attempt_count"] == 1
        assert "implicit_native_server_discovery_disabled" in primary["message"]
        if config_case == "bad_json":
            assert config.CONF.read_bytes() == before
        else:
            assert not config.CONF.exists()


def test_unknown_installed_backend_fails_before_factory(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock)
    native.quotes.__spec__ = types.SimpleNamespace(name="mootdx.quotes")
    del native.quotes.TdxHq_API
    rows, error, evidence = collector._mootdx_minute_rows(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert rows == [] and "DeadlineAdapterError" in error
    assert evidence["native_deadline_enforcement"] == "unavailable"
    assert evidence["attempt_count"] == 1 and not native.factory_kwargs


@pytest.mark.parametrize("recv_seconds", [0, 0.4])
def test_installed_python_backend_uses_its_actual_connect_globals(monkeypatch, recv_seconds):
    clock = Clock()
    native = fake_native(monkeypatch, clock, recv_seconds=recv_seconds)
    api = native.quotes.TdxHq_API
    connect = api.connect
    api.connect = types.FunctionType(connect.__code__, vars(native.transport),
                                     connect.__name__, connect.__defaults__, connect.__closure__)
    api.connect.__module__ = native.transport.__name__
    native.quotes.__spec__ = ModuleSpec("mootdx.quotes", loader=None)
    bestip = lambda **kwargs: pytest.fail("server discovery must stay disabled")
    native.quotes.config = types.SimpleNamespace(bestip=bestip)
    rows, error, evidence = collector._mootdx_minute_rows_once(
        "600000", offset=1, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert bool(rows) is (recv_seconds == 0)
    assert bool(error) is (recv_seconds != 0)
    assert evidence["native_deadline_enforcement"] == "socket_io_absolute_deadline"
    assert evidence["native_socket_module"] == "tdxpy.base_socket_client"
    assert native.quotes.config.bestip is bestip
    assert evidence["native_sockets_closed"] and clock() <= 1


@pytest.mark.parametrize("nonretryable", [
    {"socket_patch_restore_error": "fixture_restore_conflict"},
    {"native_discovery_blocked": True},
    {"native_deadline_enforcement": "unavailable"},
])
def test_failed_fallback_does_not_retry_an_unusable_native_adapter(monkeypatch, nonretryable):
    clock = Clock()
    calls = []

    def native(symbol, **kwargs):
        calls.append("native")
        clock.advance(0.1)
        return [], "provider_failed:mootdx:fixture", {"error_stage": "connect", **nonretryable}

    def sina(symbol, **kwargs):
        calls.append("sina")
        clock.advance(0.1)
        return [], "provider_failed:sina_intraday:IncompleteRead", {
            "error_type": "IncompleteRead", "message": "fixture_truncated_response",
        }

    monkeypatch.setattr(collector, "_mootdx_minute_rows", native)
    monkeypatch.setattr(collector, "_sina_minute_rows", sina)
    rows, error, evidence = collector._live_minute_rows(
        "600000", offset=1, now=NOW, timeout_seconds=2.608, monotonic=clock,
    )
    assert not rows and error and calls == ["native", "sina"]
    assert evidence["fallback_error"] == "IncompleteRead:fixture_truncated_response"
    assert [row["provider"] for row in evidence["attempts"]] == ["mootdx", "sina_intraday"]


def test_cold_import_is_a_round_cost_before_ninety_symbol_shares(tmp_path, monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock, import_seconds=3,
                         connect_seconds=0.7, send_seconds=0.04, recv_seconds=0.66)
    fallback = []
    monkeypatch.setattr(collector, "refresh_full_market_snapshot", lambda *a, **k: {
        "ok": False, "status": "fixture", "movers": [], "reason_codes": [],
    })

    def sina(symbol, **kwargs):
        fallback.append((symbol, clock(), kwargs["timeout_seconds"]))
        clock.advance(0.01)
        return [{**Frame().to_dict("records")[0], "symbol": symbol,
                 "price_basis": "raw", "source": "sina_intraday"}], "", {
            "provider": "sina_intraday", "stages": [],
        }

    monkeypatch.setattr(collector, "_sina_minute_rows", sina)
    result = collector.collect_intraday_refresh(
        tmp_path, network=True, trading_day=True, now=NOW, monotonic=clock,
        scope_builder=lambda *a, **k: {"symbols": [{"symbol": str(600000 + i)} for i in range(90)],
                                      "warnings": []},
        max_round_seconds=240, write_manifest=False,
    )
    first_allowance = (240 - 3 - 5) / 90
    assert result["provider_initialization"][0]["duration_ms"] == 3000
    assert result["provider_status"][0]["granted_budget_seconds"] == pytest.approx(first_allowance, abs=0.001)
    assert native.factory_kwargs[0]["timeout"] == pytest.approx(first_allowance / 3)
    assert fallback[0][1] == pytest.approx(3 + first_allowance / 3)
    assert len(result["provider_status"]) == len(fallback) == result["fresh_count"] == 90
    assert not result["not_attempted_symbols"] and clock() < 240


def test_utc_host_now_is_converted_instead_of_relabelled(monkeypatch):
    instant = datetime(2026, 10, 13, 1, 44, 35, tzinfo=timezone.utc)

    class UTCClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(collector, "datetime", UTCClock)
    assert collector._now_shanghai().isoformat() == "2026-10-13T09:44:35+08:00"


def test_explicit_naive_and_aware_clock_inputs_remain_compatible():
    naive = datetime(2026, 10, 13, 10, 0)
    aware = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)
    assert collector._now_shanghai(naive) == NOW
    assert collector._now_shanghai(aware) == NOW
