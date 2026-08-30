"""Low-price learning universe built from a local spot snapshot.

v2.6.8 Low Price Snapshot Universe.  This module is intentionally
small: it only filters an already-cached spot snapshot
(``data/cache/spot_snapshot_latest.csv``) into a derivative learning
pool of symbols whose latest price is below a configurable threshold.

It does **not**:

- connect to any network provider (no AKShare / Sina / Eastmoney call);
- read or scan ``data/daily/*.csv``;
- depend on ``quantdesk/daily_cache_index.py``;
- modify the spot cache, the daily report flow, or
  ``scripts/update_market_data.py``;
- generate trading instructions or predictive language.

The output file ``data/universe/low_price_learning_symbols.csv`` is a
**derivative artifact**: it is safe to delete and rebuild at any time
and is excluded from the privacy-clean export zip via the existing
runtime-data exclusion rules.

Output columns (UTF-8 with BOM for Excel compatibility):

- ``symbol``: 6-digit A-share code (no ``sh`` / ``sz`` prefix).
- ``name``: stock name as reported by the snapshot.
- ``price``: latest snapshot price as ``float``; ``NaN`` if invalid.
- ``amount``: latest turnover (元) as ``float``; ``NaN`` if missing.
- ``included``: ``True`` when ``price <= max_price`` and the row is
  not excluded by name / amount / invalid-price rules.
- ``in_buffer``: ``True`` when ``max_price < price <= buffer_price``
  and the row is not excluded by other rules.  Mutually exclusive
  with ``included``.
- ``excluded_reason``: one of ``""``, ``"invalid_price"``,
  ``"excluded_name"``, ``"low_amount"``, ``"above_buffer"``.
- ``source``: provider tag from the snapshot (e.g. ``sina_spot``).
- ``updated_at``: ISO timestamp of the build.

The module never raises on a missing or empty input; in that case it
returns an empty DataFrame with the canonical columns.
"""

from __future__ import annotations

import math
from functools import partial
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.time_utils import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOW_PRICE_UNIVERSE_PATH = PROJECT_ROOT / "data" / "universe" / "low_price_learning_symbols.csv"

DEFAULT_MAX_PRICE = 20.0
DEFAULT_BUFFER_PRICE = 22.0
DEFAULT_MIN_AMOUNT = 0.0

# Output column order — kept stable so frontends and tests can rely on it.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "symbol",
    "name",
    "price",
    "amount",
    "included",
    "in_buffer",
    "excluded_reason",
    "source",
    "updated_at",
)

# Excluded-name keywords (case-insensitive substring match).
_EXCLUDED_NAME_TOKENS: tuple[str, ...] = ("ST", "退")

# Column-name aliases tolerated on the input frame.  Multiple snapshot
# providers exist (Sina / Eastmoney / cached fallback), and historical
# code paths sometimes emit the English name.
_SYMBOL_ALIASES = ("代码", "symbol", "code", "ts_code", "代码原始")
_NAME_ALIASES = ("名称", "name", "stock_name")
_PRICE_ALIASES = ("最新价", "price", "latest_price", "latest_close", "close", "current_price")
_AMOUNT_ALIASES = ("成交额", "amount", "amount_yuan", "turnover")
_SOURCE_ALIASES = ("数据源", "source", "provider", "spot_source")


def low_price_universe_path() -> Path:
    """Return the canonical low-price learning universe CSV path."""

    return DEFAULT_LOW_PRICE_UNIVERSE_PATH


_now_text = partial(now_text, sep=" ")


def _pick_column(frame: pd.DataFrame, aliases: Iterable[str]) -> str | None:
    for name in aliases:
        if name in frame.columns:
            return name
    return None


def _normalize_symbol(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    # Strip common exchange prefixes so ``sh601868`` and ``601868`` collide.
    lower = text.lower()
    for prefix in ("sh", "sz", "bj"):
        if lower.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    # Strip Tushare-style suffixes (``601868.SH``).
    if "." in text:
        text = text.split(".", 1)[0]
    return text.strip()


def _to_float_or_nan(value: Any) -> float:
    if value is None:
        return float("nan")
    if isinstance(value, float) and math.isnan(value):
        return float("nan")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    if math.isinf(result):
        return float("nan")
    return result


def _name_is_excluded(name: str) -> bool:
    if not name:
        return False
    upper = name.upper()
    for token in _EXCLUDED_NAME_TOKENS:
        if token.upper() in upper:
            return True
    return False


def _empty_universe_frame() -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype=_dtype_for(column)) for column in OUTPUT_COLUMNS})


