"""Real parser/connection boundaries with synthetic sockets and temporary evidence."""
from __future__ import annotations

import base64
import http.client
import io
import json
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ai_trader import llm, llm_transport as transport, daytime_observation as observer, overnight
from tests.test_ai_response_evidence_20261008 import _setup_evening
from tests.test_ai_trader_transport_diagnostics import _install, _enable
from tests.test_daytime_observation_20261008 import setup_data, run as run_observation


def archive(diagnostics):
    detail = llm.persistable_response_evidence(diagnostics)["transport_detail"]
    return detail, json.loads(Path(detail["capture_path"]).read_text(encoding="utf-8"))


def raw_response(monkeypatch, raw=b"{bad", error=None, status=200):
    class Response:
        def __init__(self):
            self.status = status
        def read(self):
            if error is not None:
                raise error
            return raw
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: Response())


@pytest.mark.parametrize("raw", [b'{"choices":', b'\xffnot-utf8', b'data: {"choices":[]}\n\n'])
def test_bad_outer_response_keeps_exact_received_bytes(tmp_path, monkeypatch, raw):
    raw_response(monkeypatch, raw)
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("https://example.test/v1/chat/completions", {}, {}, diag) is None
    detail, saved = archive(diag)
    assert detail["response_headers_received"] and detail["response_complete"]
    assert detail["failure_phase"] in {"utf8_decode", "outer_json"}
    assert base64.b64decode(saved["http_body"]["body_base64"]) == raw
    assert saved["http_body"]["byte_exact"] is True
    assert not saved["http_body"]["capture_truncated"]
    assert llm.persistable_response_evidence(diag)["raw_text"] is None


def test_incomplete_read_does_not_escape_and_preserves_partial(tmp_path, monkeypatch):
    raw_response(monkeypatch, error=http.client.IncompleteRead(b'{"choices":', 40))
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("https://example.test/v1", {}, {}, diag) is None
    detail, saved = archive(diag)
    assert diag["reason"] == "transport:IncompleteRead"
    assert detail["failure_phase"] == "response_body"
    assert detail["http_status"] == 200 and detail["response_complete"] is False
    assert base64.b64decode(saved["http_body"]["body_base64"]) == b'{"choices":'


def test_body_timeout_keeps_prior_chunks(tmp_path, monkeypatch):
    class Response:
        status = 200
        count = 0
        def read1(self, size):
            self.count += 1
            if self.count == 1: return b'{"choices":'
            raise TimeoutError("do not record raw exception")
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: Response())
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("https://example.test/v1", {}, {}, diag) is None
    detail, saved = archive(diag)
    assert detail["received_bytes"] == 11 and detail["response_complete"] is False
    assert detail["failure_phase"] == "response_body"
    assert saved["http_body"]["response_complete"] is False
    assert "do not record" not in json.dumps(saved)


def test_http_error_body_is_saved_and_credentials_are_removed(tmp_path, monkeypatch):
    secret = "sk-never-store-this-value"
    error = urllib.error.HTTPError("https://example.test/?token=" + secret, 429,
                                  secret, {}, io.BytesIO(('rate limited ' + secret).encode()))
    def fail(*a, **k): raise error
    monkeypatch.setattr(llm.urllib.request, "urlopen", fail)
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("https://example.test/?token=" + secret, {},
                              {"Authorization": "Bearer " + secret}, diag) is None
    detail, saved = archive(diag)
    assert diag["reason"] == "transport:HTTPError" and detail["http_status"] == 429
    decoded = base64.b64decode(saved["http_body"]["body_base64"])
    assert b"rate limited" in decoded and secret.encode() not in decoded
    assert secret not in json.dumps(saved) and saved["http_body"]["storage"] == "redacted"


def test_capture_limit_is_not_provider_token_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "CAPTURE_LIMIT", 8)
    raw_response(monkeypatch, b"bad-json-response-long")
    diag = {}
    with transport.capture_requests(tmp_path):
        llm._post_json("https://example.test/v1", {}, {}, diag)
    detail, saved = archive(diag)
    assert detail["response_complete"] is True
    assert saved["http_body"]["capture_truncated"] is True
    assert base64.b64decode(saved["http_body"]["body_base64"]) == b"bad-json"
    assert llm.persistable_response_evidence(diag)["provider_reported_truncation"] is None


