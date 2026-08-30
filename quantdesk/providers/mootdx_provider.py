"""Optional real mootdx provider.

Missing dependency returns not_available; network=false returns skipped.
The provider remains best-effort and is never a hard dependency.
"""

from __future__ import annotations

import importlib
import socket
import time
from typing import Any

import pandas as pd

from quantdesk.a_stock_data_matrix import RISK_NOTE, dependency_available
from quantdesk.intraday.intraday_schema import normalize_intraday_frame
from quantdesk.provider_result_contract import build_provider_result, count_data_rows, provider_now_text
from quantdesk.shared import normalize_symbol, safe_float, short_error_text

PROVIDER = "mootdx"
QUOTE_PROVIDER = "mootdx_quote"
DAILY_PROVIDER = "mootdx_daily"
INTRADAY_PROVIDER = "mootdx_intraday"
FUNDAMENTAL_PROVIDER = "mootdx_fundamental"
DISCLAIMER = "仅用于本地学习研究，不接券商、不真实下单、不构成投资建议。"

# tdxpy.helper.get_volume(0) evaluates to 2**-127 instead of numeric zero.
# Values in this decoder-only subnormal range cannot be real A-share volume or
# turnover and must be normalized before quality gates consume them.
MOOTDX_ZERO_SENTINEL_MAX = 2.0 ** -126


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _classify_exception(exc: Exception) -> str:
    text = short_error_text(exc).lower()
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timeout" in text or "timed out" in text:
        return "timeout"
    if "import" in text or "no module" in text:
        return "dependency_missing"
    if "empty" in text or "no rows" in text:
        return "no_rows"
    return "provider_error"


def to_mootdx_symbol(symbol: Any) -> dict[str, Any]:
    """Normalize A-share symbols for mootdx without dropping leading zeroes."""

    original = "" if symbol is None else str(symbol).strip()
    lowered = original.lower().replace(" ", "")
    normalized = normalize_symbol(original)
    warnings: list[str] = []
    market = ""
    market_id: int | None = None
    if lowered.startswith("sh") or lowered.endswith(".sh") or normalized.startswith(("600", "601", "603", "605", "688")):
        market = "SH"
        market_id = 1
    elif lowered.startswith("sz") or lowered.endswith(".sz") or normalized.startswith(("000", "001", "002", "003", "300", "301", "302")):
        market = "SZ"
        market_id = 0
    elif lowered.startswith("bj") or lowered.endswith(".bj") or normalized.startswith(("4", "8", "9")):
        market = "BJ"
        market_id = 2
        warnings.append("北交所或特殊代码暂不保证 mootdx 行情可用。")
    elif normalized:
        market = "UNSUPPORTED"
        warnings.append("无法可靠判断交易所，未发起 mootdx 请求。")
    else:
        warnings.append("无法识别股票代码。")
    return {
        "input": original,
        "normalized_symbol": normalized,
        "market": market,
        "market_id": market_id,
        "code": normalized,
        "warnings": warnings,
        "unsupported": market in {"", "UNSUPPORTED"},
    }


def _base_result(provider: str, category: str, *, symbol: str = "", network: bool = False, status: str = "skipped") -> dict[str, Any]:
    return build_provider_result(
        provider=provider,
        category=category,
        status=status,
        symbol=symbol,
        network=network,
        provider_chain=[provider],
        data_source_trace=[provider],
        disclaimer=DISCLAIMER,
        retrieved_at=provider_now_text(),
    )


def _finish(payload: dict[str, Any], started: float) -> dict[str, Any]:
    payload["duration_ms"] = _elapsed_ms(started)
    payload["retrieved_at"] = payload.get("retrieved_at") or provider_now_text()
    payload["data_rows"] = int(payload.get("data_rows") or count_data_rows(payload))
    payload["is_reliable"] = bool(payload.get("ok"))
    if payload.get("ok") and not payload.get("provider_used"):
        payload["provider_used"] = str(payload.get("provider") or "")
    return payload


def _skipped_network(provider: str, category: str, symbol: str, started: float) -> dict[str, Any]:
    payload = _base_result(provider, category, symbol=symbol, network=False, status="skipped")
    payload.update({
        "skipped_reason": "network=false，默认不连接 mootdx 服务器。",
        "failed_type": "network_disabled",
        "suggested_next_action": "如需真实检查，请显式使用 --network true；未安装 mootdx 时会自动降级到腾讯 provider。",
    })
    return _finish(payload, started)


