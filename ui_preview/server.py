"""QuantDesk isolated read-only preview. No project imports or market network.

Only fixed localhost GET routes and narrowly named local market/report files are
read. Nothing under the canonical project is opened for writing. Run with -B.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import local_research

ROOT = Path(os.environ.get("QUANTDESK_ROOT", str(Path(__file__).resolve().parent.parent)))
HERE = Path(__file__).resolve().parent
ZONE = timezone(timedelta(hours=8))
MAX_BYTES = 12_000_000
LABEL = "纸面 · 未验证 · 只读"
SYMBOL = re.compile(r"^[0-9]{6}$")
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
UPSTREAM_ROUTES = frozenset({"/api/daily-companion", "/api/ai-trader/decisions", "/weekly-learning"})
WORKBENCH_ROUTE = re.compile(r"^/api/stock/([0-9]{6})/workbench$")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/ui.js": ("ui.js", "text/javascript; charset=utf-8")}
for asset in ("charts.js", "drawer.js", "pages.js", "drawer.css", "pages.css", "research.js", "research.css", "glossary.js", "glossary.css"):
    mime = "text/javascript" if asset.endswith(".js") else "text/css"
    STATIC["/" + asset] = (asset, mime + "; charset=utf-8")
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def today() -> str:
    return datetime.now(ZONE).date().isoformat()


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(str(value).replace(",", "").replace("%", "").strip())
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def day_value(value: Any) -> str | None:
    raw = str(value or "")[:10]
    try:
        return raw if DATE.fullmatch(raw) and date.fromisoformat(raw).isoformat() == raw else None
    except ValueError:
        return None


def safe_bytes(path: Path, limit: int = MAX_BYTES) -> bytes:
    # Do not follow directory links outside the specifically selected root.
    absolute = path.resolve()
    if not absolute.is_relative_to(ROOT.resolve()) or absolute != ROOT.resolve() / path.relative_to(ROOT):
        raise OSError("outside_allowlisted_root")
    before = path.stat()
    if before.st_size > limit:
        raise OSError("oversized_source")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    after = path.stat()
    if len(content) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("source_changed_during_read")
    return content


def object_file(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(safe_bytes(path).decode("utf-8-sig"), parse_constant=lambda _: None)
        return obj if isinstance(obj, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def csv_file(path: Path) -> list[dict[str, str]]:
    try:
        raw = safe_bytes(path).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(raw, newline="")))
    except (OSError, UnicodeError, csv.Error):
        return []


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def upstream(path: str, selected_day: str | None = None) -> Any:
    workbench_match = WORKBENCH_ROUTE.fullmatch(path)
    allowed_workbench = bool(workbench_match and known_symbol(workbench_match.group(1)))
    if path not in UPSTREAM_ROUTES and not allowed_workbench:
        return {"available": False, "warnings": ["此端点未获只读授权"]}
    if selected_day and (not day_value(selected_day) or selected_day > today()):
        return {"available": False, "warnings": ["日期无效或在未来"]}
    query = "?" + urlencode({"date": selected_day}) if selected_day and path in {"/api/daily-companion", "/weekly-learning"} else ""
    key = path + query
    with _lock:
        cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < 12:
        return cached[1]
    try:
        request = Request("http://127.0.0.1:8000" + key, headers={"Accept": "application/json, text/plain", "User-Agent": "QuantDesk-8035-ReadOnly"}, method="GET")
        # Ignore OS proxy settings; never follow redirects to another address.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=25) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("upstream_response_too_large")
            text = raw.decode("utf-8-sig")
            value = text if path == "/weekly-learning" else json.loads(text, parse_constant=lambda _: None)
        with _lock:
            _cache[key] = (time.monotonic(), value)
        return value
    except (OSError, HTTPError, URLError, ValueError, UnicodeError):
        return {"available": False, "warnings": ["8000只读接口暂不可读；未补跑任何任务"], "source": key}


def classify_original(rows: list[dict[str, str]]) -> tuple[str, str]:
    """Conservative equivalent of project classify_frame_price_basis.

    Check EVERY original row before sorting/date selection. Vocabulary follows
    enriched.columns.normalize_adjust_token; no provider/file-name inference.
    The preview only permits raw, making the cross-stock accepted basis raw.
    """
    if not rows or any("adjust" not in row for row in rows):
        return "mixed_or_unknown", "日线未声明价格口径"
    aliases = {"raw": "raw", "none": "raw", "不复权": "raw", "no": "raw", "unadjusted": "raw", "原始": "raw", "qfq": "qfq", "前复权": "qfq", "forward": "qfq", "forward_adjusted": "qfq", "qfq_factor": "qfq", "hfq": "hfq", "后复权": "hfq", "backward": "hfq", "back_adjusted": "hfq", "back": "hfq"}
    bases = {aliases.get(str(row.get("adjust") or "").strip().lower(), "unknown") for row in rows}
    if len(bases - {"unknown"}) > 1:
        return "mixed_or_unknown", "原始全表混用价格口径，走势图暂停显示"
    if "unknown" in bases:
        return "mixed_or_unknown", "原始全表存在未知价格口径，走势图暂停显示"
    if bases != {"raw"}:
        return next(iter(bases)), "非原始价格；本只读预览不转换价格口径"
    return "raw", ""


def daily_series(code: str, until: str) -> tuple[list[list[Any]], dict[str, Any]]:
    rows = csv_file(ROOT / "data/daily" / (code + ".csv"))
    basis, warning = classify_original(rows)
    meta: dict[str, Any] = {"price_basis": basis, "warnings": [warning] if warning else [], "source": "data/daily/" + code + ".csv"}
    if warning:
        return [], meta
    values: dict[str, float] = {}
    conflict: set[str] = set()
    latest_pct: dict[str, float | None] = {}
    excluded = 0
    for row in rows:
        day = day_value(row.get("date"))
        price = number(row.get("close"))
        provisional = any("partial_daily" in str(value).lower() for value in row.values())
        stamp = row.get("updated_at") or row.get("fetched_at")
        stamp_day = day_value(stamp)
        if not day or day > until or price is None or price <= 0 or provisional or (stamp_day and stamp_day > today()):
            excluded += 1
            continue
        if day in values and abs(values[day] - price) > 1e-8:
            conflict.add(day)
        values[day] = price
        latest_pct[day] = number(row.get("pct_chg"))
    pairs = [[day, values[day]] for day in sorted(values) if day not in conflict][-60:]
    if pairs:
        last_day = pairs[-1][0]
        pct = latest_pct.get(last_day)
        if pct is None and len(pairs) >= 2:
            pct = (pairs[-1][1] / pairs[-2][1] - 1) * 100
        meta.update(price=pairs[-1][1], price_date=last_day, pct_chg=pct)
    if conflict:
        meta["warnings"].append("同日收盘价冲突，相关交易日留缺")
    if len(pairs) < 60:
        meta["warnings"].append(f"只有{len(pairs)}个可用交易日，不补造60日")
    return pairs, meta


def open_dates() -> dict[str, bool]:
    path = ROOT / "data/calendar/a_share_trading_calendar.csv"
    try:
        stat = path.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        return {}
    with _lock:
        cached = _cache.get("calendar_dates")
        if cached and cached[1][0] == stamp:
            return cached[1][1]
    values = {r["date"]: r["is_open"].lower() in {"true", "1"} for r in csv_file(path)
              if day_value(r.get("date")) and str(r.get("is_open", "")).lower() in {"true", "false", "0", "1"}}
    with _lock:
        _cache["calendar_dates"] = (time.monotonic(), (stamp, values))
    return values


def stock_ohlcv(code: str, until: str) -> dict[str, Any]:
    payload = {"symbol": code, "rows": [], "columns": ["date", "open", "high", "low", "close", "volume"],
               "price_basis": None, "source": None, "warnings": [], "available": False, "label": LABEL}
    if not SYMBOL.fullmatch(code):
        payload["warnings"].append("股票代码无效")
        return payload
    payload["source"] = f"data/daily/{code}.csv"
    source = csv_file(ROOT / payload["source"])
    basis, warning = classify_original(source)
    payload["price_basis"] = basis
    if warning:
        payload["warnings"].append(warning)
        return payload
    calendar = open_dates()
    if not calendar:
        payload["warnings"].append("交易日历不可读，无法核实日K交易日")
        return payload
    by_day = {}
    conflicts = set()
    for row in source:
        day = row.get("date", "")
        if not calendar.get(day) or day > until:
            continue
        if row.get("symbol", code).zfill(6) != code or any("partial_daily" in str(v).lower() for v in row.values()):
            continue
        stamp = day_value(row.get("updated_at") or row.get("fetched_at"))
        if stamp and stamp > today():
            continue
        bar = [number(row.get(column)) for column in payload["columns"][1:]]
        if day in by_day and bar != by_day[day]:
            conflicts.add(day)
        by_day[day] = bar
    for day in sorted(by_day)[-60:]:
        o, h, l, c, v = by_day[day]
        if (day in conflicts or None in (o, h, l, c, v) or min(o, h, l, c) <= 0 or
                v < 0 or h < max(o, c, l) or l > min(o, c, h)):
            payload["warnings"].append(day + " 日K字段不完整、冲突或价格区间有误，留缺")
            continue
        payload["rows"].append([day, o, h, l, c, v])
    payload["available"] = bool(payload["rows"])
    if len(payload["rows"]) < 60:
        payload["warnings"].append(f"有 {len(payload['rows'])} 条可核实日K")
    return payload


def equity_history(until: str) -> dict[str, Any]:
    sources = {"ai": ["state/ai_trader/journal.jsonl", "state/ai_trader/ledger.json"],
               "legacy": "results/auto_paper_portfolio_history.json", "benchmark": "data/benchmark/hs300.csv",
               "calendar": "data/calendar/a_share_trading_calendar.csv"}
    warnings = []
    ai = {}
    try:
        lines = safe_bytes(ROOT / sources["ai"][0]).decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        lines = []
        warnings.append("AI 账历史不可读")
    unreadable = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(record, dict):
            unreadable += 1
            continue
        day = day_value(record.get("as_of"))
        account = record.get("account")
        if day and day <= until:
            ai[day] = number(account.get("equity") if isinstance(account, dict) else record.get("equity"))
    if unreadable:
        warnings.append(f"AI 历史有 {unreadable} 行不可读")
    book = object_file(ROOT / sources["ai"][1])
    day = day_value(book.get("as_of"))
    if day and day <= until and number(book.get("equity")) is not None:
        ai[day] = number(book["equity"])

    legacy = {}
    latest = {}
    for record in object_file(ROOT / sources["legacy"]).get("items", []):
        if not isinstance(record, dict):
            continue
        day = day_value(record.get("valuation_date")) or day_value(record.get("generated_at"))
        stamp = str(record.get("generated_at") or "")
        if day and day <= until and stamp >= latest.get(day, ""):
            legacy[day] = number(record.get("total_equity"))
            latest[day] = stamp
    index = {}
    conflicts = set()
    for record in csv_file(ROOT / sources["benchmark"]):
        day = day_value(record.get("date"))
        close = number(record.get("close"))
        if day and day <= until and close is not None and close > 0:
            if day in index and index[day] != close:
                conflicts.add(day)
            index[day] = close
    for day in conflicts:
        index[day] = None
    if conflicts:
        warnings.append("基准同日收盘值冲突，相关日期留空")
    calendar = open_dates()
    days = sorted({d for d, is_open in calendar.items() if is_open} | {d for d in index if d not in calendar})
    if not calendar:
        warnings.append("日历不可用，仅按已存沪深300日线对齐")
    for label, data in (("AI 账", ai), ("旧策略账", legacy), ("沪深300", index)):
        if not data:
            warnings.append(label + "历史缺失")
    records = set(ai) | set(legacy)
    trading_days = set(days)
    starts = [d for d in records if d in trading_days]
    ends = [d for d in records | set(index) if d in trading_days]
    start, end = min(starts, default=None), max(ends, default=None)
    rows = [{"date": d, "ai": ai.get(d), "legacy": legacy.get(d), "benchmark": index.get(d)}
            for d in days if start and end and start <= d <= end]
    return {"rows": rows, "available": bool(rows), "sources": sources,
            "units": {"ai": "CNY", "legacy": "CNY", "benchmark": "index_points"},
            "warnings": warnings, "as_of": end, "label": LABEL}


def valuation_map(until: str) -> tuple[dict[str, dict[str, Any]], str | None]:
    folder = ROOT / "data/metadata/valuation_snapshots"
    paths = sorted(p for p in folder.glob("????????.csv") if re.fullmatch(r"[0-9]{8}", p.stem) and p.stem <= until.replace("-", ""))
    if not paths:
        return {}, None
    path = paths[-1]
    result = {}
    for row in csv_file(path):
        code = str(row.get("symbol") or row.get("code") or "").zfill(6)
        day = day_value(row.get("trade_date"))
        if not SYMBOL.fullmatch(code) or not day or day > until:
            continue
        mv = number(row.get("total_mv_raw"))
        unit = row.get("total_mv_unit")
        # Never assume the provider's market-cap unit.
        yi = mv / 10000 if mv is not None and unit == "wan_yuan" else mv / 100000000 if mv is not None and unit == "yuan" else mv if unit == "yi_yuan" else None
        result[code] = {"name": row.get("name") or None, "close": number(row.get("close")), "pct_chg": number(row.get("pct_chg")), "pe": number(row.get("pe")), "pb": number(row.get("pb")), "total_mv_raw": mv, "total_mv_unit": unit, "total_mv_yi": yi, "turnover_rate": number(row.get("turnover_rate")), "trade_date": day, "pe_semantics": row.get("pe_semantics"), "source": str(path.relative_to(ROOT)).replace("\\", "/")}
    return result, path.stem[:4] + "-" + path.stem[4:6] + "-" + path.stem[6:8]


def earnings_map(until: str) -> dict[str, dict[str, Any]]:
    rows = csv_file(ROOT / "data/metadata/earnings_report_2026Q2.csv")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("股票代码") or row.get("symbol") or "").zfill(6)
        if not SYMBOL.fullmatch(code):
            continue
        notice = day_value(row.get("最新公告日期") or row.get("announcement_date"))
        valid = bool(notice and notice <= until)
        explicit_period = day_value(row.get("报告期") or row.get("report_date"))
        period_verified = explicit_period == "2026-06-30"
        warnings = []
        if not valid:
            warnings.append("公告日期缺失或晚于所选资料日，财报数字留空")
        if not period_verified:
            warnings.append("文件标为2026Q2，但无可核验报告期字段；不证明为中报")
        item = {"rev_yoy": number(row.get("营业总收入-同比增长")) if valid else None, "profit_yoy": number(row.get("净利润-同比增长")) if valid else None, "roe": number(row.get("净资产收益率")) if valid else None, "gross_margin": number(row.get("销售毛利率")) if valid else None, "industry": row.get("所处行业") or None, "announcement_date": notice, "period_label": "2026Q2", "period_verified": period_verified, "available": valid, "name": row.get("股票简称"), "warnings": warnings, "source": "data/metadata/earnings_report_2026Q2.csv"}
        if code not in result or str(notice or "") > str(result[code].get("announcement_date") or ""):
            result[code] = item
    result.update(local_research.earnings(until))
    return result


def benchmark(until: str) -> dict[str, Any]:
    values: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()
    for row in csv_file(ROOT / "data/benchmark/hs300.csv"):
        day = day_value(row.get("date"))
        close = number(row.get("close"))
        if not day or day > until or close is None or close <= 0:
            continue
        if day in values and number(values[day].get("close")) != close:
            conflicts.add(day)
        values[day] = row
    days = sorted(d for d in values if d not in conflicts)
    if not days:
        return {"available": False, "name": "沪深300", "source": "data/benchmark/hs300.csv"}
    last = values[days[-1]]
    close, pct = number(last.get("close")), number(last.get("pct_chg"))
    if pct is None and len(days) >= 2:
        prior = number(values[days[-2]].get("close"))
        pct = (close / prior - 1) * 100 if close and prior else None
    return {"available": True, "name": "沪深300", "close": close, "price": close, "pct_chg": pct, "as_of": days[-1], "price_date": days[-1], "source": "data/benchmark/hs300.csv", "note": "价格指数涨跌，不含分红"}


def account_summary(book: dict[str, Any], kind: str) -> dict[str, Any]:
    equity = number(book.get("equity") if kind == "ai" else book.get("total_equity"))
    initial = number(book.get("initial_cash"))
    cash = number(book.get("cash"))
    mv = number(book.get("market_value"))
    as_of = day_value(book.get("valuation_date") or book.get("as_of"))
    valid = bool(book and as_of and as_of <= today())
    return {"available": valid, "label": "AI账" if kind == "ai" else "旧策略对照", "as_of": as_of, "equity": equity if valid else None, "cash": cash if valid else None, "market_value": mv if valid else None, "initial_cash": initial, "return_pct": (equity / initial - 1) * 100 if valid and equity is not None and initial and initial > 0 else None, "cash_pct": cash / equity * 100 if valid and cash is not None and equity and equity > 0 else None, "position_pct": mv / equity * 100 if valid and mv is not None and equity and equity > 0 else None, "positions_count": len(book.get("positions", [])) if valid and isinstance(book.get("positions"), list) else 0, "unrealized_pnl": number(book.get("unrealized_pnl")), "realized_pnl": number(book.get("realized_pnl")), "source": "state/ai_trader/ledger.json" if kind == "ai" else "state/auto_paper_portfolio.json", "valuation_label": "账本已保存估值；不重算账本", "updated_at": book.get("updated_at")}


def position_item(row: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    cost = number(row.get("avg_cost"))
    if cost is None and row.get("entry_cost_basis_includes_buy_fee") is True:
        cost = number(row.get("entry_price"))
    price = number(row.get("market_price") if "market_price" in row else row.get("latest_price"))
    qty, pnl = number(row.get("quantity")), number(row.get("unrealized_pnl"))
    equity = account.get("equity")
    weight = number(row.get("weight"))
    if weight is None and number(row.get("market_value")) is not None and equity and equity > 0:
        weight = number(row.get("market_value")) / equity
    return {"qty": qty, "cost": cost, "price": price, "pnl": pnl, "pnl_pct": (price / cost - 1) * 100 if price is not None and cost and cost > 0 else None, "weight": weight * 100 if weight is not None else None, "acquired_on": day_value(row.get("acquired_on") or row.get("entry_date")), "style": row.get("style") or row.get("strategy_label") or None, "as_of": day_value(row.get("valuation_date") or row.get("latest_date")) or account.get("as_of"), "thesis": row.get("thesis") or row.get("entry_reason") or None, "invalidation": row.get("invalidation") or row.get("hold_reason") or None}


def local_health(day: str) -> dict[str, Any]:
    try:
        status = safe_bytes(ROOT / "STATUS.md").decode("utf-8-sig")
    except (OSError, UnicodeError):
        status = ""
    health_line = next((line.strip() for line in status.splitlines() if "日线" in line and "快照" in line and "策略账" in line and "AI账" in line), None)
    manifest = object_file(ROOT / "data/snapshots" / day / "manifest.json")
    collected = number(manifest.get("collected_slot_count"))
    expected = number(manifest.get("expected_slot_count"))
    fields = number(manifest.get("field_verified_complete_slot_count"))
    snapshot = {"as_of": day_value(manifest.get("trade_date") or manifest.get("as_of")), "collected_slots": collected, "expected_slots": expected, "field_complete_slots": fields, "valid_quote_ratio": number(manifest.get("valid_quote_ratio")), "source": "data/snapshots/" + day + "/manifest.json", "message": f"已收到{collected:g}/{expected:g}份；字段齐备{fields:g}份，收到与字段完整分开核对" if collected is not None and expected is not None and fields is not None else "快照汇总字段缺失"}
    return {"health_line": health_line, "snapshot": snapshot}


def latest_plan_stops(ledger: dict[str, Any], day: str) -> tuple[dict[str, float], dict[str, Any]]:
    plan = object_file(ROOT / "state/ai_trader/plan_latest.json")
    decision_day = day_value(plan.get("decision_date"))
    trade_day = day_value(plan.get("trade_date"))
    valid = bool(plan.get("schema_version") == "ai_trader_plan_v1" and plan.get("committed") is True and plan.get("ledger_id") == ledger.get("ledger_id") and decision_day and decision_day <= day and trade_day and trade_day >= decision_day and plan.get("status") in {"pending_open", "executed", "settled", "completed"} and (plan.get("statement_review") or {}).get("status") == "accepted")
    stops: dict[str, float] = {}
    held = {str(row.get("symbol")) for row in ledger.get("positions", []) if isinstance(row, dict)}
    rows = [*plan.get("coach_risk_register", []), *plan.get("intents", [])] if valid else []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("symbol")) not in held or row.get("status") in {"rejected", "abandoned", "invalid"}:
            continue
        if row.get("side") not in {"buy", "hold"}:
            continue
        if row.get("decision_origin") != "model" and not ((plan.get("llm") or {}).get("used") is True):
            continue
        verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
        due = day_value(verification.get("due_date"))
        if due and due < day:
            continue
        stop = number(row.get("stop_loss_price"))
        if stop is None:
            stop = number(verification.get("invalidation_level"))
        if stop is not None and stop > 0:
            stops[str(row["symbol"])] = stop
    intents = plan.get("intents") if isinstance(plan.get("intents"), list) else []
    accepted_intents = [row for row in intents if isinstance(row, dict) and row.get("status") not in {"rejected", "abandoned", "invalid"}]
    trade_intents = [row for row in accepted_intents if row.get("side") in {"buy", "sell"}]
    pending_count = len(trade_intents) if valid and plan.get("status") == "pending_open" else 0
    meta = {"source": "state/ai_trader/plan_latest.json", "plan_id": plan.get("plan_id"), "decision_date": decision_day, "trade_date": trade_day, "status": plan.get("status"), "committed": plan.get("committed"), "decision_mode": plan.get("decision_mode"), "readable": bool(plan), "accepted_envelope": valid, "numeric_stop_count": len(stops), "intents_count": len(intents), "committed_intents_count": len(trade_intents) if valid else 0, "pending_intents_count": pending_count, "hold_intents_count": sum(row.get("side") == "hold" for row in accepted_intents), "intents": [{key: row.get(key) for key in ("symbol", "side", "status", "quantity", "reason")} for row in accepted_intents], "summary": "已保存计划：无新增交易意图" if plan and not trade_intents else f"已保存{len(trade_intents)}条买卖意图；待开盘{pending_count}条" if plan else "尚无可读的保存计划", "note": "已核最新保存计划：未找到有效数字止损或失效价" if not stops else "数字价位来自最新保存计划，仅作纸面未验证展示"}
    meta['intents'] = [local_research.readable_plan_intent(row) for row in accepted_intents] if valid else []
    meta['execution_label'] = '纸面次日开盘计划 · 未成交' if pending_count else '已保存纸面计划 · 以实际流水为准'
    meta['display_warning'] = '展示原计划，不改其动作；理由与卖出动作可能矛盾，不能据此视为已成交或已验证建议。'
    return stops, meta


def candidate_data(until: str) -> dict[str, Any]:
    batch_path = ROOT / "results/candidate_refresh/formal_candidate_batch_latest.json"
    tracking_path = ROOT / "results/candidate_tracking/latest.json"
    batch = object_file(batch_path)
    tracking = object_file(tracking_path)
    as_of = day_value(batch.get("target_date"))
    warnings = list(batch.get("reason_codes", [])) if isinstance(batch.get("reason_codes"), list) else []
    valid = bool(as_of and as_of <= until and batch.get("formal_eligible") is True and batch.get("status") == "passed")
    if not valid:
        warnings.append("最新候选批次不可核实、未具正式资格或日期在未来；不冒充可用候选")
    if as_of and as_of != until:
        warnings.append("这是最新已保存批次，资料日" + as_of + "；并非当日新生成名单")
    tracked = {}
    if day_value(tracking.get("target_date")) == as_of and tracking.get("price_basis") == "raw":
        for item in tracking.get("entries", []):
            if isinstance(item, dict) and SYMBOL.fullmatch(str(item.get("symbol", ""))):
                tracked[str(item["symbol"])] = item
    items = []
    rows = batch.get("candidates") if isinstance(batch.get("candidates"), list) else []
    for row in rows if valid else []:
        if not isinstance(row, dict) or not SYMBOL.fullmatch(str(row.get("symbol", ""))):
            continue
        code = str(row["symbol"])
        data_date = day_value(row.get("data_latest_date"))
        evidence = tracked.get(code)
        items.append({"code": code, "name": row.get("name") or code, "as_of": as_of, "data_date": data_date, "rank": number(row.get("rank")), "score": number(row.get("score")), "strategy_type": row.get("strategy_type"), "strategy_label": row.get("strategy_label"), "reason": row.get("reason"), "price": number(row.get("close")) if data_date and data_date <= until else None, "batch_close": number(row.get("close")) if data_date and data_date <= until else None, "sector": row.get("sector"), "industry": row.get("industry"), "formal_eligible": valid, "tracking": {key: evidence.get(key) for key in ("sample_status", "horizons", "entry_price", "entry_price_source", "hit_flag", "warnings")} if isinstance(evidence, dict) else {"sample_status": "missing", "horizons": {}, "warnings": ["未找到与该批次同日的跟踪记录"]}, "source": "results/candidate_refresh/formal_candidate_batch_latest.json"})
    declared_count = number(batch.get("candidate_count"))
    if valid and declared_count != len(items):
        warnings.append("源文件候选总数与可读条目数不一致")
    return {"available": valid, "as_of": as_of, "count": len(items), "declared_count": declared_count, "status": batch.get("status") or "missing", "items": items, "generated_batch": batch.get("generated_batch"), "source": "results/candidate_refresh/formal_candidate_batch_latest.json", "tracking_source": "results/candidate_tracking/latest.json", "tracking_as_of": day_value(tracking.get("target_date")), "warnings": warnings, "label": LABEL}


def known_symbol(code: str) -> bool:
    """An unknown six-digit code never causes an upstream calculation request."""
    if not SYMBOL.fullmatch(code):
        return False
    pool = object_file(ROOT / "ai_trader/prompts/candidates/bluechip_coach_v1/menu.json")
    if code in pool.get("pool_symbols", []):
        return True
    for relative in ("state/ai_trader/ledger.json", "state/auto_paper_portfolio.json"):
        rows = object_file(ROOT / relative).get("positions", [])
        if isinstance(rows, list) and any(isinstance(row, dict) and row.get("symbol") == code for row in rows):
            return True
    return any(item["code"] == code for item in candidate_data(today())["items"])


def stock_reference(code: str) -> dict[str, Any]:
    source_path = "/api/stock/" + code + "/workbench"
    if not known_symbol(code):
        return {"available": False, "symbol": code, "status": "outside_preview_scope", "buy": None, "stop": None, "upper": None, "buy_low": None, "buy_high": None, "levels": [], "reason_codes": ["symbol_not_in_local_preview_scope"], "warnings": ["此代码不在本地持仓、蓝筹池或真实候选名单；没有向8000发请求"], "message": "暂无该股票可核实的本地观察资料", "source": source_path, "raw": None, "label": LABEL}
    source = upstream(source_path)
    raw = source if isinstance(source, dict) else {}
    reference = raw.get("reference") if isinstance(raw.get("reference"), dict) else {}
    status = str(reference.get("status") or "missing")
    ref_day = day_value(reference.get("as_of"))
    reason_codes = list(reference.get("reason_codes", [])) if isinstance(reference.get("reason_codes"), list) else []
    warnings = list(reference.get("warnings", [])) if isinstance(reference.get("warnings"), list) else []
    try:
        valid_until = datetime.fromisoformat(str(reference.get("valid_until") or "").replace("Z", "+00:00"))
        not_expired = valid_until.tzinfo is not None and datetime.now(ZONE) < valid_until
    except ValueError:
        not_expired = False
    available = bool(raw.get("ok") is True and raw.get("canonical_writes") is False and raw.get("llm_used") is False and raw.get("calendar_verified") is True and reference.get("available") is True and reference.get("reference_current") is True and reference.get("price_basis") == "raw" and not reference.get("degraded") and status not in {"degraded", "blocked", "missing", "failed"} and ref_day and ref_day <= today() and not_expired)
    if not available:
        warnings.append(reference.get("message") or raw.get("calendar_message") or "参考日期、价格口径、有效期或可用状态未通过；价位留空")
    levels = []
    for row in reference.get("levels", []) if isinstance(reference.get("levels"), list) else []:
        if not isinstance(row, dict):
            continue
        levels.append({"kind": row.get("kind"), "label": row.get("label"), "price": number(row.get("price")) if available else None, "condition": row.get("condition"), "basis": row.get("basis")})
    mapped = {row["kind"]: row for row in levels}
    buy = mapped.get("buy", {}).get("price")
    stop = mapped.get("stop", {}).get("price")
    upper = mapped.get("profit", {}).get("price")
    for value in (buy, stop, upper):
        if value is not None and value <= 0:
            available = False
            warnings.append("上游参考价含非正数，未显示算法价位")
    if not available:
        buy = stop = upper = None
        for row in levels:
            row["price"] = None
    return {"available": available, "symbol": code, "status": status if available else "degraded", "as_of": ref_day, "valid_until": reference.get("valid_until"), "reference_current": reference.get("reference_current") is True and not_expired, "price_basis": reference.get("price_basis"), "entry_setup_available": reference.get("entry_setup_available") if available else False, "buy": buy, "stop": stop, "upper": upper, "buy_low": None, "buy_high": None, "buy_kind": "single_confirmation_level", "buy_range_note": "原接口提供买入确认单点，未提供买入区间；不虚构上下沿", "levels": levels, "reason_codes": reason_codes, "warnings": warnings, "current_assessment": reference.get("current_assessment"), "message": reference.get("message") or ("日线研究参考 · 未验证" if available else "日线参考不可用，价位留空"), "validity_text": reference.get("validity_text"), "rule_version": reference.get("rule_version"), "source": source_path, "source_sha256": reference.get("source_sha256"), "algorithm_only": True, "actionable_order": False, "label": LABEL, "raw": raw}


def workspace() -> dict[str, Any]:
    day = today()
    with _lock:
        old = _cache.get("workspace")
    if old and time.monotonic() - old[0] < 5:
        return old[1]
    ai = object_file(ROOT / "state/ai_trader/ledger.json")
    legacy = object_file(ROOT / "state/auto_paper_portfolio.json")
    accounts = {"ai": account_summary(ai, "ai"), "legacy": account_summary(legacy, "legacy")}
    stops, plan_meta = latest_plan_stops(ai, day)
    pool = object_file(ROOT / "ai_trader/prompts/candidates/bluechip_coach_v1/menu.json")
    pool_symbols = [str(s) for s in pool.get("pool_symbols", []) if SYMBOL.fullmatch(str(s))]
    positions = {"ai": {str(p.get("symbol")): p for p in ai.get("positions", []) if isinstance(p, dict) and SYMBOL.fullmatch(str(p.get("symbol", "")))}, "legacy": {str(p.get("symbol")): p for p in legacy.get("positions", []) if isinstance(p, dict) and SYMBOL.fullmatch(str(p.get("symbol", "")))}}
    candidates = candidate_data(day)
    candidate_index = {item["code"]: item for item in candidates["items"]}
    codes = sorted(set(pool_symbols) | set(positions["ai"]) | set(positions["legacy"]) | set(candidate_index))
    vals, val_day = valuation_map(day)
    earnings = earnings_map(day)
    dividends = local_research.dividends(day)
    percentiles = local_research.percentiles()
    announcements = local_research.announcements()
    with ThreadPoolExecutor(max_workers=5) as executor:
        series_rows = dict(zip(codes, executor.map(lambda code: daily_series(code, day), codes)))
    stocks = []
    for code in codes:
        series, meta = series_rows[code]
        valuation, earning = vals.get(code, {}), earnings.get(code, {})
        name = valuation.get("name") or candidate_index.get(code, {}).get("name") or earning.get("name") or positions["legacy"].get(code, {}).get("name") or code
        ai_pos = position_item(positions["ai"][code], accounts["ai"]) if code in positions["ai"] else None
        old_pos = position_item(positions["legacy"][code], accounts["legacy"]) if code in positions["legacy"] else None
        price = meta.get("price")
        price_day = meta.get("price_date")
        pct = meta.get("pct_chg")
        # The latest dated valuation CSV is a quote source, not a replacement
        # for blocked historical bars; dates remain separately visible.
        if valuation.get("close") is not None and (not price_day or valuation.get("trade_date", "") >= price_day):
            price, price_day, pct = valuation["close"], valuation["trade_date"], valuation.get("pct_chg")
        stocks.append({"code": code, "name": name, "bluechip": code in pool_symbols, "candidate": code in candidate_index, "candidate_info": candidate_index.get(code), "series": series, "price": price, "price_date": price_day, "pct_chg": pct, "price_basis": meta["price_basis"], "valuation": valuation, "earnings": earning, "ai_position": ai_pos, "legacy_position": old_pos, "thesis": ai_pos.get("thesis") if ai_pos else None, "invalidation": ai_pos.get("invalidation") if ai_pos else None, "stop_price": stops.get(code), "stop_source": plan_meta["source"], "stop_status": plan_meta["note"], "warnings": meta["warnings"] + earning.get("warnings", []), "daily_source": meta["source"]})
    by_code = {stock["code"]: stock for stock in stocks}
    for stock in stocks:
        stock['dividend'] = dividends.get(stock['code'], {})
        cached = percentiles.get('stocks', {}).get(stock['code'], {})
        # Do not attach a stale percentile to a different current valuation.
        stock['percentiles'] = cached if (cached.get('as_of') == stock['valuation'].get('trade_date')
            and all(cached.get(k, {}).get('current') == stock['valuation'].get(k) for k in ('pe','pb'))) else {}
        stock['announcements'] = {'sampled':stock['code'] in announcements['stocks'],
            'items':announcements['stocks'].get(stock['code'],[]),'notice':announcements['notice'],
            'source_sha256':announcements['source_sha256']}
        series = stock['series']
        stock['change_30d'] = (series[-1][1] / series[-31][1] - 1) * 100 if len(series)>=31 else None
        stock['change_30d_from'] = series[-31][0] if len(series)>=31 else None
        stock['change_30d_to'] = series[-1][0] if len(series)>=31 else None
    for item in candidates["items"]:
        stock = by_code[item["code"]]
        item.update(series=stock["series"], price=stock["price"], price_date=stock["price_date"], pct_chg=stock["pct_chg"], price_basis=stock["price_basis"])
    source_status = local_health(day)
    snapshot = source_status["snapshot"]
    daily = {"available": sum(bool(s[0]) for s in series_rows.values()), "total": len(codes), "latest_date": max((s[0][-1][0] for s in series_rows.values() if s[0]), default=None), "scope": "本预览持仓、蓝筹与真实候选股票，不代表全市场"}
    warnings = ["账本数字来自各自保存值；当前股票报价不能替代历史估值", "累计浮盈不等于当日贡献", "核心26股优先使用已公告2026Q3累计；其余Q2文件期别未独立核验", "股息率沿用2026-11-24已核税前TTM快照，非实时重算", "历史PE与动态PE可能不同口径；估值分位仅作参考", "未保存明确有效数字止损价时，不绘止损线"]
    payload = {"as_of": day, "read_at": datetime.now(ZONE).isoformat(), "label": LABEL, "accounts": accounts, "benchmark": benchmark(day), "stocks": stocks, "candidates": candidates, "health": {"daily": daily, "snapshot": snapshot, "health_line": source_status.get("health_line"), "health_date": None, "warnings": warnings, "valuation_date": val_day}, "warnings": warnings, "pool_label": pool.get("pool_label"), "bluechip_count": len(pool_symbols), "latest_plan": plan_meta}
    with _lock:
        _cache["workspace"] = (time.monotonic(), payload)
    return payload


def review_dates() -> list[str]:
    folder = ROOT / "state/daily_companion"
    return sorted((p.name for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and day_value(p.name) == p.name and p.name <= today() and any((p / name).is_file() for name in ("evening.json", "2235.json", "1000.json", "1430.json"))), reverse=True) if folder.is_dir() else []


def saved_contributions(facts: dict[str, Any], day: str) -> list[dict[str, Any]]:
    """Use only the positions frozen in the selected day's AI receipt."""
    book = facts.get("book:AI账", {})
    positions = {r["symbol"]: r for r in book.get("positions", []) if isinstance(r, dict)
                 and SYMBOL.fullmatch(str(r.get("symbol", "")))} if isinstance(book, dict) and book.get("as_of") == day else {}
    saved = {r["symbol"]: r for k, r in facts.items() if k.startswith("holding:") and isinstance(r, dict)
             and r.get("as_of") == day and r.get("name") == "AI账" and SYMBOL.fullmatch(str(r.get("symbol", "")))}
    if not positions and not saved:
        return []
    earnings = earnings_map(day)
    valuations, _ = valuation_map(day)
    dividends = local_research.dividends(day)
    rows = []
    for code in sorted(positions.keys() | saved.keys()):
        pos, fact = positions.get(code, {}), saved.get(code, {})
        _, meta = daily_series(code, day)
        quote = meta if meta.get("price_date") == day else {}
        price = number(fact.get("price")) or quote.get("price")
        cost, quantity = number(pos.get("avg_cost")), number(pos.get("quantity"))
        pnl = number(fact.get("unrealized_pnl"))
        derived = pnl is None and price is not None and cost is not None and quantity is not None
        if derived:
            pnl = round((price - cost) * quantity, 4)
        earning = earnings.get(code, {})
        rows.append({"code": code, "name": fact.get("stock_name") or valuations.get(code, {}).get("name") or earning.get("name") or code,
                     "pnl": pnl, "cost": cost, "price": price, "quantity": quantity, "derived": derived,
                     "pnl_pct": (price / cost - 1) * 100 if price is not None and cost and cost > 0 else None,
                     "pct_chg": quote.get("pct_chg"), "profit_yoy": earning.get("profit_yoy"), "as_of": day,
                     "earnings": earning, "dividend": dividends.get(code, {}),
                     "note": "按当日已存成本、数量与价格计算的累计浮盈" if derived else "当日保存的累计浮盈" if pnl is not None else "当日价格或成本缺失"})
    return sorted(rows, key=lambda row: (row["pnl"] is None, row["pnl"] or 0))


