from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

PARTIAL_DAILY_MARKERS = (
    "data/intraday/partial_daily",
    "data\\intraday\\partial_daily",
    "partial_daily",
)


def _iter_paths(path_or_paths: Any) -> Iterable[Any]:
    if path_or_paths is None:
        return []
    if isinstance(path_or_paths, (str, Path)):
        return [path_or_paths]
    if isinstance(path_or_paths, dict):
        return path_or_paths.values()
    try:
        return list(path_or_paths)
    except TypeError:
        return [path_or_paths]


def is_partial_daily_path(path: str | Path) -> bool:
    text = str(path).replace("/", "\\").lower()
    normalized = text.replace("\\", "/")
    return (
        "data/intraday/partial_daily" in normalized
        or normalized.endswith("/partial_daily")
        or "/partial_daily/" in normalized
    )


def assert_no_partial_daily_for_research(path_or_paths: Any, *, caller: str) -> None:
    offenders = [str(path) for path in _iter_paths(path_or_paths) if is_partial_daily_path(path)]
    if offenders:
        raise ValueError(
            f"{caller} cannot use provisional partial_daily intraday data for research/backtest paths: "
            + ", ".join(offenders[:5])
        )


def describe_intraday_data_policy() -> dict[str, Any]:
    return {
        "partial_daily_is_provisional": True,
        "partial_daily_allowed_callers": ["intraday_live_engine", "intraday_virtual_account_snapshot"],
        "partial_daily_forbidden_callers": [
            "backtest",
            "walk_forward",
            "factor_evaluation",
            "historical_replay_engine",
        ],
        "formal_daily_paths": ["data/daily"],
        "partial_daily_paths": ["data/intraday/partial_daily"],
        "spot_snapshot_paths": ["data/intraday/spot_snapshots"],
        "disclaimer": "Partial daily snapshots are provisional intraday observations and must not feed backtests, factors, walk-forward, or formal daily features.",
    }
