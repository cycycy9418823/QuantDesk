"""Frozen independent research validation plans; never selects a live strategy."""
from __future__ import annotations

from datetime import date, datetime, time, timezone, timedelta
from hashlib import sha256
import json
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd


def identity_hash(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_validation_plan(*, selection_end: str, validation_start: str, validation_end: str,
                          selected_config: dict[str, Any], selection_data_hash: str,
                          run_id: str, holding_sessions: int = 5, selection_trials: int = 3,
                          cost: float = 0.004, min_independent_observations: int = 20) -> dict[str, Any]:
    """Freeze a plan before a new interval; dates alone never prove unseen data."""
    if not (date.fromisoformat(selection_end) < date.fromisoformat(validation_start) <= date.fromisoformat(validation_end)):
        raise ValueError("validation_window_not_after_selection")
    if not run_id or len(selection_data_hash) != 64 or holding_sessions <= 0 or selection_trials <= 0:
        raise ValueError("validation_identity_missing")
    plan = {"schema": "independent_validation.v1", "run_id": run_id, "selection_end": selection_end,
        "validation_start": validation_start, "validation_end": validation_end,
        "selected_config": selected_config, "config_sha256": identity_hash(selected_config),
        "selection_data_sha256": selection_data_hash, "holding_sessions": holding_sessions,
        "purge_sessions": holding_sessions, "selection_trials": selection_trials,
        "round_trip_cost": cost, "min_independent_observations": min_independent_observations,
        "confidence_level": 0.95, "multiple_selection_method": "bonferroni_existing_templates",
        "unseen_evidence_status": "pending", "research_only": True, "default_selection_changed": False}
    return {**plan, "plan_sha256": identity_hash(plan)}


def verify_validation_plan(plan: dict[str, Any]) -> bool:
    try:
        unsigned = {k: v for k, v in plan.items() if k != "plan_sha256"}
        return (plan.get("schema") == "independent_validation.v1" and identity_hash(unsigned) == plan["plan_sha256"]
            and identity_hash(plan["selected_config"]) == plan["config_sha256"])
    except (TypeError, ValueError, KeyError):
        return False


def evaluate_independent_returns(plan: dict[str, Any], returns: pd.Series, *,
                                 benchmark: pd.Series | None = None,
                                 unseen_evidence: dict[str, Any] | None = None,
                                 interval_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate a supplied nonoverlapping hold-period series, never reselect it.

    The caller supplies interval returns from the frozen method. This function
    reports observed statistics even while independent-sample proof is pending.
    """
    if not verify_validation_plan(plan):
        raise ValueError("validation_plan_hash_mismatch")
    dates = pd.to_datetime(returns.index, errors="coerce")
    mask = ((dates >= pd.Timestamp(plan["validation_start"])) & (dates <= pd.Timestamp(plan["validation_end"])))
    sample = pd.to_numeric(returns.loc[mask], errors="coerce")
    warnings: list[str] = []
    if dates.isna().any() or not returns.index.is_unique:
        warnings.append("invalid_validation_dates")
    if sample.isna().any() or not np.isfinite(sample).all():
        warnings.append("invalid_validation_returns")
        sample = sample.iloc[:0]
    n = len(sample)
    net = sample - float(plan["round_trip_cost"])
    z = NormalDist().inv_cdf(1 - .05 / (2 * int(plan["selection_trials"])))
    half = z * float(net.std(ddof=1)) / np.sqrt(n) if n > 1 else None
    mean = float(net.mean()) if n else None
    if n < plan["min_independent_observations"]:
        warnings.append("independent_observations_below_minimum")
    if benchmark is None or not sample.index.isin(benchmark.index).all():
        excess = None
        warnings.append("matched_benchmark_missing")
    else:
        base = pd.to_numeric(benchmark.reindex(sample.index), errors="coerce")
        excess = float((net - base).mean()) if n and np.isfinite(base).all() else None
        if excess is None:
            warnings.append("matched_benchmark_invalid")
    # An everyday overlapping 5-session return series is not 20 independent
    # observations. Demand the original session calendar and interval mapping.
    interval = interval_evidence or {}
    interval_ok = False
    return_rows = [[pd.Timestamp(day).date().isoformat(), float(value)] for day, value in sample.items()]
    return_sha = identity_hash(return_rows)
    try:
        sessions = interval["trading_sessions"]
        windows = interval["intervals"]
        signed = {k: v for k, v in interval.items() if k != "evidence_sha256"}
        if (interval.get("evidence_sha256") != identity_hash(signed)
                or interval.get("plan_sha256") != plan["plan_sha256"]
                or interval.get("returns_sha256") != return_sha
                or not isinstance(sessions, list) or sessions != sorted(set(sessions))
                or not all(date.fromisoformat(d).isoformat() == d for d in sessions)
                or len(windows) != n or not n):
            raise ValueError("interval_identity_invalid")
        positions = {d: i for i, d in enumerate(sessions)}
        cutoff = positions[plan["selection_end"]]
        previous_exit = None
        for row, (day, _value) in zip(windows, return_rows):
            entry, exit_ = positions[row["entry_date"]], positions[row["exit_date"]]
            if (row["entry_date"] != day or exit_ - entry != plan["holding_sessions"]
                    or entry - cutoff < plan["purge_sessions"]
                    or row["exit_date"] > plan["validation_end"]
                    or (previous_exit is not None and entry < previous_exit)):
                raise ValueError("overlapping_or_unpurged_intervals")
            previous_exit = exit_
        interval_ok = True
    except (KeyError, TypeError, ValueError):
        warnings.append("independent_interval_or_purge_evidence_missing")
    unseen = unseen_evidence or {}
    proven_unseen = False
    try:
        declared = datetime.fromisoformat(unseen["predeclared_at"])
        start = datetime.combine(date.fromisoformat(plan["validation_start"]), time.min,
            tzinfo=timezone(timedelta(hours=8)))
        unsigned = {k: v for k, v in unseen.items() if k != "evidence_sha256"}
        proven_unseen = (interval_ok and unseen.get("status") == "verified"
            and unseen.get("evidence_sha256") == identity_hash(unsigned)
            and unseen.get("plan_sha256") == plan["plan_sha256"]
            and unseen.get("run_id") == plan["run_id"]
            and unseen.get("config_sha256") == plan["config_sha256"]
            and unseen.get("selection_data_sha256") == plan["selection_data_sha256"]
            and unseen.get("validation_data_sha256") == return_sha
            and declared.tzinfo is not None and declared < start
            and date.fromisoformat(unseen["selection_max_label_date"]) <= date.fromisoformat(plan["selection_end"]))
    except (TypeError, ValueError, KeyError):
        pass
    if not proven_unseen:
        warnings.append("unseen_interval_not_proven")
    return {"plan_sha256": plan["plan_sha256"], "run_id": plan["run_id"], "config_sha256": plan["config_sha256"],
        "validation_n": n, "net_mean": mean, "net_mean_ci": [mean-half, mean+half] if half is not None else None,
        "benchmark_excess_mean": excess, "multiple_selection_method": plan["multiple_selection_method"],
        "independent_n": n if interval_ok else None, "interval_evidence_verified": interval_ok,
        "unseen_evidence_verified": proven_unseen, "returns_sha256": return_sha,
        "warnings": warnings, "validation_status": "pending" if warnings else "research_diagnostic_complete",
        "proven_edge": False, "label": "研究假设 · 未验证", "default_selection_changed": False}
