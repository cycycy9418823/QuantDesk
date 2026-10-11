"""Offline regression coverage for uncertain notification delivery."""
import json
import socket
from pathlib import Path
from urllib.error import URLError

import pytest

from quantdesk import data_health_alert_delivery as alerts
from quantdesk.notifications import server_chan as chan


def health(keys=("a",), day="2026-09-30", severity="warning", resolved=False):
    rows = [{"dedup_key": key, "severity": severity, "scope": "research",
             "reason_code": key} for key in keys]
    return {"scope_health": {"target_date": day},
            "data_alerts": {"current": [] if resolved else rows,
                            "resolved": rows if resolved else []}}


def dispatch(root, monkeypatch, sender, payload=None, loader=None, **options):
    monkeypatch.setenv(alerts.PUSH_ENV, "1")
    return alerts.dispatch_data_health_alerts(
        root, health_payload=payload or health(), sender=sender,
        _sendkey_loader=loader or (lambda: "SCT_FAKE_T37"),
        network=options.get("network", True), push=True,
        dry_run=options.get("dry_run", False))


def test_unknown_attempt_survives_reload_without_sent_state(tmp_path, monkeypatch):
    calls = []
    keys = []
    sender = lambda **kw: calls.append(kw) or {"status": "ambiguous"}
    first = dispatch(tmp_path, monkeypatch, sender, loader=lambda: keys.append(1) or "SCT_FAKE_T37")
    second = dispatch(tmp_path, monkeypatch, sender, loader=lambda: keys.append(1) or "SCT_FAKE_T37")
    assert first["status"] == second["status"] == "ambiguous"
    assert len(calls) == len(keys) == 1
    assert second["external_sent_count"] == 0
    assert not (tmp_path / alerts.STATE_RELATIVE).exists()


def test_unknown_event_is_not_resent_with_a_new_event(tmp_path, monkeypatch):
    bodies = []
    def sender(**kw):
        bodies.append(kw["body"])
        return {"status": "ambiguous" if len(bodies) == 1 else "sent"}
    dispatch(tmp_path, monkeypatch, sender)
    result = dispatch(tmp_path, monkeypatch, sender, health(("a", "b")))
    assert result["status"] == "sent"
    assert result["current_alert_keys"] == ["b"]
    assert len(bodies) == 2 and " | a" not in bodies[1]


@pytest.mark.parametrize("payload", [health(day="2026-10-01"), health(severity="critical")])
def test_new_day_or_severity_is_a_new_event(tmp_path, monkeypatch, payload):
    calls = []
    sender = lambda **kw: calls.append(1) or {"status": "ambiguous"}
    dispatch(tmp_path, monkeypatch, sender)
    dispatch(tmp_path, monkeypatch, sender, payload)
    assert len(calls) == 2


def test_unknown_recovery_does_not_repeat(tmp_path, monkeypatch):
    calls = []
    def sender(**kw):
        calls.append(1)
        return {"status": "sent" if len(calls) == 1 else "ambiguous"}
    dispatch(tmp_path, monkeypatch, sender)
    recovery = health(resolved=True, severity="info")
    dispatch(tmp_path, monkeypatch, sender, recovery)
    again = dispatch(tmp_path, monkeypatch, sender, recovery)
    assert again["status"] == "ambiguous" and len(calls) == 2


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_definite_non_delivery_can_retry(tmp_path, monkeypatch, status):
    calls = []
    sender = lambda **kw: calls.append(1) or {"status": status}
    dispatch(tmp_path, monkeypatch, sender)
    dispatch(tmp_path, monkeypatch, sender)
    assert len(calls) == 2


def test_dry_run_creates_no_attempt(tmp_path, monkeypatch):
    dispatch(tmp_path, monkeypatch, lambda **kw: pytest.fail("sender called"), dry_run=True)
    assert not list((tmp_path / "state").glob("*.json"))


