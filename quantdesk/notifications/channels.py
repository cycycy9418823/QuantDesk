from __future__ import annotations

import json
from pathlib import Path

from quantdesk.platform_common import PROJECT_ROOT


def send_local_file(message: dict, path: str | Path = "logs/notifications_latest.jsonl", *, dry_run: bool = True) -> dict:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    safe = {key: value for key, value in message.items() if "secret" not in key.lower() and "token" not in key.lower()}
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, allow_nan=False) + "\n")
    return {"channel": "local_file", "status": "dry_run" if dry_run else "sent", "path": str(target)}


def external_channel_skipped(name: str) -> dict:
    return {"channel": name, "status": "skipped", "reason": "channel disabled by default; no external notification sent"}
