"""Hypothesis notebook: birth, test, deploy, kill. This is the trader's brain log."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_trader.paths import HYPOTHESES_PATH, assert_not_quantdesk_ledger, ensure_runtime_dirs
from quantdesk.utils import atomic_write_json

SCHEMA = "ai_trader_hypotheses_v1"
LIVE = "live"
KILLED = "killed"
REJECTED = "rejected"
INSUFFICIENT = "insufficient_evidence"

STATUSES = (LIVE, KILLED, REJECTED, INSUFFICIENT)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def empty_book() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "updated_at": _now(),
        "items": [],
    }


def load_hypotheses(path: Path | None = None) -> dict[str, Any]:
    target = assert_not_quantdesk_ledger(path or HYPOTHESES_PATH)
    if not target.is_file():
        return empty_book()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return empty_book()
    payload.setdefault("items", [])
    return payload


def save_hypotheses(book: dict[str, Any], path: Path | None = None) -> Path:
    target = assert_not_quantdesk_ledger(path or HYPOTHESES_PATH)
    ensure_runtime_dirs()
    payload = deepcopy(book)
    payload["updated_at"] = _now()
    atomic_write_json(target, payload)
    return target


def upsert_hypothesis(book: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    hid = str(item.get("hypothesis_id") or "")
    if not hid:
        raise ValueError("hypothesis_id is required")
    items = [dict(row) for row in (book.get("items") or []) if isinstance(row, dict)]
    kept = [row for row in items if str(row.get("hypothesis_id")) != hid]
    kept.append(dict(item))
    next_book = dict(book)
    next_book["items"] = kept
    return next_book


def live_items(book: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in (book.get("items") or [])
        if isinstance(row, dict) and str(row.get("status")) == LIVE
    ]
