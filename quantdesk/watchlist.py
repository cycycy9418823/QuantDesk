from __future__ import annotations

import json
import threading
from datetime import datetime
from functools import partial, wraps
from pathlib import Path
from typing import Any

from quantdesk.paper.market_rules import paper_only_boundary
from quantdesk.shared import truthy
from quantdesk.time_utils import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = PROJECT_ROOT / "state"
DEFAULT_WATCHLIST_PATH = DEFAULT_STATE_DIR / "watchlist.json"
DISCLAIMER = "自选股只用于本地观察和模拟验证，不接券商，不自动下单。"
_WATCHLIST_LOCK = threading.RLock()


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


def watchlist_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_WATCHLIST_PATH


def _empty_watchlist() -> dict[str, Any]:
    return {"items": [], "disclaimer": DISCLAIMER, "updated_at": _now_text(), **paper_only_boundary()}


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() else text


def load_watchlist(path: Path | str | None = None) -> dict[str, Any]:
    target = watchlist_path(path)
    with _WATCHLIST_LOCK:
        if not target.exists():
            return _empty_watchlist()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = _backup_bad_json(target)
            data = _empty_watchlist()
            data["error"] = "watchlist file is unreadable and was backed up"
            data["backup_path"] = str(backup) if backup else ""
            return data
        if not isinstance(data, dict):
            backup = _backup_bad_json(target)
            data = _empty_watchlist()
            data["error"] = "watchlist root is not an object and was backed up"
            data["backup_path"] = str(backup) if backup else ""
            return data
        data.setdefault("items", [])
        data.update(paper_only_boundary())
        data.setdefault("disclaimer", DISCLAIMER)
        data.setdefault("updated_at", _now_text())
        return data


def save_watchlist(watchlist: dict[str, Any], path: Path | str | None = None) -> None:
    target = watchlist_path(path)
    with _WATCHLIST_LOCK:
        watchlist.setdefault("items", [])
        watchlist.update(paper_only_boundary())
        watchlist["disclaimer"] = DISCLAIMER
        watchlist["updated_at"] = _now_text()
        _atomic_write_json(target, watchlist)


def create_default_watchlist(path: Path | str | None = None) -> dict[str, Any]:
    watchlist = _empty_watchlist()
    save_watchlist(watchlist, path)
    return watchlist


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    symbol = _symbol(item.get("symbol") or item.get("code"))
    now = _now_text()
    enabled = item.get("enabled_strategies")
    if isinstance(enabled, str):
        enabled = [enabled]
    if not isinstance(enabled, list):
        enabled = []
    tags = item.get("tags")
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        tags = []
    return {
        "symbol": symbol,
        "name": str(item.get("name") or ""),
        "tags": tags,
        "group": str(item.get("group") or "默认"),
        "enabled_strategies": enabled,
        "alert_enabled": truthy(item.get("alert_enabled", True)),
        "notes": str(item.get("notes") or ""),
        "created_at": str(item.get("created_at") or now),
        "updated_at": str(item.get("updated_at") or now),
        "disclaimer": DISCLAIMER,
        **paper_only_boundary(),
    }


def list_watchlist(path: Path | str | None = None) -> list[dict[str, Any]]:
    data = load_watchlist(path)
    return [_normalize_item(item) for item in data.get("items", []) if isinstance(item, dict)]


def watchlist_summary(items: list[dict[str, Any]] | None = None) -> dict[str, int]:
    rows = items if items is not None else list_watchlist()
    enabled_alerts = sum(1 for item in rows if item.get("alert_enabled"))
    return {"total": len(rows), "alert_enabled": enabled_alerts, "alert_disabled": len(rows) - enabled_alerts}


def add_watchlist_item(item: dict[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    normalized = _normalize_item(item)
    if not normalized["symbol"]:
        return {"ok": False, "message": "股票代码不能为空。", "disclaimer": DISCLAIMER}
    data = load_watchlist(path)
    items = [_normalize_item(entry) for entry in data.get("items", []) if isinstance(entry, dict)]
    items = [entry for entry in items if entry.get("symbol") != normalized["symbol"]]
    items.append(normalized)
    data["items"] = items
    save_watchlist(data, path)
    return {"ok": True, "item": normalized, "summary": watchlist_summary(items), "disclaimer": DISCLAIMER}


def remove_watchlist_item(symbol: str, path: Path | str | None = None) -> dict[str, Any]:
    normalized_symbol = _symbol(symbol)
    data = load_watchlist(path)
    items = [_normalize_item(entry) for entry in data.get("items", []) if isinstance(entry, dict)]
    new_items = [entry for entry in items if entry.get("symbol") != normalized_symbol]
    if len(new_items) == len(items):
        return {"ok": False, "message": "未找到对应自选股。", "disclaimer": DISCLAIMER}
    data["items"] = new_items
    save_watchlist(data, path)
    return {"ok": True, "summary": watchlist_summary(new_items), "disclaimer": DISCLAIMER}


def update_watchlist_item(symbol: str, updates: dict[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    normalized_symbol = _symbol(symbol)
    data = load_watchlist(path)
    items = [_normalize_item(entry) for entry in data.get("items", []) if isinstance(entry, dict)]
    found: dict[str, Any] | None = None
    for item in items:
        if item.get("symbol") == normalized_symbol:
            item.update(updates)
            item["symbol"] = normalized_symbol
            item["updated_at"] = _now_text()
            found = _normalize_item(item)
            item.update(found)
            break
    if not found:
        return {"ok": False, "message": "未找到对应自选股。", "disclaimer": DISCLAIMER}
    data["items"] = items
    save_watchlist(data, path)
    return {"ok": True, "item": found, "summary": watchlist_summary(items), "disclaimer": DISCLAIMER}


def watchlist_payload(path: Path | str | None = None) -> dict[str, Any]:
    items = list_watchlist(path)
    return {
        "ok": True,
        "items": items,
        "summary": watchlist_summary(items),
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


load_watchlist = _boundary_result(load_watchlist)
create_default_watchlist = _boundary_result(create_default_watchlist)
add_watchlist_item = _boundary_result(add_watchlist_item)
remove_watchlist_item = _boundary_result(remove_watchlist_item)
update_watchlist_item = _boundary_result(update_watchlist_item)
watchlist_payload = _boundary_result(watchlist_payload)


__all__ = [
    "DISCLAIMER",
    "create_default_watchlist",
    "load_watchlist",
    "save_watchlist",
    "list_watchlist",
    "watchlist_summary",
    "add_watchlist_item",
    "remove_watchlist_item",
    "update_watchlist_item",
    "watchlist_payload",
]
