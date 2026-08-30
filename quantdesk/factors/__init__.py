"""Basic factor framework for QuantDesk.

Provides price-volume and fundamental factor calculations for
local research reference only. No ML models, no complex scoring.
Does not constitute investment advice.
"""

from quantdesk.factors.base import Factor, FactorResult
from quantdesk.factors.fundamental import FundamentalFactor
from quantdesk.factors.liquidity import compute_liquidity_factors
from quantdesk.factors.momentum import compute_momentum_factors
from quantdesk.factors.pipeline import (
    compute_rank_ic,
    cross_sectional_zscore,
    ic_summary,
    neutralize_by_industry,
    standardize_factors,
    winsorize_series,
)
from quantdesk.factors.price_volume import PriceVolumeFactor
from quantdesk.factors.scoring import compute_factor_summary
from quantdesk.factors.volatility import compute_volatility_factors

__all__ = [
    "Factor",
    "FactorResult",
    "PriceVolumeFactor",
    "FundamentalFactor",
    "compute_factor_summary",
    "compute_momentum_factors",
    "compute_volatility_factors",
    "compute_liquidity_factors",
    "winsorize_series",
    "cross_sectional_zscore",
    "neutralize_by_industry",
    "standardize_factors",
    "compute_rank_ic",
    "ic_summary",
]
