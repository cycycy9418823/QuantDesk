"""Intraday provider capability diagnostics for scoped 1-minute data.

This module is diagnostic only. It can be invoked by CLI or POST-style
workflows, but it never connects to brokers, never places orders, and never
creates fallback market data when providers fail.
"""

from __future__ import annotations

# Compatibility markers: supports_historical_day skipped_reason.
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.intraday.intraday_providers import (
    DEFAULT_INTRADAY_PROVIDER_ORDER,
    INTRADAY_1M_DIR,
    PROVIDER_SPECS,
    fetch_intraday_1m,
    provider_registry_status,
)
from quantdesk.shared import normalize_symbol, now_text, read_json, short_error_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"
INTRADAY_PROVIDER_CAPABILITY_LATEST_JSON = RESULTS_DIR / "intraday_provider_capability_latest.json"

DISCLAIMER = "仅用于本地数据源能力诊断，不接券商、不真实下单、不构成投资建议。"


def minute_exchange_capability(provider: str, symbol: str) -> dict[str, Any]:
    """Distinguish a coded route from real provider availability."""
    code = normalize_symbol(symbol)
    exchange = "BJ" if code.startswith(("92", "4", "8")) else "SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else "unknown"
    known = {"mootdx": {"SH", "SZ", "BJ"}, "mootdx_intraday": {"SH", "SZ", "BJ"},
        "sina_intraday": {"SH", "SZ"}, "local_existing": {"SH", "SZ", "BJ"}}
    supported = exchange in known.get(provider, set())
    return {"provider": provider, "symbol": code, "exchange": exchange,
        "route_supported": supported, "live_availability": "unverified",
        "market_id": {"SH": 1, "SZ": 0, "BJ": 2}.get(exchange),
        "reason_code": "availability_requires_authorized_probe" if supported else "exchange_route_unsupported",
        "bounded_probe_attempts": 1 if exchange == "BJ" else None}


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _as_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _date_scope(provider: str, target_date: str, status: str) -> str:
    spec = PROVIDER_SPECS.get(provider)
    if status == "failed":
        return "failed"
    if not spec:
        return "unknown"
    if spec.supports_historical_day:
        return "historical_supported"
    if target_date[:10] and target_date[:10] != _today():
        return "current_day_only"
    return "current_day_only" if spec.supports_current_day else "unknown"


def _frame_rows(result: dict[str, Any]) -> int:
    frame = result.get("data")
    if isinstance(frame, pd.DataFrame):
        return int(len(frame))
    return int(result.get("rows") or 0)


def _sanitize_result(result: dict[str, Any], provider: str, symbol: str, target_date: str) -> dict[str, Any]:
    spec = PROVIDER_SPECS.get(provider)
    status = str(result.get("status") or "failed")
    rows = _frame_rows(result)
    ohlcv_complete = result.get("ohlcv_complete") is True
    signal_eligible = result.get("signal_eligible") is True
    price_only_intraday = result.get("price_only_intraday") is True
    formal_execution_eligible = (
        result.get("formal_execution_eligible") is True
    )
    formal_session_status = str(result.get("formal_session_status") or "")
    skipped_reason = str(result.get("skipped_reason") or "")
    failed_reason = short_error_text(result.get("failed_reason") or "")
    if status == "skipped" and not skipped_reason:
        if spec and not spec.supports_historical_day and target_date[:10] and target_date[:10] != _today():
            skipped_reason = "provider 仅支持当日分钟线，不支持历史目标日期"
        else:
            skipped_reason = failed_reason or "provider skipped"
    suggested = str(result.get("suggested_next_action") or "")
    if status == "ok" and not formal_execution_eligible:
        suggested = (
            "not formal: provider 已返回目标日数据，但尚未通过 raw 与严格完整会话审计；"
            "不得据此执行条件计划。"
        )
    if not suggested:
        if status == "ok":
            suggested = "已获得目标日期分钟线，可执行条件交易计划。"
        elif status == "not_available":
            suggested = "安装依赖，或改用 CSV 导入。"
        elif status == "failed":
            suggested = "检查网络或 provider 接口，或改用 CSV 导入。"
        elif status == "skipped":
            suggested = "请导入目标日期分钟线 CSV，或在 provider 支持的日期范围内重试。"
        else:
            suggested = "请查看 provider 状态后决定是否导入 CSV。"
    return {
        "provider": provider,
        "exchange_capability": minute_exchange_capability(provider, symbol),
        "symbol": normalize_symbol(symbol),
        "target_date": target_date[:10],
        "status": status,
        "dependency_installed": status != "not_available",
        "network_request_success": status == "ok",
        "supports_current_day": getattr(spec, "supports_current_day", "unknown") if spec else "unknown",
        "supports_historical_day": getattr(spec, "supports_historical_day", "unknown") if spec else "unknown",
        "supports_target_date": status == "ok" and rows > 0,
        "date_scope": str(result.get("date_scope") or _date_scope(provider, target_date, status)),
        "supported_periods": list(getattr(spec, "supported_periods", ()) if spec else ()),
        "max_history_days": getattr(spec, "max_history_days", 0) if spec else 0,
        "requires_sdk": bool(getattr(spec, "requires_sdk", False)) if spec else False,
        "network_required": bool(getattr(spec, "network_required", True)) if spec else True,
        "requires_network": bool(getattr(spec, "network_required", True)) if spec else True,
        "requires_dependency": bool(getattr(spec, "dependencies", ())) if spec else False,
        "dependency_available": status != "not_available",
        "rows": rows,
        "ohlcv_complete": ohlcv_complete,
        "signal_eligible": signal_eligible,
        "price_only_intraday": price_only_intraday,
        "formal_execution_eligible": formal_execution_eligible,
        "formal_session_status": formal_session_status,
        "latest_datetime": str(result.get("latest_datetime") or ""),
        "covers_target_date": status == "ok" and rows > 0,
        "failed_reason": failed_reason,
        "skipped_reason": skipped_reason,
        "suggested_next_action": suggested,
        "warnings": list(result.get("warnings") or []),
    }


