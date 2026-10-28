"""Cache-only stock views and explicitly authorized, in-memory quote refresh."""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from threading import Lock
from typing import Any, Callable

import pandas as pd
from flask import Blueprint, Flask, jsonify, request

from quantdesk.market_clock import expected_settlement_date, get_market_session, to_shanghai_datetime
from quantdesk.quote_evidence import inspect_quote, provider_quote_datetime, quote_number
from quantdesk.stock_workbench import build_stock_reference
from scripts.server.helpers.util import clean_payload, request_truthy

LOG = logging.getLogger(__name__)
REJECTED_QUOTE_STATES = {"invalid_identity", "invalid_time", "unverified_time", "unverified_source", "missing"}


@dataclass(frozen=True)
class StockWorkbenchDeps:
    load_original_daily: Callable[[str], dict[str, Any]]
    cached_quote: Callable[[str], dict[str, Any]]
    fetch_quote: Callable[[str], dict[str, Any]]
    research_builder: Callable[[pd.DataFrame], dict[str, Any]]
    load_calendar: Callable[[], dict[str, Any]]
    network_enabled: Callable[[], bool]
    refresh_allowed: Callable[[], bool]
    now: Callable[[], datetime] = to_shanghai_datetime
    fee_assumptions: Callable[[], dict[str, Any]] = lambda: {}
    account_context: Callable[[], dict[str, Any]] = lambda: {}
    load_minute_bars: Callable[[str], dict[str, Any]] = lambda symbol: {
        "ok": True, "available": False, "rows": [],
        "message": "当前服务未接入本地分钟读取；不补齐、不编造。",
    }


