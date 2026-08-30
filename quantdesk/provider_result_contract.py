"""Small helpers for provider result envelopes.

The goal is observability, not a heavy framework: providers can keep their
existing payloads while smoke tests and UI diagnostics get consistent fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from quantdesk.a_stock_data_matrix import RISK_NOTE
from quantdesk.shared import ensure_list, normalize_symbol, short_error_text


def provider_now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def count_data_rows(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        if "data_rows" in value:
            try:
                return int(value.get("data_rows") or 0)
            except Exception:
                return 0
        if "row_count" in value:
            try:
                return int(value.get("row_count") or 0)
            except Exception:
                return 0
        return count_data_rows(value.get("data"))
    if hasattr(value, "empty") and hasattr(value, "__len__"):
        try:
            return 0 if bool(value.empty) else int(len(value))
        except Exception:
            return 0
    if isinstance(value, (list, tuple, set)):
        return len(value)
    return 0


def normalize_failed_reason(value: Any, *, max_len: int = 300) -> str:
    if isinstance(value, dict):
        candidates = [
            value.get("failed_reason"),
            "; ".join(str(item) for item in ensure_list(value.get("errors")) if item),
            value.get("message"),
        ]
        value = next((item for item in candidates if item), "")
    return short_error_text(value, max_len=max_len) if value else ""


def normalize_skipped_reason(value: Any, *, max_len: int = 300) -> str:
    if isinstance(value, dict):
        value = value.get("skipped_reason") or value.get("skip_reason") or ""
    return short_error_text(value, max_len=max_len) if value else ""


def provider_step(payload: dict[str, Any] | str, *, provider: str = "", category: str = "") -> dict[str, Any]:
    if isinstance(payload, str):
        return {
            "provider": provider or payload,
            "category": category,
            "status": "unknown",
            "ok": False,
            "failed_reason": "",
            "skipped_reason": "",
            "data_rows": 0,
        }
    return {
        "provider": provider or str(payload.get("provider") or payload.get("provider_used") or ""),
        "category": category or str(payload.get("category") or ""),
        "status": str(payload.get("status") or ("ok" if payload.get("ok") else "unknown")),
        "ok": bool(payload.get("ok")),
        "failed_reason": normalize_failed_reason(payload),
        "skipped_reason": normalize_skipped_reason(payload),
        "suggested_next_action": str(payload.get("suggested_next_action") or ""),
        "data_rows": count_data_rows(payload),
        "duration_ms": payload.get("duration_ms"),
        "retrieved_at": payload.get("retrieved_at") or "",
    }


def build_provider_result(
    *,
    provider: str,
    category: str,
    ok: bool = False,
    status: str = "skipped",
    symbol: str = "",
    symbols: list[str] | None = None,
    data: Any = None,
    warnings: list[Any] | None = None,
    errors: list[Any] | None = None,
    failed_reason: str = "",
    skipped_reason: str = "",
    suggested_next_action: str = "",
    provider_chain: list[Any] | None = None,
    data_source_trace: list[Any] | None = None,
    is_reliable: bool | None = None,
    network: bool = False,
    duration_ms: int | float | None = None,
    retrieved_at: str | None = None,
    disclaimer: str = RISK_NOTE,
    **extra: Any,
) -> dict[str, Any]:
    normalized_symbols = [normalize_symbol(item) for item in ensure_list(symbols) if normalize_symbol(item)]
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "status": status,
        "provider": provider,
        "provider_used": provider if ok else "",
        "category": category,
        "symbol": normalize_symbol(symbol) if symbol else "",
        "symbols": normalized_symbols,
        "data": data if data is not None else [],
        "data_rows": count_data_rows(data),
        "warnings": [short_error_text(item) for item in ensure_list(warnings)],
        "errors": [short_error_text(item) for item in ensure_list(errors)],
        "failed_reason": short_error_text(failed_reason) if failed_reason else "",
        "skipped_reason": short_error_text(skipped_reason) if skipped_reason else "",
        "suggested_next_action": suggested_next_action,
        "provider_chain": provider_chain or [provider],
        "data_source_trace": data_source_trace or provider_chain or [provider],
        "is_reliable": bool(ok) if is_reliable is None else bool(is_reliable),
        "network": bool(network),
        "retrieved_at": retrieved_at or provider_now_text(),
        "duration_ms": duration_ms,
        "disclaimer": disclaimer,
    }
    payload.update(extra)
    return payload


def summarize_provider_payload(
    payload: dict[str, Any] | None,
    *,
    provider: str,
    category: str,
    symbol: str = "",
    network: bool = False,
    duration_ms: int | float | None = None,
) -> dict[str, Any]:
    source = dict(payload or {})
    chain = source.get("provider_chain") or [provider]
    return build_provider_result(
        provider=str(source.get("provider") or provider),
        category=str(source.get("category") or category),
        ok=bool(source.get("ok")),
        status=str(source.get("status") or ("ok" if source.get("ok") else "failed")),
        symbol=str(source.get("symbol") or symbol),
        symbols=source.get("symbols") or [],
        data=source.get("data") or [],
        warnings=source.get("warnings") or [],
        errors=source.get("errors") or [],
        failed_reason=normalize_failed_reason(source),
        skipped_reason=normalize_skipped_reason(source),
        suggested_next_action=str(source.get("suggested_next_action") or ""),
        provider_chain=chain,
        data_source_trace=source.get("data_source_trace") or chain,
        is_reliable=source.get("is_reliable", bool(source.get("ok"))),
        network=bool(source.get("network", network)),
        duration_ms=source.get("duration_ms", duration_ms),
        retrieved_at=str(source.get("retrieved_at") or provider_now_text()),
        date_scope=source.get("date_scope"),
        supports_current_day=source.get("supports_current_day"),
        supports_historical_day=source.get("supports_historical_day"),
        failed_type=source.get("failed_type") or source.get("error_type") or "",
        data_rows=count_data_rows(source),
        row_count=source.get("row_count") or source.get("rows") or count_data_rows(source),
    )