def _dtype_for(column: str) -> str:
    if column in ("price", "amount"):
        return "float64"
    if column in ("included", "in_buffer"):
        return "bool"
    return "object"


def build_low_price_universe_from_spot(
    spot_frame: pd.DataFrame | None,
    max_price: float = DEFAULT_MAX_PRICE,
    buffer_price: float = DEFAULT_BUFFER_PRICE,
    min_amount: float = DEFAULT_MIN_AMOUNT,
    *,
    updated_at: str | None = None,
) -> pd.DataFrame:
    """Filter a spot snapshot frame into the low-price learning universe.

    The function never reads from disk or the network — every input
    must already be in memory.  It is safe to call on an empty or
    malformed frame; the result is then an empty canonical frame.
    """

    max_price = float(max_price)
    buffer_price = float(buffer_price) if buffer_price is not None else max_price
    if buffer_price < max_price:
        # Defensive: a buffer below max_price would make rule 2 unreachable.
        buffer_price = max_price
    min_amount = float(min_amount or 0.0)

    if spot_frame is None or not isinstance(spot_frame, pd.DataFrame) or spot_frame.empty:
        return _empty_universe_frame()

    symbol_col = _pick_column(spot_frame, _SYMBOL_ALIASES)
    name_col = _pick_column(spot_frame, _NAME_ALIASES)
    price_col = _pick_column(spot_frame, _PRICE_ALIASES)
    amount_col = _pick_column(spot_frame, _AMOUNT_ALIASES)
    source_col = _pick_column(spot_frame, _SOURCE_ALIASES)

    timestamp = updated_at or _now_text()

    rows: list[dict[str, Any]] = []
    for _, row in spot_frame.iterrows():
        symbol = _normalize_symbol(row.get(symbol_col)) if symbol_col else ""
        if not symbol:
            continue
        name = "" if name_col is None else str(row.get(name_col) or "").strip()
        price = _to_float_or_nan(row.get(price_col)) if price_col else float("nan")
        amount = _to_float_or_nan(row.get(amount_col)) if amount_col else float("nan")
        source = "" if source_col is None else str(row.get(source_col) or "").strip()

        included = False
        in_buffer = False
        excluded_reason = ""

        # Order of precedence — locked in so unit tests can assert it.
        if math.isnan(price) or price <= 0:
            excluded_reason = "invalid_price"
        elif _name_is_excluded(name):
            excluded_reason = "excluded_name"
        elif min_amount > 0 and (math.isnan(amount) or amount < min_amount):
            excluded_reason = "low_amount"
        elif price <= max_price:
            included = True
        elif price <= buffer_price:
            in_buffer = True
        else:
            excluded_reason = "above_buffer"

        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "price": price,
                "amount": amount,
                "included": bool(included),
                "in_buffer": bool(in_buffer),
                "excluded_reason": excluded_reason,
                "source": source,
                "updated_at": timestamp,
            }
        )

    if not rows:
        return _empty_universe_frame()

    frame = pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS))
    # Force numeric dtype on price / amount so downstream consumers do not
    # accidentally see object dtype for an all-NaN column.
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce").astype("float64")
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").astype("float64")
    frame["included"] = frame["included"].astype(bool)
    frame["in_buffer"] = frame["in_buffer"].astype(bool)
    # Mutual-exclusion safety net (rules 6 and 7).  If a future change
    # accidentally sets both, we always keep ``included`` as the
    # primary classification and clear ``in_buffer``.
    both_mask = frame["included"] & frame["in_buffer"]
    if bool(both_mask.any()):
        frame.loc[both_mask, "in_buffer"] = False
    return frame


def save_low_price_universe(frame: pd.DataFrame, path: str | Path | None = None) -> Path:
    """Atomically write the universe frame to ``path`` (UTF-8 BOM)."""

    target = Path(path) if path else low_price_universe_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if frame is None or not isinstance(frame, pd.DataFrame):
        frame = _empty_universe_frame()
    # Ensure stable column order even if caller built a custom frame.
    ordered_columns = [c for c in OUTPUT_COLUMNS if c in frame.columns]
    extra_columns = [c for c in frame.columns if c not in OUTPUT_COLUMNS]
    output = frame.reindex(columns=ordered_columns + extra_columns)
    temp = target.with_name(target.name + ".tmp")
    output.to_csv(temp, index=False, encoding="utf-8-sig")
    import os
    os.replace(temp, target)
    return target