def test_reasoning_is_excluded_from_archive_and_answer(tmp_path, monkeypatch):
    payload = {"choices": [{"message": {"content": "", "reasoning_content": "private-thinking-sentinel"},
                             "finish_reason": "length"}], "usage": {"completion_tokens": 2000}}
    _install(monkeypatch, payload=payload)
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._call("s", "u", {"base_url": "https://example.test/v1", "provider": "openai",
                                    "model": "fake", "token": "test"}, diag) is None
    _, saved = archive(diag)
    text = base64.b64decode(saved["http_body"]["body_base64"]).decode()
    assert "private-thinking-sentinel" not in text
    assert saved["http_body"]["storage"] == "redacted"
    evidence = llm.persistable_response_evidence(diag)
    assert evidence["raw_text"] == "" and evidence["provider_reported_truncation"] is True


@pytest.mark.parametrize("raw", [b"[]", b"42", b'"text"'])
def test_outer_root_has_explicit_error(tmp_path, monkeypatch, raw):
    raw_response(monkeypatch, raw)
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("https://example.test/v1", {}, {}, diag) is None
    assert diag["reason"].startswith("invalid_response_root:")
    assert archive(diag)[0]["response_complete"] is True


def test_evidence_survives_failure_before_plan_creation(tmp_path, monkeypatch):
    runtime = _setup_evening(monkeypatch, tmp_path)
    raw_response(monkeypatch, b'{"choices":')
    def fail(**kwargs): raise RuntimeError("plan construction failed")
    monkeypatch.setattr(overnight, "build_plan", fail)
    with pytest.raises(RuntimeError, match="plan construction"):
        overnight.run_evening(paths=runtime, network_news=False)
    files = list((runtime.ledger.parent / "llm_requests").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["http_body"]["body_base64"]


def test_observer_persists_original_answer_and_phase(tmp_path, monkeypatch):
    setup_data(tmp_path)
    _enable(monkeypatch, tmp_path)
    raw = '  {"orders":  '
    _install(monkeypatch, payload={"model": "returned-model", "choices": [
        {"message": {"content": raw}, "finish_reason": "length"}], "usage": {"completion_tokens": 2000}})
    result = run_observation(tmp_path)
    assert result["status"] == "model_failed"
    saved = json.loads(Path(result["receipt"]).read_text())
    evidence = saved["response_evidence"]
    assert evidence["raw_text"] == raw and evidence["response_model"] == "returned-model"
    assert evidence["provider_reported_truncation"] is True
    assert evidence["parse_stage"] == "observation_json_rejected"
    assert Path(evidence["transport_detail"]["capture_path"]).is_file()


def test_observer_unicode_answer_remains_readable(tmp_path, monkeypatch):
    setup_data(tmp_path); _enable(monkeypatch, tmp_path)
    raw = '{"orders":[],"note":"\ud800"}'
    _install(monkeypatch, payload={"choices": [{"message": {"content": raw}, "finish_reason": "stop"}]})
    result = run_observation(tmp_path)
    assert result["status"] == "recorded_unverified" and result["written"]
    assert json.loads(Path(result["receipt"]).read_text())["raw_response"] == raw


def test_archive_write_failure_is_visible(tmp_path, monkeypatch):
    raw_response(monkeypatch, b"{}")
    def fail(*a, **k): raise OSError("disk-full")
    monkeypatch.setattr(transport, "_atomic_json", fail)
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("https://example.test/v1", {}, {}, diag) == {}
    detail = llm.persistable_response_evidence(diag)["transport_detail"]
    assert detail["capture_status"] == "write_failed" and detail["capture_error_type"] == "OSError"
    assert not list(tmp_path.iterdir())


class FakeSocket:
    def __init__(self, wire, failure="", tcp_fail=False):
        self.wire, self.failure, self.tcp_fail = wire, failure, tcp_fail
    def settimeout(self, timeout): pass
    def setsockopt(self, *args): pass
    def bind(self, address): pass
    def connect(self, address):
        if self.tcp_fail: raise ConnectionRefusedError()
    def sendall(self, data):
        if self.failure == "request_write": raise ConnectionResetError()
    def makefile(self, *args):
        if self.failure == "response_headers":
            class TimeoutFile(io.BytesIO):
                def readline(self, *args): raise TimeoutError()
            return TimeoutFile()
        return io.BytesIO(self.wire)
    def close(self): pass


@pytest.mark.parametrize("failure", ["dns", "tcp", "tls", "request_write", "response_headers", ""])
def test_actual_connection_boundaries_are_distinct(tmp_path, monkeypatch, failure):
    def resolve(*args):
        if failure == "dns": raise socket.gaierror()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(transport.socket, "socket", lambda *a: FakeSocket(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", failure, failure == "tcp"))
    class Context:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True
        post_handshake_auth = None
        def wrap_socket(self, sock, **kwargs):
            if failure == "tls": raise ssl.SSLCertVerificationError()
            return sock
    with transport.capture_requests(tmp_path):
        trace = transport.Exchange("https://example.test/v1", {}, {}, 5)
        connection = transport.TracedHTTPSConnection("example.test", trace=trace, context=Context())
        try:
            connection.request("POST", "/v1", body=b"{}")
            response = connection.getresponse(); assert trace.read_body(response) == b"{}"
        except (OSError, http.client.HTTPException):
            assert failure
        trace.finish()
    if failure:
        assert trace.detail["failure_phase"] == failure
    else:
        assert trace.detail["request_written"] and trace.detail["response_complete"]
        assert [row["phase"] for row in trace.detail["phases"]] == [
            "dns", "tcp", "tls", "request_write", "response_headers", "response_body"]


def test_tcp_retry_records_actual_attempts_without_false_terminal_failure(tmp_path, monkeypatch):
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)) for port in (1, 2)]
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a: addresses)
    made = []
    def new_socket(*args):
        sock = FakeSocket(b"", tcp_fail=not made);made.append(sock);return sock
    monkeypatch.setattr(transport.socket, "socket", new_socket)
    trace = transport.Exchange("http://example.test/v1", {}, {}, 5)
    connection = transport.TracedHTTPConnection("example.test", trace=trace)
    connection.connect()
    assert len(made) == 2
    assert [r["outcome"] for r in trace.detail["phases"] if r["phase"] == "tcp"] == ["failed", "completed"]
    assert "failure_phase" not in trace.detail


