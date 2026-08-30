"""Price-volume factor calculations.

All factors are for local research reference only and do not
constitute investment advice.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from quantdesk.factors.base import Factor, FactorResult


class MomentumFactor(Factor):
    name = "momentum_20d"
    category = "price_volume"
    description = "20日价格动量（最近收盘/20日前收盘 - 1）"

    def compute(self, df: pd.DataFrame, **kwargs: Any) -> FactorResult:
        close = df.get("close")
        if close is None or len(close) < 20:
            return FactorResult(name=self.name, available=False, reason="收盘价不足20行")
        close = pd.to_numeric(close, errors="coerce")
        current = close.iloc[-1]
        past = close.iloc[-20]
        if pd.isna(current) or pd.isna(past) or past <= 0:
            return FactorResult(name=self.name, available=False, reason="收盘价含缺失值")
        value = (current / past) - 1.0
        return FactorResult(name=self.name, value=round(value, 6), available=True, unit="ratio")


class VolatilityFactor(Factor):
    name = "volatility_20d"
    category = "price_volume"
    description = "20日收益率标准差"

    def compute(self, df: pd.DataFrame, **kwargs: Any) -> FactorResult:
        close = df.get("close")
        if close is None or len(close) < 21:
            return FactorResult(name=self.name, available=False, reason="收盘价不足21行")
        close = pd.to_numeric(close, errors="coerce")
        returns = close.ffill().pct_change(fill_method=None).dropna()
        if len(returns) < 20:
            return FactorResult(name=self.name, available=False, reason="收益率样本不足")
        value = float(returns.tail(20).std())
        return FactorResult(name=self.name, value=round(value, 6), available=True, unit="std")


class TurnoverFactor(Factor):
    name = "avg_turnover_20d"
    category = "price_volume"
    description = "20日平均换手率"

    def compute(self, df: pd.DataFrame, **kwargs: Any) -> FactorResult:
        turnover = df.get("turnover")
        if turnover is None or len(turnover) < 20:
            return FactorResult(name=self.name, available=False, reason="换手率数据不足20行")
        turnover = pd.to_numeric(turnover, errors="coerce")
        tail = turnover.tail(20).dropna()
        if len(tail) < 10:
            return FactorResult(name=self.name, available=False, reason="换手率缺失过多")
        value = float(tail.mean())
        return FactorResult(name=self.name, value=round(value, 6), available=True, unit="pct")


class VolumeRatioFactor(Factor):
    name = "volume_ratio_5_20"
    category = "price_volume"
    description = "5日成交量均值 / 20日成交量均值"

    def compute(self, df: pd.DataFrame, **kwargs: Any) -> FactorResult:
        volume = df.get("volume")
        if volume is None or len(volume) < 20:
            return FactorResult(name=self.name, available=False, reason="成交量数据不足20行")
        volume = pd.to_numeric(volume, errors="coerce")
        ma5 = volume.tail(5).mean()
        ma20 = volume.tail(20).mean()
        if pd.isna(ma5) or pd.isna(ma20) or ma20 <= 0:
            return FactorResult(name=self.name, available=False, reason="成交量均值无效")
        value = float(ma5 / ma20)
        return FactorResult(name=self.name, value=round(value, 4), available=True, unit="ratio")


class AmplitudeFactor(Factor):
    name = "avg_amplitude_20d"
    category = "price_volume"
    description = "20日平均振幅（(high-low)/close）"

    def compute(self, df: pd.DataFrame, **kwargs: Any) -> FactorResult:
        high = df.get("high")
        low = df.get("low")
        close = df.get("close")
        if high is None or low is None or close is None or len(df) < 20:
            return FactorResult(name=self.name, available=False, reason="缺少 high/low/close 或数据不足")
        high = pd.to_numeric(high, errors="coerce")
        low = pd.to_numeric(low, errors="coerce")
        close = pd.to_numeric(close, errors="coerce")
        amplitude = ((high - low) / close.replace(0, pd.NA)).tail(20).dropna()
        if len(amplitude) < 10:
            return FactorResult(name=self.name, available=False, reason="振幅样本不足")
        value = float(amplitude.mean())
        return FactorResult(name=self.name, value=round(value, 6), available=True, unit="ratio")


class PriceVolumeFactor:
    """Convenience class that runs all price-volume factors at once."""

    _factors = [
        MomentumFactor(),
        VolatilityFactor(),
        TurnoverFactor(),
        VolumeRatioFactor(),
        AmplitudeFactor(),
    ]

    @classmethod
    def compute_all(cls, df: pd.DataFrame) -> list[FactorResult]:
        return [f.compute(df) for f in cls._factors]
