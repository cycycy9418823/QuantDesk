"""Per-request HTTP evidence. No model policy, retries, or global socket hooks."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

CAPTURE_LIMIT = 512 * 1024
_DIRECTORY: ContextVar[Path | None] = ContextVar("llm_capture_directory", default=None)
_STDLIB_URLOPEN = urllib.request.urlopen
PHASES = frozenset({"dns", "tcp", "proxy_connect", "tls", "request_write",
                   "response_headers", "response_body", "utf8_decode", "outer_json",
                   "request_uninstrumented"})
_PRIVATE_FIELDS = {"reasoning_content", "reasoning", "reasoning_details", "thinking", "signature", "authorization",
                   "api_key", "access_token", "auth_token", "cookie", "set-cookie"}


def _unescape_view(text: str) -> str:
    """Detection view only; never presented as the original response bytes."""
    for _ in range(4):
        decoded = re.sub(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])',
                         lambda match: json.loads('"' + match.group() + '"'), text)
        if decoded == text:
            break
        text = decoded
    return text


@contextmanager
def capture_requests(directory: Path) -> Iterator[None]:
    """Place this caller's evidence in its own directory; never changes another thread."""
    token = _DIRECTORY.set(Path(directory))
    try:
        yield
    finally:
        _DIRECTORY.reset(token)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


