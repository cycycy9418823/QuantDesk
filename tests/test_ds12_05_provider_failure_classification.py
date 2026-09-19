"""DS12-05: provider failure classification is explicit and not fabricated.

Offline only. Every class that the batch catch-up chain can emit is pinned so a
provider error can never collapse into an invented "real data" outcome.
"""
from __future__ import annotations

import pytest

from quantdesk import provider_failure_classifier as classifier


@pytest.mark.parametrize(
    ("reason", "expected", "retryable"),
    [
        ("akshare remote disconnected", "akshare_remote_disconnect", True),
        (
            "akshare remote disconnected; tencent daily rows do not cover the target date",
            "tencent_date_missing",
            True,
        ),
        (
            "akshare remote disconnected; tencent daily returned no usable rows: empty",
            "tencent_empty_response",
            True,
        ),
        ("request timeout after 10s", "timeout", True),
        ("readback date_mismatch detected", "date_mismatch", False),
        ("schema validation failed: missing columns", "schema_validation_failed", False),
        ("symbol delisted or no recent data", "delisted_or_no_recent_data", False),
        ("stock suspended on target date", "suspended_or_no_trade_on_target_date", True),
        ("symbol format error: 600000", "tencent_symbol_format_error", True),
        ("some totally unexpected upstream failure", "unknown_provider_error", True),
    ],
)
def test_classify_failure_distinct_classes(reason: str, expected: str, retryable: bool) -> None:
    result = classifier.classify_failure("600000", reason)

    assert result["final_failure_reason"] == expected
    assert result["retryable"] is retryable
    assert result["recommended_next_action"]


def test_classify_failure_unsupported_market_is_not_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        classifier,
        "normalize_symbol_market",
        lambda symbol: {
            "normalized_symbol": symbol,
            "provider_symbol": symbol,
            "support_status": "unsupported_market",
        },
    )
    result = classifier.classify_failure("BTCUSD", "anything")

    assert result["final_failure_reason"] == "unsupported_market"
    assert result["retryable"] is False
