"""Research-report provider placeholders."""

from __future__ import annotations

from typing import Any

from quantdesk.a_stock_data_matrix import RISK_NOTE, dependency_available
from quantdesk.providers.iwencai_provider import capability as iwencai_capability


def capability(*, network: bool = False) -> list[dict[str, Any]]:
    ak_ok = dependency_available("akshare")
    return [
        {
            "provider": "akshare_report",
            "status": "available" if ak_ok else "not_available",
            "dependency_name": "akshare",
            "dependency_available": ak_ok,
            "requires_network": True,
            "checked_network": bool(network),
            "supports_historical_day": True,
            "suggested_next_action": "AKShare 研报仅用于学习参考，不作为买卖建议。",
            "disclaimer": RISK_NOTE,
        },
        iwencai_capability(network=network),
    ]


def fetch_research_reports(symbol: str = "", start_date: str = "", end_date: str = "", *, network: bool = False) -> dict[str, Any]:
    if not dependency_available("akshare"):
        return {
            "ok": False,
            "provider": "akshare_report",
            "data": [],
            "status": "not_available",
            "failed_reason": "未安装 akshare",
            "suggested_next_action": "安装 akshare 后可尝试研报列表；研报只用于学习参考。",
            "disclaimer": RISK_NOTE,
        }
    return {
        "ok": False,
        "provider": "akshare_report",
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "data": [],
        "status": "skipped" if not network else "planned",
        "implemented": False,
        "placeholder": True,
        "failed_reason": "network=false，未请求研报 provider" if not network else "v3.0.0 仅提供研报 provider 规划。",
        "disclaimer": RISK_NOTE,
    }
