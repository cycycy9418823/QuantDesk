from __future__ import annotations

from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown


FIELDS = ["pe", "pb", "ps", "roe", "gross_margin", "net_margin", "debt_to_asset", "goodwill_to_equity", "receivables_growth", "revenue_growth", "profit_growth", "cashflow_quality"]


def evaluate_fundamental_metrics(row: dict) -> dict:
    score = sum(1 for field in FIELDS if row.get(field) not in (None, "")) / len(FIELDS) * 100
    return {"symbol": row.get("symbol", ""), "fundamental_score": round(score, 4), "available_fields": [field for field in FIELDS if row.get(field) not in (None, "")]}


def build_fundamental_metrics_report(config: dict | None = None) -> dict:
    item = evaluate_fundamental_metrics({"symbol": "600744", "pe": 15, "pb": 1.2, "roe": 12}) if (config or {}).get("synthetic_test_data") else {}
    warnings = [] if item else ["fundamental_metrics_csv_missing"]
    payload = base_payload("fundamental_metrics", status="warning" if warnings else "passed", warnings=warnings)
    payload.update({"items": [item] if item else [], "field_count": len(FIELDS)})
    return finalize_outputs(payload, "fundamental_metrics", simple_markdown("Fundamental Metrics", payload, "items"))
