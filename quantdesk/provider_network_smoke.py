"""Scoped real-provider network smoke checks.

This module is intentionally explicit: network access only happens when the
caller passes ``network=True``. The default path is a no-network diagnostic.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable


from quantdesk.a_stock_data_matrix import RISK_NOTE
from quantdesk.csv_io import read_csv_frame
from quantdesk.provider_result_contract import summarize_provider_payload
from quantdesk.shared import normalize_symbol, truthy, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOW_PRICE_UNIVERSE_CSV = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"
PROVIDER_NETWORK_SMOKE_LATEST_JSON = PROJECT_ROOT / "results" / "provider_network_smoke_latest.json"


def _split_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _split_providers(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip().lower() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = [str(item).strip().lower() for item in value if str(item).strip()]
    else:
        raw = ["tencent"]
    allowed = {"tencent", "mootdx"}
    out: list[str] = []
    for item in raw or ["tencent"]:
        if item in allowed and item not in out:
            out.append(item)
    return out or ["tencent"]


def load_low_price_sample(limit: int = 3, *, universe_path: Path | None = None) -> list[str]:
    path = universe_path or LOW_PRICE_UNIVERSE_CSV
    if not path.exists():
        return []
    frame = read_csv_frame(path, dtype=str, encoding="utf-8-sig")
    if any(item.startswith("csv_read_failed:") for item in list(frame.attrs.get("warnings") or [])):
        return []
    if "symbol" not in frame.columns:
        return []
    if "included" in frame.columns:
        frame = frame[frame["included"].map(truthy)]
    symbols = [normalize_symbol(item) for item in frame["symbol"].tolist()]
    symbols = [item for item in symbols if item]
    deduped = list(dict.fromkeys(symbols))
    return deduped[: max(0, int(limit or 0)) or 3]


def resolve_smoke_symbols(symbols: Any = None, *, limit: int = 3) -> list[str]:
    explicit = _split_symbols(symbols)
    if explicit:
        return explicit
    return load_low_price_sample(limit=limit)[: max(0, int(limit or 0)) or 3]


def _timed_call(func: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    try:
        payload = func()
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "status": "failed",
            "failed_reason": str(exc),
            "errors": [str(exc)],
            "warnings": [],
            "data": [],
        }
    return payload, int(round((time.perf_counter() - started) * 1000))


def _summarize_step(
    payload: dict[str, Any],
    *,
    provider: str,
    category: str,
    symbol: str,
    network: bool,
    duration_ms: int,
) -> dict[str, Any]:
    row = summarize_provider_payload(
        payload,
        provider=provider,
        category=category,
        symbol=symbol,
        network=network,
        duration_ms=duration_ms,
    )
    # The smoke report is an observability artifact, not a data cache.
    row["data"] = []
    return row


def _provider_calls(
    provider_key: str,
    symbol: str,
    target_date: str,
    *,
    network: bool,
    timeout: int,
) -> list[tuple[str, str, str, Callable[[], dict[str, Any]]]]:
    if provider_key == "mootdx":
        from quantdesk.providers import mootdx_provider

        return [
            (
                "realtime quote",
                "market_quote",
                "mootdx_quote",
                lambda s=symbol: mootdx_provider.fetch_realtime_quote(s, network=network, timeout=timeout),
            ),
            (
                "daily bars",
                "daily_kline",
                "mootdx_daily",
                lambda s=symbol: mootdx_provider.fetch_daily_bars(s, target_date, target_date, network=network, timeout=timeout),
            ),
            (
                "intraday bars",
                "intraday_kline",
                "mootdx_intraday",
                lambda s=symbol: mootdx_provider.fetch_intraday_bars(s, target_date, period="1m", network=network, timeout=timeout),
            ),
            (
                "fundamentals",
                "fundamentals",
                "mootdx_fundamental",
                lambda s=symbol: mootdx_provider.fetch_fundamentals(s, network=network, timeout=timeout),
            ),
        ]
    from quantdesk.providers import tencent_provider

    return [
        (
            "realtime quote",
            "market_quote",
            "tencent_realtime",
            lambda s=symbol: tencent_provider.fetch_realtime_quote([s], network=network, timeout=timeout),
        ),
        (
            "daily bars",
            "daily_kline",
            "tencent_daily",
            lambda s=symbol: tencent_provider.fetch_daily_bars(s, target_date, target_date, network=network, timeout=timeout),
        ),
        (
            "intraday bars",
            "intraday_kline",
            "tencent_intraday",
            lambda s=symbol: tencent_provider.fetch_intraday_bars(s, target_date, period="1m", network=network, timeout=timeout),
        ),
        (
            "valuation",
            "valuation",
            "tencent_valuation",
            lambda s=symbol: tencent_provider.fetch_valuation(s, network=network, timeout=timeout),
        ),
    ]


def run_provider_network_smoke(
    *,
    symbols: Any = None,
    target_date: str = "",
    network: bool = False,
    limit: int = 3,
    output_path: Path | None = PROVIDER_NETWORK_SMOKE_LATEST_JSON,
    write: bool = True,
    timeout: int = 8,
    providers: Any = "tencent",
) -> dict[str, Any]:
    selected_symbols = resolve_smoke_symbols(symbols, limit=limit)
    selected_providers = _split_providers(providers)
    steps: list[dict[str, Any]] = []

    for symbol in selected_symbols:
        for provider_key in selected_providers:
            for label, category, provider, call in _provider_calls(provider_key, symbol, target_date, network=network, timeout=timeout):
                payload, elapsed = _timed_call(call)
                step = _summarize_step(payload, provider=provider, category=category, symbol=symbol, network=network, duration_ms=elapsed)
                step["label"] = label
                step["provider_group"] = provider_key
                steps.append(step)

    counts = {"ok": 0, "skipped": 0, "failed": 0, "partial": 0}
    for step in steps:
        status = str(step.get("status") or "").lower()
        if step.get("ok") and status not in {"partial"}:
            counts["ok"] += 1
        elif status == "partial":
            counts["partial"] += 1
        elif status == "skipped":
            counts["skipped"] += 1
        else:
            counts["failed"] += 1

    payload = {
        "ok": counts["failed"] == 0,
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "task_type": "provider_network_smoke",
        "network": bool(network),
        "full_scan": False,
        "target_date": target_date,
        "symbols": selected_symbols,
        "providers": selected_providers,
        "summary": {
            "symbol_count": len(selected_symbols),
            "provider_count": len(selected_providers),
            "step_count": len(steps),
            **counts,
        },
        "steps": steps,
        "path": str(output_path) if output_path else "",
        "disclaimer": RISK_NOTE,
    }
    if write and output_path is not None:
        write_json(output_path, payload)
    return payload


def print_smoke_summary(payload: dict[str, Any]) -> None:
    print("QuantDesk Provider Network Smoke")
    print("")
    print(f"network: {str(payload.get('network')).lower()}")
    print(f"providers: {', '.join(payload.get('providers') or []) or '-'}")
    print(f"symbols: {', '.join(payload.get('symbols') or []) or '-'}")
    print(f"target_date: {payload.get('target_date') or '-'}")
    print("")
    steps = payload.get("steps") or []
    for symbol in payload.get("symbols") or []:
        print(f"{symbol}:")
        for step in [item for item in steps if item.get("symbol") == symbol]:
            reason = step.get("failed_reason") or step.get("skipped_reason") or ""
            reason_text = f", reason={reason}" if reason else ""
            print(
                f"* {step.get('provider_group')}/{step.get('label')}: {step.get('status')}, "
                f"provider={step.get('provider')}, rows={step.get('data_rows')}{reason_text}"
            )
        print("")
    summary = payload.get("summary") or {}
    print("总计：")
    print(f"ok: {summary.get('ok', 0)}")
    print(f"partial: {summary.get('partial', 0)}")
    print(f"skipped: {summary.get('skipped', 0)}")
    print(f"failed: {summary.get('failed', 0)}")
    if payload.get("path"):
        print(f"输出：{payload.get('path')}")
