from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from quantdesk.notifications.redaction import redact_payload


SERVER_CHAN_HOST = "sctapi.ftqq.com"
MAX_TITLE_CHARS = 64
MAX_BODY_CHARS = 8000
MAX_RESPONSE_BYTES = 65536


class AmbiguousDeliveryError(RuntimeError):
    """The POST may have reached the provider but no result was observed."""


class ServerChanTransport(Protocol):
    def __call__(
        self, *, url: str, form: dict[str, str], timeout_seconds: float
    ) -> Mapping[str, Any]: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise HTTPError(newurl, code, "server_chan_redirect_rejected", headers, fp)


def _transport_diagnostics(error: BaseException) -> dict[str, Any]:
    """Return bounded, secret-free diagnostics for a failed connection."""
    current: BaseException = error
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        cause = getattr(current, "__cause__", None)
        reason = getattr(current, "reason", None)
        if isinstance(cause, BaseException):
            current = cause
            continue
        if isinstance(reason, BaseException):
            current = reason
            continue
        break
    name = type(current).__name__
    category = {
        "gaierror": "dns_resolution_failed",
        "ConnectionRefusedError": "connection_refused",
        "ConnectionResetError": "connection_reset",
        "PermissionError": "permission_denied",
        "TimeoutError": "timeout",
        "SSLError": "tls_failed",
    }.get(name, "transport_error")
    result: dict[str, Any] = {"transport_error_type": name, "transport_error_category": category}
    errno = getattr(current, "errno", None)
    if isinstance(errno, int):
        result["transport_error_errno"] = errno
    return result


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SERVER_CHAN_HOST
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.endswith(".send")
        or parsed.query
        or parsed.fragment
    ):
        raise ConnectionError("server_chan_transport_target_rejected")


def _urllib_transport(
    *, url: str, form: dict[str, str], timeout_seconds: float
) -> dict[str, Any]:
    _validate_url(url)
    request = Request(
        url,
        data=urlencode(form).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "QuantDesk-Morning-Brief/1",
        },
        method="POST",
    )
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=float(timeout_seconds)) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                body = b""
            return {
                "http_status": int(response.getcode()),
                "body": body,
            }
    except HTTPError as exc:
        body = b""
        try:
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            body = b""
        if len(body) > MAX_RESPONSE_BYTES:
            body = b""
        return {"http_status": int(exc.code), "body": body}
    except (socket.timeout, TimeoutError) as exc:
        raise AmbiguousDeliveryError("server_chan_response_timeout") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise AmbiguousDeliveryError("server_chan_response_timeout") from exc
        raise ConnectionError("server_chan_transport_failed") from exc
    except OSError as exc:
        raise ConnectionError("server_chan_transport_failed") from exc


