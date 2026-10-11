"""Gated Server 酱 delivery for verified local data-health alerts.

This module never fetches market data and never runs from an 8000 GET route.
It only summarizes the already-computed A3 alert contract after a writer has
finished a data-health refresh.  A SendKey is loaded only after every local
gate and duplicate check has passed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantdesk.interprocess_lock import interprocess_file_lock
from quantdesk.notifications.redaction import redact_payload
from quantdesk.notifications.server_chan import deliver_server_chan
from quantdesk.utils import atomic_write_json


SHANGHAI = ZoneInfo("Asia/Shanghai")
PUSH_ENV = "QUANTDESK_DATA_HEALTH_ALERTS_PUSH"
STATE_RELATIVE = Path("state/data_health_alert_delivery.json")
ATTEMPTS_RELATIVE = Path("state/data_health_alert_attempts.json")
LOCK_RELATIVE = Path("state/data_health_alert_delivery.lock")
RECEIPT_RELATIVE = Path("results/notifications/data_health")
MAX_ALERTS_IN_BODY = 10


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _safe_path(root: Path, relative: Path) -> Path | None:
    target = (root / relative).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def _read_state(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {"schema_version": "data_health_alert_delivery_v1", "active": {}}, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, "delivery_state_invalid"
    if not isinstance(payload, dict) or not isinstance(payload.get("active", {}), dict):
        return {}, "delivery_state_invalid"
    return payload, ""


def _target_date(health: Mapping[str, Any]) -> str:
    scope_health = health.get("scope_health")
    if isinstance(scope_health, Mapping):
        return _text(scope_health.get("target_date"))[:10]
    return _text(health.get("target_date"))[:10]


def _alert_key(alert: Mapping[str, Any]) -> str:
    return _text(alert.get("dedup_key") or alert.get("alert_key"))


def _event_token(target_date: str, alert: Mapping[str, Any]) -> str:
    return ":".join((target_date, _alert_key(alert), _text(alert.get("severity"))))


def _attempt_key(event: Mapping[str, Any], target: str, active: Mapping[str, Any]) -> str:
    key = _alert_key(event["alert"])
    # Recovery belongs to the confirmed active episode, not every future
    # recovery for the same alert key.
    token = event["token"] if event["kind"] == "active" else (active.get(key) or {}).get("token", "")
    identity = [event["kind"], target, key, token]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_attempts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "data_health_alert_attempts_v1":
        raise ValueError("delivery_attempts_invalid")
    pending = payload.get("pending")
    if not isinstance(pending, dict) or any(not isinstance(value, dict) for value in pending.values()):
        raise ValueError("delivery_attempts_invalid")
    return pending


def _save_attempts(path: Path, pending: Mapping[str, Any]) -> None:
    atomic_write_json(path, {"schema_version": "data_health_alert_attempts_v1", "pending": dict(pending)})


def _legacy_unknown_keys(root: Path, target: str) -> set[str]:
    directory = _safe_path(root, RECEIPT_RELATIVE)
    if directory is None:
        raise ValueError("notification_receipt_path_escape")
    keys: set[str] = set()
    for path in directory.glob("*.json"):
        if not path.resolve().is_relative_to(root):
            raise ValueError("notification_receipt_path_escape")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("delivery_receipt_invalid")
        if receipt.get("status") != "ambiguous" or receipt.get("target_date") != target:
            continue
        if "event_attempt_keys" in receipt:
            # New receipts use the finer journal identity (including severity).
            continue
        event_keys = receipt.get("event_keys")
        if not isinstance(event_keys, list) or any(not isinstance(key, str) for key in event_keys):
            raise ValueError("delivery_receipt_invalid")
        # Legacy receipts did not retain kind/severity: suppress conservatively
        # by target date and alert key without claiming they were sent.
        keys.update(event_keys)
    return keys


def _events(
    health: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], bool]:
    alerts = health.get("data_alerts")
    alerts = alerts if isinstance(alerts, Mapping) else {}
    current = alerts.get("current") if isinstance(alerts.get("current"), list) else []
    resolved = alerts.get("resolved") if isinstance(alerts.get("resolved"), list) else []
    active = state.get("active") if isinstance(state.get("active"), dict) else {}
    target = _target_date(health)
    next_active = {str(key): dict(value) for key, value in active.items() if isinstance(value, dict)}
    events: list[dict[str, Any]] = []
    observed_alert = False
    for raw in current:
        if not isinstance(raw, Mapping) or not _alert_key(raw):
            continue
        observed_alert = True
        key = _alert_key(raw)
        token = _event_token(target, raw)
        previous = next_active.get(key) or {}
        if previous.get("token") != token:
            events.append({"kind": "active", "alert": dict(raw), "token": token})
    for raw in resolved:
        if not isinstance(raw, Mapping) or not _alert_key(raw):
            continue
        observed_alert = True
        key = _alert_key(raw)
        if key in next_active:
            events.append({"kind": "recovery", "alert": dict(raw), "token": ""})
    return events, next_active, observed_alert


def _render(events: list[dict[str, Any]], target_date: str) -> tuple[str, str]:
    critical = sum(
        1
        for event in events
        if _text(event["alert"].get("severity")).lower() == "critical"
    )
    title = f"QuantDesk 数据告警 {target_date or '待核'}"
    if critical:
        title += f" · {critical} 项需处理"
    rows = ["数据健康提醒，仅用于本地研究与纸面模拟。", f"目标交易日：{target_date or '未知'}"]
    for event in events[:MAX_ALERTS_IN_BODY]:
        alert = event["alert"]
        prefix = "恢复" if event["kind"] == "recovery" else _text(alert.get("severity")) or "warning"
        sample = alert.get("affected") if isinstance(alert.get("affected"), Mapping) else {}
        symbols = list(sample.get("symbols_sample") or [])[:3]
        suffix = f"；样本 {','.join(str(item) for item in symbols)}" if symbols else ""
        rows.append(
            f"- {prefix} | {_text(alert.get('scope'))} | {_text(alert.get('reason_code'))}{suffix}"
        )
    rows.append("不含账户、密钥、订单或投资建议。")
    return title[:64], "\n".join(rows)[:8000]


def _receipt_path(root: Path, now: datetime, content_sha256: str) -> Path:
    directory = _safe_path(root, RECEIPT_RELATIVE)
    if directory is None:
        raise ValueError("notification_receipt_path_escape")
    stamp = now.astimezone(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
    return directory / f"data_health_{stamp}_{content_sha256[:12]}.json"


def _result(
    *,
    status: str,
    reason_code: str,
    target_date: str,
    events: list[dict[str, Any]],
    external_sent_count: int = 0,
    receipt_path: Path | None = None,
    root: Path | None = None,
    delivery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current_keys = [
        _alert_key(event["alert"])
        for event in events
        if event["kind"] == "active"
    ]
    resolved_keys = [
        _alert_key(event["alert"])
        for event in events
        if event["kind"] == "recovery"
    ]
    payload: dict[str, Any] = {
        "task_type": "data_health_alert_delivery",
        "status": status,
        "reason_code": reason_code,
        "target_date": target_date,
        "current_alert_keys": current_keys,
        "new_alert_keys": current_keys,
        "resolved_alert_keys": resolved_keys,
        "event_count": len(events),
        "external_sent_count": external_sent_count,
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
    }
    if receipt_path is not None and root is not None:
        payload["receipt_path"] = str(receipt_path.relative_to(root)).replace("\\", "/")
    if delivery:
        payload["delivery"] = dict(delivery)
    return payload


def dispatch_data_health_alerts(
    project_root: str | Path,
    *,
    health_payload: Mapping[str, Any] | None = None,
    network: bool = False,
    push: bool = False,
    dry_run: bool = True,
    _sendkey_loader: Callable[[], str | None] | None = None,
    sender: Callable[..., Mapping[str, Any]] = deliver_server_chan,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Send one verified, redacted data-health summary after explicit opt-in."""

    root = Path(project_root).resolve()
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    health = health_payload if isinstance(health_payload, Mapping) else {}
    target = _target_date(health)
    state_path = _safe_path(root, STATE_RELATIVE)
    attempts_path = _safe_path(root, ATTEMPTS_RELATIVE)
    lock_path = _safe_path(root, LOCK_RELATIVE)
    if state_path is None or lock_path is None or attempts_path is None:
        return _result(status="blocked", reason_code="delivery_state_path_escape", target_date=target, events=[])
    try:
        with interprocess_file_lock(lock_path, timeout_seconds=0):
            state, error = _read_state(state_path)
            if error:
                return _result(status="blocked", reason_code=error, target_date=target, events=[])
            events, next_active, observed_alert = _events(health, state)
            try:
                pending = _read_attempts(attempts_path)
            except (OSError, ValueError, UnicodeDecodeError):
                return _result(status="blocked", reason_code="delivery_attempts_invalid", target_date=target, events=[])
            try:
                legacy_unknown = _legacy_unknown_keys(root, target)
            except (OSError, ValueError, UnicodeDecodeError):
                return _result(status="blocked", reason_code="delivery_receipts_invalid", target_date=target, events=[])
            eligible = [event for event in events if _attempt_key(event, target, next_active) not in pending
                        and _alert_key(event["alert"]) not in legacy_unknown]
            suppressed = len(events) - len(eligible)
            events = eligible
            if not events:
                if suppressed:
                    return _result(status="ambiguous", reason_code="previous_delivery_unknown", target_date=target, events=[])
                return _result(
                    status="skipped_duplicate" if observed_alert else "skipped",
                    reason_code="duplicate_alert_state" if observed_alert else "skipped_no_alerts",
                    target_date=target,
                    events=[],
                )
            if dry_run:
                return _result(status="preview_generated", reason_code="dry_run", target_date=target, events=events)
            if not _truthy(os.environ.get(PUSH_ENV)):
                # Owner 2026-09-05: missing push switch must not fail the data package.
                return _result(
                    status="preview_generated",
                    reason_code="data_health_alert_push_not_explicitly_enabled",
                    target_date=target,
                    events=events,
                )
            if not network:
                return _result(status="blocked", reason_code="network_not_authorized", target_date=target, events=events)
            if not push:
                return _result(status="blocked", reason_code="push_not_authorized", target_date=target, events=events)
            secret = _sendkey_loader() if _sendkey_loader is not None else None
            if not _text(secret):
                return _result(status="blocked", reason_code="sendkey_missing", target_date=target, events=events)
            title, body = _render(events, target)
            attempt_keys = [_attempt_key(event, target, next_active) for event in events]
            for key in attempt_keys:
                pending[key] = {"status": "unknown", "attempted_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds")}
            # Reserve before invoking the sender. If the process or receipt
            # write fails afterwards, a later invocation cannot blindly resend.
            # This journal never claims delivery and is separate from active.
            _save_attempts(attempts_path, pending)
            try:
                raw = sender(
                    title=title,
                    body=body,
                    sendkey=secret,
                    enabled=True,
                    channel_enabled=True,
                    network=True,
                    push=True,
                    dry_run=False,
                    now=current,
                )
            except Exception:
                raw = {
                    "channel": "server_chan",
                    "status": "failed",
                    "reason_code": "sender_failed",
                }
            delivery = redact_payload(raw, known_secrets=[str(secret or "")])
            status = _text(delivery.get("status")) or "failed"
            reason = _text(delivery.get("reason_code")) or "provider_failure"
            digest = hashlib.sha256((title + "\n" + body).encode("utf-8")).hexdigest()
            receipt_path = _receipt_path(root, current, digest)
            receipt = {
                "schema_version": "data_health_alert_receipt_v1",
                "observed_at": current.astimezone(SHANGHAI).isoformat(timespec="seconds"),
                "target_date": target,
                "status": status,
                "reason_code": reason,
                "content_sha256": digest,
                "event_keys": [_alert_key(event["alert"]) for event in events],
                "event_attempt_keys": attempt_keys,
                "delivery": delivery,
            }
            atomic_write_json(receipt_path, receipt)
            if status != "sent":
                if status in {"failed", "blocked"}:
                    for key in attempt_keys:
                        pending.pop(key, None)
                    _save_attempts(attempts_path, pending)
                return _result(status=status if status in {"failed", "ambiguous", "blocked"} else "failed", reason_code=reason, target_date=target, events=events, receipt_path=receipt_path, root=root, delivery=delivery)
            for event in events:
                key = _alert_key(event["alert"])
                if event["kind"] == "recovery":
                    next_active.pop(key, None)
                else:
                    next_active[key] = {"token": event["token"], "severity": _text(event["alert"].get("severity")), "target_date": target}
            atomic_write_json(state_path, {"schema_version": "data_health_alert_delivery_v1", "active": next_active})
            for key in attempt_keys:
                pending.pop(key, None)
            _save_attempts(attempts_path, pending)
            return _result(status="sent", reason_code=reason, target_date=target, events=events, external_sent_count=1, receipt_path=receipt_path, root=root, delivery=delivery)
    except TimeoutError:
        return _result(status="busy", reason_code="delivery_lock_busy", target_date=target, events=[])
    except Exception:
        return _result(status="failed", reason_code="delivery_internal_failure", target_date=target, events=[])


__all__ = ["PUSH_ENV", "dispatch_data_health_alerts"]