def _not_available(provider: str, category: str, symbol: str, started: float) -> dict[str, Any]:
    payload = _base_result(provider, category, symbol=symbol, network=True, status="not_available")
    payload.update({
        "failed_reason": "未安装 mootdx。",
        "skipped_reason": "未安装 mootdx。",
        "failed_type": "dependency_missing",
        "dependency_available": False,
        "suggested_next_action": "未安装 mootdx，可继续使用腾讯 provider；如需启用 mootdx，请按 docs/provider_setup.md 安装。",
    })
    return _finish(payload, started)


def _failed(provider: str, category: str, symbol: str, started: float, reason: Any, *, failed_type: str | None = None) -> dict[str, Any]:
    text = short_error_text(reason)
    payload = _base_result(provider, category, symbol=symbol, network=True, status="failed")
    payload.update({
        "failed_reason": text,
        "errors": [text] if text else [],
        "failed_type": failed_type or (_classify_exception(reason) if isinstance(reason, Exception) else "provider_error"),
        "dependency_available": dependency_available("mootdx"),
        "suggested_next_action": "检查 mootdx 是否安装、网络是否可达、服务器是否可用；失败时 QuantDesk 会继续尝试腾讯 provider fallback。",
    })
    return _finish(payload, started)


def _import_quotes_client(*, timeout: int = 8) -> Any:
    quotes_mod = importlib.import_module("mootdx.quotes")
    quotes_cls = quotes_mod.Quotes
    return quotes_cls.factory(market="std", timeout=max(1, int(timeout)))


def _close_quotes_client(client: Any) -> None:
    """Best-effort close for mootdx wrappers and their underlying transports."""

    for target in (client, getattr(client, "client", None)):
        if target is None:
            continue
        for method_name in ("close", "disconnect"):
            method = getattr(target, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                return
            except Exception:  # noqa: BLE001 - cleanup must not mask provider results
                continue


def _as_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, dict):
        return pd.DataFrame([value])
    if isinstance(value, (list, tuple)):
        return pd.DataFrame(value)
    return pd.DataFrame()