def check_intraday_provider_capability(
    *,
    symbols: str | list[str] | tuple[str, ...] = "600744,002115",
    dates: str | list[str] | tuple[str, ...] = "",
    provider_order: list[str] | None = None,
    network: bool = False,
    intraday_dir: Path | None = None,
    output_path: Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    symbol_list = [normalize_symbol(item) for item in _as_list(symbols) if normalize_symbol(item)]
    date_list = [item[:10] for item in _as_list(dates) if item[:10]]
    if not symbol_list:
        symbol_list = ["600744", "002115"]
    if not date_list:
        date_list = [_today()]
    order = [name for name in (provider_order or DEFAULT_INTRADAY_PROVIDER_ORDER) if name in PROVIDER_SPECS]
    intraday_dir = Path(intraday_dir or INTRADAY_1M_DIR)
    output_path = Path(output_path or INTRADAY_PROVIDER_CAPABILITY_LATEST_JSON)

    rows: list[dict[str, Any]] = []
    for target_date in date_list:
        for symbol in symbol_list:
            for provider in order:
                result = fetch_intraday_1m(
                    provider,
                    symbol,
                    target_date,
                    intraday_dir=intraday_dir,
                    network=bool(network),
                )
                rows.append(_sanitize_result(result, provider, symbol, target_date))

    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[str(row.get("status") or "unknown")] = status_counts.get(str(row.get("status") or "unknown"), 0) + 1
    payload = {
        "ok": True,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "intraday_provider_capability",
        "generated_at": now_text(),
        "network": bool(network),
        "full_scan": False,
        "is_real_order": False,
        "symbols": symbol_list,
        "dates": date_list,
        "provider_order": order,
        "provider_registry": provider_registry_status(check_network=False),
        "provider_results": rows,
        "summary": {
            "total_checks": len(rows),
            "ok_count": status_counts.get("ok", 0),
            "failed_count": status_counts.get("failed", 0),
            "skipped_count": status_counts.get("skipped", 0),
            "not_available_count": status_counts.get("not_available", 0),
            "historical_supported_provider_count": len([name for name in order if PROVIDER_SPECS[name].supports_historical_day]),
            "current_day_only_provider_count": len([name for name in order if not PROVIDER_SPECS[name].supports_historical_day and PROVIDER_SPECS[name].supports_current_day]),
        },
        "disclaimer": DISCLAIMER,
    }
    if write:
        write_json(output_path, payload)
        payload["path"] = _display_path(output_path)
    return payload


def latest_intraday_provider_capability(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or INTRADAY_PROVIDER_CAPABILITY_LATEST_JSON)
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return {
            "ok": True,
            "exists": False,
            "path": _display_path(path),
            "message": "尚未运行分钟线 provider 能力探测。",
            "disclaimer": DISCLAIMER,
        }
    payload["exists"] = True
    payload.setdefault("path", _display_path(path))
    payload.setdefault("disclaimer", DISCLAIMER)
    return payload


__all__ = [
    "INTRADAY_PROVIDER_CAPABILITY_LATEST_JSON",
    "check_intraday_provider_capability",
    "latest_intraday_provider_capability",
]
