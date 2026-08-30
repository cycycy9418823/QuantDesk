from __future__ import annotations

import math

import numpy as np
import pandas as pd


def deflated_sharpe_ratio(
    sharpe_ratios: list[float],
    T: int,
    skew: float = 0.0,
    kurt: float = 3.0,
    var_sharpes: float | None = None,
) -> float:
    values = np.array([x for x in sharpe_ratios if np.isfinite(x)], dtype=float)
    if len(values) == 0 or T <= 1:
        return float("nan")
    if len(values) == 1:
        expected_max = 0.0
        variance = var_sharpes if var_sharpes is not None else 0.001
    else:
        n = len(values)
        variance = float(np.var(values)) if var_sharpes is None else float(var_sharpes)
        expected_max = math.sqrt(2 * math.log(n)) - (0.5772156649 + math.log(math.log(n))) / math.sqrt(2 * math.log(n))
    best = float(np.max(values))
    se = math.sqrt(max(1e-12, (1 + 0.5 * best**2 - skew * best + (kurt - 3) / 4 * best**2) / T))
    return round(float((best - expected_max * math.sqrt(max(variance, 0.0))) / se), 4)


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
) -> int:
    if sharpe <= target_sharpe:
        return 99999
    try:
        from scipy import stats

        z_alpha = float(stats.norm.ppf(1 - alpha))
    except Exception:
        z_alpha = 1.6448536269514722
    numerator = z_alpha * math.sqrt(max(1e-12, 1 - skew * sharpe + (kurt - 1) / 4 * sharpe**2))
    periods = (numerator / (sharpe - target_sharpe)) ** 2
    return int(math.ceil(periods / 252))
