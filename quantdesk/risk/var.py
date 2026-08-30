from __future__ import annotations

import numpy as np
import pandas as pd


def historical_var(returns: pd.Series, confidence: float = 0.95, lookback: int = 500) -> dict:
    window = pd.to_numeric(returns, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().tail(lookback)
    if len(window) < 100:
        return {"var_95": None, "cvar_95": None, "error": "insufficient_observations", "observations": int(len(window))}
    percentile = (1 - confidence) * 100
    var_value = float(np.percentile(window, percentile))
    tail = window[window <= var_value]
    cvar_value = float(tail.mean()) if len(tail) else var_value
    level = int(confidence * 100)
    return {
        f"var_{level}": round(var_value, 6),
        f"cvar_{level}": round(cvar_value, 6),
        "worst_return": round(float(window.min()), 6),
        "observations": int(len(window)),
    }


def portfolio_var(weights: dict[str, float], returns_df: pd.DataFrame, confidence: float = 0.95) -> dict:
    common = [name for name in weights if name in returns_df.columns]
    if not common:
        return {"error": "no_common_assets"}
    w = np.array([float(weights[name]) for name in common], dtype=float)
    values = returns_df[common].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    portfolio_returns = values.to_numpy() @ w
    return historical_var(pd.Series(portfolio_returns), confidence=confidence)
