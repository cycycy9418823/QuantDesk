"""A0 provider-degradation classification hardening (2026-09-11).

On 2026-09-11 the A0 snapshot day ended 0 complete / 14 partial / 34 missing.
Only the 14:50 and 14:55 attempt records carry full per-source failure detail
(Sina ``HTTP 456`` and an HTML body instead of JSON, Eastmoney closing the
connection); the remaining 32 kept only a source name and an exception type.
The root cause of the day's partial coverage is therefore not proven, and this
file makes no such claim.

What it does pin is the classification of whatever failure does occur, so the
degradation record distinguishes "the provider refused / throttled us" from
"our code is broken":

* only an explicit ``429`` proves rate limiting; ``401/403`` is
  ``access_denied`` (refused, reason unknown); a non-standard code such as
  ``456`` keeps its raw status but is not generalised into rate limiting;
* the real ``akshare.utils.demjson`` HTML-parse error (no ``.doc``) still yields
  ``response_format == "html_or_non_json"``, while the deterministic cases need
  no AkShare at all.

Which providers run, and the request format they use, are unchanged.  No test
contacts the network; the AkShare-dependent sample is skipped (never passed)
when the optional dependency is absent.
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture(autouse=True)
def _legacy_hq_unavailable(monkeypatch):
    """These existing cases exercise the original providers with HQ unavailable."""
    from quantdesk import snapshot_history

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture_hq_unavailable")

    monkeypatch.setattr(snapshot_history, "_sina_hq_fetch", unavailable)
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    HTTPError,
    ReadTimeout,
)


def _history():
    return importlib.import_module("quantdesk.snapshot_history")


def _http_error(status: int, text: str = "provider rejected") -> HTTPError:
    import requests

    response = requests.Response()
    response.status_code = status
    response._content = text.encode("utf-8")
    return HTTPError(f"{status} Client Error: for url: https://provider.example", response=response)


def _html_json_error(body: str = "<html><body>blocked</body></html>") -> json.JSONDecodeError:
    return json.JSONDecodeError("Expecting value", body, 0)


def _real_demjson():
    """Load the real parser from the active interpreter, or skip honestly.

    AkShare is an optional dependency, so this sample is skipped - never
    passed - when it is unavailable; the deterministic classification tests
    above stay independent of it.
    """

    return pytest.importorskip("akshare.utils.demjson")


# ---------------------------------------------------------------------------
# A0-02: HTTP status classification
# ---------------------------------------------------------------------------


def test_http_status_reads_response_status_code() -> None:
    history = _history()
    assert history._http_status(_http_error(456)) == 456
    assert history._http_status(ReadTimeout("slow")) is None
    assert history._http_status(RequestsConnectionError("aborted")) is None
    assert history._http_status(RuntimeError("plain")) is None


def test_429_is_the_only_proven_rate_limit() -> None:
    history = _history()
    assert history._failure_stage(_http_error(429)) == "rate_limited"


def test_403_is_access_denied_not_rate_limited() -> None:
    history = _history()
    error = _http_error(403)
    assert history._http_status(error) == 403
    assert history._failure_stage(error) == "access_denied"


def test_nonstandard_status_keeps_its_code_without_overclaiming() -> None:
    # Sina's 456 is non-standard: it is not generalised into rate limiting, but
    # the raw status is preserved in the evidence.
    history = _history()
    error = _http_error(456)
    stage = history._failure_stage(error)
    assert stage not in {"rate_limited", "access_denied"}
    assert stage == "request"


def test_transport_and_parse_classification_is_unchanged() -> None:
    history = _history()
    assert history._failure_stage(ReadTimeout("slow")) == "timeout"
    assert history._failure_stage(RequestsConnectionError("aborted")) == "connect"
    assert history._failure_stage(_html_json_error()) == "parse"


# ---------------------------------------------------------------------------
# A0-01: response-format evidence for real parser exceptions
# ---------------------------------------------------------------------------


def test_response_format_hint_flags_stdlib_html_body() -> None:
    history = _history()
    assert history._response_format_hint(_html_json_error()) == "html_or_non_json"
    assert history._response_format_hint(json.JSONDecodeError("Expecting value", '{"a": 1}', 0)) == ""
    assert history._response_format_hint(ReadTimeout("slow")) == ""


def test_response_format_hint_flags_real_akshare_demjson_html() -> None:
    history = _history()
    demjson = _real_demjson()

    def fake_provider() -> object:
        return demjson.decode("<html><body>upstream response</body></html>")

    _, evidence = history._provider_stage("sina.spot_ak", fake_provider)
    assert evidence["error_type"] == "JSONDecodeError"
    assert evidence["stage"] == "parse"
    assert evidence["response_format"] == "html_or_non_json"


def test_unrecognized_error_has_no_invented_evidence() -> None:
    history = _history()
    assert history._http_status(RuntimeError("unknown upstream failure")) is None
    assert history._response_format_hint(RuntimeError("unknown")) == ""


def test_provider_stage_records_status_and_format() -> None:
    history = _history()
    _, throttled = history._provider_stage("sina.spot_direct", lambda: (_ for _ in ()).throw(_http_error(429)))
    assert throttled["stage"] == "rate_limited"
    assert throttled["http_status"] == 429

    _, denied = history._provider_stage("sina.spot_direct", lambda: (_ for _ in ()).throw(_http_error(403)))
    assert denied["stage"] == "access_denied"
    assert denied["http_status"] == 403

    _, html = history._provider_stage("sina.spot_ak", lambda: (_ for _ in ()).throw(_html_json_error()))
    assert html["response_format"] == "html_or_non_json"
    assert "http_status" not in html

    _, timeout = history._provider_stage("sina.spot_direct", lambda: (_ for _ in ()).throw(ReadTimeout("slow")))
    assert timeout["stage"] == "timeout"
    assert "http_status" not in timeout
    assert "response_format" not in timeout


def test_unbounded_chain_surfaces_rate_limit_without_dropping_original_sources(monkeypatch) -> None:
    """Original sources and HQ all run; throttling remains visible."""

    history = _history()

    def throttled() -> object:
        raise _http_error(429)

    def html() -> object:
        raise _html_json_error()

    def aborted() -> object:
        raise RequestsConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(history, "_sina_direct_fetch", throttled)
    monkeypatch.setattr(history, "_akshare_provider", lambda name: html() if name == "fetch_spot_sina" else aborted())

    with pytest.raises(history.SnapshotProviderError) as captured:
        history.default_network_fetch(symbols=["600000"], budget_seconds=None)

    evidence = {item["source"]: item for item in captured.value.provider_errors}
    assert set(evidence) == {"sina.spot_direct", "sina.hq_batch", "eastmoney.spot_ak", "sina.spot_ak"}
    assert evidence["sina.spot_direct"]["stage"] == "rate_limited"
    assert evidence["sina.spot_direct"]["http_status"] == 429
    assert evidence["eastmoney.spot_ak"]["stage"] == "connect"
    assert evidence["sina.spot_ak"]["response_format"] == "html_or_non_json"