def test_encoded_credentials_and_metadata_are_not_saved(tmp_path):
    secret = "credential-secret-example-123456"
    with transport.capture_requests(tmp_path):
        trace = transport.Exchange("https://example.test/v1", {}, {"Authorization": "Bearer " + secret}, 5)
        encoded = "".join("\\u%04x" % ord(character) for character in secret)
        trace.received(('{"error":"' + encoded + '"}').encode(), complete=True)
        response = type("Response", (), {"status": 429, "headers": {
            "x-request-id": "trace-" + secret, "Content-Type": "application/" + secret}})()
        trace.headers_received(response)
    saved = json.loads(trace.path.read_text())
    safe_body = base64.b64decode(saved["http_body"]["body_base64"])
    assert json.loads(safe_body)["error"] == "[redacted]"
    assert secret not in json.dumps(saved) and secret.encode() not in safe_body
    assert "provider_request_ids" not in saved["transport_detail"]
    assert "response_content_type" not in saved["transport_detail"]


@pytest.mark.parametrize("body", [b'{"\\u0074hinking":"private-sentinel"',
                                  b'{"THINKING":"private-sentinel"',
                                  b'{"type":"thinking","text":"private-sentinel"'])
def test_incomplete_private_fields_are_suppressed(tmp_path, body):
    with transport.capture_requests(tmp_path):
        trace = transport.Exchange("https://example.test/v1", {}, {}, 5)
        trace.received(body, complete=False)
    assert trace.body_record["storage"] == "suppressed_private_fields"
    assert trace.body_record["body_base64"] is None
    assert "private-sentinel" not in trace.path.read_text()


def test_retention_cap_and_network_chunks_cannot_expose_a_key_prefix(tmp_path, monkeypatch):
    secret = "credential-secret-example-123456"
    monkeypatch.setattr(transport, "CAPTURE_LIMIT", 18)
    with transport.capture_requests(tmp_path):
        trace = transport.Exchange("https://example.test/v1", {}, {"Authorization": secret}, 5)
        trace.received(("error:" + secret).encode(), complete=True)
        retained = base64.b64decode(trace.body_record["body_base64"])
        assert b"credential" not in retained and b"[redacted]" in retained
        trace.received(("error:" + secret[:12]).encode(), complete=False)
        retained = base64.b64decode(trace.body_record["body_base64"])
        assert b"credential" not in retained
        assert not trace.body_record["byte_exact"]