def normalize_mootdx_zero_sentinels(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Map tdxpy's decoded raw-zero volume/amount sentinel back to zero."""

    result = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    if result.empty:
        return result, {"row_count": 0, "field_count": 0, "columns": []}
    row_mask = pd.Series(False, index=result.index)
    field_count = 0
    columns: list[str] = []
    for column in ("volume", "vol", "amount", "money"):
        if column not in result.columns:
            continue
        numeric = pd.to_numeric(result[column], errors="coerce")
        mask = numeric.notna() & (numeric > 0) & (numeric <= MOOTDX_ZERO_SENTINEL_MAX)
        count = int(mask.sum())
        if not count:
            continue
        replacement: object = "0" if pd.api.types.is_string_dtype(result[column].dtype) else 0.0
        result.loc[mask, column] = replacement
        row_mask = row_mask | mask
        field_count += count
        columns.append(column)
    return result, {
        "row_count": int(row_mask.sum()),
        "field_count": field_count,
        "columns": columns,
    }


def _normalize_mootdx_bar_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove duplicate mootdx aliases before schema normalization."""

    result = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    if result.empty:
        return result
    result = result.reset_index(drop=True)
    if "datetime" in result.columns:
        component_columns = [
            column
            for column in ("year", "month", "day", "hour", "minute")
            if column in result.columns
        ]
        if component_columns:
            result = result.drop(columns=component_columns)
    for canonical, alias in (("volume", "vol"), ("amount", "money")):
        if canonical in result.columns and alias in result.columns:
            primary = pd.to_numeric(result[canonical], errors="coerce")
            fallback = pd.to_numeric(result[alias], errors="coerce")
            result[canonical] = primary.combine_first(fallback)
            result = result.drop(columns=[alias])
        elif alias in result.columns:
            result = result.rename(columns={alias: canonical})
    return result


def _zero_sentinel_warning(diagnostics: dict[str, Any]) -> str:
    return (
        "mootdx_zero_decoder_sentinel_normalized: "
        f"rows={int(diagnostics.get('row_count') or 0)}; "
        f"fields={int(diagnostics.get('field_count') or 0)}"
    )


def _call_first(client: Any, method_names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    errors: list[str] = []
    for name in method_names:
        method = getattr(client, name, None)
        if not callable(method):
            continue
        try:
            return method(*args, **kwargs)
        except TypeError as exc:
            errors.append(f"{name}: {short_error_text(exc)}")
            continue
    raise RuntimeError("; ".join(errors) or f"mootdx client methods unavailable: {', '.join(method_names)}")


def _fetch_security_bar_page(
    client: Any,
    symbol: str,
    *,
    market_id: int | None,
    frequency: int,
    start: int,
    offset: int,
) -> Any:
    """Fetch one bar page with an explicit BSE market id when required."""

    if market_id == 2:
        transport = getattr(client, "client", None)
        getter = getattr(transport, "get_security_bars", None)
        if not callable(getter):
            raise RuntimeError("mootdx BSE transport does not expose get_security_bars")
        return getter(int(frequency), 2, str(symbol), int(start), int(offset))
    return _call_first(
        client,
        ("bars",),
        symbol=symbol,
        frequency=int(frequency),
        start=int(start),
        offset=int(offset),
    )


def _fetch_intraday_bar_pages(
    client: Any,
    symbol: str,
    target_date: str,
    *,
    market_id: int | None = None,
    page_size: int = 800,
    max_pages: int = 96,
) -> pd.DataFrame:
    """Page backward until the requested trading date is reached."""

    target = pd.to_datetime(str(target_date)[:10], errors="coerce")
    target = None if pd.isna(target) else pd.Timestamp(target).normalize()
    pages: list[pd.DataFrame] = []
    for page_number in range(max(1, int(max_pages))):
        page = _fetch_security_bar_page(
            client,
            symbol,
            market_id=market_id,
            frequency=8,
            start=page_number * int(page_size),
            offset=int(page_size),
        )
        frame = _normalize_mootdx_bar_columns(_as_frame(page))
        if frame.empty:
            break
        pages.append(frame)
        if target is None:
            break
        time_column = "datetime" if "datetime" in frame.columns else "time" if "time" in frame.columns else ""
        if not time_column:
            break
        dates = pd.to_datetime(frame[time_column], errors="coerce").dropna().dt.normalize()
        if dates.empty:
            break
        if bool((dates == target).any()) or pd.Timestamp(dates.min()) <= target:
            break
    if not pages:
        return pd.DataFrame()
    return pd.concat(pages, ignore_index=True).drop_duplicates()


def _normalize_quote_frame(frame: pd.DataFrame, symbol: str) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if frame is None or frame.empty:
        return [], ["mootdx quote returned empty rows."]
    frame, sentinel_diagnostics = normalize_mootdx_zero_sentinels(frame)
    if sentinel_diagnostics["field_count"]:
        warnings.append(_zero_sentinel_warning(sentinel_diagnostics))
    row = frame.iloc[0].to_dict()
    norm = normalize_symbol(row.get("code") or row.get("symbol") or symbol)

    def pick(*names: str) -> Any:
        for name in names:
            if name in row and row.get(name) not in (None, ""):
                return row.get(name)
        return None

    out = {
        "symbol": norm,
        "name": str(pick("name", "stock_name") or ""),
        "price": safe_float(pick("price", "now", "close")),
        "prev_close": safe_float(pick("last_close", "pre_close", "prev_close")),
        "open": safe_float(pick("open")),
        "high": safe_float(pick("high")),
        "low": safe_float(pick("low")),
        "volume": safe_float(pick("volume", "vol")),
        "amount": safe_float(pick("amount", "money")),
        "turnover_rate": safe_float(pick("turnover_rate")),
        "pe": safe_float(pick("pe")),
        "market_cap": safe_float(pick("market_cap")),
        "trade_time": str(pick("datetime", "time", "trade_time") or ""),
        "source": "mootdx",
        "provider_status": "ok",
    }
    for field in ("turnover_rate", "pe", "market_cap"):
        if out.get(field) is None:
            warnings.append(f"mootdx quote 未提供 {field} 字段。")
    missing = [field for field in ("price", "open", "high", "low") if out.get(field) is None]
    if missing:
        warnings.append("mootdx quote 缺少字段：" + ", ".join(missing))
    return [out], warnings


def _normalize_daily_frame(frame: pd.DataFrame, symbol: str, start_date: str, end_date: str) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = ["mootdx 日线当前按原始/未复权数据处理，adjust=raw。"]
    if frame is None or frame.empty:
        return [], ["mootdx daily returned empty rows."]
    frame, sentinel_diagnostics = normalize_mootdx_zero_sentinels(frame)
    frame = _normalize_mootdx_bar_columns(frame)
    if sentinel_diagnostics["field_count"]:
        warnings.append(_zero_sentinel_warning(sentinel_diagnostics))
    df = frame.rename(columns={"datetime": "date", "time": "date"}).copy()
    if "date" not in df.columns:
        return [], ["mootdx daily missing date column."]
    if "close" not in df.columns:
        return [], ["mootdx daily missing close column."]
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    out["symbol"] = normalize_symbol(symbol)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        out[column] = pd.to_numeric(df[column], errors="coerce") if column in df.columns else None
        if column not in df.columns and column in {"volume", "amount"}:
            warnings.append(f"mootdx daily 未提供 {column} 字段。")
    if "volume" in out.columns:
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100.0
        warnings.append("mootdx_daily_volume_lots_converted_to_shares")
    out = out.dropna(subset=["date", "close"]).sort_values("date")
    if start_date:
        out = out.loc[out["date"] >= pd.Timestamp(start_date)]
    if end_date:
        out = out.loc[out["date"] <= pd.Timestamp(end_date)]
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out["source"] = "mootdx"
    out["adjust"] = "raw"
    out["provider_status"] = "ok"
    out = out.drop_duplicates("date", keep="last")
    return out.to_dict(orient="records"), warnings


def capability(*, network: bool = False, symbol: str = "600744") -> dict[str, Any]:
    started = time.perf_counter()
    dep_ok = dependency_available("mootdx")
    base = {
        "provider": PROVIDER,
        "status": "skipped" if not network else ("unknown" if dep_ok else "not_available"),
        "dependency_name": "mootdx",
        "dependency_available": dep_ok,
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
        "supports_full_market": False,
        "provider_chain": [PROVIDER],
        "duration_ms": None,
        "retrieved_at": provider_now_text(),
        "suggested_next_action": "mootdx 是可选 provider；未安装时可继续使用腾讯 provider。",
        "disclaimer": RISK_NOTE,
    }
    if not dep_ok:
        base["status"] = "not_available"
        base["failed_reason"] = "未安装 mootdx。"
        base["skipped_reason"] = "未安装 mootdx。"
        base["suggested_next_action"] = "未安装 mootdx，可继续使用腾讯 provider；如需启用 mootdx，请按 docs/provider_setup.md 安装。"
        base["duration_ms"] = _elapsed_ms(started)
        return base
    if not network:
        base["skipped_reason"] = "默认不联网检测。"
        base["duration_ms"] = _elapsed_ms(started)
        return base
    probe = fetch_realtime_quote(symbol, network=True)
    base.update({
        "status": probe.get("status", "failed"),
        "ok": bool(probe.get("ok")),
        "row_count": probe.get("data_rows", 0),
        "failed_reason": probe.get("failed_reason", ""),
        "warnings": probe.get("warnings", []),
        "duration_ms": _elapsed_ms(started),
    })
    return base


def fetch_realtime_quote(symbol: Any, *, network: bool = False, timeout: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    info = to_mootdx_symbol(symbol)
    norm = info["normalized_symbol"]
    if not network:
        return _skipped_network(QUOTE_PROVIDER, "market_quote", norm, started)
    if info.get("unsupported"):
        return _failed(QUOTE_PROVIDER, "market_quote", norm, started, "; ".join(info.get("warnings") or ["unsupported symbol"]), failed_type="unsupported_symbol")
    if not dependency_available("mootdx"):
        return _not_available(QUOTE_PROVIDER, "market_quote", norm, started)
    client = None
    try:
        client = _import_quotes_client(timeout=timeout)
        raw = _call_first(client, ("quotes", "quote"), [norm])
        rows, warnings = _normalize_quote_frame(_as_frame(raw), norm)
        warnings = list(info.get("warnings") or []) + warnings
        if not rows:
            return _failed(QUOTE_PROVIDER, "market_quote", norm, started, "; ".join(warnings) or "mootdx quote returned no rows.", failed_type="no_rows")
        payload = _base_result(QUOTE_PROVIDER, "market_quote", symbol=norm, network=True, status="ok")
        payload.update({"ok": True, "data": rows, "data_rows": len(rows), "warnings": warnings, "dependency_available": True})
        return _finish(payload, started)
    except Exception as exc:  # noqa: BLE001
        return _failed(QUOTE_PROVIDER, "market_quote", norm, started, exc)
    finally:
        _close_quotes_client(client)


def fetch_daily_bars(symbol: str, start_date: str = "", end_date: str = "", *, network: bool = False, timeout: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    info = to_mootdx_symbol(symbol)
    norm = info["normalized_symbol"]
    if not network:
        return _skipped_network(DAILY_PROVIDER, "daily_kline", norm, started)
    if info.get("unsupported"):
        return _failed(DAILY_PROVIDER, "daily_kline", norm, started, "; ".join(info.get("warnings") or ["unsupported symbol"]), failed_type="unsupported_symbol")
    if not dependency_available("mootdx"):
        return _not_available(DAILY_PROVIDER, "daily_kline", norm, started)
    client = None
    try:
        client = _import_quotes_client(timeout=timeout)
        try:
            raw = _fetch_security_bar_page(
                client,
                norm,
                market_id=info.get("market_id"),
                frequency=9,
                start=0,
                offset=800,
            )
        except RuntimeError:
            raw = _call_first(client, ("bars", "k"), norm, 9)
        rows, warnings = _normalize_daily_frame(_as_frame(raw), norm, start_date, end_date)
        warnings = list(info.get("warnings") or []) + warnings
        if not rows:
            return _failed(DAILY_PROVIDER, "daily_kline", norm, started, "; ".join(warnings) or "mootdx daily returned no rows.", failed_type="no_rows")
        payload = _base_result(DAILY_PROVIDER, "daily_kline", symbol=norm, network=True, status="ok")
        payload.update({
            "ok": True,
            "data": rows,
            "data_rows": len(rows),
            "row_count": len(rows),
            "latest_date": rows[-1].get("date", ""),
            "adjust": "raw",
            "warnings": warnings,
            "dependency_available": True,
        })
        return _finish(payload, started)
    except Exception as exc:  # noqa: BLE001
        return _failed(DAILY_PROVIDER, "daily_kline", norm, started, exc)
    finally:
        _close_quotes_client(client)


def fetch_intraday_bars(symbol: str, target_date: str = "", period: str = "1m", *, network: bool = False, timeout: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    info = to_mootdx_symbol(symbol)
    norm = info["normalized_symbol"]
    if not network:
        return _skipped_network(INTRADAY_PROVIDER, "intraday_kline", norm, started)
    if period != "1m":
        payload = _base_result(INTRADAY_PROVIDER, "intraday_kline", symbol=norm, network=True, status="skipped")
        payload.update({"period": period, "skipped_reason": "mootdx 当前适配仅尝试 1m 分钟线。", "failed_type": "unsupported_period", "suggested_next_action": "请使用 period=1m，或导入目标周期 CSV。"})
        return _finish(payload, started)
    if info.get("unsupported"):
        return _failed(INTRADAY_PROVIDER, "intraday_kline", norm, started, "; ".join(info.get("warnings") or ["unsupported symbol"]), failed_type="unsupported_symbol")
    if not dependency_available("mootdx"):
        return _not_available(INTRADAY_PROVIDER, "intraday_kline", norm, started)
    client = None
    try:
        client = _import_quotes_client(timeout=timeout)
        fallback_warnings: list[str] = []

        def normalize_raw(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            raw_frame, diagnostics = normalize_mootdx_zero_sentinels(_as_frame(value))
            raw_frame = _normalize_mootdx_bar_columns(raw_frame)
            return (
                normalize_intraday_frame(
                    raw_frame,
                    default_symbol=norm,
                    source="mootdx_intraday",
                    target_date=target_date,
                ),
                diagnostics,
            )

        try:
            raw = _fetch_intraday_bar_pages(
                client,
                norm,
                target_date,
                market_id=info.get("market_id"),
            )
            normalized, sentinel_diagnostics = normalize_raw(raw)
        except Exception as exc:  # noqa: BLE001 - price-only fallback remains useful evidence
            fallback_warnings.append(
                f"mootdx_full_ohlcv_bars_failed:{short_error_text(exc)}"
            )
            normalized = {"frame": pd.DataFrame(), "warnings": [], "errors": []}
            sentinel_diagnostics = {"row_count": 0, "field_count": 0, "columns": []}

        frame = normalized.get("frame")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            minute_kwargs: dict[str, Any] = {"symbol": norm}
            if target_date:
                minute_kwargs["date"] = str(target_date)[:10].replace("-", "")
            raw = _call_first(client, ("minutes", "minute"), **minute_kwargs)
            normalized, sentinel_diagnostics = normalize_raw(raw)
            frame = normalized.get("frame")
        warnings = (
            list(info.get("warnings") or [])
            + fallback_warnings
            + list(normalized.get("warnings") or [])
        )
        if sentinel_diagnostics["field_count"]:
            warnings.append(_zero_sentinel_warning(sentinel_diagnostics))
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            reason = "; ".join(normalized.get("errors") or warnings) or "mootdx intraday returned no target-date minute rows."
            return _failed(INTRADAY_PROVIDER, "intraday_kline", norm, started, reason, failed_type="no_rows")
        rows = frame.to_dict(orient="records")
        status = "ok" if len(rows) >= 180 else "partial"
        if status == "partial":
            warnings.append("mootdx_intraday_rows_insufficient")
        payload = _base_result(INTRADAY_PROVIDER, "intraday_kline", symbol=norm, network=True, status=status)
        payload.update({
            "ok": True,
            "period": period,
            "target_date": target_date[:10],
            "data": rows,
            "data_rows": len(rows),
            "row_count": len(rows),
            "latest_datetime": rows[-1].get("datetime", ""),
            "date_scope": "historical_supported",
            "supports_current_day": True,
            "supports_historical_day": "partial",
            "price_basis": "raw",
            "price_only_intraday": bool(normalized.get("price_only_intraday")),
            "warnings": warnings,
            "dependency_available": True,
        })
        return _finish(payload, started)
    except Exception as exc:  # noqa: BLE001
        return _failed(INTRADAY_PROVIDER, "intraday_kline", norm, started, exc)
    finally:
        _close_quotes_client(client)


def fetch_fundamentals(symbol: str, *, network: bool = False, timeout: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    info = to_mootdx_symbol(symbol)
    norm = info["normalized_symbol"]
    if not network:
        return _skipped_network(FUNDAMENTAL_PROVIDER, "fundamentals", norm, started)
    if info.get("unsupported"):
        return _failed(FUNDAMENTAL_PROVIDER, "fundamentals", norm, started, "; ".join(info.get("warnings") or ["unsupported symbol"]), failed_type="unsupported_symbol")
    if not dependency_available("mootdx"):
        return _not_available(FUNDAMENTAL_PROVIDER, "fundamentals", norm, started)
    client = None
    try:
        client = _import_quotes_client(timeout=timeout)
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for method_name in ("finance", "financial", "xdxr", "company"):
            method = getattr(client, method_name, None)
            if not callable(method):
                continue
            try:
                raw = method(symbol=norm)
            except TypeError:
                raw = method(norm)
            frame = _as_frame(raw)
            if not frame.empty:
                record = frame.iloc[0].to_dict()
                record.update({"symbol": norm, "source": "mootdx", "provider_status": "ok", "method": method_name})
                rows.append(record)
                break
        if not rows:
            warnings.append("mootdx fundamentals 当前未返回可用基础/F10字段。")
            return _failed(FUNDAMENTAL_PROVIDER, "fundamentals", norm, started, "; ".join(warnings), failed_type="no_rows")
        payload = _base_result(FUNDAMENTAL_PROVIDER, "fundamentals", symbol=norm, network=True, status="ok")
        payload.update({"ok": True, "data": rows, "data_rows": len(rows), "warnings": warnings, "dependency_available": True})
        return _finish(payload, started)
    except Exception as exc:  # noqa: BLE001
        return _failed(FUNDAMENTAL_PROVIDER, "fundamentals", norm, started, exc)
    finally:
        _close_quotes_client(client)