def review(selected_day: str | None = None) -> dict[str, Any]:
    dates = review_dates()
    # Latest day with an actual evening receipt is default; today remains
    # selectable even while the natural task has yet to save its result.
    default = next((d for d in dates if (ROOT / "state/daily_companion" / d / "evening.json").is_file() or object_file(ROOT / "state/daily_companion" / d / "2235.json").get("status") == "recorded"), dates[0] if dates else today())
    day = selected_day or default
    if not day_value(day) or day > today():
        return {"available": False, "day": day, "dates": dates, "warnings": ["日期无效或在未来"]}
    source = upstream("/api/daily-companion", day)
    if not isinstance(source, dict):
        source = {}
    evening = source.get("evening") if isinstance(source.get("evening"), dict) else None
    records = source.get("records") if isinstance(source.get("records"), list) else []
    by_slot = {r.get("slot"): r for r in records if isinstance(r, dict)}
    raw_texts = []
    raw_records = []
    folder = ROOT / "state/daily_companion" / day
    for path in sorted(folder.glob("[0-9][0-9][0-9][0-9].json")):
        slot = path.stem
        if not ("0930" <= slot <= "1130" or "1300" <= slot <= "1500" or slot == "2235"):
            continue
        record = object_file(path)
        if record.get("version") != "daily-companion-1" or record.get("as_of") != day or record.get("slot") != slot:
            continue
        # Exact model response preserved, including failed/unparseable output;
        # prompt, credentials and private chat records are not exposed.
        raw = record.get("raw_response")
        if isinstance(raw, str) and raw:
            raw_texts.append({"title": slot[:2] + ":" + slot[2:] + " AI完整原文", "text": raw, "slot": slot, "status": record.get("status")})
        raw_records.append({k: record.get(k) for k in ("slot", "status", "reason", "recorded_at", "model", "answer", "response_evidence")})
    runtime = source.get("runtime", {})
    snapshot = runtime.get("snapshot", {}) if isinstance(runtime, dict) else {}
    profile = runtime.get("profiles", {}).get("default", {}) if isinstance(runtime, dict) else {}
    latest = by_slot.get("2235", {})
    facts = latest.get("facts") if isinstance(latest.get("facts"), dict) else {}
    saved_profile = facts.get("runtime:default")
    if isinstance(saved_profile, dict) and saved_profile.get("as_of") == day:
        profile = saved_profile
    same_profile = profile.get("as_of") == day
    manifest = local_health(day)["snapshot"]
    if manifest["as_of"] == day:
        snapshot = {**snapshot, "as_of": day, "collected_slots": manifest["collected_slots"],
                    "expected_slots": manifest["expected_slots"], "message": manifest["message"]}
    collected, expected = snapshot.get("collected_slots"), snapshot.get("expected_slots")
    snapshot_note = f"已收{collected}/{expected}" if snapshot.get("as_of") == day and collected is not None and expected is not None else "记录缺失"
    snapshot_ok = snapshot.get("as_of") == day and collected is not None and expected is not None and collected >= expected
    note_map = {"recorded": "已记未验证", "skipped": "未调用模型", "model_failed": "模型未完成", "invalid_answer": "引用未通过", "reserved": "结果未知", "missing": "尚无记录", "unreadable": "原件不可读"}
    pipeline = [{"time": "全市场", "name": "全市场快照", "status": "recorded" if snapshot_ok else "partial" if snapshot.get("as_of") == day and collected else "missing", "note": snapshot_note, "detail": snapshot.get("message")}]
    for slot in ("1000", "1430"):
        row = by_slot.get(slot, {})
        status = row.get("status", "missing")
        pipeline.append({"time": slot[:2] + ":" + slot[2:], "name": "盘中观察", "status": status, "note": note_map.get(status, "状态未知"), "detail": row.get("reason")})
    pipeline.append({"time": "20:20", "name": "AI判断", "status": "recorded" if same_profile and profile.get("model_replied") is True else "model_failed" if same_profile else "missing", "note": "模型有回应" if same_profile and profile.get("model_replied") is True else "模型未完成" if same_profile else "尚无记录", "detail": profile.get("message") if same_profile else None})
    status = latest.get("status", "missing")
    pipeline.append({"time": "22:35", "name": "晚间复盘", "status": status, "note": note_map.get(status, "状态未知"), "detail": latest.get("reason")})
    answer = latest.get("answer") if isinstance(latest.get("answer"), dict) else {}
    explanation = (evening or {}).get("ai_explanation")
    explanation_summary = explanation.get("summary") if isinstance(explanation, dict) else explanation if isinstance(explanation, str) and explanation not in {"", "数据缺失"} else None
    insight = answer.get("summary") or explanation_summary
    contributions = saved_contributions(facts, day)
    books = {}
    for book in (evening or {}).get("books", []):
        if isinstance(book, dict) and book.get("name") in {"AI账", "策略账"}:
            books["ai" if book["name"] == "AI账" else "legacy"] = book
    payload = {"day": day, "dates": sorted(set([today(), *dates]), reverse=True), "available": bool(evening or latest.get("status") == "recorded"), "evening": evening, "intraday": [r for r in records if r.get("slot") != "2235"], "pipeline": pipeline, "insight": insight, "books": books, "benchmark": benchmark(day), "contributions": contributions, "contribution_label": "当日持仓累计浮盈，含按当日收盘与保存成本复算的数值", "source": source, "raw_texts": raw_texts, "raw_records": raw_records, "warnings": ["历史页不沿用今日账本或财报冒充当日持仓", "全市场快照收到份数与字段齐备份数分别显示"], "message": "当日晚间复盘尚未落盘，可切换前一有记录日期" if not evening and latest.get("status") != "recorded" else None}
    return payload


