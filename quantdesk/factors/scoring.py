"""Factor scoring utilities for QuantDesk.

No ML or complex scoring — just a simple summary of computed factors.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quantdesk.factors.fundamental import FundamentalFactor
from quantdesk.factors.price_volume import PriceVolumeFactor


def compute_factor_summary(df: pd.DataFrame) -> dict[str, Any]:
    """Compute all available factors and return a summary dict."""
    pv_results = PriceVolumeFactor.compute_all(df)
    fund_results = FundamentalFactor.compute_all(df)
    all_results = pv_results + fund_results
    available_count = sum(1 for r in all_results if r.available)
    missing_count = sum(1 for r in all_results if not r.available)
    factors_dict = {r.name: r.to_dict() for r in all_results}
    return {
        "factors": factors_dict,
        "available_count": available_count,
        "missing_count": missing_count,
        "total_count": len(all_results),
        "missing_reasons": {r.name: r.reason for r in all_results if not r.available},
        "disclaimer": "因子值仅用于本地研究参考，不构成投资建议。",
    }
