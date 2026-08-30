from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.platform_common import PROJECT_ROOT


def _resolve(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def load_dedupe_state(path: str | Path = "state/notification_dedupe.json") -> dict:
    target = _resolve(path)
    if not target.exists():
        return {"items": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"items": {}}
    return payload if isinstance(payload, dict) else {"items": {}}


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except Exception:  # noqa: BLE001
        return None


def should_send(dedupe_key: str, now: datetime, cooldown_minutes: int, state: dict) -> bool:
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    record = items.get(dedupe_key) if isinstance(items, dict) else None
    if not isinstance(record, dict):
        return True
    last = _parse(record.get("last_sent_at"))
    if not last:
        return True
    return (now - last).total_seconds() >= cooldown_minutes * 60


def update_dedupe_state(dedupe_key: str, now: datetime, state: dict, *, path: str | Path = "state/notification_dedupe.json") -> dict:
    state = dict(state or {})
    items = state.setdefault("items", {})
    items[dedupe_key] = {"last_sent_at": now.replace(microsecond=0).isoformat()}
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return state
