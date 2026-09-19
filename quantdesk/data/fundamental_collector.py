from __future__ import annotations

from datetime import datetime
from typing import Any

from quantdesk.data_store.repositories import FundamentalRepository
from quantdesk.data_store.sqlite_store import QuantDeskDB

FUNDAMENTAL_FIELDS = [
    "pe_ttm",
    "pb",
    "ps_ttm",
    "roe",
    "roa",
    "gross_profit_margin",
    "net_profit_margin",
    "debt_to_assets",
    "current_ratio",
    "revenue_yoy",
    "profit_yoy",
    "dividend_yield",
]


def _safe_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(str(value).replace("%", "").replace(",", ""))
    except Exception:
        return None


def collect_fundamental_for_symbol(symbol: str, *, network: bool = False) -> dict:
    """Collect one symbol's fundamentals only when network is explicitly enabled."""

    if not network:
        return {"symbol": symbol, "status": "skipped", "reason": "network_disabled"}
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:
        return {"symbol": symbol, "status": "skipped", "reason": f"akshare_unavailable:{type(exc).__name__}"}
    try:
        fin = ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
    except Exception as exc:  # pragma: no cover - provider/network dependent
        return {"symbol": symbol, "status": "failed", "reason": f"fetch_failed:{type(exc).__name__}"}
    if fin is None or fin.empty:
        return {"symbol": symbol, "status": "warning", "reason": "empty_response"}
    latest = fin.iloc[0]
    return {
        "symbol": symbol,
        "report_date": str(latest.get("报告期", "") or latest.get("报告日期", "") or datetime.now().date()),
        "pe_ttm": _safe_float(latest.get("市盈率")),
        "pb": _safe_float(latest.get("市净率")),
        "roe": _safe_float(latest.get("净资产收益率")),
        "roa": _safe_float(latest.get("总资产报酬率")),
        "revenue_yoy": _safe_float(latest.get("营业收入同比增长率")),
        "profit_yoy": _safe_float(latest.get("净利润同比增长率")),
        "debt_to_assets": _safe_float(latest.get("资产负债率")),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "akshare_ths_financial_abstract",
        "status": "passed",
    }


def upsert_fundamental(db: QuantDeskDB, data: dict) -> dict:
    if not data or data.get("status") != "passed":
        return {"status": "skipped", "reason": "no_passed_data"}
    db.initialize()
    return FundamentalRepository(db).upsert_fundamentals([normalize_fundamental_row(data)])


def normalize_fundamental_row(data: dict) -> dict:
    return {
        "symbol": data.get("symbol"),
        "report_date": data.get("report_date") or datetime.now().date().isoformat(),
        "pe": data.get("pe_ttm"),
        "pb": data.get("pb"),
        "ps": data.get("ps_ttm"),
        "roe": data.get("roe"),
        "roa": data.get("roa"),
        "gross_margin": data.get("gross_profit_margin"),
        "net_margin": data.get("net_profit_margin"),
        "debt_ratio": data.get("debt_to_assets"),
        "revenue_yoy": data.get("revenue_yoy"),
        "net_profit_yoy": data.get("profit_yoy"),
        "source": data.get("source") or "fundamental_collector",
        "updated_at": data.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
        "raw_json": {key: data.get(key) for key in FUNDAMENTAL_FIELDS if key in data},
    }


def get_latest_fundamental(symbol: str, db: QuantDeskDB | None = None) -> dict | None:
    database = db or QuantDeskDB(read_only=True)
    try:
        return FundamentalRepository(database).latest_fundamentals(symbol)
    except Exception:
        return None


def bulk_collect_and_store(symbols: list[str], db: QuantDeskDB | None = None, *, network: bool = False) -> dict:
    database = db or QuantDeskDB()
    collected = 0
    skipped = 0
    failed = 0
    for symbol in symbols:
        data = collect_fundamental_for_symbol(symbol, network=network)
        if data.get("status") == "passed":
            upsert_fundamental(database, data)
            collected += 1
        elif data.get("status") == "failed":
            failed += 1
        else:
            skipped += 1
    return {"status": "passed" if failed == 0 else "warning", "collected": collected, "skipped": skipped, "failed": failed}
