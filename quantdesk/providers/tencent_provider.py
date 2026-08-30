"""Tencent Finance HTTP provider for scoped A-share data.

The adapter is deliberately conservative: ``network=False`` never opens a
socket, HTTP failures are returned as structured diagnostics, and realtime
quotes are never reused as daily or intraday bars.
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

import pandas as pd

from quantdesk.a_stock_data_matrix import RISK_NOTE
from quantdesk.intraday.intraday_schema import normalize_intraday_frame
from quantdesk.provider_result_contract import count_data_rows, provider_now_text
from quantdesk.shared import ensure_list, normalize_symbol, safe_float, short_error_text

PROVIDER = "tencent"
QUOTE_PROVIDER = "tencent_realtime"
DAILY_PROVIDER = "tencent_daily"
INTRADAY_PROVIDER = "tencent_intraday"
VALUATION_PROVIDER = "tencent_valuation"

QUOTE_URL = "https://qt.gtimg.cn/q={query}"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"

DISCLAIMER = "仅用于本地学习研究，不接券商、不真实下单、不构成投资建议。"


def _base_result(provider: str, *, network: bool, status: str = "skipped") -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "provider": provider,
        "provider_used": "",
        "network": bool(network),
        "data": [],
        "data_rows": 0,
        "provider_chain": [provider],
        "data_source_trace": [provider],
        "warnings": [],
        "errors": [],
        "failed_reason": "",
        "skipped_reason": "",
        "suggested_next_action": "",
        "failed_type": "",
        "retrieved_at": provider_now_text(),
        "duration_ms": None,
        "is_reliable": False,
        "disclaimer": DISCLAIMER,
    }


def _finish_result(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["duration_ms"] = int(round((time.perf_counter() - started) * 1000))
    result["retrieved_at"] = result.get("retrieved_at") or provider_now_text()
    result["data_rows"] = int(result.get("row_count") or result.get("rows") or count_data_rows(result.get("data")))
    result["is_reliable"] = bool(result.get("ok"))
    if result.get("ok") and not result.get("provider_used"):
        result["provider_used"] = str(result.get("provider") or "")
    return result


def _classify_exception(exc: Exception) -> str:
    text = short_error_text(exc).lower()
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in text or "timeout" in text:
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return "http_error"
    if isinstance(exc, urllib.error.URLError):
        return "http_error"
    if isinstance(exc, UnicodeDecodeError):
        return "decode_error"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "parse_error"
    return "http_error"


def _set_failed(
    result: dict[str, Any],
    *,
    reason: Any,
    failed_type: str,
    suggested_next_action: str = "检查网络、接口变化，或改用本地 CSV / mootdx / AKShare。",
) -> dict[str, Any]:
    text = short_error_text(reason)
    result.update({
        "ok": False,
        "status": "failed",
        "failed_reason": text,
        "failed_type": failed_type,
        "errors": [text] if text else [],
        "suggested_next_action": suggested_next_action,
    })
    return result


def to_tencent_symbol(symbol: Any) -> dict[str, Any]:
    """Normalize an A-share symbol for Tencent Finance endpoints."""

    original = "" if symbol is None else str(symbol).strip()
    lowered = original.lower().replace(" ", "")
    warnings: list[str] = []
    market = ""
    normalized = normalize_symbol(original)

    if lowered.startswith("sh") and normalized:
        market = "SH"
    elif lowered.startswith("sz") and normalized:
        market = "SZ"
    elif lowered.startswith("bj") and normalized:
        market = "BJ"
    elif lowered.endswith(".sh") and normalized:
        market = "SH"
    elif lowered.endswith(".sz") and normalized:
        market = "SZ"
    elif lowered.endswith(".bj") and normalized:
        market = "BJ"
    elif normalized.startswith(("600", "601", "603", "605", "688")):
        market = "SH"
    elif normalized.startswith(("000", "001", "002", "003", "300", "301", "302")):
        market = "SZ"
    elif normalized.startswith(("4", "8", "9")):
        market = "BJ"
        warnings.append("北交所或特殊代码，Tencent endpoint 兼容性需以实际返回为准。")
    elif normalized:
        market = "UNSUPPORTED"
        warnings.append("无法可靠判断交易所前缀，未发起默认请求。")
    else:
        warnings.append("无法识别股票代码。")

    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market, "")
    return {
        "input": original,
        "normalized_symbol": normalized,
        "tencent_symbol": f"{prefix}{normalized}" if prefix and normalized else "",
        "market": market,
        "warnings": warnings,
    }


def _http_get_bytes(url: str, timeout: int = 8) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "QuantDesk/3.1.0 (+https://local.quantdesk)",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-triggered public market data
        return resp.read()


def _decode_response(raw: bytes) -> str:
    for encoding in ("gbk", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _float_at(fields: list[str], index: int) -> float | None:
    if index >= len(fields):
        return None
    return safe_float(fields[index])


def _text_at(fields: list[str], index: int) -> str:
    if index >= len(fields):
        return ""
    return str(fields[index] or "").strip()


def _format_trade_time(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{14}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}:{text[12:14]}"
    return text


def _parse_realtime_text(text: str, requested_symbols: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    matches = re.findall(r'v_([^=]+)="([^"]*)"', text or "")
    if not matches:
        return rows, ["Tencent realtime response did not contain quote records."]

    requested = {normalize_symbol(item) for item in requested_symbols}
    for tencent_code, body in matches:
        fields = body.split("~")
        normalized = normalize_symbol(_text_at(fields, 2) or tencent_code)
        if requested and normalized not in requested:
            continue
        missing: list[str] = []
        def required_float(
            index: int,
            name: str,
            *,
            row_fields: list[str] = fields,
            missing_fields: list[str] = missing,
        ) -> float | None:
            value = _float_at(row_fields, index)
            if value is None:
                missing_fields.append(name)
            return value

        row = {
            "symbol": normalized,
            "name": _text_at(fields, 1),
            "price": required_float(3, "price"),
            "prev_close": required_float(4, "prev_close"),
            "open": required_float(5, "open"),
            "high": required_float(33, "high"),
            "low": required_float(34, "low"),
            "volume": _float_at(fields, 36) if _float_at(fields, 36) is not None else _float_at(fields, 6),
            "amount": _float_at(fields, 37),
            "turnover_rate": _float_at(fields, 38),
            "pe": _float_at(fields, 39),
            "pb": _float_at(fields, 46),
            "market_cap": _float_at(fields, 45),
            "float_market_cap": _float_at(fields, 44),
            "amplitude": _float_at(fields, 43),
            "trade_time": _format_trade_time(_text_at(fields, 30)),
            "source": "tencent",
            "provider_status": "ok",
            "raw_provider": "qt.gtimg.cn",
            "tencent_symbol": tencent_code,
        }
        if missing:
            row["warnings"] = [f"missing realtime fields: {', '.join(missing)}"]
            warnings.extend(row["warnings"])
        rows.append(row)
    return rows, warnings


def fetch_realtime_quote(symbols: Any, *, network: bool = False, timeout: int = 8) -> dict[str, Any]:
    """Fetch realtime quotes from ``qt.gtimg.cn`` when explicitly enabled."""

    started = time.perf_counter()
    requested_symbols = [normalize_symbol(item) for item in ensure_list(symbols) if normalize_symbol(item)]
    result = _base_result(QUOTE_PROVIDER, network=network)
    result["symbols"] = requested_symbols
    result["category"] = "market_quote"
    if not network:
        result.update({
            "status": "skipped",
            "skipped_reason": "network=false，默认未联网检测。",
            "failed_type": "network_disabled",
            "suggested_next_action": "如需真实检查，请使用 --network true 或在 UI 中点击联网检查。",
        })
        return _finish_result(result, started)
    converted = [to_tencent_symbol(item) for item in requested_symbols]
    warnings = [warning for item in converted for warning in item.get("warnings", [])]
    query_symbols = [item["tencent_symbol"] for item in converted if item.get("tencent_symbol") and item.get("market") != "UNSUPPORTED"]
    if not query_symbols:
        _set_failed(result, reason="; ".join(warnings) or "No supported Tencent symbols.", failed_type="unsupported_symbol")
        return _finish_result(result, started)
    url = QUOTE_URL.format(query=urllib.parse.quote(",".join(query_symbols), safe=","))
    try:
        text = _decode_response(_http_get_bytes(url, timeout=timeout))
        rows, parse_warnings = _parse_realtime_text(text, requested_symbols)
        result["warnings"] = warnings + parse_warnings
        result["data"] = rows
        if rows:
            result.update({"ok": True, "status": "ok" if len(rows) == len(query_symbols) else "partial"})
        else:
            _set_failed(result, reason="Tencent realtime returned no usable rows.", failed_type="empty_response")
        result["url"] = url
        return _finish_result(result, started)
    except Exception as exc:  # noqa: BLE001
        _set_failed(result, reason=exc, failed_type=_classify_exception(exc))
        result["url"] = url
        return _finish_result(result, started)


def _daily_key(adjust: str) -> tuple[str, str]:
    normalized = str(adjust or "qfq").lower()
    if normalized in {"none", "", "bfq", "no"}:
        return "day", "none"
    if normalized == "hfq":
        return "hfqday", "hfq"
    return "qfqday", "qfq"


def _parse_daily_payload(payload: dict[str, Any], tencent_symbol: str, symbol: str, key: str, adjust: str) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    symbol_payload = (payload.get("data") or {}).get(tencent_symbol) or {}
    rows = symbol_payload.get(key)
    effective_adjust = "raw" if adjust == "none" else adjust
    if rows is None and key != "day":
        rows = symbol_payload.get("day")
        warnings.append(f"Tencent did not return {key}; fallback to day data.")
        effective_adjust = "raw"
    if not isinstance(rows, list):
        return pd.DataFrame(), warnings + ["Tencent daily payload did not contain kline rows."]
    normalized_rows: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            warnings.append("Skipped malformed daily kline row.")
            continue
        # Tencent fqkline daily volume is reported in lots (hands), while the
        # rest of QuantDesk's daily cache uses shares. Convert it here so
        # volume-based indicators stay comparable with other providers.
        volume_lots = safe_float(item[5])
        normalized_rows.append({
            "symbol": normalize_symbol(symbol),
            "date": str(item[0])[:10],
            "open": safe_float(item[1]),
            "close": safe_float(item[2]),
            "high": safe_float(item[3]),
            "low": safe_float(item[4]),
            "volume": volume_lots * 100 if volume_lots is not None else None,
            "amount": safe_float(item[6]) if len(item) > 6 else None,
            "source": "tencent",
            "adjust": effective_adjust,
            "provider_status": "ok",
        })
    frame = pd.DataFrame(normalized_rows)
    if not frame.empty:
        frame = frame.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    return frame, warnings


def fetch_daily_bars(
    symbol: str,
    start_date: str = "",
    end_date: str = "",
    *,
    network: bool = False,
    timeout: int = 8,
    adjust: str = "qfq",
) -> dict[str, Any]:
    """Fetch daily K-line bars from Tencent fqkline endpoint."""

    started = time.perf_counter()
    result = _base_result(DAILY_PROVIDER, network=network)
    norm = normalize_symbol(symbol)
    key, normalized_adjust = _daily_key(adjust)
    requested_adjust = "raw" if normalized_adjust == "none" else normalized_adjust
    result.update({
        "symbol": norm,
        "start_date": start_date,
        "end_date": end_date,
        "adjust": requested_adjust,
        "requested_adjust": requested_adjust,
        "effective_adjust": requested_adjust,
        "category": "daily_kline",
    })
    if not network:
        result.update({
            "status": "skipped",
            "skipped_reason": "network=false，默认未联网检测。",
            "failed_type": "network_disabled",
            "suggested_next_action": "如需真实检查，请使用 --network true 或在 UI 中点击联网检查。",
        })
        return _finish_result(result, started)
    converted = to_tencent_symbol(norm)
    result["warnings"].extend(converted.get("warnings", []))
    if not converted.get("tencent_symbol") or converted.get("market") == "UNSUPPORTED":
        _set_failed(result, reason="; ".join(result["warnings"]) or "Unsupported Tencent symbol.", failed_type="unsupported_symbol")
        return _finish_result(result, started)
    count = 800
    param_parts = [converted["tencent_symbol"], "day", "", "", str(count)]
    if normalized_adjust != "none":
        param_parts.append(normalized_adjust)
    url = KLINE_URL.format(param=urllib.parse.quote(",".join(param_parts), safe=","))
    try:
        payload = json.loads(_decode_response(_http_get_bytes(url, timeout=timeout)))
        frame, warnings = _parse_daily_payload(payload, converted["tencent_symbol"], norm, key, normalized_adjust)
        result["warnings"].extend(warnings)
        result["warnings"].append("provider_return_window_limited")
        if frame.empty:
            _set_failed(result, reason="Tencent daily returned no usable rows.", failed_type="no_rows", suggested_next_action="腾讯日线未返回可用行；请检查代码、日期范围、网络或改用本地 CSV / AKShare。")
            return _finish_result(result, started)
        dates = pd.to_datetime(frame["date"], errors="coerce")
        if start_date:
            frame = frame.loc[dates >= pd.Timestamp(start_date)]
            dates = pd.to_datetime(frame["date"], errors="coerce")
        if end_date:
            frame = frame.loc[dates <= pd.Timestamp(end_date)]
        frame = frame.reset_index(drop=True)
        effective_values = [
            str(item).strip().lower()
            for item in frame.get("adjust", pd.Series(dtype="string")).dropna().unique().tolist()
            if str(item).strip()
        ]
        effective_adjust = effective_values[0] if len(effective_values) == 1 else "unknown"
        result["effective_adjust"] = effective_adjust
        result["adjust"] = effective_adjust
        rows = frame.to_dict(orient="records")
        result["data"] = rows
        if rows:
            status = "ok"
            if start_date and rows[0]["date"] > start_date:
                status = "partial"
            if end_date and rows[-1]["date"] < end_date:
                status = "partial"
            result.update({
                "ok": True,
                "status": status,
                "row_count": len(rows),
                "latest_date": rows[-1]["date"],
                "fields": list(frame.columns),
                "url": url,
            })
        else:
            _set_failed(result, reason="Tencent daily rows do not cover requested date range.", failed_type="date_out_of_scope", suggested_next_action="目标日期不在腾讯返回窗口内；请缩小日期范围、稍后重试或导入本地日线 CSV。")
        return _finish_result(result, started)
    except Exception as exc:  # noqa: BLE001
        _set_failed(result, reason=exc, failed_type=_classify_exception(exc))
        result["url"] = url
        return _finish_result(result, started)


def _is_current_day(target_date: str) -> bool:
    if not target_date:
        return True
    return str(target_date)[:10] == datetime.now().strftime("%Y-%m-%d")


def fetch_intraday_bars(
    symbol: str,
    target_date: str = "",
    period: str = "1m",
    *,
    network: bool = False,
    timeout: int = 8,
) -> dict[str, Any]:
    """Fetch current-day minute bars when explicitly enabled.

    Tencent's public minute endpoint is treated as current-day only by this
    adapter. Historical target dates are skipped with a clear reason.
    """

    started = time.perf_counter()
    result = _base_result(INTRADAY_PROVIDER, network=network)
    norm = normalize_symbol(symbol)
    result.update({
        "symbol": norm,
        "target_date": target_date[:10],
        "period": period,
        "category": "intraday_kline",
        "supports_current_day": True,
        "supports_historical_day": False,
        "date_scope": "current_day_only",
    })
    if not network:
        result.update({
            "status": "skipped",
            "skipped_reason": "network=false，默认未联网检测。",
            "failed_type": "network_disabled",
            "suggested_next_action": "如需真实检查，请使用 --network true 或在 UI 中点击联网检查。",
        })
        return _finish_result(result, started)
    if period != "1m":
        result.update({"status": "skipped", "skipped_reason": "Tencent adapter 当前仅实现 1m。", "failed_type": "unsupported_period"})
        return _finish_result(result, started)
    if target_date and not _is_current_day(target_date):
        result.update({
            "status": "skipped",
            "skipped_reason": "腾讯财经当前适配仅支持当日分钟线，不支持历史目标日期。",
            "failed_type": "date_out_of_scope",
            "suggested_next_action": "请导入历史 1 分钟线 CSV，或在目标交易日盘后尝试当日 provider。",
        })
        return _finish_result(result, started)
    converted = to_tencent_symbol(norm)
    result["warnings"].extend(converted.get("warnings", []))
    if not converted.get("tencent_symbol") or converted.get("market") == "UNSUPPORTED":
        _set_failed(result, reason="; ".join(result["warnings"]) or "Unsupported Tencent symbol.", failed_type="unsupported_symbol")
        return _finish_result(result, started)
    url = MINUTE_URL.format(code=urllib.parse.quote(converted["tencent_symbol"]))
    try:
        payload = json.loads(_decode_response(_http_get_bytes(url, timeout=timeout)))
        data = (((payload.get("data") or {}).get(converted["tencent_symbol"]) or {}).get("data") or {}).get("data") or []
        day = target_date[:10] or datetime.now().strftime("%Y-%m-%d")
        rows: list[dict[str, Any]] = []
        for item in data:
            parts = str(item).split()
            if len(parts) < 2:
                continue
            time_text = parts[0]
            price = safe_float(parts[1])
            if price is None:
                continue
            if re.fullmatch(r"\d{4}", time_text):
                dt = f"{day} {time_text[:2]}:{time_text[2:4]}:00"
            else:
                dt = f"{day} {time_text}"
            rows.append({"symbol": norm, "datetime": dt, "price": price, "volume": safe_float(parts[2]) if len(parts) > 2 else None, "source": "tencent", "period": period})
        normalized = normalize_intraday_frame(pd.DataFrame(rows), default_symbol=norm, source="tencent_intraday", target_date=day)
        frame = normalized.get("frame")
        result["warnings"].extend(normalized.get("warnings") or [])
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            _set_failed(result, reason="Tencent intraday returned no usable minute rows.", failed_type="no_rows", suggested_next_action="腾讯分钟线未返回可用行；请稍后重试、检查接口变化，或导入目标日期 1 分钟线 CSV。")
            return _finish_result(result, started)
        records = frame.to_dict(orient="records")
        status = "ok" if len(records) >= 180 else "partial"
        if status == "partial":
            result["warnings"].append("insufficient_intraday_rows")
        result.update({
            "ok": True,
            "status": status,
            "data": records,
            "row_count": len(records),
            "latest_datetime": records[-1].get("datetime", ""),
            "price_only_intraday": bool(normalized.get("price_only_intraday")),
            "url": url,
        })
        return _finish_result(result, started)
    except Exception as exc:  # noqa: BLE001
        _set_failed(result, reason=exc, failed_type=_classify_exception(exc), suggested_next_action="检查网络、接口变化，或导入目标日期 1 分钟线 CSV。")
        result["url"] = url
        return _finish_result(result, started)


def fetch_valuation(symbol: str, *, network: bool = False, timeout: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    result = _base_result(VALUATION_PROVIDER, network=network)
    norm = normalize_symbol(symbol)
    result.update({"symbol": norm, "category": "valuation"})
    quote = fetch_realtime_quote([norm], network=network, timeout=timeout)
    result["provider_chain"] = [VALUATION_PROVIDER, *quote.get("provider_chain", [])]
    result["data_source_trace"] = result["provider_chain"]
    if not network:
        result.update({
            "status": "skipped",
            "skipped_reason": "network=false，默认未联网检测。",
            "failed_type": "network_disabled",
            "suggested_next_action": "如需真实检查，请使用 --network true 或在 UI 中点击联网检查。",
        })
        return _finish_result(result, started)
    if not quote.get("ok") or not quote.get("data"):
        result.update({
            "status": quote.get("status", "failed"),
            "failed_reason": quote.get("failed_reason") or "; ".join(quote.get("errors") or []),
            "failed_type": quote.get("failed_type") or "no_rows",
            "errors": quote.get("errors", []),
            "warnings": quote.get("warnings", []),
            "suggested_next_action": quote.get("suggested_next_action") or "估值字段来自腾讯实时行情；请检查 quote 是否可用。",
        })
        return _finish_result(result, started)
    row = dict(quote["data"][0])
    valuation = {
        "symbol": row.get("symbol") or norm,
        "pe": row.get("pe"),
        "pb": row.get("pb"),
        "market_cap": row.get("market_cap"),
        "float_market_cap": row.get("float_market_cap"),
        "turnover_rate": row.get("turnover_rate"),
        "amplitude": row.get("amplitude"),
        "source": "tencent",
        "provider_status": "ok",
    }
    missing = [key for key in ("pe", "pb", "market_cap", "float_market_cap", "turnover_rate", "amplitude") if valuation.get(key) is None]
    warnings = list(quote.get("warnings") or [])
    if missing:
        warnings.append("missing valuation fields: " + ", ".join(missing))
    result.update({"ok": True, "status": "ok", "data": [valuation], "warnings": warnings})
    return _finish_result(result, started)


def capability(*, network: bool = False, symbol: str = "600744") -> dict[str, Any]:
    base = {
        "provider": PROVIDER,
        "status": "skipped" if not network else "unknown",
        "dependency_available": True,
        "requires_network": True,
        "checked_network": bool(network),
        "implemented": True,
        "api_key_required": False,
        "supports_realtime": True,
        "supports_daily": True,
        "supports_intraday": True,
        "supports_current_day": True,
        "supports_historical_day": "partial",
        "supports_single_symbol": True,
        "supports_batch": True,
        "date_scope": "quote_current_day_daily_history_intraday_current_day_only",
        "skipped_reason": "" if network else "默认不联网检测。",
        "suggested_next_action": "如需真实检查，请使用 --network true；失败时可改用 CSV、mootdx 或 AKShare。",
        "disclaimer": RISK_NOTE,
    }
    if not network:
        return base
    probe = fetch_realtime_quote([symbol], network=True)
    base["status"] = probe.get("status", "failed")
    base["ok"] = bool(probe.get("ok"))
    base["row_count"] = len(probe.get("data") or [])
    base["latest_date"] = (probe.get("data") or [{}])[-1].get("trade_time", "")[:10] if probe.get("data") else ""
    base["fields"] = list((probe.get("data") or [{}])[0].keys()) if probe.get("data") else []
    base["failed_reason"] = probe.get("failed_reason") or "; ".join(probe.get("errors") or [])
    base["warnings"] = probe.get("warnings") or []
    return base
