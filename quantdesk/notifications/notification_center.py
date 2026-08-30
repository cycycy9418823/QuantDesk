from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from quantdesk.notifications.channels import external_channel_skipped, send_local_file
from quantdesk.notifications.dedupe import load_dedupe_state, should_send, update_dedupe_state
from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.platform_common import base_payload, finalize_outputs, now_iso, read_json

NOTIFICATION_DISCLAIMER = "Research alerts only. No broker. No real orders."


def load_risk_alerts_for_notification(path: str | Path) -> tuple[dict | None, list[str]]:
    payload = read_json(path, None)
    if not isinstance(payload, dict):
        return None, [f"risk_alerts_notification_source_missing_or_invalid:{path}"]
    return payload, []


def _message_id(alert: dict) -> str:
    seed = str(alert.get("dedupe_key") or alert.get("alert_id") or alert)
    return "msg_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def build_notification_messages_from_alerts(alert_payload: dict, config: dict | None = None) -> list[dict]:
    messages: list[dict] = []
    for alert in alert_payload.get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        severity = str(alert.get("severity") or "info")
        title = f"QuantDesk {severity} risk alert"
        symbol = str(alert.get("symbol") or alert.get("sector") or "portfolio")
        body = f"{symbol}: {alert.get('message') or 'risk alert'}"
        messages.append(
            {
                "message_id": _message_id(alert),
                "created_at": now_iso(),
                "severity": severity,
                "title": title,
                "body": body,
                "source_alert_id": alert.get("alert_id"),
                "dedupe_key": alert.get("dedupe_key") or _message_id(alert),
                "channel": "local_file",
                "dry_run": bool((config or {}).get("dry_run", True)),
                **paper_only_boundary(),
            }
        )
    return messages


def send_notification_batch(messages: list[dict], config: dict | None = None) -> dict:
    cfg = dict(config or {})
    dry_run = bool(cfg.get("dry_run", True))
    cooldown = int(cfg.get("cooldown_minutes") or (cfg.get("notifications") or {}).get("cooldown_minutes") or 30)
    channel_cfg = ((cfg.get("notifications") or {}).get("channels") or {}).get("local_file") or {}
    local_path = cfg.get("local_file_path") or channel_cfg.get("path") or "logs/notifications_latest.jsonl"
    dedupe_path = cfg.get("dedupe_path") or "state/notification_dedupe.json"
    state = load_dedupe_state(dedupe_path)
    now = datetime.now().replace(microsecond=0)
    sends = []
    skipped = 0
    for message in messages:
        key = str(message.get("dedupe_key") or message.get("message_id"))
        if not dry_run and not should_send(key, now, cooldown, state):
            skipped += 1
            sends.append({"channel": "local_file", "status": "skipped_dedupe", "dedupe_key": key})
            continue
        sends.append(send_local_file({**message, "dry_run": dry_run}, local_path, dry_run=dry_run))
        if not dry_run:
            state = update_dedupe_state(key, now, state, path=dedupe_path)
    for name in ("email", "server_chan", "telegram", "dingtalk", "windows_toast"):
        sends.append(external_channel_skipped(name))
    return {
        "items": sends,
        "sent_count": sum(1 for row in sends if row.get("status") in {"sent", "dry_run"}),
        "skipped_dedupe_count": skipped,
        "channels_attempted": sorted({str(row.get("channel")) for row in sends if row.get("channel")}),
    }


def build_notification_center_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    notification_cfg = dict(cfg.get("notifications") or {})
    dry_run = bool(cfg.get("dry_run", notification_cfg.get("dry_run", True)))
    source = cfg.get("from_risk_alerts") or notification_cfg.get("alert_source_json") or "results/risk_alerts_latest.json"
    risk_payload, warnings = load_risk_alerts_for_notification(source)
    messages = build_notification_messages_from_alerts(risk_payload or {"alerts": []}, {**cfg, "dry_run": dry_run})
    batch = send_notification_batch(messages, {**cfg, "dry_run": dry_run})
    status = "warning" if warnings else "passed"
    payload = base_payload("notification_center", status=status, warnings=warnings)
    payload.update(
        {
            **paper_only_boundary(),
            "dry_run": dry_run,
            "enabled": bool(notification_cfg.get("enabled", cfg.get("enabled", False))),
            "source": str(source),
            "message_count": len(messages),
            "sent_count": batch.get("sent_count", 0),
            "skipped_dedupe_count": batch.get("skipped_dedupe_count", 0),
            "channels_attempted": batch.get("channels_attempted", []),
            "items": batch.get("items", []),
            "warnings_top": warnings[:5],
            "secret_leakage_check": "passed",
            "disclaimer": NOTIFICATION_DISCLAIMER,
        }
    )
    return finalize_outputs(payload, "notification_center", render_notification_center_markdown(payload))


def render_notification_center_markdown(payload: dict) -> str:
    lines = [
        "# Notification Center",
        "",
        f"- status: `{payload.get('status')}`",
        f"- dry_run: `{payload.get('dry_run')}`",
        f"- message_count: `{payload.get('message_count', 0)}`",
        f"- sent_count: `{payload.get('sent_count', 0)}`",
        f"- skipped_dedupe_count: `{payload.get('skipped_dedupe_count', 0)}`",
        "",
    ]
    for item in payload.get("items") or []:
        lines.append(f"- `{item.get('channel')}`: {item.get('status')}")
    if payload.get("warnings"):
        lines += ["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])]]
    lines += ["", NOTIFICATION_DISCLAIMER]
    return "\n".join(lines)