def test_cannot_record_attempt_never_calls_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "atomic_write_json", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    calls = []
    result = dispatch(tmp_path, monkeypatch, lambda **kw: calls.append(1) or {"status": "ambiguous"})
    assert result["status"] != "sent" and calls == []


def test_receipt_write_failure_preserves_unknown_guard(tmp_path, monkeypatch):
    original = alerts.atomic_write_json
    def write(path, data):
        if Path(path).is_relative_to(tmp_path / alerts.RECEIPT_RELATIVE):
            raise OSError("receipt unavailable")
        return original(path, data)
    monkeypatch.setattr(alerts, "atomic_write_json", write)
    calls = []
    sender = lambda **kw: calls.append(1) or {"status": "ambiguous"}
    dispatch(tmp_path, monkeypatch, sender)
    monkeypatch.setattr(alerts, "atomic_write_json", original)
    second = dispatch(tmp_path, monkeypatch, sender)
    assert second["status"] == "ambiguous" and len(calls) == 1


class Response:
    def __init__(self, error, on_exit=False):
        self.error, self.on_exit = error, on_exit
    def __enter__(self):
        return self
    def __exit__(self, *args):
        if self.on_exit:
            raise self.error
    def read(self, size):
        if not self.on_exit:
            raise self.error
        return b'{"code":0}'
    def getcode(self):
        return 200


def delivery():
    return chan.deliver_server_chan(title="test", body="synthetic", sendkey="SCT_FAKE_T37",
                                    enabled=True, channel_enabled=True, network=True,
                                    push=True, dry_run=False)


@pytest.mark.parametrize("error", [ConnectionResetError("reset"), OSError("read")])
@pytest.mark.parametrize("on_exit", [False, True])
def test_response_read_or_close_failure_is_unknown(monkeypatch, error, on_exit):
    class Opener:
        def open(self, *args, **kwargs):
            return Response(error, on_exit)
    monkeypatch.setattr(chan, "build_opener", lambda *args: Opener())
    result = delivery()
    assert result["status"] == "ambiguous" and result["attempted"] is True


@pytest.mark.parametrize("error", [socket.gaierror(-2, "dns"), ConnectionRefusedError("refused")])
def test_pre_response_connection_failures_remain_failed(monkeypatch, error):
    class Opener:
        def open(self, *args, **kwargs):
            raise URLError(error)
    monkeypatch.setattr(chan, "build_opener", lambda *args: Opener())
    assert delivery()["status"] == "failed"


def test_real_transport_classification_drives_alert_suppression(tmp_path, monkeypatch):
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(1)
            return Response(ConnectionResetError("SCT_FAKE_T37"))
    monkeypatch.setattr(chan, "build_opener", lambda *args: Opener())
    first = dispatch(tmp_path, monkeypatch, chan.deliver_server_chan)
    second = dispatch(tmp_path, monkeypatch, chan.deliver_server_chan)
    assert first["status"] == second["status"] == "ambiguous"
    assert len(calls) == 1
    for path in tmp_path.rglob("*.json"):
        assert "SCT_FAKE_T37" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("keys,day,expected", [(("a",), "2026-09-30", []),
                                            (("a", "b"), "2026-09-30", ["b"]),
                                            (("a",), "2026-10-01", ["a"])])
def test_legacy_unknown_receipt_suppresses_only_same_day_keys(tmp_path, monkeypatch, keys, day, expected):
    directory = tmp_path / alerts.RECEIPT_RELATIVE
    directory.mkdir(parents=True)
    (directory / "legacy.json").write_text(json.dumps({
        "status": "ambiguous", "target_date": "2026-09-30", "event_keys": ["a"]}), encoding="utf-8")
    calls = []
    result = dispatch(tmp_path, monkeypatch,
                      lambda **kw: calls.append(kw) or {"status": "sent"}, health(keys, day))
    assert result["current_alert_keys"] == expected
    assert len(calls) == bool(expected)
    assert result["status"] == ("sent" if expected else "ambiguous")
