from __future__ import annotations

import json
import threading
from datetime import datetime
from functools import partial, wraps
from pathlib import Path
from typing import Any

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.time_utils import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = PROJECT_ROOT / "state"
DEFAULT_QUEUE_PATH = DEFAULT_STATE_DIR / "manual_confirmation_queue.json"
DISCLAIMER = "人工确认清单只记录本地复核事项，不接券商，不自动下单，最终由用户自行判断。"
_QUEUE_LOCK = threading.RLock()

USER_STATUS_LABELS = {
    "pending": "待确认",
    "reviewed": "已复核",
    "ignored": "已忽略",
    "simulated": "已加入虚拟模拟",
    "manually_executed_outside_quantdesk": "用户已在外部手动处理",
}


_now_text = partial(now_text, sep=" ")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _backup_bad_json(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.stem}.bad_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.stem}.bad_{datetime.now():%Y%m%d_%H%M%S}_{counter}{path.suffix}")
        counter += 1
    try:
        path.replace(backup)
    except OSError:
        return None
    return backup


def queue_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_QUEUE_PATH


def _empty_queue() -> dict[str, Any]:
    return {"items": [], "disclaimer": DISCLAIMER, "updated_at": _now_text(), **paper_only_boundary()}


def load_manual_confirmation_queue(path: Path | str | None = None) -> dict[str, Any]:
    target = queue_path(path)
    with _QUEUE_LOCK:
        if not target.exists():
            return _empty_queue()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = _backup_bad_json(target)
            data = _empty_queue()
            data["error"] = "manual confirmation queue file is unreadable and was backed up"
            data["backup_path"] = str(backup) if backup else ""
            return data
        if not isinstance(data, dict):
            backup = _backup_bad_json(target)
            data = _empty_queue()
            data["error"] = "manual confirmation queue root is not an object and was backed up"
            data["backup_path"] = str(backup) if backup else ""
            return data
        data.setdefault("items", [])
        data.update(paper_only_boundary())
        data.setdefault("disclaimer", DISCLAIMER)
        data.setdefault("updated_at", _now_text())
        return data


def save_manual_confirmation_queue(queue: dict[str, Any], path: Path | str | None = None) -> None:
    target = queue_path(path)
    with _QUEUE_LOCK:
        queue.setdefault("items", [])
        queue.update(paper_only_boundary())
        queue["disclaimer"] = DISCLAIMER
        queue["updated_at"] = _now_text()
        _atomic_write_json(target, queue)


def create_default_manual_confirmation_queue(path: Path | str | None = None) -> dict[str, Any]:
    queue = _empty_queue()
    save_manual_confirmation_queue(queue, path)
    return queue


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    symbol = str(item.get("symbol") or item.get("code") or "").strip()
    if symbol.isdigit():
        symbol = symbol.zfill(6)
    created_at = str(item.get("created_at") or _now_text())
    plan_action = str(item.get("plan_action") or "watch")
    item_id = str(item.get("id") or f"{symbol or 'unknown'}_{plan_action}_{created_at.replace(' ', '_').replace(':', '')}")
    status = str(item.get("user_status") or "pending")
    return {
        "id": item_id,
        "created_at": created_at,
        "updated_at": str(item.get("updated_at") or _now_text()),
        "symbol": symbol,
        "name": str(item.get("name") or ""),
        "strategy_name": str(item.get("strategy_name") or ""),
        "plan_action": plan_action,
        "plan_action_label": str(item.get("plan_action_label") or ""),
        "confirmation_level": str(item.get("confirmation_level") or item.get("manual_confirmation_level") or "normal_review"),
        "confirmation_level_label": str(item.get("confirmation_level_label") or item.get("manual_confirmation_level_label") or ""),
        "data_date_label": str(item.get("data_date_label") or ""),
        "triggered_conditions": list(item.get("triggered_conditions") or []),
        "risk_notes": list(item.get("risk_notes") or []),
        "user_status": status,
        "user_status_label": USER_STATUS_LABELS.get(status, status),
        "user_note": str(item.get("user_note") or item.get("manual_note") or ""),
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }


def list_manual_confirmation_items(path: Path | str | None = None) -> list[dict[str, Any]]:
    queue = load_manual_confirmation_queue(path)
    return [_normalize_item(item) for item in queue.get("items", []) if isinstance(item, dict)]


def manual_confirmation_queue_summary(items: list[dict[str, Any]] | None = None) -> dict[str, int]:
    rows = items if items is not None else list_manual_confirmation_items()
    summary = {key: 0 for key in USER_STATUS_LABELS}
    for item in rows:
        status = str(item.get("user_status") or "pending")
        summary[status] = summary.get(status, 0) + 1
    summary["total"] = len(rows)
    return summary


def add_manual_confirmation_item(item: dict[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    queue = load_manual_confirmation_queue(path)
    normalized = _normalize_item(item)
    items = [entry for entry in queue.get("items", []) if isinstance(entry, dict) and entry.get("id") != normalized["id"]]
    items.append(normalized)
    queue["items"] = items
    save_manual_confirmation_queue(queue, path)
    return {"ok": True, "item": normalized, "summary": manual_confirmation_queue_summary(items), "disclaimer": DISCLAIMER}


def update_manual_confirmation_item(
    item_id: str,
    user_status: str | None = None,
    user_note: str | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    queue = load_manual_confirmation_queue(path)
    items = [_normalize_item(item) for item in queue.get("items", []) if isinstance(item, dict)]
    found: dict[str, Any] | None = None
    for item in items:
        if item.get("id") == item_id:
            if user_status:
                item["user_status"] = user_status
                item["user_status_label"] = USER_STATUS_LABELS.get(user_status, user_status)
            if user_note is not None:
                item["user_note"] = user_note
            item["updated_at"] = _now_text()
            found = item
            break
    if not found:
        return {"ok": False, "message": "未找到对应人工确认事项。", "disclaimer": DISCLAIMER}
    queue["items"] = items
    save_manual_confirmation_queue(queue, path)
    return {"ok": True, "item": found, "summary": manual_confirmation_queue_summary(items), "disclaimer": DISCLAIMER}


def manual_confirmation_queue_payload(path: Path | str | None = None) -> dict[str, Any]:
    items = list_manual_confirmation_items(path)
    return {
        "ok": True,
        "items": items,
        "summary": manual_confirmation_queue_summary(items),
        "paper_trading_only": True,
        "disclaimer": DISCLAIMER,
    }


def _boundary_result(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        payload = function(*args, **kwargs)
        if isinstance(payload, dict):
            payload.update(paper_only_boundary())
        return payload

    return wrapped


load_manual_confirmation_queue = _boundary_result(load_manual_confirmation_queue)
create_default_manual_confirmation_queue = _boundary_result(create_default_manual_confirmation_queue)
add_manual_confirmation_item = _boundary_result(add_manual_confirmation_item)
update_manual_confirmation_item = _boundary_result(update_manual_confirmation_item)
manual_confirmation_queue_payload = _boundary_result(manual_confirmation_queue_payload)


__all__ = [
    "DISCLAIMER",
    "USER_STATUS_LABELS",
    "create_default_manual_confirmation_queue",
    "load_manual_confirmation_queue",
    "save_manual_confirmation_queue",
    "list_manual_confirmation_items",
    "manual_confirmation_queue_summary",
    "add_manual_confirmation_item",
    "update_manual_confirmation_item",
    "manual_confirmation_queue_payload",
]
