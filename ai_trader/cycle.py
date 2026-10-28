"""Evening entry: settle today's open fills, then post tomorrow's open plan."""

from __future__ import annotations

from typing import Any

from ai_trader.journal import append_journal
from ai_trader.mandate import DEFAULT_MANDATE
from ai_trader.overnight import DailySessionNotReady, run_evening
from ai_trader.paths import (
    CyclePaths,
    assert_not_quantdesk_ledger,
)
from quantdesk.utils import atomic_write_json

__all__ = ["CyclePaths", "run_cycle", "_write_outputs"]


def _write_outputs(runtime: CyclePaths, payload: dict[str, Any], note: dict[str, Any]) -> None:
    append_journal(note, latest_path=runtime.journal_latest, log_path=runtime.journal_log)
    atomic_write_json(assert_not_quantdesk_ledger(runtime.cycle_latest), payload)
    atomic_write_json(assert_not_quantdesk_ledger(runtime.snapshot), payload)


def run_cycle(
    *,
    paths: CyclePaths | None = None,
    lookback: int = 180,
    universe_limit: int = 240,
    network: bool | None = None,
    target_date: str | None = None,
    require_current_data: bool | None = None,
    data_wait_seconds: float = 7200.0,
) -> dict[str, Any]:
    runtime = paths or CyclePaths()
    use_network = DEFAULT_MANDATE.network_enabled if network is None else bool(network)
    result = run_evening(
        paths=runtime,
        lookback=lookback,
        universe_limit=universe_limit,
        network_news=use_network,
        target_date=target_date,
        require_current_data=require_current_data,
        data_wait_seconds=data_wait_seconds,
    )
    if result.get("status") == "deferred":
        # The CLI's existing success return must not turn a deferred account
        # into a green scheduler exit. No ledger or judgment was changed.
        raise DailySessionNotReady(result)
    return result
