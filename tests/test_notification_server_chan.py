from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

from quantdesk.notifications.redaction import redact_payload, redact_text
from quantdesk.notifications.server_chan import (
    AmbiguousDeliveryError,
    _NoRedirectHandler,
    deliver_server_chan,
)


class Recorder:
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.calls: list[dict] = []
        self.response = response or {
            "http_status": 200,
            "body": b'{"code":0,"message":"success"}',
        }
        self.error = error

    def __call__(self, *, url: str, form: dict[str, str], timeout_seconds: float) -> dict:
        self.calls.append(
            {"url": url, "form": form, "timeout_seconds": timeout_seconds}
        )
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"enabled": False}, "notifications_disabled"),
        ({"channel_enabled": False}, "channel_disabled"),
        ({"network": False}, "network_not_authorized"),
        ({"push": False}, "push_not_authorized"),
        ({"dry_run": True}, "dry_run"),
        ({"sendkey": None}, "sendkey_missing"),
    ],
)
def test_gates_do_not_construct_transport_request(
    overrides: dict, reason_code: str
) -> None:
    recorder = Recorder()
    values = {
        "title": "QuantDesk research brief",
        "body": "Research Only",
        "sendkey": "SCT_FAKE_SECRET",
        "enabled": True,
        "channel_enabled": True,
        "network": True,
        "push": True,
        "dry_run": False,
        "transport": recorder,
    }
    values.update(overrides)

    result = deliver_server_chan(**values)

    assert result["reason_code"] == reason_code
    assert result["status"] == ("dry_run" if reason_code == "dry_run" else "blocked")
    assert recorder.calls == []
    assert "SCT_FAKE_SECRET" not in json.dumps(result, ensure_ascii=False)


def test_redaction_removes_nested_key_url_and_exact_secret() -> None:
    secret = "SCT_FAKE_SECRET"
    value = {
        "sendkey": secret,
        "nested": {"authorization": f"Bearer {secret}"},
        "error": f"https://sctapi.ftqq.com/{secret}.send failed",
    }

    rendered = repr(redact_payload(value, known_secrets=[secret]))

    assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert secret not in redact_text(str(value), known_secrets=[secret])


def test_success_returns_only_bounded_provider_metadata() -> None:
    recorder = Recorder()

    result = deliver_server_chan(
        title="QuantDesk research brief",
        body="Research Only / Not Investment Advice",
        sendkey="SCT_FAKE_SECRET",
        enabled=True,
        channel_enabled=True,
        network=True,
        push=True,
        dry_run=False,
        transport=recorder,
    )

    assert result["status"] == "sent"
    assert result["reason_code"] == "provider_accepted"
    assert result["http_status"] == 200
    assert result["provider_code"] == 0
    assert "body" not in result
    assert "url" not in result
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["form"] == {
        "title": "QuantDesk research brief",
        "desp": "Research Only / Not Investment Advice",
    }
    assert "SCT_FAKE_SECRET" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("response", "status", "reason_code"),
    [
        (
            {"http_status": 500, "body": b'{"code":0}'},
            "failed",
            "provider_http_error",
        ),
        (
            {"http_status": 200, "body": b"not-json"},
            "failed",
            "provider_response_invalid",
        ),
        (
            {"http_status": 200, "body": b'{"code":40001,"message":"bad"}'},
            "failed",
            "provider_rejected",
        ),
    ],
)
def test_provider_failure_matrix(
    response: dict, status: str, reason_code: str
) -> None:
    result = deliver_server_chan(
        title="QuantDesk",
        body="Research Only",
        sendkey="SCT_FAKE_SECRET",
        enabled=True,
        channel_enabled=True,
        network=True,
        push=True,
        dry_run=False,
        transport=Recorder(response=response),
    )

    assert result["status"] == status
    assert result["reason_code"] == reason_code
    assert "SCT_FAKE_SECRET" not in json.dumps(result, ensure_ascii=False)


def test_ambiguous_delivery_is_distinct_and_redacted() -> None:
    result = deliver_server_chan(
        title="QuantDesk",
        body="Research Only",
        sendkey="SCT_FAKE_SECRET",
        enabled=True,
        channel_enabled=True,
        network=True,
        push=True,
        dry_run=False,
        transport=Recorder(
            error=AmbiguousDeliveryError(
                "https://sctapi.ftqq.com/SCT_FAKE_SECRET.send response lost"
            )
        ),
    )

    assert result["status"] == "ambiguous"
    assert result["reason_code"] == "ambiguous_delivery"
    assert "SCT_FAKE_SECRET" not in json.dumps(result, ensure_ascii=False)


def test_connection_failure_is_not_reported_as_sent() -> None:
    result = deliver_server_chan(
        title="QuantDesk",
        body="Research Only",
        sendkey="SCT_FAKE_SECRET",
        enabled=True,
        channel_enabled=True,
        network=True,
        push=True,
        dry_run=False,
        transport=Recorder(error=ConnectionError("connection refused")),
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "transport_failed"


@pytest.mark.parametrize(
    ("title", "body"),
    [("x" * 65, "Research Only"), ("QuantDesk", "x" * 8001), ("", "body")],
)
def test_message_bounds_fail_before_transport(title: str, body: str) -> None:
    recorder = Recorder()

    result = deliver_server_chan(
        title=title,
        body=body,
        sendkey="SCT_FAKE_SECRET",
        enabled=True,
        channel_enabled=True,
        network=True,
        push=True,
        dry_run=False,
        transport=recorder,
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "message_invalid"
    assert recorder.calls == []


def test_redirect_handler_rejects_cross_host_and_same_host_redirects() -> None:
    handler = _NoRedirectHandler()

    with pytest.raises(HTTPError):
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "https://evil.example/steal",
        )
    with pytest.raises(HTTPError):
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "https://sctapi.ftqq.com/another.send",
        )
