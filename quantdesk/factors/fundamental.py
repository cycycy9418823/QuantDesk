"""Fundamental factors backed by local SQLite fundamentals when available.

The factor framework remains research-only and offline by default. These
classes never fetch network data; callers may pass a symbol and an initialized
``QuantDeskDB`` instance to read the latest locally stored fundamentals.
Legacy DataFrame-column fallback is preserved for old screens and tests.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from quantdesk.data.fundamental_collector import get_latest_fundamental
from quantdesk.factors.base import Factor, FactorResult


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _symbol_from_context(df: pd.DataFrame, kwargs: dict[str, Any]) -> str:
    symbol = str(kwargs.get("symbol") or df.attrs.get("symbol") or "").strip()
    if symbol:
        return symbol
    if "symbol" in df.columns and not df.empty:
        value = df["symbol"].dropna()
        if not value.empty:
            return str(value.iloc[-1]).strip()
    return ""


def _latest_fundamental_row(df: pd.DataFrame, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    symbol = _symbol_from_context(df, kwargs)
    if not symbol:
        return None
    db = kwargs.get("db")
    return get_latest_fundamental(symbol, db=db)


class _FundamentalScalarFactor(Factor):
    column_name: str = ""
    sqlite_name: str = ""
    unit: str = ""

    def _from_sqlite(self, df: pd.DataFrame, kwargs: dict[str, Any]) -> FactorResult | None:
        row = _latest_fundamental_row(df, kwargs)
        if not row:
            return None
        value = _finite_float(row.get(self.sqlite_name))
        if value is None:
            return FactorResult(name=self.name, available=False, reason=f"local fundamentals missing {self.sqlite_name}")
        return FactorResult(name=self.name, value=round(value, 4), available=True, unit=self.unit)

    def _from_dataframe(self, df: pd.DataFrame) -> FactorResult:
        if self.column_name not in df.columns:
            return FactorResult(name=self.name, available=False, reason=f"local data missing {self.column_name} column")
        series = pd.to_numeric(df[self.column_name], errors="coerce").dropna()
        if series.empty:
            return FactorResult(name=self.name, available=False, reason=f"{self.column_name} is empty")
        value = _finite_float(series.iloc[-1])
        if value is None:
            return FactorResult(name=self.name, available=False, reason=f"{self.column_name} is not finite")
        return FactorResult(name=self.name, value=round(value, 4), available=True, unit=self.unit)

    def compute(self, df: pd.DataFrame, **kwargs: Any) -> FactorResult:
        sqlite_result = self._from_sqlite(df, kwargs)
        if sqlite_result is not None:
            return sqlite_result
        return self._from_dataframe(df)


class PEFactor(_FundamentalScalarFactor):
    name = "pe_ttm"
    category = "fundamental"
    description = "Trailing PE from local fundamentals or legacy pe_ttm column."
    column_name = "pe_ttm"
    sqlite_name = "pe"
    unit = "x"


class PBFactor(_FundamentalScalarFactor):
    name = "pb"
    category = "fundamental"
    description = "Price-to-book from local fundamentals or legacy pb column."
    column_name = "pb"
    sqlite_name = "pb"
    unit = "x"


class PSFactor(_FundamentalScalarFactor):
    name = "ps_ttm"
    category = "fundamental"
    description = "Price-to-sales from local fundamentals or legacy ps_ttm column."
    column_name = "ps_ttm"
    sqlite_name = "ps"
    unit = "x"


class ROEFactor(_FundamentalScalarFactor):
    name = "roe"
    category = "fundamental"
    description = "Return on equity from local fundamentals."
    column_name = "roe"
    sqlite_name = "roe"
    unit = "pct"


class ROAFactor(_FundamentalScalarFactor):
    name = "roa"
    category = "fundamental"
    description = "Return on assets from local fundamentals."
    column_name = "roa"
    sqlite_name = "roa"
    unit = "pct"


class GrossMarginFactor(_FundamentalScalarFactor):
    name = "gross_profit_margin"
    category = "fundamental"
    description = "Gross margin from local fundamentals."
    column_name = "gross_profit_margin"
    sqlite_name = "gross_margin"
    unit = "pct"


class NetMarginFactor(_FundamentalScalarFactor):
    name = "net_profit_margin"
    category = "fundamental"
    description = "Net margin from local fundamentals."
    column_name = "net_profit_margin"
    sqlite_name = "net_margin"
    unit = "pct"


class DebtRatioFactor(_FundamentalScalarFactor):
    name = "debt_to_assets"
    category = "fundamental"
    description = "Debt ratio from local fundamentals."
    column_name = "debt_to_assets"
    sqlite_name = "debt_ratio"
    unit = "pct"


class RevenueYoYFactor(_FundamentalScalarFactor):
    name = "revenue_yoy"
    category = "fundamental"
    description = "Revenue growth from local fundamentals."
    column_name = "revenue_yoy"
    sqlite_name = "revenue_yoy"
    unit = "pct"


class ProfitYoYFactor(_FundamentalScalarFactor):
    name = "profit_yoy"
    category = "fundamental"
    description = "Net profit growth from local fundamentals."
    column_name = "profit_yoy"
    sqlite_name = "net_profit_yoy"
    unit = "pct"


class DividendYieldFactor(_FundamentalScalarFactor):
    name = "dividend_yield"
    category = "fundamental"
    description = "Dividend yield from legacy local column when available."
    column_name = "dividend_yield"
    sqlite_name = "dividend_yield"
    unit = "pct"


class FundamentalFactor:
    """Convenience class that runs local fundamental factors.

    ``compute_all`` keeps the historical three-factor default for compatibility.
    Pass ``include_extended=True`` to evaluate the v3.8 local SQLite-backed
    expansion.
    """

    _core_factors = [
        PEFactor(),
        PBFactor(),
        DividendYieldFactor(),
    ]
    _extended_factors = [
        PSFactor(),
        ROEFactor(),
        ROAFactor(),
        GrossMarginFactor(),
        NetMarginFactor(),
        DebtRatioFactor(),
        RevenueYoYFactor(),
        ProfitYoYFactor(),
    ]

    @classmethod
    def compute_all(cls, df: pd.DataFrame, *, include_extended: bool = False, **kwargs: Any) -> list[FactorResult]:
        factors = [*cls._core_factors, *(cls._extended_factors if include_extended else [])]
        return [factor.compute(df, **kwargs) for factor in factors]
