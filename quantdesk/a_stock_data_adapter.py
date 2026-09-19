"""High-level A-share data adapter facade.

The adapter returns normalized envelopes and keeps provider chains visible.
Provider failures return ``ok=false`` instead of raising or fabricating data.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quantdesk.a_stock_data_matrix import RISK_NOTE, providers_by_category
from quantdesk.daily_price_providers import DAILY_DIR, read_local_daily_with_warnings
from quantdesk.intraday.intraday_providers import INTRADAY_1M_DIR
from quantdesk.intraday.intraday_schema import read_intraday_csv
from quantdesk.provider_result_contract import count_data_rows, provider_now_text
from quantdesk.shared import ensure_list, normalize_symbol, short_error_text


def _empty_result(
    *,
    category: str,
    provider_chain: list[dict[str, Any]],
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    provider_used: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "data": [],
        "provider_used": provider_used,
        "provider_chain": provider_chain,
        "warnings": warnings or [],
        "errors": errors or [],
        "category": category,
        "is_reliable": False,
        "disclaimer": RISK_NOTE,
    }


def _chain(category: str, *, network: bool = False) -> list[dict[str, Any]]:
    return providers_by_category(category, check_network=network)


def _unsupported(category: str, symbol: str = "", *, network: bool = False) -> dict[str, Any]:
    rows = _chain(category, network=network)
    reason = "没有可执行 provider 或 provider 未启用；不会用其他数据冒充。"
    if rows:
        reason = "; ".join(
            short_error_text(row.get("failed_reason") or row.get("suggested_next_action") or row.get("fallback_next_action"))
            for row in rows[:4]
        )
    result = _empty_result(category=category, provider_chain=rows, errors=[reason])
    if symbol:
        result["symbol"] = normalize_symbol(symbol)
    return result


def _step(provider: str, status: str, detail: str = "") -> dict[str, Any]:
    return {"provider": provider, "status": status, "detail": short_error_text(detail)}


def _normalize_provider_payload(payload: dict[str, Any], *, category: str, provider_name: str, chain: list[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(payload or {})
    payload.setdefault("ok", bool(payload.get("data")))
    payload.setdefault("category", category)
    payload.setdefault("provider_used", provider_name if payload.get("ok") else "")
    # The adapter chain is the authoritative normalized envelope. Provider
    # adapters may expose a legacy list of bare names, which is not suitable
    # for consumers expecting `{provider, status, detail}` records.
    payload["provider_chain"] = list(chain)
    payload["data_source_trace"] = list(chain)
    payload.setdefault("warnings", [])
    payload.setdefault("errors", [])
    payload.setdefault("is_reliable", bool(payload.get("ok")))
    payload.setdefault("data_rows", count_data_rows(payload))
    payload.setdefault("retrieved_at", provider_now_text())
    payload.setdefault("network", False)
    payload.setdefault("disclaimer", RISK_NOTE)
    return payload


def get_realtime_quote(symbols: Any, *, network: bool = False) -> dict[str, Any]:
    normalized = [normalize_symbol(item) for item in ensure_list(symbols) if normalize_symbol(item)]
    chain = [_step("local_cache", "skipped", "no realtime quote cache")]
    try:
        from quantdesk.providers import mootdx_provider

        for symbol in normalized:
            payload = mootdx_provider.fetch_realtime_quote(symbol, network=network)
            chain.append(_step("mootdx_quote", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
            if payload.get("ok"):
                return _normalize_provider_payload(payload, category="market_quote", provider_name="mootdx_quote", chain=chain)
            break
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("mootdx_quote", "failed", str(exc)))
    try:
        from quantdesk.providers import tencent_provider

        payload = tencent_provider.fetch_realtime_quote(normalized, network=network)
        chain.append(_step("tencent_realtime", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
        if payload.get("ok"):
            return _normalize_provider_payload(payload, category="market_quote", provider_name="tencent_realtime", chain=chain)
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("tencent_realtime", "failed", str(exc)))
    chain.append(_step("akshare_quote", "skipped", "akshare quote fallback not wired in adapter"))
    result = _empty_result(category="market_quote", provider_chain=chain, errors=["No quote provider returned data."])
    result["symbols"] = normalized
    return result


def get_daily_bars(symbol: str, start_date: str = "", end_date: str = "", *, network: bool = False) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    chain: list[dict[str, Any]] = []
    warnings: list[str] = []
    filter_warning = ""
    start_ts = None
    end_ts = None
    if start_date:
        start_ts = pd.to_datetime(start_date, errors="coerce")
        if pd.isna(start_ts):
            filter_warning = f"invalid_start_date:{start_date}"
    if end_date and not filter_warning:
        end_ts = pd.to_datetime(end_date, errors="coerce")
        if pd.isna(end_ts):
            filter_warning = f"invalid_end_date:{end_date}"
    if filter_warning:
        chain.append(_step("request", "failed", filter_warning))
        result = _empty_result(
            category="daily_kline",
            provider_chain=chain,
            warnings=["invalid_daily_date_filter"],
            errors=[filter_warning],
        )
        result.update({"symbol": norm, "start_date": start_date, "end_date": end_date, "status": "failed"})
        return result
    local, local_warnings = read_local_daily_with_warnings(norm, daily_dir=DAILY_DIR)
    warnings.extend(local_warnings)
    local_step_recorded = False
    if not local.empty:
        frame = local.copy()
        try:
            frame_dates = pd.to_datetime(frame["date"], errors="coerce")
            if start_ts is not None:
                frame = frame.loc[frame_dates >= start_ts]
            if end_ts is not None:
                frame = frame.loc[frame_dates <= end_ts]
        except Exception as exc:  # noqa: BLE001 - local cache problems are surfaced in the chain
            chain.append(_step("local_existing", "failed", f"date_filter_failed:{exc}"))
            local_step_recorded = True
            frame = pd.DataFrame()
        if not frame.empty:
            chain.append(
                _step(
                    "local_existing",
                    "warning" if local_warnings else "ok",
                    ("; ".join(local_warnings[:2]) if local_warnings else "read data/daily cache"),
                )
            )
            local_step_recorded = True
            return {
                "ok": True,
                "status": "ok",
                "category": "daily_kline",
                "symbol": norm,
                "start_date": start_date,
                "end_date": end_date,
                "data": frame.to_dict(orient="records"),
                "provider_used": "local_existing",
                "provider_chain": chain,
                "data_source_trace": chain,
                "data_rows": len(frame),
                "warnings": warnings,
                "errors": [],
                "is_reliable": True,
                "network": False,
                "retrieved_at": provider_now_text(),
                "disclaimer": RISK_NOTE,
            }
    if not local_step_recorded:
        chain.append(
            _step(
                "local_existing",
                "failed" if local_warnings else "skipped",
                ("; ".join(local_warnings[:2]) if local_warnings else "no local rows for requested range"),
            )
        )
    try:
        from quantdesk.providers import mootdx_provider

        payload = mootdx_provider.fetch_daily_bars(norm, start_date, end_date, network=network)
        chain.append(_step("mootdx_daily", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
        if payload.get("ok"):
            payload["symbol"] = norm
            payload["warnings"] = warnings + list(payload.get("warnings") or [])
            return _normalize_provider_payload(payload, category="daily_kline", provider_name="mootdx_daily", chain=chain)
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("mootdx_daily", "failed", str(exc)))
    try:
        from quantdesk.providers import tencent_provider

        payload = tencent_provider.fetch_daily_bars(norm, start_date, end_date, network=network)
        chain.append(_step("tencent_daily", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
        if payload.get("ok"):
            payload["symbol"] = norm
            payload["warnings"] = warnings + list(payload.get("warnings") or [])
            return _normalize_provider_payload(payload, category="daily_kline", provider_name="tencent_daily", chain=chain)
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("tencent_daily", "failed", str(exc)))
    chain.append(_step("akshare_daily", "skipped", "akshare fallback remains in legacy daily update engine"))
    result = _empty_result(category="daily_kline", provider_chain=chain, warnings=warnings, errors=["No daily provider returned data."])
    result.update({"symbol": norm, "start_date": start_date, "end_date": end_date})
    return result


def get_intraday_bars(symbol: str, date: str, period: str = "1m", *, network: bool = False) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    chain: list[dict[str, Any]] = []
    local_path = INTRADAY_1M_DIR / f"{norm}.csv"
    local = read_intraday_csv(local_path, default_symbol=norm, target_date=date)
    frame = local.get("frame")
    if local.get("ok") and frame is not None and not frame.empty:
        chain.append(_step("local_existing", "ok", "read local intraday cache"))
        return {
            "ok": True,
            "status": "ok",
            "category": "intraday_kline",
            "symbol": norm,
            "target_date": date,
            "period": period,
            "data": frame.to_dict(orient="records"),
            "provider_used": "local_existing",
            "provider_chain": chain,
            "data_source_trace": chain,
            "data_rows": len(frame),
            "warnings": local.get("warnings") or [],
            "errors": [],
            "is_reliable": True,
            "network": False,
            "retrieved_at": provider_now_text(),
            "disclaimer": RISK_NOTE,
        }
    chain.append(_step("local_existing", "skipped", "no local intraday rows for target date"))
    try:
        from quantdesk.providers import mootdx_provider

        payload = mootdx_provider.fetch_intraday_bars(norm, date, period=period, network=network)
        chain.append(_step("mootdx_intraday", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
        if payload.get("ok"):
            return _normalize_provider_payload(payload, category="intraday_kline", provider_name="mootdx_intraday", chain=chain)
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("mootdx_intraday", "failed", str(exc)))
    try:
        from quantdesk.providers import tencent_provider

        payload = tencent_provider.fetch_intraday_bars(norm, date, period=period, network=network)
        chain.append(_step("tencent_intraday", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
        if payload.get("ok"):
            return _normalize_provider_payload(payload, category="intraday_kline", provider_name="tencent_intraday", chain=chain)
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("tencent_intraday", "failed", str(exc)))
    chain.append(_step("akshare_intraday", "skipped", "akshare intraday fallback remains in intraday update engine"))
    result = _empty_result(category="intraday_kline", provider_chain=chain, errors=["No intraday provider returned data."])
    result.update({"symbol": norm, "target_date": date, "period": period})
    return result


def get_valuation(symbol: str, *, network: bool = False) -> dict[str, Any]:
    norm = normalize_symbol(symbol)
    chain: list[dict[str, Any]] = []
    try:
        from quantdesk.providers import tencent_provider

        payload = tencent_provider.fetch_valuation(norm, network=network)
        chain.append(_step("tencent_valuation", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
        if payload.get("ok"):
            return _normalize_provider_payload(payload, category="valuation", provider_name="tencent_valuation", chain=chain)
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("tencent_valuation", "failed", str(exc)))
    try:
        from quantdesk.providers import mootdx_provider

        payload = mootdx_provider.fetch_fundamentals(norm, network=network)
        chain.append(_step("mootdx_fundamental", payload.get("status", "unknown"), payload.get("failed_reason") or payload.get("skipped_reason") or ""))
        if payload.get("ok"):
            return _normalize_provider_payload(payload, category="fundamentals", provider_name="mootdx_fundamental", chain=chain)
    except Exception as exc:  # noqa: BLE001
        chain.append(_step("mootdx_fundamental", "failed", str(exc)))
    chain.append(_step("akshare_basic", "skipped", "akshare basic fallback not wired in adapter"))
    result = _empty_result(category="valuation", provider_chain=chain, errors=["No valuation provider returned data."])
    result["symbol"] = norm
    return result


def get_fundamentals(symbol: str, *, network: bool = False) -> dict[str, Any]:
    return _unsupported("fundamentals", symbol, network=network)


def get_announcements(symbol: str, start_date: str = "", end_date: str = "", *, network: bool = False) -> dict[str, Any]:
    try:
        from quantdesk.providers.announcement_provider import fetch_announcements

        payload = fetch_announcements(symbol, start_date, end_date, network=network)
        payload.setdefault("provider_chain", _chain("announcement", network=network))
        payload.setdefault("disclaimer", RISK_NOTE)
        return payload
    except Exception as exc:  # noqa: BLE001
        return _empty_result(category="announcement", provider_chain=_chain("announcement"), errors=[short_error_text(exc)])


def get_research_reports(symbol: str, start_date: str = "", end_date: str = "", *, network: bool = False) -> dict[str, Any]:
    try:
        from quantdesk.providers.research_report_provider import fetch_research_reports

        payload = fetch_research_reports(symbol, start_date, end_date, network=network)
        payload.setdefault("provider_chain", _chain("research_report", network=network))
        payload.setdefault("disclaimer", RISK_NOTE)
        return payload
    except Exception as exc:  # noqa: BLE001
        return _empty_result(category="research_report", provider_chain=_chain("research_report"), errors=[short_error_text(exc)])


def get_hot_themes(date: str = "", *, network: bool = False) -> dict[str, Any]:
    try:
        from quantdesk.providers.ths_hotspot_provider import fetch_hot_themes

        payload = fetch_hot_themes(date, network=network)
        payload.setdefault("provider_chain", _chain("hot_theme", network=network))
        payload.setdefault("disclaimer", RISK_NOTE)
        return payload
    except Exception as exc:  # noqa: BLE001
        return _empty_result(category="hot_theme", provider_chain=_chain("hot_theme"), errors=[short_error_text(exc)])


def get_news(symbol: str | None = None, limit: int = 20, *, network: bool = False) -> dict[str, Any]:
    try:
        from quantdesk.providers.news_provider import fetch_news

        payload = fetch_news(symbol, limit, network=network)
        payload.setdefault("provider_chain", _chain("news", network=network))
        payload.setdefault("disclaimer", RISK_NOTE)
        return payload
    except Exception as exc:  # noqa: BLE001
        return _empty_result(category="news", provider_chain=_chain("news"), errors=[short_error_text(exc)])
