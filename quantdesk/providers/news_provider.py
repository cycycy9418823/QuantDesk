"""News / announcement provider backed by Eastmoney API.

AKShare's stock_news_em() has an Arrow/Parquet regex bug in v1.18.x.
This provider calls the Eastmoney announcement API directly via urllib.
"""

from __future__ import annotations

import csv
import json
import ssl
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.a_stock_data_matrix import RISK_NOTE

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
NEWS_DIR = DATA_DIR / "news"

EASTMONEY_ANNOUNCE_URL = (
    "https://np-anotice-stock.eastmoney.com/api/security/ann"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_SSL_CONTEXT = ssl.create_default_context()


def _symbol_code(value: object) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def capability(*, network: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "provider": "eastmoney_announce",
            "status": "ready" if network else "offline",
            "implemented": True,
            "placeholder": False,
            "requires_network": True,
            "checked_network": bool(network),
            "supports_current_day": True,
            "suggested_next_action": "Calls Eastmoney announcement API directly (bypasses broken akshare stock_news_em).",
            "disclaimer": RISK_NOTE,
        },
        {
            "provider": "local_news_cache",
            "status": "ready",
            "implemented": True,
            "placeholder": False,
            "requires_network": False,
            "supports_current_day": False,
            "suggested_next_action": "Reads cached announcements from data/news/*.csv.",
            "disclaimer": RISK_NOTE,
        },
    ]


def _fetch_announce_eastmoney(code: str, limit: int = 20, *, insecure_ssl: bool = False) -> list[dict[str, str]]:
    """Directly call Eastmoney announcement API."""
    params = {
        "sr": -1,
        "page_size": limit,
        "page_index": 1,
        "ann_type": "A",
        "stock_list": code,
        "f_node": 0,
        "s_node": 0,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{EASTMONEY_ANNOUNCE_URL}?{query}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": "https://www.eastmoney.com/",
        "Accept": "application/json",
    })
    resp = urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT)
    data = json.loads(resp.read().decode("utf-8"))
    items: list[dict[str, str]] = []
    for item in data.get("data", {}).get("list", []):
        items.append({
            "title": item.get("title", ""),
            "content": item.get("title", ""),
            "published": item.get("notice_date", ""),
            "source": "东方财富公告",
            "url": item.get("url", ""),
        })
    return items


def _ssl_visibility(*, network: bool, insecure_ssl: bool) -> dict[str, Any]:
    if network and insecure_ssl:
        return {
            "ssl_verification": "default",
            "warnings": ["insecure_ssl_requested_but_ignored"],
        }
    return {"ssl_verification": "default", "warnings": []}


def fetch_news(
    symbol: str | None = None,
    limit: int = 20,
    *,
    provider: str = "eastmoney_announce",
    network: bool = False,
    insecure_ssl: bool = False,
) -> dict[str, Any]:
    code = _symbol_code(symbol) if symbol else ""
    ssl_visibility = _ssl_visibility(network=network, insecure_ssl=insecure_ssl)
    if not code:
        return {
            "ok": False, "provider": provider, "symbol": "",
            "limit": limit, "data": [], "status": "invalid_symbol",
            "failed_reason": "Need a valid 6-digit symbol.",
            "disclaimer": RISK_NOTE,
            **ssl_visibility,
        }

    items: list[dict[str, str]] = []
    live_fetch_failed_reason = ""

    # Try live fetch
    if network:
        try:
            items = _fetch_announce_eastmoney(code, limit, insecure_ssl=insecure_ssl)
            if items:
                # Cache to disk
                date_str = datetime.now().strftime("%Y%m%d")
                cache_path = NEWS_DIR / f"{date_str}_{code}.csv"
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with cache_path.open("w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=["title", "content", "published", "source", "url"])
                    writer.writeheader()
                    for item in items:
                        writer.writerow(item)
                return {
                    "ok": True, "provider": "eastmoney_announce", "symbol": code,
                    "limit": limit, "data": items, "status": "fetched",
                    "cached_to": str(cache_path),
                    "disclaimer": RISK_NOTE,
                    **ssl_visibility,
                }
        except Exception as exc:
            # Fall through to cache
            live_fetch_failed_reason = f"Live fetch failed: {exc}"

    # Read from local cache
    items = _read_local_news(code, limit)
    if items:
        return {
            "ok": True, "provider": "local_news_cache", "symbol": code,
            "limit": limit, "data": items, "status": "cached",
            "failed_reason": "" if not network else f"{live_fetch_failed_reason or 'Live fetch returned no rows'}; showing cached data.",
            "disclaimer": RISK_NOTE,
            **ssl_visibility,
        }

    if network and live_fetch_failed_reason:
        return {
            "ok": False, "provider": provider, "symbol": code,
            "limit": limit, "data": [], "status": "failed",
            "failed_reason": f"{live_fetch_failed_reason}; no cached news.",
            "disclaimer": RISK_NOTE,
            **ssl_visibility,
        }

    return {
        "ok": True, "provider": provider, "symbol": code,
        "limit": limit, "data": [], "status": "no_news",
        "failed_reason": "No cached news and network fetch failed or disabled.",
        "disclaimer": RISK_NOTE,
        **ssl_visibility,
    }


def fetch_news_batch(
    symbols: list[str],
    limit_per_symbol: int = 5,
    *,
    network: bool = False,
) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        results[sym] = fetch_news(sym, limit=limit_per_symbol, network=network)
    total = sum(len(r["data"]) for r in results.values())
    return {
        "ok": True, "total_items": total, "symbols_checked": len(symbols),
        "network": network, "results": results,
        "disclaimer": RISK_NOTE,
    }


def _read_local_news(code: str, limit: int) -> list[dict[str, str]]:
    """Read cached news CSVs for a symbol, newest first."""
    if not NEWS_DIR.exists():
        return []
    files = sorted(NEWS_DIR.glob(f"*_{code}.csv"), reverse=True)
    items: list[dict[str, str]] = []
    for path in files[:5]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    # Handle both old format (新闻标题) and new format (title)
                    items.append({
                        "title": row.get("title") or row.get("新闻标题", ""),
                        "content": row.get("content") or row.get("新闻内容", ""),
                        "published": row.get("published") or row.get("发布时间", ""),
                        "source": row.get("source") or row.get("文章来源", ""),
                        "url": row.get("url") or row.get("新闻链接", ""),
                    })
                    if len(items) >= limit:
                        return items
        except (OSError, UnicodeDecodeError):
            continue
    return items


def refresh_single_news(symbol: str) -> dict[str, Any]:
    """On-demand refresh for one symbol, always with network."""
    return fetch_news(symbol, limit=20, network=True)


def news_cache_stats() -> dict[str, Any]:
    """Return statistics about the local news cache."""
    if not NEWS_DIR.exists():
        return {"exists": False, "file_count": 0}
    files = list(NEWS_DIR.glob("*.csv"))
    dates: set[str] = set()
    symbols: set[str] = set()
    for f in files:
        parts = f.stem.split("_")
        if len(parts) >= 2 and len(parts[0]) == 8:
            dates.add(parts[0])
            symbols.add(parts[1])
    return {
        "exists": True,
        "file_count": len(files),
        "unique_symbols": len(symbols),
        "unique_dates": len(dates),
        "latest_date": max(dates) if dates else None,
        "oldest_date": min(dates) if dates else None,
    }