def scorecard() -> dict[str, Any]:
    source = upstream("/api/ai-trader/decisions")
    if not isinstance(source, dict):
        source = {}
    rows = source.get("records", []) if isinstance(source.get("records"), list) else []
    summary = source.get("review_summary", {})
    def priority(row: dict[str, Any]) -> tuple[int, str]:
        result = str(row.get("result") or "")
        return (0 if "错" in result or "wrong" in result.lower() else 1 if row.get("statement_unverified") else 2, str(row.get("decision_date") or ""))
    rows = sorted((r for r in rows if isinstance(r, dict)), key=priority)
    return {"available": source.get("available", False), "as_of": source.get("as_of"), "count": len(rows), "matured_count": (summary.get("credible_model") or {}).get("denominator"), "summary": summary, "records": rows, "source": source, "label": LABEL}


def weekly() -> dict[str, Any]:
    folder = ROOT / "reports/weekly_learning"
    paths = sorted(p for p in folder.glob("????-??-??.md") if not p.is_symlink() and day_value(p.stem) == p.stem and p.stem <= today() and date.fromisoformat(p.stem).weekday() == 4)
    for path in reversed(paths):
        try:
            text = safe_bytes(path, 1_000_000).decode("utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if not text.strip():
            continue
        sections, active = [], {"title": "本周学习周报", "text": ""}
        for line in text.splitlines():
            if line.startswith("## "):
                if active["text"].strip():
                    sections.append(active)
                active = {"title": line[3:], "text": ""}
            else:
                active["text"] += line + "\n"
        if active["text"].strip():
            sections.append(active)
        return {"available": True, "date": path.stem, "title": "本周学习周报", "text": text, "sections": sections, "message": None, "source": str(path.relative_to(ROOT)).replace("\\", "/")}
    return {"available": False, "date": None, "title": "本周学习周报", "text": None, "sections": [], "message": "首期将于周五晚自动生成；当前尚无真实周报文件", "source": "reports/weekly_learning/"}


class Handler(BaseHTTPRequestHandler):
    server_version = "QuantDeskReadOnly/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # The launcher redirects this output exclusively into the preview folder.
        return

    def send(self, status: int, value: Any, content_type: str = "application/json; charset=utf-8") -> None:
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path, args = split.path, parse_qs(split.query)
        if self.headers.get("Host", "").split(":")[0] not in {"127.0.0.1", "localhost"}:
            self.send(403, {"error": "loopback_host_required"})
            return
        if path in STATIC:
            filename, content_type = STATIC[path]
            try:
                self.send(200, (HERE / filename).read_bytes(), content_type)
            except OSError:
                self.send(404, {"error": "static_file_missing"})
            return
        if path == "/favicon.ico":
            self.send(200, b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" fill="#090a0c"/><path d="M8 23V9h10l6 14M11 18h10" fill="none" stroke="#fff" stroke-width="1.7"/></svg>', "image/svg+xml")
            return
        try:
            if path == "/api/workspace":
                value = workspace()
            elif path == "/api/equity-history":
                value = equity_history(today())
            elif path == "/api/etf-research":
                value = local_research.etf_research()
            elif path == "/api/ohlcv":
                value = stock_ohlcv(args.get("symbol", [""])[0], today())
            elif path == "/api/review":
                value = review(args.get("date", [None])[0])
            elif path == "/api/scorecard":
                value = scorecard()
            elif path == "/api/weekly":
                value = weekly()
            elif path == "/api/reference":
                value = stock_reference(args.get("symbol", [""])[0])
            elif WORKBENCH_ROUTE.fullmatch(path):
                code = WORKBENCH_ROUTE.fullmatch(path).group(1)
                value = upstream(path) if known_symbol(code) else {"available": False, "reason_codes": ["symbol_not_in_local_preview_scope"], "warnings": ["没有向8000请求未知股票"], "label": LABEL}
            elif path == "/api/health":
                value = {"ok": True, "read_only": True, "port": self.server.server_port, "label": LABEL, "health": workspace()["health"]}
            elif path == "/api/proxy":
                target = args.get("path", [""])[0]
                if target not in UPSTREAM_ROUTES:
                    self.send(403, {"available": False, "error": "route_not_allowlisted"})
                    return
                value = upstream(target, args.get("date", [None])[0])
            elif path in UPSTREAM_ROUTES:
                value = upstream(path, args.get("date", [None])[0])
                if isinstance(value, str):
                    self.send(200, value.encode("utf-8"), "text/plain; charset=utf-8")
                    return
            else:
                self.send(404, {"error": "read_only_route_not_found"})
                return
            self.send(200, value)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Closing a drawer cancels its read; there is no client to send to.
            return
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.send(200, {"available": False, "warnings": ["真实来源暂不可读，未使用合成数据"], "label": LABEL})

    do_HEAD = do_GET

    def write_rejected(self) -> None:
        self.send(405, {"error": "read_only", "message": "只读预览不提供任何写操作"})

    do_POST = write_rejected
    do_PUT = write_rejected
    do_PATCH = write_rejected
    do_DELETE = write_rejected
    do_OPTIONS = write_rejected
    do_TRACE = write_rejected

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self.write_rejected
        raise AttributeError(name)


def main() -> None:
    parser = argparse.ArgumentParser(description="独立本地只读预览，不改正式目录")
    parser.add_argument("--port", type=int, default=8035)
    parser.add_argument("--strict-port", action="store_true")
    args = parser.parse_args()
    ports = [args.port] if args.strict_port else list(range(args.port, args.port + 10))
    server = None
    for port in ports:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("没有可用本地预览端口，未停止其他进程")
    print(f"http://127.0.0.1:{server.server_port}/ · {LABEL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
