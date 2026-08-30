from __future__ import annotations

import csv
from pathlib import Path

from quantdesk.data_store.repositories import SectorRepository
from quantdesk.data_store.sqlite_store import QuantDeskDB

from .akshare_provider import fetch_concept_boards_em, fetch_industry_boards_em


def load_sectors(symbol: str | None = None, network: bool = False, db_path: str = "data/quantdesk.db", local_csv: str = "data/universe/stock_theme_map.csv") -> dict:
    db = QuantDeskDB(db_path)
    db.initialize()
    repo = SectorRepository(db)
    if symbol:
        cached = repo.get_sectors(symbol)
        if cached:
            db.close()
            return {"ok": True, "status": "passed", "source": "sqlite_cache", "rows": cached, "warnings": []}
    path = Path(local_csv)
    rows = []
    warnings: list[str] = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for raw in csv.DictReader(fh):
                rows.append({"symbol": raw.get("symbol") or raw.get("code"), "sector_name": raw.get("sector") or raw.get("theme") or raw.get("sector_name"), "sector_type": raw.get("sector_type") or "local", "source": "local_csv"})
        repo.upsert_sector_mappings(rows)
        db.close()
        return {"ok": True, "status": "passed", "source": "local_csv", "rows": rows, "warnings": warnings}
    if not network:
        db.close()
        return {"ok": True, "status": "warning", "source": "none", "rows": [], "warnings": ["network_false_sector_provider_skipped", "local_sector_csv_missing"]}
    for fetcher in (fetch_industry_boards_em, fetch_concept_boards_em):
        payload = fetcher(network=network)
        warnings.extend(payload.get("warnings", []))
    db.close()
    return {"ok": True, "status": "warning", "source": "akshare", "rows": [], "warnings": warnings or ["sector_mapping_requires_board_member_adapter"]}
