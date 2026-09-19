"""Dispatch one explicit, gated Server 酱 summary for verified data-health alerts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantdesk.data_health_alert_delivery import dispatch_data_health_alerts  # noqa: E402
from quantdesk.shared import truthy  # noqa: E402

HEALTH_PATH = PROJECT_ROOT / "results" / "data_health" / "data_health_dashboard_latest.json"
SENDKEY_ENV = "QUANTDESK_SERVERCHAN_SENDKEY"
LEGACY_SENDKEY_ENV = "SERVERCHAN_SEND_KEY"


def _load_sendkey() -> str | None:
    for name in (SENDKEY_ENV, LEGACY_SENDKEY_ENV):
        value = os.environ.get(name)
        if value and value.strip():
            return value
    return None


def _load_health(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", default="false")
    parser.add_argument("--enable-network", action="store_true")
    parser.add_argument("--push", nargs="?", const="true", default="")
    parser.add_argument("--dry-run", default="")
    parser.add_argument("--json", default="true")
    parser.add_argument("--summary", default="false")
    parser.add_argument("--pipeline-run-id", default="")
    parser.add_argument("--target-date", default="")
    return parser


def _exit_code(payload: dict) -> int:
    status = str(payload.get("status") or "")
    if status in {"preview_generated", "sent", "skipped", "skipped_duplicate"}:
        return 0
    if status == "busy":
        return 3
    if status == "blocked":
        return 4
    return 5


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    network = bool(args.enable_network or truthy(args.network))
    push = truthy(args.push) if str(args.push).strip() else False
    dry_run = truthy(args.dry_run) if str(args.dry_run).strip() else not (network and push)
    health = _load_health(HEALTH_PATH)
    if not health:
        payload = {
            "task_type": "data_health_alert_delivery",
            "status": "blocked",
            "reason_code": "data_health_payload_missing_or_invalid",
            "external_sent_count": 0,
        }
    else:
        payload = dispatch_data_health_alerts(
            PROJECT_ROOT,
            health_payload=health,
            network=network,
            push=push,
            dry_run=dry_run,
            _sendkey_loader=_load_sendkey,
        )
    pipeline_run_id = str(args.pipeline_run_id or "").strip()
    if pipeline_run_id:
        payload["pipeline_run_id"] = pipeline_run_id
        payload["target_date"] = str(
            args.target_date or payload.get("target_date") or health.get("target_date") or ""
        )
        dest = PROJECT_ROOT / "results" / "data_health" / "data_health_alert_delivery_latest.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["artifact_path"] = "results/data_health/data_health_alert_delivery_latest.json"
        payload["artifact_sha256"] = "sha256:" + hashlib.sha256(dest.read_bytes()).hexdigest()
    if truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, indent=None if truthy(args.summary) else 2))
    else:
        print(
            "data-health-alerts "
            f"status={payload.get('status')} reason={payload.get('reason_code')} "
            f"sent={payload.get('external_sent_count', 0)}"
        )
    return _exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
