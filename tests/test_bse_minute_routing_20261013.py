"""Beijing minute bars must use the explicit native market without changing budgets."""

from copy import deepcopy
from datetime import datetime
import math
import struct
from types import SimpleNamespace

import pytest

from quantdesk.intraday_refresh import collector
from tests.test_minute_connection_budget_20261013 import Clock, fake_native


NOW = datetime(2026, 10, 13, 15, 5, tzinfo=collector.SHANGHAI)
RAW_RECORDS = [{
    "datetime": "2026-10-13 15:00", "open": 9.10, "high": 9.13,
    "low": 9.09, "close": 9.12, "vol": 125.5, "amount": 1144.56,
}]


class NativeTransport:
    def __init__(self, records=None, *, error=None):
        self.records = records
        self.error = error
        self.calls = []
        self.timeouts = []

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def get_security_bars(self, frequency, market, symbol, start, count):
        self.calls.append((frequency, market, symbol, start, count))
        if self.error is not None:
            raise self.error
        return self.records


class Frame:
    def __init__(self, records):
        self.records = records

    def __len__(self):
        return len(self.records)

    def to_dict(self, orient):
        assert orient == "records"
        return deepcopy(self.records)


class QuotesClient:
    def __init__(self, native, *, ordinary_frame=None, close_error=None):
        self.client = native
        self.ordinary_frame = ordinary_frame
        self.close_error = close_error
        self.bars_calls = []
        self.close_count = 0

    def bars(self, **kwargs):
        self.bars_calls.append(kwargs)
        if self.ordinary_frame is None:
            raise AssertionError("Beijing must bypass automatic market inference")
        return self.ordinary_frame

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def fetch_once(client, symbol="920066", *, offset=800, **kwargs):
    return collector._mootdx_minute_rows_once(
        symbol, offset=offset, now=NOW, client_factory=lambda: client, **kwargs,
    )


@pytest.mark.parametrize("symbol", ["920066", "920768", "920821", "430047", "830799"])
@pytest.mark.parametrize("offset,expected_count", [(1, 1), (800, 800), (801, 800)])
def test_beijing_native_route_preserves_full_raw_bars(symbol, offset, expected_count):
    records = deepcopy(RAW_RECORDS)
    transport = NativeTransport(records)
    client = QuotesClient(transport)
    rows, error, evidence = fetch_once(client, symbol, offset=offset)

    assert not error, evidence
    assert transport.calls == [(8, 2, symbol, 0, expected_count)]
    assert not client.bars_calls
    assert rows == [{
        "symbol": symbol, "datetime": "2026-10-13 15:00:00",
        "date": "2026-10-13", "time": "15:00:00", "open": 9.10,
        "high": 9.13, "low": 9.09, "close": 9.12, "volume": 125.5,
        "amount": 1144.56, "price_basis": "raw", "source": "mootdx_minute",
    }]
    assert records == RAW_RECORDS, "normalization must not mutate provider records"
    assert evidence["native_request"] == {
        "method": "get_security_bars", "market": 2, "symbol": symbol,
        "frequency": 8, "start": 0, "count": expected_count,
    }
    assert client.close_count == 1


@pytest.mark.parametrize("symbol", ["600000", "000001", "300750", "900901"])
def test_non_beijing_keeps_existing_high_level_request(symbol):
    transport = NativeTransport(RAW_RECORDS)
    client = QuotesClient(transport, ordinary_frame=Frame([
        {**RAW_RECORDS[0], "volume": 125.5},
    ]))
    rows, error, evidence = fetch_once(client, symbol, offset=801)
    assert not error and len(rows) == 1
    assert client.bars_calls == [{"symbol": symbol, "frequency": 8, "offset": 801}]
    assert transport.calls == []
    assert "native_request" not in evidence
    assert rows[0]["volume"] == 125.5
    assert client.close_count == 1


def test_native_vol_matches_mootdx_conversion_without_scaling_or_invented_amount():
    record = {**RAW_RECORDS[0], "volume": 999.0}
    del record["amount"]
    client = QuotesClient(NativeTransport([record]))
    rows, error, evidence = fetch_once(client)
    assert not error, evidence
    assert rows[0]["volume"] == 125.5
    assert math.isnan(rows[0]["amount"])
    assert record["volume"] == 999.0


@pytest.mark.parametrize("records", [None, []])
def test_empty_native_response_remains_missing(records):
    client = QuotesClient(NativeTransport(records))
    rows, error, evidence = fetch_once(client)
    assert rows == [] and error == "provider_no_rows:mootdx"
    assert evidence["error_stage"] == "empty"
    assert evidence["error_type"] == "EmptyResponse"
    assert evidence["native_request"]["market"] == 2
    assert client.close_count == 1


