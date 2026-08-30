from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from quantdesk.data_store.repositories import FundamentalRepository
from quantdesk.data_store.sqlite_store import QuantDeskDB

from .akshare_provider import (
    fetch_financial_statements_em,
    fetch_fundamental_abstract_em,
    fetch_fundamental_abstract_ths,
)


def _first_payload_report_date(rows: list[dict[str, Any]]) -> str:
    aliases = (
        "report_date",
        "报告期",
        "公告日期",
        "披露日期",
        "date",
        "end_date",
        "报告日期",
        "统计截止日期",
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in aliases:
            value = row.get(key)
            if value:
                text = str(value).strip()
                parsed = None
                if "-" in text.replace("/", "-"):
                    try:
                        parsed = datetime.fromisoformat(text.replace("/", "-")).date()
                    except ValueError:
                        continue
                if parsed:
                    return parsed.isoformat()
                return text
    return ""


def load_fundamentals(symbol: str, network: bool = False, db_path: str = "data/quantdesk.db", ttl_days: int = 7) -> dict:
    db = QuantDeskDB(db_path)
    db.initialize()
    repo = FundamentalRepository(db)
    cached = repo.latest_fundamentals(symbol)
    warnings: list[str] = []
    if cached and cached.get("updated_at"):
        try:
            if datetime.fromisoformat(cached["updated_at"]) >= datetime.now() - timedelta(days=ttl_days):
                db.close()
                return {"ok": True, "status": "passed", "source": "sqlite_cache", "rows": [cached], "warnings": []}
        except Exception as exc:
            warnings.append(f"cached_fundamentals_updated_at_invalid:{exc}")
    if not network:
        db.close()
        return {"ok": True, "status": "warning", "source": "sqlite_cache", "rows": [cached] if cached else [], "warnings": warnings + ["network_false_external_fundamentals_skipped"]}
    fetchers = (
        ("fetch_fundamental_abstract_ths", fetch_fundamental_abstract_ths),
        ("fetch_fundamental_abstract_em", fetch_fundamental_abstract_em),
        ("fetch_financial_statements_em", fetch_financial_statements_em),
    )
    for fetcher_name, fetcher in fetchers:
        try:
            payload = fetcher(symbol, network=network)
        except Exception as exc:
            warnings.append(f"{fetcher_name}_failed:{exc}")
            continue
        warnings.extend(payload.get("warnings", []))
        if payload.get("rows"):
            payload_rows = payload["rows"][:5]
            report_date = _first_payload_report_date(payload_rows)
            row_warnings = [] if report_date else ["report_date_fallback_to_now"]
            row = {"symbol": symbol, "report_date": report_date or datetime.now().date().isoformat(), "raw_json": json.dumps(payload_rows, ensure_ascii=False), "source": payload.get("source", "akshare")}
            repo.upsert_fundamentals([row])
            db.close()
            return {"ok": True, "status": "warning", "source": "akshare", "rows": [row], "warnings": warnings + row_warnings + ["partial_field_mapping"]}
    db.close()
    return {"ok": True, "status": "warning", "source": "none", "rows": [], "warnings": warnings + ["mootdx_limited_basic_indicators_only", "tushare_adapter_placeholder_no_token_used"]}
