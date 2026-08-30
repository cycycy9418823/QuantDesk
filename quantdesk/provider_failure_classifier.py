from __future__ import annotations

from typing import Any

from quantdesk.symbol_market_normalizer import normalize_symbol_market


def classify_failure(symbol: str, reason: Any, *, akshare_status: str = "", tencent_status: str = "") -> dict[str, Any]:
    text = str(reason or "").lower()
    norm = normalize_symbol_market(symbol)
    support = norm["support_status"]
    if support.startswith("unsupported"):
        final = "unsupported_market"
        retryable = False
        action = "human_review_or_skip_unsupported_market"
    elif "remote disconnected" in text or "connection aborted" in text:
        if "tencent daily rows do not cover" in text:
            final = "tencent_date_missing"
            retryable = True
            action = "retry_later_or_next_trading_day_with_tencent_first"
        elif "tencent daily returned no usable rows" in text or "empty" in text:
            final = "tencent_empty_response"
            retryable = True
            action = "retry_later_with_tencent_first"
        else:
            final = "akshare_remote_disconnect"
            retryable = True
            action = "prefer_tencent_fallback_or_retry_akshare_later"
    elif "timeout" in text:
        final = "timeout"
        retryable = True
        action = "retry_with_delay_and_rate_limit"
    elif "date_mismatch" in text or "readback" in text:
        final = "date_mismatch"
        retryable = False
        action = "inspect_provider_date_before_retry"
    elif "schema" in text:
        final = "schema_validation_failed"
        retryable = False
        action = "inspect_provider_columns_before_retry"
    elif "no recent" in text or "delist" in text:
        final = "delisted_or_no_recent_data"
        retryable = False
        action = "check_delist_or_suspension_metadata"
    elif "suspend" in text:
        final = "suspended_or_no_trade_on_target_date"
        retryable = True
        action = "retry_next_trading_day_or_verify_suspension"
    elif "symbol" in text and "format" in text:
        final = "tencent_symbol_format_error"
        retryable = True
        action = "fix_symbol_normalization_then_retry"
    else:
        final = "unknown_provider_error"
        retryable = True
        action = "retry_small_batch_and_inspect_provider_payload"
    return {
        "symbol": symbol,
        "normalized_symbol": norm["normalized_symbol"],
        "provider_symbol": norm["provider_symbol"],
        "support_status": support,
        "akshare_status": akshare_status,
        "tencent_status": tencent_status,
        "final_failure_reason": final,
        "retryable": retryable,
        "recommended_next_action": action,
    }
