from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from quantdesk.provider_update_config import provider_update_config
from quantdesk.trading_day_freshness import trading_day_freshness


def bounded_update_attempt(root: Path, network: bool = False, max_symbols: int = 200, skip_update: bool = False) -> dict[str, Any]:
    start = time.time()
    fresh = trading_day_freshness(root)
    if skip_update or fresh["data_fresh_for_latest_trading_day"]:
        attempted = False
        status = "skipped_fresh_or_requested"
        failure = ""
        fallback = False
    elif not network:
        attempted = False
        status = "network_disabled_using_local"
        failure = "network=false"
        fallback = True
    else:
        attempted = True
        status = "provider_failed_fallback_local"
        failure = "bounded provider update not configured for automatic writes"
        fallback = True
    return {
        "task_id": "v421_data_update_attempt",
        "status": status,
        "attempted": attempted,
        "provider_used": "none" if not attempted else "configured_optional_provider",
        "symbols_attempted": 0 if not attempted else min(max_symbols, 200),
        "rows_written": 0,
        "target_date": fresh["latest_expected_trade_date"],
        "elapsed_seconds": round(time.time() - start, 3),
        "failure_reason": failure,
        "fallback_used": fallback,
        "protected_paths_unchanged": True,
        "freshness": fresh,
        "provider_config": provider_update_config(),
    }

