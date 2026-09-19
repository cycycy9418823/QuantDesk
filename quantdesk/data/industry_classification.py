from __future__ import annotations

from quantdesk.platform_common import base_payload, finalize_outputs, simple_markdown


SYSTEMS = {"sw_l1", "sw_l2", "citic_l1", "citic_l2", "ths_theme", "eastmoney_theme", "stock_theme_map"}


def normalize_industry_system(system: str | None) -> str:
    value = str(system or "stock_theme_map").strip().lower()
    return value if value in SYSTEMS else "stock_theme_map"


def map_symbol_industry(symbol: str, rows: list[dict], system: str = "stock_theme_map") -> dict:
    key = normalize_industry_system(system)
    for row in rows:
        if str(row.get("symbol")) == str(symbol):
            return {"symbol": symbol, "industry_system": key, "industry": row.get(key) or row.get("sector") or row.get("industry") or "unknown"}
    return {"symbol": symbol, "industry_system": key, "industry": "unknown", "warnings": ["industry_mapping_missing"]}


def build_industry_classification_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    system = normalize_industry_system(cfg.get("industry_system") or cfg.get("sector") or "stock_theme_map")
    payload = base_payload("industry_classification", status="warning", warnings=["industry_classification_csv_missing"])
    payload.update({"industry_system": system, "items": [], "mapping_count": 0})
    return finalize_outputs(payload, "industry_classification", simple_markdown("Industry Classification", payload, "items"))
