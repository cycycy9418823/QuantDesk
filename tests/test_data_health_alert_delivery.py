from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quantdesk.data_health_alert_delivery import dispatch_data_health_alerts


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _health(*, current: list[dict], resolved: list[dict] | None = None) -> dict:
    return {
        "scope_health": {"target_date": "2026-08-28"},
        "data_alerts": {
            "current": current,
            "resolved": resolved or [],
        },
    }


def _alert(
    key: str = "research_pool:coverage_incomplete_missing",
    *,
    severity: str = "critical",
) -> dict:
    return {
        "alert_key": key,
        "dedup_key": key,
        "scope": "research_pool",
        "reason_code": "coverage_incomplete_missing",
        "severity": severity,
        "as_of": "2026-08-28",
        "detail": "研究池缺失 4 项。",
        "affected": {"symbols_sample": ["920167", "920523"]},
        "resolved": False,
    }


def _allow_push(monkeypatch) -> None:
    monkeypatch.setenv("QUANTDESK_DATA_HEALTH_ALERTS_PUSH", "1")


def test_critical_alert_sends_one_redacted_summary_and_records_dedupe(
    tmp_path: Path, monkeypatch
) -> None:
    _allow_push(monkeypatch)
    calls: list[dict] = []

    result = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=lambda **kwargs: calls.append(kwargs)
        or {
            "channel": "server_chan",
            "status": "sent",
            "reason_code": "provider_accepted",
            "http_status": 200,
            "provider_code": 0,
        },
        now=datetime(2026, 8, 29, 20, 35, tzinfo=SHANGHAI),
    )

    assert result["status"] == "sent"
    assert result["external_sent_count"] == 1
    assert len(calls) == 1
    assert "SCT_FAKE_SECRET" not in json.dumps(result, ensure_ascii=False)
    assert "SCT_FAKE_SECRET" not in calls[0]["title"]
    assert "SCT_FAKE_SECRET" not in calls[0]["body"]
    state = json.loads((tmp_path / "state/data_health_alert_delivery.json").read_text(encoding="utf-8"))
    assert state["active"]["research_pool:coverage_incomplete_missing"]["severity"] == "critical"
    receipt = Path(tmp_path / result["receipt_path"])
    assert receipt.is_file()
    assert "SCT_FAKE_SECRET" not in receipt.read_text(encoding="utf-8")


def test_same_alert_is_not_sent_twice_but_recovery_is_sent_once(
    tmp_path: Path, monkeypatch
) -> None:
    _allow_push(monkeypatch)
    calls: list[str] = []

    def sender(**_kwargs) -> dict:
        calls.append("send")
        return {
            "channel": "server_chan",
            "status": "sent",
            "reason_code": "provider_accepted",
            "http_status": 200,
            "provider_code": 0,
        }

    first = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
        now=datetime(2026, 8, 29, 20, 35, tzinfo=SHANGHAI),
    )
    duplicate = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
        now=datetime(2026, 8, 29, 20, 36, tzinfo=SHANGHAI),
    )
    recovery = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(
            current=[],
            resolved=[{**_alert(), "resolved": True, "severity": "info"}],
        ),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=sender,
        now=datetime(2026, 8, 29, 20, 37, tzinfo=SHANGHAI),
    )

    assert first["status"] == "sent"
    assert duplicate["status"] == "skipped_duplicate"
    assert recovery["status"] == "sent"
    assert recovery["resolved_alert_keys"] == ["research_pool:coverage_incomplete_missing"]
    assert calls == ["send", "send"]


def test_missing_opt_in_network_push_or_key_never_calls_sender(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def sender(**_kwargs) -> dict:
        calls.append("sender")
        return {"status": "sent"}

    blocked_env = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: calls.append("key") or "SCT_FAKE_SECRET",
        sender=sender,
    )
    _allow_push(monkeypatch)
    blocked_network = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=False,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: calls.append("key") or "SCT_FAKE_SECRET",
        sender=sender,
    )
    blocked_key = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: calls.append("key") or None,
        sender=sender,
    )

    assert blocked_env["reason_code"] == "data_health_alert_push_not_explicitly_enabled"
    assert blocked_env["status"] == "preview_generated"
    assert blocked_network["reason_code"] == "network_not_authorized"
    assert blocked_key["reason_code"] == "sendkey_missing"
    assert calls == ["key"]


def test_ambiguous_delivery_does_not_advance_dedupe_state(tmp_path: Path, monkeypatch) -> None:
    _allow_push(monkeypatch)
    result = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=lambda **_kwargs: {
            "channel": "server_chan",
            "status": "ambiguous",
            "reason_code": "ambiguous_delivery",
        },
    )

    assert result["status"] == "ambiguous"
    assert not (tmp_path / "state/data_health_alert_delivery.json").exists()


def test_sender_exception_is_a_redacted_failed_receipt_not_an_internal_success(
    tmp_path: Path, monkeypatch
) -> None:
    _allow_push(monkeypatch)

    result = dispatch_data_health_alerts(
        tmp_path,
        health_payload=_health(current=[_alert()]),
        network=True,
        push=True,
        dry_run=False,
        _sendkey_loader=lambda: "SCT_FAKE_SECRET",
        sender=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("SCT_FAKE_SECRET")),
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "sender_failed"
    receipt = Path(tmp_path / result["receipt_path"])
    assert receipt.is_file()
    assert "SCT_FAKE_SECRET" not in receipt.read_text(encoding="utf-8")
    assert not (tmp_path / "state/data_health_alert_delivery.json").exists()
