from __future__ import annotations

import csv
from pathlib import Path

from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown


def load_money_flow_csv(path: str | Path) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_money_flow_report(config: dict | None = None) -> dict:
    rows = load_money_flow_csv((config or {}).get("path") or "data/money_flow/money_flow.csv")
    warnings = [] if rows else ["money_flow_csv_missing"]
    payload = base_payload("money_flow", status="warning" if warnings else "passed", warnings=warnings)
    payload.update({"items": rows[:50], "row_count": len(rows)})
    return finalize_outputs(payload, "money_flow", simple_markdown("Money Flow", payload, "items"))
