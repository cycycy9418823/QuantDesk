"""Official limit-price / suspension metadata for backtest fidelity (Round 21).

The backtest limit-up/limit-down checks historically used a pct-change
heuristic. This module loads the official tables

- ``data/metadata/limit_price_history.csv`` (trade_date, symbol, pre_close,
  up_limit, down_limit, source, provider)
- ``data/metadata/suspension_history.csv`` (trade_date, symbol,
  suspend_timing, suspend_type, source, provider)

and attaches them onto a per-symbol daily frame as ``official_up_limit``,
``official_down_limit``, ``official_suspended`` and a per-row
``limit_price_source`` provenance column (``official`` / ``heuristic_fallback``).

Rows without official coverage fall back to the existing heuristic and are
annotated as such — official data is never fabricated for missing dates.

Research only; no network, no broker, no real orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd

from quantdesk.shared import normalize_symbol

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_METADATA_DIR = PROJECT_ROOT / "data" / "metadata"
LIMIT_PRICE_HISTORY_CSV = "limit_price_history.csv"
SUSPENSION_HISTORY_CSV = "suspension_history.csv"

LIMIT_SOURCE_OFFICIAL = "official"
LIMIT_SOURCE_HEURISTIC = "heuristic_fallback"

_CHUNK_SIZE = 500_000


@dataclass
class OfficialLimitMetadata:
    """Filtered official metadata for a bounded symbol set."""

    limit_prices: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    suspensions: set[tuple[str, str]] = field(default_factory=set)
    symbols_with_limit_data: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _normalize_date(value: object) -> str:
    return str(value or "")[:10]


def load_official_limit_metadata(
    symbols: Iterable[object],
    *,
    metadata_dir: Path | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> OfficialLimitMetadata:
    """One chunked pass over each table, filtered by symbol set and date range."""

    base = Path(metadata_dir) if metadata_dir else DEFAULT_METADATA_DIR
    wanted = {normalize_symbol(item) for item in symbols if normalize_symbol(item)}
    result = OfficialLimitMetadata()
    if not wanted:
        result.warnings.append("official_limit_metadata_no_symbols_requested")
        return result

    limit_path = base / LIMIT_PRICE_HISTORY_CSV
    if limit_path.exists():
        try:
            for chunk in pd.read_csv(
                limit_path,
                usecols=["trade_date", "symbol", "up_limit", "down_limit"],
                dtype={"symbol": str},
                chunksize=_CHUNK_SIZE,
                encoding="utf-8-sig",
            ):
                chunk["symbol"] = chunk["symbol"].map(normalize_symbol)
                chunk = chunk[chunk["symbol"].isin(wanted)]
                if start_date:
                    chunk = chunk[chunk["trade_date"] >= start_date]
                if end_date:
                    chunk = chunk[chunk["trade_date"] <= end_date]
                for row in chunk.itertuples(index=False):
                    up = pd.to_numeric(row.up_limit, errors="coerce")
                    down = pd.to_numeric(row.down_limit, errors="coerce")
                    if pd.isna(up) or pd.isna(down):
                        continue
                    symbol = str(row.symbol)
                    result.limit_prices[(symbol, _normalize_date(row.trade_date))] = (
                        float(up),
                        float(down),
                    )
                    result.symbols_with_limit_data.add(symbol)
        except Exception as exc:  # noqa: BLE001 - degraded, never fabricated
            result.warnings.append(f"official_limit_price_load_failed:{limit_path}:{exc}")
    else:
        result.warnings.append(f"official_limit_price_file_missing:{limit_path}")

    suspension_path = base / SUSPENSION_HISTORY_CSV
    if suspension_path.exists():
        try:
            for chunk in pd.read_csv(
                suspension_path,
                usecols=["trade_date", "symbol", "suspend_type"],
                dtype={"symbol": str},
                chunksize=_CHUNK_SIZE,
                encoding="utf-8-sig",
            ):
                chunk["symbol"] = chunk["symbol"].map(normalize_symbol)
                chunk = chunk[chunk["symbol"].isin(wanted)]
                if start_date:
                    chunk = chunk[chunk["trade_date"] >= start_date]
                if end_date:
                    chunk = chunk[chunk["trade_date"] <= end_date]
                suspend_rows = chunk[
                    chunk["suspend_type"].astype(str).str.upper().str.startswith("S")
                ]
                for row in suspend_rows.itertuples(index=False):
                    result.suspensions.add((str(row.symbol), _normalize_date(row.trade_date)))
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"official_suspension_load_failed:{suspension_path}:{exc}")
    else:
        result.warnings.append(f"official_suspension_file_missing:{suspension_path}")
    return result


def attach_official_limit_metadata(
    df: pd.DataFrame,
    symbol: object,
    metadata: OfficialLimitMetadata,
    *,
    date_column: str = "date",
) -> pd.DataFrame:
    """Attach official columns onto a daily frame; unmatched rows stay heuristic."""

    frame = df.copy()
    normalized = normalize_symbol(symbol)
    if frame.empty or date_column not in frame.columns:
        return frame
    dates = pd.to_datetime(frame[date_column], errors="coerce").dt.strftime("%Y-%m-%d")
    up_values: list[float] = []
    down_values: list[float] = []
    suspended: list[bool] = []
    source: list[str] = []
    for day in dates.fillna(""):
        limits = metadata.limit_prices.get((normalized, day))
        if limits is not None:
            up_values.append(limits[0])
            down_values.append(limits[1])
            source.append(LIMIT_SOURCE_OFFICIAL)
        else:
            up_values.append(float("nan"))
            down_values.append(float("nan"))
            source.append(LIMIT_SOURCE_HEURISTIC)
        suspended.append((normalized, day) in metadata.suspensions)
    frame["official_up_limit"] = up_values
    frame["official_down_limit"] = down_values
    frame["official_suspended"] = suspended
    frame["limit_price_source"] = source
    return frame


__all__ = [
    "LIMIT_SOURCE_HEURISTIC",
    "LIMIT_SOURCE_OFFICIAL",
    "OfficialLimitMetadata",
    "attach_official_limit_metadata",
    "load_official_limit_metadata",
]