def _result(
    *,
    status: str,
    reason_code: str,
    attempted: bool = False,
    http_status: int | None = None,
    provider_code: int | str | None = None,
    transport_error_type: str | None = None,
    transport_error_category: str | None = None,
    transport_error_errno: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now().astimezone()
    result = {
        "channel": "server_chan",
        "status": status,
        "reason_code": reason_code,
        "attempted": bool(attempted),
        "http_status": http_status,
        "provider_code": provider_code,
        "observed_at": current.isoformat(timespec="seconds"),
    }
    if transport_error_type:
        result["transport_error_type"] = transport_error_type
    if transport_error_category:
        result["transport_error_category"] = transport_error_category
    if isinstance(transport_error_errno, int):
        result["transport_error_errno"] = transport_error_errno
    return result


def _message_is_valid(title: str, body: str) -> bool:
    return bool(
        title.strip()
        and body.strip()
        and len(title) <= MAX_TITLE_CHARS
        and len(body) <= MAX_BODY_CHARS
        and "\x00" not in title
        and "\x00" not in body
    )


def deliver_server_chan(
    *,
    title: str,
    body: str,
    sendkey: str | None,
    enabled: bool,
    channel_enabled: bool,
    network: bool,
    push: bool,
    dry_run: bool,
    timeout_seconds: float = 10.0,
    transport: ServerChanTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    secret = str(sendkey or "")

    def safe(payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(redact_payload(payload, known_secrets=[secret]))

    if not enabled:
        return safe(_result(status="blocked", reason_code="notifications_disabled", now=now))
    if not channel_enabled:
        return safe(_result(status="blocked", reason_code="channel_disabled", now=now))
    if not network:
        return safe(_result(status="blocked", reason_code="network_not_authorized", now=now))
    if not push:
        return safe(_result(status="blocked", reason_code="push_not_authorized", now=now))
    if dry_run:
        return safe(_result(status="dry_run", reason_code="dry_run", now=now))
    if not secret.strip():
        return safe(_result(status="blocked", reason_code="sendkey_missing", now=now))
    if not _message_is_valid(str(title), str(body)):
        return safe(_result(status="blocked", reason_code="message_invalid", now=now))

    sender: Callable[..., Mapping[str, Any]] = transport or _urllib_transport
    url = f"https://{SERVER_CHAN_HOST}/{secret}.send"
    try:
        response = sender(
            url=url,
            form={"title": str(title), "desp": str(body)},
            timeout_seconds=max(1.0, min(float(timeout_seconds), 30.0)),
        )
    except AmbiguousDeliveryError:
        return safe(
            _result(
                status="ambiguous",
                reason_code="ambiguous_delivery",
                attempted=True,
                now=now,
            )
        )
    except (ConnectionError, TimeoutError, OSError) as exc:
        return safe(
            _result(
                status="failed",
                reason_code="transport_failed",
                attempted=True,
                **_transport_diagnostics(exc),
                now=now,
            )
        )
    except Exception as exc:
        return safe(
            _result(
                status="failed",
                reason_code="transport_failed",
                attempted=True,
                **_transport_diagnostics(exc),
                now=now,
            )
        )

    try:
        http_status = int(response.get("http_status"))
    except (TypeError, ValueError):
        return safe(
            _result(
                status="failed",
                reason_code="provider_response_invalid",
                attempted=True,
                now=now,
            )
        )
    if http_status < 200 or http_status >= 300:
        return safe(
            _result(
                status="failed",
                reason_code="provider_http_error",
                attempted=True,
                http_status=http_status,
                now=now,
            )
        )
    raw_body = response.get("body")
    if not isinstance(raw_body, (bytes, bytearray)) or len(raw_body) > MAX_RESPONSE_BYTES:
        return safe(
            _result(
                status="failed",
                reason_code="provider_response_invalid",
                attempted=True,
                http_status=http_status,
                now=now,
            )
        )
    try:
        provider_payload = json.loads(bytes(raw_body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return safe(
            _result(
                status="failed",
                reason_code="provider_response_invalid",
                attempted=True,
                http_status=http_status,
                now=now,
            )
        )
    if not isinstance(provider_payload, dict) or "code" not in provider_payload:
        return safe(
            _result(
                status="failed",
                reason_code="provider_response_invalid",
                attempted=True,
                http_status=http_status,
                now=now,
            )
        )
    provider_code = provider_payload.get("code")
    if str(provider_code) != "0":
        return safe(
            _result(
                status="failed",
                reason_code="provider_rejected",
                attempted=True,
                http_status=http_status,
                provider_code=provider_code
                if isinstance(provider_code, (int, str))
                else None,
                now=now,
            )
        )
    return safe(
        _result(
            status="sent",
            reason_code="provider_accepted",
            attempted=True,
            http_status=http_status,
            provider_code=0,
            now=now,
        )
    )


__all__ = [
    "AmbiguousDeliveryError",
    "MAX_BODY_CHARS",
    "MAX_TITLE_CHARS",
    "SERVER_CHAN_HOST",
    "ServerChanTransport",
    "deliver_server_chan",
]
