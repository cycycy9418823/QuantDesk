from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd


def deflated_sharpe_ratio(
    sharpe_ratios: list[float],
    T: int,
    skew: float = 0.0,
    kurt: float = 3.0,
    var_sharpes: float | None = None,
    *,
    periods_per_year: float = 252.0,
    observed_sharpe: float | None = None,
) -> float:
    """Probability that an annualized Sharpe exceeds the multiple-trial baseline.

    T counts return observations, not years. Convert annualized Sharpes and their
    variance to per-observation units before applying the PSR/DSR formula. The
    returned value is a probability in [0, 1], not the old unlabelled z score.
    The candidate list must describe the actual trial family; it is not proof
    against unrecorded strategy searches or dependent returns.
    """
    values = np.array([x for x in sharpe_ratios if np.isfinite(x)], dtype=float)
    if len(values) == 0 or T <= 1 or periods_per_year <= 0:
        return float("nan")
    values = values / math.sqrt(periods_per_year)
    if len(values) == 1:
        expected_max = 0.0
    else:
        n = len(values)
        variance = float(np.var(values, ddof=1)) if var_sharpes is None else float(var_sharpes) / periods_per_year
        if not math.isfinite(variance) or variance < 0:
            return float("nan")
        normal = NormalDist()
        gamma = 0.5772156649015329
        expected_max = math.sqrt(variance) * (
            (1.0 - gamma) * normal.inv_cdf(1.0 - 1.0 / n)
            + gamma * normal.inv_cdf(1.0 - 1.0 / (n * math.e))
        )
    observed = float(np.max(values)) if observed_sharpe is None else float(observed_sharpe) / math.sqrt(periods_per_year)
    variance_factor = 1.0 - skew * observed + (kurt - 1.0) * observed**2 / 4.0
    if not math.isfinite(variance_factor) or variance_factor <= 0:
        return float("nan")
    z_score = (observed - expected_max) * math.sqrt(T - 1) / math.sqrt(variance_factor)
    return float(NormalDist().cdf(z_score))


def pbo_test(returns_matrix: pd.DataFrame, n_splits: int = 16, n_trials: int = 100, seed: int | None = 7) -> dict:
    clean = returns_matrix.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    T, N = clean.shape
    if N < 2:
        return {"error": "need_at_least_two_strategies"}
    if T < n_splits * 4:
        return {"error": "insufficient_observations", "observations": int(T), "minimum": int(n_splits * 4)}
    rng = np.random.default_rng(seed)
    split_size = T // n_splits
    ranks: list[float] = []
    for _ in range(n_trials):
        splits = np.arange(n_splits)
        rng.shuffle(splits)
        in_sample = set(splits[: n_splits // 2])
        is_idx: list[int] = []
        oos_idx: list[int] = []
        for split in range(n_splits):
            start = split * split_size
            end = T if split == n_splits - 1 else min((split + 1) * split_size, T)
            (is_idx if split in in_sample else oos_idx).extend(range(start, end))
        is_returns = clean.iloc[is_idx]
        oos_returns = clean.iloc[oos_idx]
        is_sharpes = is_returns.mean() / is_returns.std(ddof=0).replace(0, np.nan) * np.sqrt(252)
        oos_sharpes = oos_returns.mean() / oos_returns.std(ddof=0).replace(0, np.nan) * np.sqrt(252)
        if is_sharpes.dropna().empty or oos_sharpes.dropna().empty:
            continue
        best = is_sharpes.idxmax()
        rank = float((oos_sharpes.values >= oos_sharpes[best]).sum() / N)
        ranks.append(rank)
    if not ranks:
        return {"error": "no_valid_trials"}
    pbo = sum(1 for rank in ranks if rank > 0.5) / len(ranks)
    return {
        "pbo": round(float(pbo), 4),
        "interpretation": "high_overfitting_risk" if pbo > 0.5 else "moderate_or_low_overfitting_risk",
        "oos_rank_mean": round(float(np.mean(ranks)), 4),
        "oos_rank_std": round(float(np.std(ranks)), 4),
        "n_trials": int(len(ranks)),
        "n_splits": int(n_splits),
    }


def minimum_track_record_length(
    sharpe: float,
    target_sharpe: float = 0.0,
    skew: float = -0.5,
    kurt: float = 5.0,
    alpha: float = 0.05,
    *,
    periods_per_year: float = 252.0,
) -> int | None:
    """Minimum whole years, using annualized Sharpe inputs and observed periods."""
    if not 0.0 < alpha < 0.5 or periods_per_year <= 0:
        raise ValueError("alpha must be in (0, .5) and periods_per_year positive")
    if not math.isfinite(sharpe) or not math.isfinite(target_sharpe):
        return None
    if sharpe <= target_sharpe:
        return None
    sharpe /= math.sqrt(periods_per_year)
    target_sharpe /= math.sqrt(periods_per_year)
    variance_factor = 1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe**2
    if not math.isfinite(variance_factor) or variance_factor <= 0:
        return None
    z_alpha = NormalDist().inv_cdf(1.0 - alpha)
    periods = 1.0 + variance_factor * (z_alpha / (sharpe - target_sharpe)) ** 2
    return int(math.ceil(periods / periods_per_year))
