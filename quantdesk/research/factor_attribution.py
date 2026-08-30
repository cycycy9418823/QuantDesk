from __future__ import annotations

import math
from typing import Any

DISCLAIMER = "Linear factor attribution is research-only, not causal proof, not investment advice."


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return value
    return value


def _series(values: Any) -> list[float]:
    if values is None:
        return []
    if hasattr(values, "tolist"):
        values = values.tolist()
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, (list, tuple)):
        values = [values]
    return [_safe_float(item) for item in values]


def _factor_matrix(factor_returns: Any) -> tuple[list[str], list[list[float]]]:
    if factor_returns is None:
        return [], []
    if hasattr(factor_returns, "to_dict"):
        try:
            payload = factor_returns.to_dict(orient="list")
        except TypeError:
            payload = factor_returns.to_dict()
    else:
        payload = factor_returns
    if not isinstance(payload, dict):
        return [], []
    names = [str(name) for name in payload.keys()]
    columns = [_series(payload[name]) for name in payload.keys()]
    if not columns:
        return [], []
    rows = min(len(col) for col in columns)
    return names, [[col[i] for col in columns] for i in range(rows)]


def estimate_factor_contributions(
    portfolio_excess_returns: Any,
    factor_returns: Any,
) -> dict[str, Any]:
    """Estimate linear factor beta and contributions without statsmodels."""

    warnings: list[str] = []
    y = _series(portfolio_excess_returns)
    names, x_rows = _factor_matrix(factor_returns)
    rows = min(len(y), len(x_rows))
    if rows == 0 or not names:
        return {
            "available": False,
            "method": "linear",
            "factor_count": 0,
            "factors": [],
            "residual_alpha": None,
            "warnings": ["missing_factor_inputs"],
            "disclaimer": DISCLAIMER,
        }
    y = y[:rows]
    x_rows = x_rows[:rows]
    total = sum(y) / rows
    betas = []
    mean_factors = []
    for col in range(len(names)):
        xs = [row[col] for row in x_rows]
        mx = sum(xs) / rows
        my = total
        var = sum((item - mx) ** 2 for item in xs)
        cov = sum((xs[i] - mx) * (y[i] - my) for i in range(rows))
        beta = cov / var if var else 0.0
        betas.append(beta)
        mean_factors.append(mx)
    alpha = total - sum(betas[i] * mean_factors[i] for i in range(len(names)))
    contributions = [betas[i] * mean_factors[i] for i in range(len(names))]
    explained = sum(contributions)
    denominator = abs(total) if abs(total) > 1e-12 else 1.0
    factors = []
    for idx, name in enumerate(names):
        contribution = contributions[idx]
        factors.append({
            "factor": name,
            "beta": round(betas[idx], 10),
            "mean_factor_return": round(mean_factors[idx], 10),
            "contribution": round(contribution, 10),
            "contribution_pct": round(contribution / denominator, 10),
        })
    return _json_safe({
        "available": True,
        "method": "linear",
        "factor_count": len(names),
        "observation_count": rows,
        "factors": factors,
        "total_mean_excess_return": round(total, 10),
        "explained_by_factors": round(explained, 10),
        "residual_alpha": round(alpha, 10),
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    })


def factor_attribution(
    portfolio_returns: Any,
    factor_exposures: Any,
    factor_returns: Any,
    *,
    method: str = "linear",
) -> dict[str, Any]:
    if method != "linear":
        return {
            "available": False,
            "method": method,
            "factor_count": 0,
            "factors": [],
            "residual_alpha": None,
            "warnings": [f"unsupported_factor_attribution_method:{method}"],
            "disclaimer": DISCLAIMER,
        }
    if factor_exposures is not None and isinstance(factor_exposures, dict) and factor_returns is not None:
        weighted_returns: dict[str, list[float]] = {}
        names, returns_rows = _factor_matrix(factor_returns)
        for name in names:
            exposure = _safe_float(factor_exposures.get(name), 1.0)
            source = factor_returns[name] if isinstance(factor_returns, dict) else []
            weighted_returns[name] = [exposure * item for item in _series(source)]
        return estimate_factor_contributions(portfolio_returns, weighted_returns)
    return estimate_factor_contributions(portfolio_returns, factor_returns)
