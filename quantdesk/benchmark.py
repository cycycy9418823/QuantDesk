from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from quantdesk.backtest import benchmark_total_return, load_benchmark_series
from quantdesk.shared import safe_float

_log = logging.getLogger(__name__)

BENCHMARK_DISCLAIMER = "基准对比仅用于历史研究，不代表未来收益，也不下载网络数据。"


@dataclass
class BenchmarkSnapshot:
    benchmark_name: str
    benchmark_symbol: str
    benchmark_return: float | None
    benchmark_curve: list[dict[str, Any]]
    provider_status: str
    data_date: str
    benchmark_close: float | None
    benchmark_ma60: float | None
    source_path: str
    disclaimer: str


def _as_frame(source: Any | None) -> pd.DataFrame:
    if source is None:
        return pd.DataFrame()
    if isinstance(source, pd.DataFrame):
        return source.copy()
    if isinstance(source, pd.Series):
        return source.to_frame().T
    if isinstance(source, Mapping):
        return pd.DataFrame([dict(source)])
    if isinstance(source, list):
        rows = [row for row in source if isinstance(row, Mapping)]
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    return pd.DataFrame()


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    columns = {column.lower(): column for column in frame.columns}
    date_column = next((columns.get(name) for name in ("date", "trade_date", "datetime", "time")), None)
    close_column = next((columns.get(name) for name in ("close", "benchmark_close", "index_close", "price")), None)
    if close_column is None and "close" not in frame.columns:
        return pd.DataFrame()
    normalized = frame.copy()
    if date_column and date_column in normalized.columns:
        normalized[date_column] = pd.to_datetime(normalized[date_column], errors="coerce")
        normalized = normalized.dropna(subset=[date_column])
        normalized = normalized.sort_values(date_column)
    if close_column and close_column != "close":
        normalized["close"] = pd.to_numeric(normalized[close_column], errors="coerce")
    else:
        normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
    normalized = normalized.dropna(subset=["close"])
    if normalized.empty:
        return pd.DataFrame()
    if date_column:
        normalized["date"] = pd.to_datetime(normalized[date_column], errors="coerce").dt.strftime("%Y-%m-%d")
    elif "date" not in normalized.columns:
        normalized["date"] = ["" for _ in range(len(normalized))]
    normalized["benchmark_ma60"] = normalized["close"].rolling(60, min_periods=1).mean()
    first_close = float(normalized["close"].iloc[0])
    normalized["normalized_close"] = normalized["close"] / first_close if first_close else normalized["close"]
    normalized["return_pct"] = normalized["normalized_close"] - 1
    return normalized


def load_local_benchmark_frame(path: str | Path | None = None, source: Any | None = None) -> pd.DataFrame:
    """Load a local benchmark series without making any network request."""

    if source is not None:
        return _normalize_frame(_as_frame(source))
    if path is None:
        _log.warning("load_local_benchmark_frame: no path provided, returning empty frame")
        return pd.DataFrame()
    target = Path(path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parents[1] / target
    if not target.exists():
        _log.warning("load_local_benchmark_frame: file not found: %s", target)
        return pd.DataFrame()
    suffix = target.suffix.lower()
    if suffix == ".csv":
        series = load_benchmark_series(target)
        if series.empty:
            _log.warning("load_local_benchmark_frame: empty CSV series: %s", target)
            return pd.DataFrame()
        frame = series.rename("close").to_frame().reset_index()
        frame.columns = ["date", "close"]
        return _normalize_frame(frame)
    try:
        frame = pd.read_parquet(target)
    except Exception as exc:
        _log.warning("load_local_benchmark_frame: parquet read failed: %s: %s", target, exc)
        return pd.DataFrame()
    return _normalize_frame(frame)


def build_benchmark_snapshot(
    source: Any | None = None,
    *,
    path: str | Path | None = None,
    benchmark_name: str = "沪深300",
    benchmark_symbol: str = "000300",
) -> BenchmarkSnapshot:
    frame = load_local_benchmark_frame(path, source)
    source_path = str(path or "")
    if frame.empty:
        provider_status = "provider_unavailable" if source is None and path is None else "local_cache_missing"
        return BenchmarkSnapshot(
            benchmark_name=benchmark_name,
            benchmark_symbol=benchmark_symbol,
            benchmark_return=None,
            benchmark_curve=[],
            provider_status=provider_status,
            data_date="",
            benchmark_close=None,
            benchmark_ma60=None,
            source_path=source_path,
            disclaimer=BENCHMARK_DISCLAIMER,
        )

    benchmark_return = benchmark_total_return(frame["close"])
    latest = frame.iloc[-1]
    curve = [
        {
            "date": str(row.get("date") or ""),
            "close": safe_float(row.get("close")),
            "benchmark_ma60": safe_float(row.get("benchmark_ma60")),
            "normalized_close": safe_float(row.get("normalized_close")),
            "return_pct": safe_float(row.get("return_pct")),
        }
        for _, row in frame.tail(120).iterrows()
    ]
    return BenchmarkSnapshot(
        benchmark_name=benchmark_name,
        benchmark_symbol=benchmark_symbol,
        benchmark_return=benchmark_return if benchmark_return == benchmark_return else None,
        benchmark_curve=curve,
        provider_status="local_cache_used" if source is None and path else "local_frame_used",
        data_date=str(latest.get("date") or ""),
        benchmark_close=safe_float(latest.get("close")),
        benchmark_ma60=safe_float(latest.get("benchmark_ma60")),
        source_path=source_path,
        disclaimer=BENCHMARK_DISCLAIMER,
    )


def benchmark_snapshot_to_dict(snapshot: BenchmarkSnapshot) -> dict[str, Any]:
    return asdict(snapshot)


__all__ = [
    "BENCHMARK_DISCLAIMER",
    "BenchmarkSnapshot",
    "benchmark_snapshot_to_dict",
    "build_benchmark_snapshot",
    "load_local_benchmark_frame",
]
