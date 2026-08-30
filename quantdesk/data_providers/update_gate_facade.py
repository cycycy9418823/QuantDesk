"""Unified data-update gate facade (Phase 0.5 闸门合并).

The project historically spreads the "should I refresh market data right now?"
decision across several independent gates:

- ``app_open_refresh_gate.evaluate_app_open_refresh_gate`` — throttle on app open
- ``work_start_update_gate.evaluate_work_start_update_gate`` — throttle at work start
- ``bounded_data_updater.run_bounded_update`` — bounded (capped) catch-up update

Each has its own interval/lock logic, so the combined behaviour is hard to
predict.  Rather than rip them out in a 770-file live project (high risk), this
module is a **single consolidated decision point** following the strangler-fig
pattern: new code asks one function, and the facade composes the existing gates
into one explicit, logged decision.  Legacy callers keep working unchanged.

Pure-ish: no network unless ``run_update=True`` is explicitly requested via
:func:`maybe_run_bounded_update`.  It is for local research orchestration only;
it never connects to a broker or places orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


@dataclass
class GateDecision:
    """Consolidated decision from all update gates."""

    should_update: bool
    reasons: list[str] = field(default_factory=list)
    gate_results: dict[str, Any] = field(default_factory=dict)
    blocked_by: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_update": self.should_update,
            "reasons": list(self.reasons),
            "blocked_by": list(self.blocked_by),
            "gate_results": dict(self.gate_results),
        }


def _safe_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str]:
    """Call a gate function defensively; return (result, error_text)."""
    try:
        return fn(*args, **kwargs), ""
    except Exception as exc:  # noqa: BLE001 - gate must never crash the caller
        return None, f"{type(exc).__name__}: {exc}"


def _truthy_should_update(result: Any) -> bool | None:
    """Extract a should-update boolean from a gate result dict, if present."""
    if isinstance(result, dict):
        for key in ("should_update", "update", "allowed", "due", "should_refresh"):
            if key in result:
                return bool(result[key])
    if isinstance(result, bool):
        return result
    return None


def evaluate_update_gate(
    *,
    now: datetime | None = None,
    check_app_open: bool = True,
    check_work_start: bool = True,
    app_open_min_interval_minutes: int = 60,
    work_start_min_interval_minutes: int = 30,
    state_path: Path | None = None,
    last_update_path: Path | None = None,
    last_refresh_path: Path | None = None,
    market_calendar: Any = None,
) -> GateDecision:
    """Compose the historical gates into one explicit decision.

    The combined rule is conservative-AND: an update is recommended only when
    *every* enabled gate agrees it is due.  Each gate's raw result is preserved
    under ``gate_results`` for transparency, and any gate that errors is
    recorded in ``blocked_by`` (and forces ``should_update=False``).
    """

    decision = GateDecision(should_update=True)
    votes: list[bool] = []

    if check_app_open:
        from quantdesk.app_open_refresh_gate import evaluate_app_open_refresh_gate

        result, err = _safe_call(
            evaluate_app_open_refresh_gate,
            now=now,
            last_refresh_path=last_refresh_path,
            min_interval_minutes=app_open_min_interval_minutes,
            market_calendar=market_calendar,
        )
        decision.gate_results["app_open"] = result if err == "" else {"error": err}
        if err:
            decision.blocked_by.append(f"app_open_gate_error:{err}")
        else:
            vote = _truthy_should_update(result)
            if vote is not None:
                votes.append(vote)
                decision.reasons.append(f"app_open_gate={vote}")

    if check_work_start:
        from quantdesk.work_start_update_gate import evaluate_work_start_update_gate

        result, err = _safe_call(
            evaluate_work_start_update_gate,
            now=now,
            state_path=state_path,
            last_update_path=last_update_path,
            market_calendar=market_calendar,
            min_interval_minutes=work_start_min_interval_minutes,
        )
        decision.gate_results["work_start"] = result if err == "" else {"error": err}
        if err:
            decision.blocked_by.append(f"work_start_gate_error:{err}")
        else:
            vote = _truthy_should_update(result)
            if vote is not None:
                votes.append(vote)
                decision.reasons.append(f"work_start_gate={vote}")

    # Conservative-AND across the gates that produced a clear vote.
    if decision.blocked_by:
        decision.should_update = False
        decision.reasons.append("blocked_by_gate_error")
    elif votes:
        decision.should_update = all(votes)
    else:
        # No gate produced an actionable vote -> do not update by default.
        decision.should_update = False
        decision.reasons.append("no_gate_vote_default_skip")

    return decision


def maybe_run_bounded_update(
    symbols: list[str] | None = None,
    *,
    max_symbols: int = 50,
    network: bool = False,
    daily_dir: Path | None = None,
    decision: GateDecision | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the bounded updater only when the gate decision allows (or forced).

    ``network`` defaults to False so this stays safe to call in tests / offline.
    """

    gate = decision or evaluate_update_gate()
    if not force and not gate.should_update:
        return {
            "ran": False,
            "reason": "update_gate_skip",
            "gate": gate.as_dict(),
        }
    from quantdesk.bounded_data_updater import bounded_update_attempt
    from quantdesk.paths import PATHS

    root = daily_dir if daily_dir is not None else PATHS.root
    result, err = _safe_call(
        bounded_update_attempt,
        root,
        network=network,
        max_symbols=max_symbols,
    )
    if err:
        return {"ran": False, "reason": "bounded_update_error", "error": err, "gate": gate.as_dict()}
    return {"ran": True, "result": result, "gate": gate.as_dict()}


__all__ = [
    "GateDecision",
    "evaluate_update_gate",
    "maybe_run_bounded_update",
]