def test_credentials_in_an_encoded_json_answer_are_redacted_only_in_archive(tmp_path):
    secret = "credential-secret-example-123456"
    encoded = "".join("\\u%04x" % ord(character) for character in secret)
    inner = '{"error":"' + encoded + '"}'
    raw = json.dumps({"choices": [{"message": {"content": inner}}]}).encode()
    with transport.capture_requests(tmp_path):
        trace = transport.Exchange("https://example.test/v1", {}, {"Authorization": secret}, 5)
        trace.received(raw, complete=True)
    safe = json.loads(base64.b64decode(trace.body_record["body_base64"]))
    assert safe["choices"][0]["message"]["content"] == "[redacted_encoded_secret]"
    assert trace.body_record["storage"] == "redacted"
    assert trace.body == raw  # Evidence cleanup never changes the parser input.


def test_http_error_secondary_read_and_close_failures_preserve_http_error(tmp_path, monkeypatch):
    class BrokenBody(io.BytesIO):
        def read1(self, size): raise TimeoutError()
        def close(self): raise OSError()
    body = BrokenBody()
    error = urllib.error.HTTPError("https://example.test/v1", 429, "limited", {}, body)
    def fail(*a, **k): raise error
    monkeypatch.setattr(llm.urllib.request, "urlopen", fail)
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("https://example.test/v1", {}, {}, diag) is None
    detail, _ = archive(diag)
    assert diag["reason"] == "transport:HTTPError" and detail["http_status"] == 429
    assert detail["error_body_read_error"] == "TimeoutError"
    assert detail["error_body_close_error"] == "OSError"
    io.BytesIO.close(body)


def test_replay_does_not_borrow_a_previous_request_archive():
    diag = {"transport_detail": {"request_id": "wrong-prior-request"}}
    assert llm.propose_picks({}, diag, reuse_text='{"buys":[],"sells":[]}') is not None
    assert "transport_detail" not in llm.persistable_response_evidence(diag)


def test_already_initialised_stdlib_opener_is_traced_without_global_changes(tmp_path, monkeypatch):
    existing = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    existing.addheaders = [("User-Agent", "unchanged-agent")]
    monkeypatch.setattr(urllib.request, "_opener", existing)
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    monkeypatch.setattr(transport.socket, "socket", lambda *a: FakeSocket(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"))
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("http://example.test/v1", {}, {}, diag) == {}
    detail, _ = archive(diag)
    assert urllib.request._opener is existing and existing.addheaders == [("User-Agent", "unchanged-agent")]
    assert detail["instrumentation"] == "per_request_stdlib_connections"
    assert [p["phase"] for p in detail["phases"]][:4] == ["dns", "tcp", "request_write", "response_headers"]


def test_cross_origin_redirect_does_not_send_a_second_request(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(urllib.request.ProxyHandler({})))
    resolutions = []
    def resolve(host, *args):
        resolutions.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(transport.socket, "socket", lambda *a: FakeSocket(
        b"HTTP/1.1 302 Found\r\nLocation: http://other.test/\r\nContent-Length: 0\r\n\r\n"))
    diag = {}
    with transport.capture_requests(tmp_path):
        assert llm._post_json("http://example.test/v1", {}, {"Authorization": "secret"}, diag) is None
    detail, _ = archive(diag)
    assert resolutions == ["example.test"]
    assert detail["redirect_blocked"] == "cross_origin" and detail["http_status"] == 302


def test_proxy_connect_failure_has_its_own_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))])
    monkeypatch.setattr(transport.socket, "socket", lambda *a: FakeSocket(
        b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"))
    with transport.capture_requests(tmp_path):
        trace = transport.Exchange("https://example.test/v1", {}, {}, 5)
        connection = transport.TracedHTTPConnection("proxy.test", trace=trace)
        connection.set_tunnel("example.test", port=443)
        with pytest.raises(OSError):
            connection.request("POST", "/v1", body=b"{}")
    assert trace.detail["failure_phase"] == "proxy_connect"
    assert [row["target"] for row in trace.detail["phases"] if "target" in row] == ["proxy", "proxy"]
    assert trace.detail["request_written"] is False