@pytest.mark.parametrize("native", [None, SimpleNamespace(), SimpleNamespace(get_security_bars=None)])
def test_missing_native_method_fails_without_inferred_market_fallback(native):
    client = QuotesClient(native)
    rows, error, evidence = fetch_once(client)
    assert rows == [] and error == "provider_failed:mootdx:RuntimeError"
    assert evidence["error_stage"] == "request"
    assert evidence["message"] == "mootdx BSE transport does not expose get_security_bars"
    assert client.bars_calls == []
    assert client.close_count == 1


def test_wrapped_native_protocol_error_retains_route_and_original_cause():
    class TdxFunctionCallError(RuntimeError):
        def __init__(self):
            super().__init__("calling function error")
            self.original_exception = struct.error("unpack requires a buffer of 4 bytes")

    client = QuotesClient(NativeTransport(error=TdxFunctionCallError()))
    rows, error, evidence = fetch_once(client)
    assert rows == [] and error == "provider_failed:mootdx:TdxFunctionCallError"
    assert evidence["error_stage"] == "request"
    assert evidence["original_exception"] == "error: unpack requires a buffer of 4 bytes"
    assert evidence["native_request"]["market"] == 2
    assert client.close_count == 1


def test_malformed_native_values_remain_visible_parse_failure():
    client = QuotesClient(NativeTransport([{**RAW_RECORDS[0], "vol": "invalid-volume"}]))
    rows, error, evidence = fetch_once(client)
    assert rows == [] and error == "provider_parse_failed:mootdx:ValueError"
    assert evidence["error_stage"] == "parse"
    assert client.close_count == 1


def test_native_beijing_request_uses_same_socket_deadline_and_cleanup(monkeypatch):
    clock = Clock()
    native = fake_native(monkeypatch, clock, connect_seconds=0.1, send_seconds=0.01,
                         recv_seconds=0.4, setup_count=0)
    calls = []

    def get_security_bars(api, frequency, market, symbol, start, count):
        calls.append((frequency, market, symbol, start, count))
        api.client.send(b"bars")
        for _ in range(3):
            api.client.recv(1)
        return RAW_RECORDS

    monkeypatch.setattr(native.quotes.TdxHq_API, "get_security_bars", get_security_bars, raising=False)
    rows, error, evidence = collector._mootdx_minute_rows_once(
        "920066", offset=800, now=NOW, timeout_seconds=1, monotonic=clock,
    )
    assert calls == [(8, 2, "920066", 0, 800)]
    assert rows == [] and error == "provider_failed:mootdx:TimeoutError"
    assert clock() == pytest.approx(1)
    assert evidence["native_deadline_operation"] == "recv"
    reads = [event for event in native.events if event[0] == "recv"]
    assert [event[2] for event in reads] == pytest.approx([0.89, 0.49, 0.09])
    assert evidence["native_sockets_closed"] and native.sockets[0].close_count == 1
    assert evidence["socket_patch_restored"]
    assert native.transport.TrafficStatSocket is native.original_socket


def test_native_beijing_retries_stay_bounded_and_keep_every_request_route(monkeypatch):
    clock = Clock()
    clients = []
    monkeypatch.setattr(collector.time, "sleep", clock.advance)

    def factory():
        client = QuotesClient(NativeTransport(error=RuntimeError("fixture_protocol_failure")))
        clients.append(client)
        return client

    rows, error, evidence = collector._mootdx_minute_rows(
        "920821", offset=800, now=NOW, client_factory=factory,
        monotonic=clock, timeout_seconds=1, max_attempts=99,
    )
    assert rows == [] and error == "provider_failed:mootdx:RuntimeError"
    assert evidence["attempt_count"] == len(clients) == 3
    assert [client.client.timeouts[0] for client in clients] == pytest.approx([1, 0.9, 0.7])
    assert all(client.close_count == 1 and not client.bars_calls for client in clients)
    assert all(attempt["native_request"]["market"] == 2 for attempt in evidence["attempts"])


def test_native_beijing_close_failure_discards_rows():
    client = QuotesClient(NativeTransport(RAW_RECORDS), close_error=OSError("fixture_close_error"))
    rows, error, evidence = fetch_once(client)
    assert rows == [] and error == "provider_close_failed:mootdx:OSError"
    assert evidence["close_error"] == error
    assert client.close_count == 1


def test_interrupt_in_native_beijing_request_still_closes_client():
    client = QuotesClient(NativeTransport(error=KeyboardInterrupt("fixture_interrupt")))
    with pytest.raises(KeyboardInterrupt, match="fixture_interrupt"):
        fetch_once(client)
    assert client.close_count == 1
