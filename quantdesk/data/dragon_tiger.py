from __future__ import annotations

import csv
from pathlib import Path

from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown


def load_dragon_tiger_csv(path: str | Path) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_dragon_tiger_report(config: dict | None = None) -> dict:
    rows = load_dragon_tiger_csv((config or {}).get("path") or "data/dragon_tiger/dragon_tiger.csv")
    warnings = [] if rows else ["dragon_tiger_csv_missing"]
    payload = base_payload("dragon_tiger", status="warning" if warnings else "passed", warnings=warnings)
    payload.update({"items": rows[:50], "active_seats": [], "institution_buying": [], "hot_money_activity": [], "row_count": len(rows)})
    return finalize_outputs(payload, "dragon_tiger", simple_markdown("Dragon Tiger", payload, "items"))
