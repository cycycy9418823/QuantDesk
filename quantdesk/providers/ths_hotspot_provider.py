"""THS hotspot provider placeholder for theme attribution."""

from __future__ import annotations

from typing import Any

from quantdesk.a_stock_data_matrix import RISK_NOTE

PROVIDER = "ths_hotspot"
HOTSPOT_NOTE = "热点数据仅用于题材学习和归因观察，不构成投资建议。"


def capability(*, network: bool = False) -> dict[str, Any]:
    return {
        "provider": PROVIDER,
        "status": "planned",
        "implemented": False,
        "placeholder": True,
        "default_enabled": False,
        "requires_network": True,
        "checked_network": bool(network),
        "supports_current_day": True,
        "supports_historical_day": True,
        "suggested_next_action": "需要用户明确启用后再刷新热点；默认不参与买卖决策。",
        "risk_note": HOTSPOT_NOTE,
        "disclaimer": RISK_NOTE,
    }


def fetch_hot_themes(date: str = "", *, network: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": PROVIDER,
        "date": date,
        "data": [],
        "status": "skipped" if not network else "planned",
        "implemented": False,
        "placeholder": True,
        "failed_reason": "默认不联网刷新同花顺热点；v3.0.0 仅保留热点 provider 占位。",
        "risk_note": HOTSPOT_NOTE,
        "disclaimer": RISK_NOTE,
    }
