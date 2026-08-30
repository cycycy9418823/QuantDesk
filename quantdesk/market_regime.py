from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

import pandas as pd

from quantdesk.benchmark import benchmark_snapshot_to_dict, build_benchmark_snapshot, safe_float

MARKET_REGIME_DISCLAIMER = "市场环境层仅用于本地研究复核，不代表未来收益，也不构成投资建议。"


@dataclass
class MarketRegimeAssessment:
    market_regime: str
    market_regime_label: str
    index_name: str
    index_close: float | None
    index_ma20: float | None
    index_ma60: float | None
    index_vol20: float | None
    recent_drawdown_20d: float | None
    data_date: str
    explanation: str
    provider_status: str
    disclaimer: str


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in {None, ""}:
            return value
    return None


def _from_frame(frame: pd.DataFrame | None) -> tuple[float | None, float | None, float | None, float | None, str]:
    if frame is None or frame.empty:
        return None, None, None, None, ""
    normalized = frame.copy()
    if "date" in normalized.columns:
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
        normalized = normalized.dropna(subset=["date"]).sort_values("date")
    if "close" in normalized.columns:
        close_series = pd.to_numeric(normalized["close"], errors="coerce")
    else:
        close_series = pd.Series(dtype=float)
    close = safe_float(close_series.iloc[-1]) if not close_series.empty else None
    ma20 = safe_float(close_series.rolling(20, min_periods=1).mean().iloc[-1]) if not close_series.empty else None
    ma60 = safe_float(close_series.rolling(60, min_periods=1).mean().iloc[-1]) if not close_series.empty else None
    vol20 = None
    if "vol20" in normalized.columns:
        vol20 = safe_float(pd.to_numeric(normalized["vol20"], errors="coerce").iloc[-1])
    elif len(close_series) > 1:
        returns = close_series.ffill().pct_change(fill_method=None).dropna()
        if not returns.empty:
            vol20 = safe_float(returns.rolling(20, min_periods=1).std().iloc[-1])
    drawdown = None
    if not close_series.empty:
        drawdown_series = close_series / close_series.cummax() - 1
        drawdown = safe_float(drawdown_series.rolling(20, min_periods=1).min().iloc[-1])
    date = ""
    if "date" in normalized.columns and not normalized.empty:
        date = pd.to_datetime(normalized["date"].iloc[-1], errors="coerce").strftime("%Y-%m-%d")
    return close, ma20, ma60, vol20, drawdown, date


def assess_market_regime(
    index_data: Any | None = None,
    *,
    index_name: str = "沪深300",
    benchmark_symbol: str = "000300",
) -> MarketRegimeAssessment:
    benchmark_snapshot: dict[str, Any] = {}
    if isinstance(index_data, Mapping) and "benchmark_curve" in index_data:
        benchmark_snapshot = dict(index_data)
    elif isinstance(index_data, pd.DataFrame):
        close, ma20, ma60, vol20, drawdown, data_date = _from_frame(index_data)
        benchmark_snapshot = {
            "benchmark_name": index_name,
            "benchmark_symbol": benchmark_symbol,
            "benchmark_close": close,
            "benchmark_ma20": ma20,
            "benchmark_ma60": ma60,
            "benchmark_vol20": vol20,
            "benchmark_drawdown_20d": drawdown,
            "data_date": data_date,
            "provider_status": "local_frame_used" if close is not None else "provider_unavailable",
            "benchmark_curve": [],
        }
    elif isinstance(index_data, Mapping):
        benchmark_snapshot = dict(index_data)

    close = safe_float(_first_value(benchmark_snapshot, ("benchmark_close", "index_close", "close", "latest_close", "price", "current_price")))
    ma20 = safe_float(_first_value(benchmark_snapshot, ("benchmark_ma20", "index_ma20", "ma20", "ma_20", "moving_average_20", "technical_snapshot.ma20")))
    ma60 = safe_float(_first_value(benchmark_snapshot, ("benchmark_ma60", "index_ma60", "ma60", "moving_average_60", "technical_snapshot.ma60")))
    vol20 = safe_float(_first_value(benchmark_snapshot, ("benchmark_vol20", "index_vol20", "vol20", "technical_snapshot.vol20")))
    drawdown = safe_float(_first_value(benchmark_snapshot, ("benchmark_drawdown_20d", "recent_drawdown_20d", "drawdown_20d")))
    data_date = str(_first_value(benchmark_snapshot, ("data_date", "quote_date", "date", "latest_date")) or "")
    provider_status = str(benchmark_snapshot.get("provider_status") or "provider_unavailable")

    if close is None or ma20 is None or ma60 is None:
        regime = "market_unknown"
        label = "市场环境未知"
        explanation = "缺少指数收盘价或均线数据，暂时只能做环境未知提示。"
    elif close > ma20 and ma20 > ma60 and (vol20 is None or vol20 <= 0.08) and (drawdown is None or drawdown >= -0.05):
        regime = "market_strong"
        label = "市场偏强"
        explanation = f"{index_name} 站上 20 日与 60 日均线，且波动未明显放大，市场偏强。"
    elif close > ma20 and ma20 > ma60:
        regime = "market_supportive"
        label = "市场相对平稳"
        explanation = f"{index_name} 站上 20 日与 60 日均线，市场环境相对平稳。"
    elif (drawdown is not None and drawdown <= -0.08) or (close < ma20 and ma20 < ma60):
        regime = "market_correction"
        label = "市场回调中"
        explanation = f"{index_name} 最近 20 日回撤偏大或短中期均线结构转弱，市场处于回调观察阶段。"
    elif close < ma20 or (ma20 < ma60 and (vol20 is None or vol20 > 0.08)):
        regime = "market_pressure"
        label = "市场承压"
        explanation = f"{index_name} 收盘价弱于 20 日均线，或波动偏高，计划动作需要更严格人工确认。"
    else:
        regime = "market_supportive"
        label = "市场相对平稳"
        explanation = f"{index_name} 的指数结构尚可，适合继续观察。"

    return MarketRegimeAssessment(
        market_regime=regime,
        market_regime_label=label,
        index_name=index_name,
        index_close=close,
        index_ma20=ma20,
        index_ma60=ma60,
        index_vol20=vol20,
        recent_drawdown_20d=drawdown,
        data_date=data_date,
        explanation=explanation,
        provider_status=provider_status,
        disclaimer=MARKET_REGIME_DISCLAIMER,
    )


def market_regime_to_dict(assessment: MarketRegimeAssessment) -> dict[str, Any]:
    return asdict(assessment)


def market_regime_from_benchmark(
    source: Any | None = None,
    *,
    benchmark_name: str = "沪深300",
    benchmark_symbol: str = "000300",
) -> dict[str, Any]:
    snapshot = build_benchmark_snapshot(source, benchmark_name=benchmark_name, benchmark_symbol=benchmark_symbol)
    assessment = assess_market_regime(benchmark_snapshot_to_dict(snapshot), index_name=benchmark_name, benchmark_symbol=benchmark_symbol)
    result = market_regime_to_dict(assessment)
    result["benchmark"] = benchmark_snapshot_to_dict(snapshot)
    result["disclaimer"] = MARKET_REGIME_DISCLAIMER
    return result


__all__ = [
    "MARKET_REGIME_DISCLAIMER",
    "MarketRegimeAssessment",
    "assess_market_regime",
    "market_regime_from_benchmark",
    "market_regime_to_dict",
]