class StockQuoteMemory:
    """Bounded memory only: no CSV, ledger, report or scheduled-task writes."""
    def __init__(self, *, cooldown_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.cooldown_seconds, self.clock = cooldown_seconds, clock
        self._lock = Lock()
        self._rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._pending = False
        self._last_attempt = float("-inf")

    def read(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._rows[symbol]) if symbol in self._rows else None

    def refresh(
        self, symbol: str, provider: Callable[[str], dict[str, Any]],
        validator: Callable[[dict[str, Any]], bool] | None = None,
        *, previous_quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            elapsed = self.clock() - self._last_attempt
            if self._pending or elapsed < self.cooldown_seconds:
                return {"ok": False, "status": "refresh_deferred", "message_for_ui": "上一笔报价请求正在处理或尚在请求间隔内，保留已有报价。"}
            self._pending, self._last_attempt = True, self.clock()
        try:
            result = provider(symbol)
            row = result.get("row") if isinstance(result, dict) else None
            valid = isinstance(row, dict) and str(row.get("symbol")) == symbol
            valid = valid and (quote_number(row.get("price")) or 0) > 0 and provider_quote_datetime(row) is not None
            if valid and validator:
                valid = validator(row)
            if result.get("ok") and valid:
                with self._lock:
                    previous = self._rows.get(symbol)
                    if previous_quote and (not previous or provider_quote_datetime(previous_quote) > provider_quote_datetime(previous)):
                        previous = previous_quote
                    if previous and provider_quote_datetime(row) < provider_quote_datetime(previous):
                        return {"ok": False, "status": "older_quote_rejected", "message_for_ui": "报价源返回更早的报价，保留已有较新报价。"}
                    self._rows[symbol] = {**row, "available": True}
                    self._rows.move_to_end(symbol)
                    while len(self._rows) > 64:
                        self._rows.popitem(last=False)
                return result
            # The provider answered but the row did not pass this service's
            # checks; keep the established status but do not repeat a success
            # message that would contradict the refusal.
            return {"ok": False, "status": "provider_failed",
                    "message_for_ui": "报价源返回的数据未通过日期/来源/口径核验，已保留原有行情与时间。" if result.get("ok")
                    else str(result.get("message_for_ui") or "报价源未返回匹配证券的价格，保留已有行情。")}
        except Exception:
            return {"ok": False, "status": "provider_failed", "message_for_ui": "报价源暂不可用，保留已有行情和时间。"}
        finally:
            with self._lock:
                self._pending = False


def create_stock_workbench_blueprint(deps: StockWorkbenchDeps, *, memory: StockQuoteMemory | None = None) -> Blueprint:
    bp = Blueprint("stock_workbench_api", __name__)
    quotes = memory or StockQuoteMemory()

    def market_context(now: datetime) -> tuple[dict[str, Any], str, date | None]:
        try:
            calendar = deps.load_calendar()
            days = calendar.get("is_open_by_date") if isinstance(calendar, dict) else None
            if not isinstance(days, dict) or now.date().isoformat() not in days:
                return calendar if isinstance(calendar, dict) else {}, "calendar_unverified", None
            session = get_market_session(now, calendar)
            complete = expected_settlement_date(now, calendar)
            if complete and not days.get(complete.isoformat()):
                complete = None
            return calendar, session, complete
        except Exception as exc:
            LOG.warning("Stock workbench calendar unavailable: %s", type(exc).__name__)
            return {}, "calendar_unverified", None

    def selected_quote(symbol: str, now: datetime, session: str, complete: date | None) -> tuple[dict[str, Any], dict[str, Any]]:
        rows = []
        memory_row = quotes.read(symbol)
        if memory_row:
            rows.append(memory_row)
        try:
            cached = deps.cached_quote(symbol)
            if isinstance(cached, dict):
                rows.append(cached)
        except Exception as exc:
            LOG.warning("Stock %s saved quote unavailable: %s", symbol, type(exc).__name__)
        if not rows:
            rows = [{"symbol": symbol, "available": False}]
        checked = [(row, inspect_quote(row, symbol=symbol, now=now, session=session,
                                      expected_trade_date=complete)) for row in rows]
        verified = [item for item in checked if item[1]["status"] not in REJECTED_QUOTE_STATES]
        return max(verified, key=lambda item: provider_quote_datetime(item[0])) if verified else checked[0]

    def view(symbol: str) -> dict[str, Any]:
        now = to_shanghai_datetime(deps.now())
        calendar, session, complete = market_context(now)
        quote, evidence = selected_quote(symbol, now, session, complete)
        try:
            source = deps.load_original_daily(symbol)
            if not isinstance(source, dict):
                raise ValueError("daily source must be a mapping")
        except Exception as exc:
            LOG.warning("Stock %s original daily data unavailable: %s", symbol, type(exc).__name__)
            source = {"frame": pd.DataFrame(), "error": "本地日线读取失败。"}
        try:
            reference = build_stock_reference(
                source.get("frame"), symbol=symbol, now=now, complete_trade_date=complete,
                calendar=calendar, research_builder=deps.research_builder, quote=quote,
                quote_evidence=evidence, source_sha256=str(source.get("sha256") or ""),
                fee_assumptions=deps.fee_assumptions(), account_context=deps.account_context(),
            )
            if source.get("error") and not reference["available"]:
                reference.update(status="degraded", reason_codes=["daily_history_read_failed"],
                                 message="本地日线读取失败；报价仍可单独查看。")
        except Exception as exc:
            LOG.warning("Stock %s reference calculation unavailable: %s", symbol, type(exc).__name__)
            reference = {"available": False, "status": "degraded", "label": "纸面 · 未验证",
                         "reason_codes": ["reference_calculation_failed"], "levels": [],
                         "as_of": "", "valid_until": "", "actionable_order": False,
                         "current_assessment": "观望：日线参考计算失败。",
                         "message": "日线参考计算失败；报价仍可单独查看，暂不提供算法价位。"}
        return {
            "ok": True, "symbol": symbol, "quote": quote, "quote_evidence": evidence,
            "reference": reference, "market_active": evidence["market_active"],
            "network_enabled": deps.network_enabled(), "refresh_allowed": deps.refresh_allowed(),
            "next_refresh_seconds": 15, "canonical_writes": False, "llm_used": False,
            "paper_only": True, "no_broker": True, "label": "纸面 · 未验证",
            "calendar_verified": session != "calendar_unverified" and complete is not None,
            "calendar_message": "" if session != "calendar_unverified" and complete else "交易日历未完整核实；暂不判断盘中信号和参考有效期。",
        }

    @bp.get("/api/stock/<symbol>/workbench")
    def stock_view(symbol: str) -> Any:
        if not re.fullmatch(r"[0-9]{6}", symbol):
            return jsonify(ok=False, message_for_ui="请输入六位股票代码。"), 400
        return jsonify(clean_payload(view(symbol)))

    @bp.get("/api/stock/<symbol>/minute")
    def stock_minute(symbol: str) -> Any:
        """Local minute cache view only; missing rows stay missing."""
        if not re.fullmatch(r"[0-9]{6}", symbol):
            return jsonify(ok=False, message_for_ui="请输入六位股票代码。"), 400
        try:
            payload = deps.load_minute_bars(symbol)
        except Exception as exc:  # noqa: BLE001 - a read failure must degrade, not fabricate
            LOG.warning("Stock %s minute cache unavailable: %s", symbol, type(exc).__name__)
            payload = {"ok": False, "available": False, "rows": [],
                       "message": "本地分钟缓存读取失败；不补齐、不编造。"}
        return jsonify(clean_payload({"symbol": symbol, "paper_only": True, "no_broker": True,
                                      "label": "本地分钟缓存 · 纸面 · 未验证", **payload}))

    @bp.post("/api/stock/<symbol>/quote-refresh")
    def stock_refresh(symbol: str) -> Any:
        if not re.fullmatch(r"[0-9]{6}", symbol):
            return jsonify(ok=False, message_for_ui="请输入六位股票代码。"), 400
        if not deps.refresh_allowed():
            return jsonify(ok=False, status="read_only", message_for_ui="当前预览只读，实时刷新待应用后启用。"), 423
        body = request.get_json(silent=True) or {}
        if not (deps.network_enabled() or request_truthy(body.get("network"))):
            return jsonify(ok=False, status="network_disabled", message_for_ui="实时行情需要显式开启联网刷新。"), 403
        before = to_shanghai_datetime(deps.now())
        _, session, complete = market_context(before)
        previous, previous_evidence = selected_quote(symbol, before, session, complete)
        def valid(row: dict[str, Any]) -> bool:
            # Quote and collection clocks must be checked after the request returns.
            checked_at = to_shanghai_datetime(deps.now())
            _, current_session, current_complete = market_context(checked_at)
            checked = inspect_quote(row, symbol=symbol, now=checked_at,
                                    session=current_session, expected_trade_date=current_complete)
            return checked["status"] not in REJECTED_QUOTE_STATES
        refreshed = quotes.refresh(
            symbol, deps.fetch_quote, validator=valid,
            previous_quote=previous if previous_evidence["status"] not in REJECTED_QUOTE_STATES else None,
        )
        return jsonify(clean_payload({**refreshed, "view": view(symbol), "canonical_writes": False}))

    return bp


def register_stock_workbench_routes(app: Flask, deps: StockWorkbenchDeps) -> None:
    app.register_blueprint(create_stock_workbench_blueprint(deps))