def load_low_price_universe(path: str | Path | None = None) -> pd.DataFrame:
    """Load a previously saved low-price universe CSV.

    Returns an empty canonical frame when the file does not exist or
    is unreadable, so callers can blindly chain ``summarize_…``.
    """

    target = Path(path) if path else low_price_universe_path()
    if not target.exists():
        return _empty_universe_frame()
    frame = read_csv_frame(target, encoding="utf-8-sig", dtype={"symbol": str})
    csv_read_warnings = list(frame.attrs.get("warnings") or [])
    if any(item.startswith("csv_read_failed:") for item in csv_read_warnings):
        empty = _empty_universe_frame()
        empty.attrs["warnings"] = csv_read_warnings
        return empty
    for column in OUTPUT_COLUMNS:
        if column not in frame.columns:
            if column in ("price", "amount"):
                frame[column] = float("nan")
            elif column in ("included", "in_buffer"):
                frame[column] = False
            else:
                frame[column] = ""
    if "price" in frame.columns:
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce").astype("float64")
    if "amount" in frame.columns:
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").astype("float64")
    if "included" in frame.columns:
        frame["included"] = frame["included"].astype(bool) if frame["included"].dtype == bool else _to_bool_series(frame["included"])
    if "in_buffer" in frame.columns:
        frame["in_buffer"] = frame["in_buffer"].astype(bool) if frame["in_buffer"].dtype == bool else _to_bool_series(frame["in_buffer"])
    frame.attrs["warnings"] = csv_read_warnings
    return frame


def _to_bool_series(series: pd.Series) -> pd.Series:
    truthy = {"true", "1", "yes", "y", "t"}
    return series.astype(str).str.strip().str.lower().isin(truthy)


def summarize_low_price_universe(
    frame: pd.DataFrame,
    *,
    max_price: float | None = None,
    buffer_price: float | None = None,
    min_amount: float | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return a small JSON-friendly summary of the universe frame."""

    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        summary: dict[str, Any] = {
            "total": 0,
            "included_count": 0,
            "buffer_count": 0,
            "excluded_count": 0,
            "exclusion_reasons": {},
            "max_price": float(max_price) if max_price is not None else DEFAULT_MAX_PRICE,
            "buffer_price": float(buffer_price) if buffer_price is not None else DEFAULT_BUFFER_PRICE,
            "min_amount": float(min_amount) if min_amount is not None else DEFAULT_MIN_AMOUNT,
            "updated_at": "",
            "output_path": str(Path(output_path) if output_path else low_price_universe_path()),
        }
        return summary

    included_mask = frame["included"].astype(bool) if "included" in frame.columns else pd.Series(False, index=frame.index)
    buffer_mask = frame["in_buffer"].astype(bool) if "in_buffer" in frame.columns else pd.Series(False, index=frame.index)
    excluded_mask = ~(included_mask | buffer_mask)
    reasons = (
        frame.loc[excluded_mask, "excluded_reason"].fillna("").astype(str)
        if "excluded_reason" in frame.columns
        else pd.Series([], dtype=str)
    )
    reason_counts = reasons[reasons != ""].value_counts().to_dict()
    updated_at = ""
    if "updated_at" in frame.columns and not frame["updated_at"].empty:
        updated_at = str(frame["updated_at"].iloc[0] or "")

    return {
        "total": int(len(frame)),
        "included_count": int(included_mask.sum()),
        "buffer_count": int(buffer_mask.sum()),
        "excluded_count": int(excluded_mask.sum()),
        "exclusion_reasons": {str(k): int(v) for k, v in reason_counts.items()},
        "max_price": float(max_price) if max_price is not None else DEFAULT_MAX_PRICE,
        "buffer_price": float(buffer_price) if buffer_price is not None else DEFAULT_BUFFER_PRICE,
        "min_amount": float(min_amount) if min_amount is not None else DEFAULT_MIN_AMOUNT,
        "updated_at": updated_at,
        "output_path": str(Path(output_path) if output_path else low_price_universe_path()),
    }


__all__ = [
    "DEFAULT_BUFFER_PRICE",
    "DEFAULT_LOW_PRICE_UNIVERSE_PATH",
    "DEFAULT_MAX_PRICE",
    "DEFAULT_MIN_AMOUNT",
    "OUTPUT_COLUMNS",
    "build_low_price_universe_from_spot",
    "load_low_price_universe",
    "low_price_universe_path",
    "save_low_price_universe",
    "summarize_low_price_universe",
]
