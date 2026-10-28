"""Bounded current-day BSE observations; provider finality stays unverified."""
from __future__ import annotations

import json
import math
import urllib.parse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
PROVIDER = "eastmoney_bse_intraday"
MAX_RESPONSE_BYTES = 1024 * 1024
JsonGetter = Callable[[str, float], Mapping[str, Any]]


def _get_json(url: str, timeout: float) -> Mapping[str, Any]:
    # The socket-owning child bounds DNS, TLS, headers, body and cleanup together.
    from quantdesk.intraday_refresh.collector import _sina_http_get_text

    body, _ = _sina_http_get_text(url, timeout=timeout, user_agent="Mozilla/5.0")
    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("bse_minute_response_too_large")
    payload = json.loads(body.removeprefix("\ufeff"))
    if not isinstance(payload, dict):
        raise ValueError("bse_minute_response_invalid")
    return payload


def _supported_symbol(symbol: str) -> bool:
    # Current BSE equity codes only. Older codes retain their native route;
    # 900xxx B-shares must never be guessed as Beijing equities.
    return len(symbol) == 6 and symbol.isascii() and symbol.isdigit() and symbol.startswith("920")


def _number(value: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("bse_minute_numeric_invalid") from None
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        raise ValueError("bse_minute_numeric_invalid")
    return number


def parse_bse_minute_payload(
    payload: Mapping[str, Any], *, symbol: str, now: datetime, offset: int = 240,
) -> list[dict[str, Any]]:
    """Check identity and source order before selecting closed reported minutes."""
    if not _supported_symbol(symbol):
        raise ValueError("bse_symbol_unsupported")
    if now.tzinfo is None or now.utcoffset() is None or type(offset) is not int or offset <= 0:
        raise ValueError("bse_minute_scope_invalid")
    current = now.astimezone(SHANGHAI)
    data = payload.get("data")
    if (payload.get("rc", 0) != 0 or not isinstance(data, dict)
            or str(data.get("code")) != symbol or type(data.get("market")) is not int
            or data["market"] != 0):
        raise ValueError("bse_minute_response_identity_mismatch")
    lines = data.get("trends")
    if not isinstance(lines, list):
        raise ValueError("bse_minute_rows_missing")
    if len(lines) > 241:
        raise ValueError("bse_minute_rows_out_of_bounds")
    previous: datetime | None = None
    rows: list[dict[str, Any]] = []
    cutoff = current.replace(second=0, microsecond=0)
    for line in lines:
        if not isinstance(line, str):
            raise ValueError("bse_minute_row_invalid")
        fields = line.split(",")
        if len(fields) != 8:
            raise ValueError("bse_minute_row_invalid")
        try:
            stamp = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
        except ValueError:
            raise ValueError("bse_minute_time_invalid") from None
        if previous is not None and stamp <= previous:
            raise ValueError("bse_minute_source_order_invalid")
        previous = stamp
        # Auction records and the mutable current minute do not enter this path.
        minute = stamp.hour * 60 + stamp.minute
        if (stamp.date() != current.date() or stamp >= cutoff
                or not (571 <= minute <= 690 or 781 <= minute <= 900)):
            continue
        opening, closing, high, low = (_number(v, positive=True) for v in fields[1:5])
        if not low <= min(opening, closing) <= max(opening, closing) <= high:
            raise ValueError("bse_minute_ohlc_invalid")
        hands, amount = _number(fields[5]), _number(fields[6])
        rows.append({
            "symbol": symbol, "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": format(opening, "f"), "high": format(high, "f"),
            "low": format(low, "f"), "close": format(closing, "f"),
            "volume": format(hands * 100, "f"), "amount": format(amount, "f"),
            "source_volume_hands": fields[5], "source": PROVIDER, "provider": PROVIDER,
            "price_basis": "raw", "volume_unit": "shares", "amount_unit": "CNY",
            "volume_semantics": "per_minute", "timestamp_convention": "unknown",
            "aggregation_method": "provider_reported", "is_final": False,
            "finality": "unknown", "source_quote_time": "",
            "provider_contract": "eastmoney.trends2.current_day.v1",
        })
    return rows[-min(offset, 240):]


def fetch_bse_minute_rows(
    symbol: str, *, offset: int, now: datetime, timeout_seconds: float,
    network: bool = False, get_json: JsonGetter | None = None,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Fetch observations only when network was explicitly enabled by the caller."""
    detail: dict[str, Any] = {
        "provider": PROVIDER, "coverage_status": "missing", "network_attempted": False,
        "formal_execution_eligible": False, "finality_verified": False,
    }
    if not network:
        return [], "network_not_authorized:eastmoney_bse_intraday", detail
    if not _supported_symbol(symbol):
        return [], "provider_unsupported:eastmoney_bse_intraday", detail
    if now.tzinfo is None or now.utcoffset() is None or type(offset) is not int or offset <= 0:
        return [], "provider_failed:bse_minute_scope_invalid", {**detail, "error_stage": "scope"}
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return [], "provider_failed:deadline_exhausted", {**detail, "error_stage": "timeout"}
    params = {"fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "ndays": "1",
              "iscr": "0", "secid": "0." + symbol}
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    detail["network_attempted"] = True
    try:
        payload = (get_json or _get_json)(url, min(float(timeout_seconds), 8.0))
        rows = parse_bse_minute_payload(payload, symbol=symbol, now=now, offset=offset)
    except Exception as exc:
        from quantdesk.intraday_refresh.collector import _sanitize_error_text

        http = dict(getattr(exc, "http", {}))
        error_type = str(getattr(exc, "error_type", type(exc).__name__))
        stage = ("cleanup" if http.get("stop_collection") else "timeout" if error_type == "TimeoutError"
                 else "parse" if isinstance(exc, ValueError) else "request")
        return [], f"provider_failed:{PROVIDER}:{error_type}", {
            **detail, "error_type": error_type, "message": _sanitize_error_text(exc, limit=400),
            "error_stage": stage, "http": http, "stop_collection": bool(http.get("stop_collection")),
        }
    if not rows:
        return [], f"provider_no_rows:{PROVIDER}", detail
    return rows, "", {**detail, "coverage_status": "observations_available",
                      "timestamp_convention": "unknown", "volume_unit": "shares",
                      "amount_unit": "CNY", "live_availability": "live_response"}