class Exchange:
    """One actual HTTP exchange, including durable progress before blocking operations."""

    def __init__(self, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float):
        parsed = urlsplit(url)
        self.secrets = [str(value) for key, value in headers.items()
                        if key.lower() in {"authorization", "x-api-key", "api-key", "cookie"}]
        self.secrets += [value.split(" ", 1)[-1] for value in self.secrets]
        self.secrets += [value for _, value in parse_qsl(parsed.query) if value]
        self.secrets += [value for value in (parsed.username, parsed.password) if value]
        stamp = datetime.now().astimezone()
        request_id = stamp.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:12]
        directory = _DIRECTORY.get()
        self.path = directory / (request_id + ".json") if directory is not None else None
        self.detail: dict[str, Any] = {
            "schema": "llm_transport_evidence_v1", "request_id": request_id,
            "started_at": stamp.isoformat(), "endpoint_host": parsed.hostname or "",
            "requested_model": str(body.get("model") or ""), "socket_timeout_seconds": timeout,
            "request_body_sha256": hashlib.sha256(json.dumps(body).encode("utf-8")).hexdigest(),
            "request_written": False, "response_headers_received": False,
            "response_complete": False, "received_bytes": 0, "phases": [],
            "capture_path": str(self.path) if self.path else None,
            "capture_status": "not_configured" if self.path is None else "pending",
        }
        self.body_record: dict[str, Any] = {"storage": "not_received"}
        self.body = b""
        self.checkpoint()

    def checkpoint(self) -> None:
        if self.path is None:
            return
        try:
            # Written is only set in memory after the atomic replacement returns.
            detail = {**self.detail, "capture_status": "written"}
            _atomic_json(self.path, {"transport_detail": detail, "http_body": self.body_record})
        except (OSError, ValueError, TypeError) as exc:
            self.detail["capture_status"] = "write_failed"
            self.detail["capture_error_type"] = type(exc).__name__
        else:
            self.detail["capture_status"] = "written"
            self.detail.pop("capture_error_type", None)

    @contextmanager
    def phase(self, name: str, **extra: Any) -> Iterator[None]:
        assert name in PHASES
        row = {"phase": name, "started_at": datetime.now().astimezone().isoformat(),
               "outcome": "running", **extra}
        self.detail["phases"].append(row)
        self.detail["active_phase"] = name
        self.checkpoint()
        started = time.perf_counter()
        try:
            yield
        except BaseException as exc:
            row.update(outcome="failed", error_type=type(exc).__name__)
            self.detail.setdefault("failure_phase", name)
            raise
        else:
            row["outcome"] = "completed"
            if self.detail.get("failure_phase") == name:
                self.detail.pop("failure_phase", None)
        finally:
            row["elapsed_ms"] = max(0, int((time.perf_counter() - started) * 1000))
            self.checkpoint()

    def _scrub_text(self, text: str, *, partial: bool = False) -> str:
        view = _unescape_view(text)
        if view != text and (any(secret and secret in view for secret in self.secrets)
                             or re.search(r"(?i)(?:Bearer\s+|nvapi-|sk-)[A-Za-z0-9._-]+", view)):
            return "[redacted_encoded_secret]"
        for secret in sorted(set(self.secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[redacted]")
                if partial:
                    # Do not leave the beginning of an echoed key after a
                    # connection ends in the middle of that key.
                    for size in range(min(len(secret) - 1, len(text)), 3, -1):
                        if text.endswith(secret[:size]):
                            text = text[:-size] + "[redacted_partial]"
                            break
        return re.sub(r"(?i)(?:Bearer\s+|nvapi-|sk-)[A-Za-z0-9._-]+", "[redacted]", text)

    def _redact(self, raw: bytes, *, complete: bool) -> tuple[bytes | None, str]:
        # Inspect all received bytes before applying the retention cap. Cutting
        # first would leave an unrecognisable prefix of a credential at the cap.
        text = raw.decode("utf-8", errors="surrogateescape")
        changed = False
        try:
            value = json.loads(text)
        except (ValueError, UnicodeError):
            # Decode escape tokens only to detect secrets/private fields; never
            # call this decoded view the original response. This also handles a
            # Unicode-escaped field name in an unfinished JSON object.
            visible = _unescape_view(text)
            private = "|".join(re.escape(field) for field in _PRIVATE_FIELDS)
            if (re.search(r'"(?:' + private + r')"\s*:', visible, re.IGNORECASE)
                    or re.search(r'"type"\s*:\s*"(?:thinking|reasoning|redacted_thinking)"', visible)
                    or "<think>" in visible.lower()):
                return None, "suppressed_private_fields"
            scrubbed = self._scrub_text(text, partial=not complete)
            if self._scrub_text(visible, partial=not complete) != visible and visible != text:
                return None, "suppressed_encoded_secret"
            return (scrubbed.encode("utf-8", errors="surrogateescape"), "redacted"
                    if scrubbed != text else "original_bytes")

        def clean(item: Any) -> Any:
            nonlocal changed
            if isinstance(item, dict):
                if isinstance(item.get("type"), str) and item["type"] in {"thinking", "reasoning", "redacted_thinking"}:
                    changed = True
                    return {"type": "redacted_private_block"}
                result = {}
                for key, val in item.items():
                    if _unescape_view(key).lower() in _PRIVATE_FIELDS:
                        changed = True
                        continue
                    safe_key = self._scrub_text(key)
                    changed |= safe_key != key
                    result[safe_key] = clean(val)
                return result
            if isinstance(item, list):
                return [clean(val) for val in item]
            if isinstance(item, str):
                scrubbed = self._scrub_text(item)
                if _unescape_view(item) != item and "<think>" in _unescape_view(item).lower():
                    scrubbed = "[redacted_private_block]"
                scrubbed = re.sub(r"(?is)<think>.*?(?:</think>|$)", "[redacted_private_block]", scrubbed)
                changed |= scrubbed != item
                return scrubbed
            return item

        clean_value = clean(value)
        return (json.dumps(clean_value, ensure_ascii=True).encode("utf-8"), "redacted") if changed else (raw, "original_bytes")

    def received(self, raw: bytes, *, complete: bool) -> None:
        self.body = raw
        self.detail.update(received_bytes=len(raw), response_complete=complete)
        retained, storage = self._redact(raw, complete=complete)
        truncated = len(raw) > CAPTURE_LIMIT or (retained is not None and len(retained) > CAPTURE_LIMIT)
        if retained is not None:
            retained = retained[:CAPTURE_LIMIT]
        self.body_record = {
            "storage": storage, "received_bytes": len(raw),
            "received_sha256": hashlib.sha256(raw).hexdigest(),
            "response_complete": complete, "capture_truncated": truncated,
            "retained_bytes": len(retained) if retained is not None else 0,
            "body_base64": base64.b64encode(retained).decode("ascii") if retained is not None else None,
            "byte_exact": storage == "original_bytes" and len(raw) <= CAPTURE_LIMIT,
        }
        self.checkpoint()

    def headers_received(self, response: Any) -> None:
        self.detail["response_headers_received"] = True
        status = getattr(response, "status", getattr(response, "code", None))
        if isinstance(status, int):
            self.detail["http_status"] = status
        headers = getattr(response, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            content_type = headers.get("Content-Type")
            if content_type and self._scrub_text(str(content_type)) == str(content_type):
                self.detail["response_content_type"] = str(content_type).split(";", 1)[0][:80]
            for key in ("x-request-id", "x-correlation-id", "nvcf-reqid"):
                value = str(headers.get(key) or "")
                if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) and self._scrub_text(value) == value:
                    self.detail.setdefault("provider_request_ids", {})[key] = value
        self.checkpoint()

    def read_body(self, response: Any) -> bytes:
        chunks: list[bytes] = []
        with self.phase("response_body"):
            try:
                reader = getattr(response, "read1", None)
                if callable(reader):
                    while True:
                        chunk = reader(65536)
                        if not chunk:
                            remaining = getattr(response, "length", None)
                            if isinstance(remaining, int) and remaining > 0:
                                raise http.client.IncompleteRead(b"", remaining)
                            break
                        chunks.append(chunk)
                        self.received(b"".join(chunks), complete=False)
                else:
                    chunks.append(response.read())
            except http.client.IncompleteRead as exc:
                if exc.partial:
                    chunks.append(exc.partial)
                self.received(b"".join(chunks), complete=False)
                raise
            except BaseException:
                self.received(b"".join(chunks), complete=False)
                raise
            self.received(b"".join(chunks), complete=True)
        return self.body

    def finish(self) -> None:
        self.detail["finished_at"] = datetime.now().astimezone().isoformat()
        self.detail.pop("active_phase", None)
        self.checkpoint()


class _ConnectionTrace:
    def __init__(self, *args: Any, trace: Exchange, proxy_connection: bool = False, **kwargs: Any):
        self.trace = trace
        self.proxy_connection = proxy_connection
        super().__init__(*args, **kwargs)
        self._create_connection = self._connect_socket

    def _connect_socket(self, address: tuple[str, int], timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
                        source_address: Any = None, **kwargs: Any) -> socket.socket:
        target = "proxy" if self._tunnel_host or self.proxy_connection else "origin"
        with self.trace.phase("dns", target=target):
            addresses = socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM)
        last_error: OSError | None = None
        for index, (family, kind, proto, _name, sockaddr) in enumerate(addresses):
            connection = None
            try:
                with self.trace.phase("tcp", target=target, address_attempt=index + 1):
                    connection = socket.socket(family, kind, proto)
                    if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                        connection.settimeout(timeout)
                    if source_address:
                        connection.bind(source_address)
                    connection.connect(sockaddr)
                return connection
            except OSError as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
        if last_error is not None:
            raise last_error
        raise OSError("getaddrinfo returned no addresses")

    def _tunnel(self) -> None:
        with self.trace.phase("proxy_connect"):
            return super()._tunnel()

    def _send_output(self, *args: Any, **kwargs: Any) -> None:
        if self.sock is None:
            self.connect()
        with self.trace.phase("request_write"):
            return super()._send_output(*args, **kwargs)

    def request(self, *args: Any, **kwargs: Any) -> None:
        result = super().request(*args, **kwargs)
        self.trace.detail["request_written"] = True
        self.trace.checkpoint()
        return result

    def getresponse(self) -> http.client.HTTPResponse:
        with self.trace.phase("response_headers"):
            response = super().getresponse()
            self.trace.headers_received(response)
            return response


class TracedHTTPConnection(_ConnectionTrace, http.client.HTTPConnection):
    pass


class TracedHTTPSConnection(_ConnectionTrace, http.client.HTTPSConnection):
    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        hostname = self._tunnel_host or self.host
        with self.trace.phase("tls"):
            self.sock = self._context.wrap_socket(self.sock, server_hostname=hostname)


def _standard_opener(opener: Any) -> bool:
    """Recognise default handlers without modifying or copying their parents."""
    if type(opener) is not urllib.request.OpenerDirector:
        return False
    required = {urllib.request.UnknownHandler, urllib.request.HTTPHandler,
                urllib.request.HTTPDefaultErrorHandler, urllib.request.HTTPRedirectHandler,
                urllib.request.FTPHandler, urllib.request.FileHandler,
                urllib.request.DataHandler, urllib.request.HTTPSHandler,
                urllib.request.HTTPErrorProcessor}
    types = [type(handler) for handler in opener.handlers]
    if set(types) not in (required, required | {urllib.request.ProxyHandler}) or len(types) != len(set(types)):
        return False
    if set(vars(opener)) != {"addheaders", "handlers", "handle_open", "handle_error", "process_response", "process_request"}:
        return False
    for handler in opener.handlers:
        allowed = {"parent"}
        if type(handler) is urllib.request.HTTPHandler:
            allowed.add("_debuglevel")
        elif type(handler) is urllib.request.HTTPSHandler:
            allowed.update({"_debuglevel", "_context", "_check_hostname"})
        elif type(handler) is urllib.request.ProxyHandler:
            allowed.add("proxies")
            allowed.update(protocol + "_open" for protocol in handler.proxies)
        if set(vars(handler)) - allowed:
            return False
    return True


def open_response(request: urllib.request.Request, timeout: float, trace: Exchange,
                  *, open_func: Callable[..., Any]) -> Any:
    """Respect custom openers; trace both fresh and already-used default ones."""
    existing = getattr(urllib.request, "_opener", None)
    if (open_func is not _STDLIB_URLOPEN
            or getattr(open_func, "__module__", "") != "urllib.request"
            or (existing is not None and not _standard_opener(existing))):
        trace.detail["instrumentation"] = "custom_opener; connection_phases_not_observed"
        trace.detail["request_written"] = None
        trace.detail["response_headers_received"] = None
        with trace.phase("request_uninstrumented"):
            return open_func(request, timeout=timeout)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> Any:
            factory = partial(TracedHTTPConnection, trace=trace, proxy_connection=req.has_proxy())
            return self.do_open(factory, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request) -> Any:
            factory = partial(TracedHTTPSConnection, trace=trace, proxy_connection=bool(req.has_proxy() or req._tunnel_host))
            options = {"context": self._context}
            if hasattr(self, "_check_hostname"):
                options["check_hostname"] = self._check_hostname
            return self.do_open(factory, req, **options)

    class RedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req: urllib.request.Request, fp: Any, code: int,
                             msg: str, headers: Any, newurl: str) -> Any:
            def origin(url: str) -> tuple[str, str | None, int | None]:
                parsed = urlsplit(url)
                return parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            if origin(req.full_url) != origin(newurl):
                trace.detail["redirect_blocked"] = "cross_origin"
                raise urllib.error.HTTPError(req.full_url, code, "cross_origin_redirect_blocked", headers, fp)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    trace.detail["instrumentation"] = "per_request_stdlib_connections"
    http_options: dict[str, Any] = {}
    https_options: dict[str, Any] = {}
    handlers: list[Any] = []
    if existing is not None:
        proxies = {}
        for handler in existing.handlers:
            if type(handler) is urllib.request.ProxyHandler:
                proxies = dict(handler.proxies)
            elif type(handler) is urllib.request.HTTPHandler:
                http_options["debuglevel"] = handler._debuglevel
            elif type(handler) is urllib.request.HTTPSHandler:
                https_options.update(debuglevel=handler._debuglevel, context=handler._context)
                if hasattr(handler, "_check_hostname"):
                    https_options["check_hostname"] = handler._check_hostname
        # An existing opener without proxies stays without proxies, even if the
        # process environment changed after its first ordinary urlopen call.
        handlers.append(urllib.request.ProxyHandler(proxies))
    opener = urllib.request.build_opener(HTTPHandler(**http_options), HTTPSHandler(**https_options),
                                         RedirectHandler(), *handlers)
    if existing is not None:
        opener.addheaders = list(existing.addheaders)
    return opener.open(request, timeout=timeout)
