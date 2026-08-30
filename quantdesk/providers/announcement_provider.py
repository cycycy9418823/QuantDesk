"""Announcement provider placeholders for CNInfo/AKShare and mootdx."""

from __future__ import annotations

from typing import Any

from quantdesk.a_stock_data_matrix import RISK_NOTE, dependency_available


def capability(*, network: bool = False) -> list[dict[str, Any]]:
    ak_ok = dependency_available("akshare")
    mootdx_ok = dependency_available("mootdx")
    return [
        {
            "provider": "cninfo_via_akshare",
            "status": "available" if ak_ok else "not_available",
            "dependency_name": "akshare",
            "dependency_available": ak_ok,
            "requires_network": True,
            "checked_network": bool(network),
            "supports_historical_day": True,
            "suggested_next_action": "巨潮公告优先通过 AKShare 封装低频刷新，用于风险提示。",
            "disclaimer": RISK_NOTE,
        },
        {
            "provider": "mootdx_announcement",
            "status": "available" if mootdx_ok else "not_available",
            "dependency_name": "mootdx",
            "dependency_available": mootdx_ok,
            "requires_network": True,
            "checked_network": bool(network),
            "supports_historical_day": True,
            "suggested_next_action": "mootdx 可用时可作为公告补充来源。",
            "disclaimer": RISK_NOTE,
        },
    ]


def fetch_announcements(symbol: str = "", start_date: str = "", end_date: str = "", *, network: bool = False) -> dict[str, Any]:
    if not dependency_available("akshare"):
        return {
            "ok": False,
            "provider": "cninfo_via_akshare",
            "data": [],
            "status": "not_available",
            "failed_reason": "未安装 akshare",
            "suggested_next_action": "安装 akshare 或稍后通过本地公告文件补充；公告只用于风险提示。",
            "disclaimer": RISK_NOTE,
        }
    return {
        "ok": False,
        "provider": "cninfo_via_akshare",
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "data": [],
        "status": "skipped" if not network else "planned",
        "implemented": False,
        "placeholder": True,
        "failed_reason": "network=false，未请求公告 provider" if not network else "v3.0.0 仅提供公告 provider 规划。",
        "disclaimer": RISK_NOTE,
    }
