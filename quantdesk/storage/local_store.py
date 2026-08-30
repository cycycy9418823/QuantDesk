from __future__ import annotations

import sqlite3
from pathlib import Path

from quantdesk.platform_common import PROJECT_ROOT, base_payload, finalize_outputs, simple_markdown


def init_local_store(path: str | Path = "state/quantdesk_local_store.sqlite") -> dict:
    target = Path(path)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    try:
        conn.execute("create table if not exists results (task_type text, generated_at text, path text)")
        conn.commit()
    finally:
        conn.close()
    return {"status": "passed", "sqlite_path": str(target), "duckdb_available": False}


def build_local_store_report(config: dict | None = None) -> dict:
    info = init_local_store((config or {}).get("path") or "state/quantdesk_local_store.sqlite")
    payload = base_payload("local_store")
    payload.update(info)
    payload["items"] = [{"name": "sqlite", "status": "passed"}, {"name": "duckdb", "status": "optional_fallback"}]
    return finalize_outputs(payload, "local_store", simple_markdown("Local Store", payload, "items"))
