from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.shared import normalize_symbol


def local_full_a_backtest_scope(root: Path, daily_dir: Path, *, max_symbols: int = 0) -> dict[str, Any]:
    """Use the full local archive plus the current roster, never a low-price list.

    Including archived names avoids silently discarding delisted local histories.
    This does not create missing historical membership or prove PIT coverage.
    """
    symbols = {path.stem for path in daily_dir.glob("*.csv") if re.fullmatch(r"[0-9]{6}", path.stem)} if daily_dir.exists() else set()
    warnings = ["historical_universe_not_proven_point_in_time", "out_of_sample_performance_not_established"]
    roster = root / "data" / "universe" / "a_share_symbols.csv"
    if roster.exists():
        frame = read_csv_frame(roster)
        column = next((key for key in ("symbol", "code") if key in frame.columns), None)
        if column:
            symbols.update(symbol for value in frame[column] if re.fullmatch(r"[0-9]{6}", symbol := normalize_symbol(value)))
        else:
            warnings.append("full_a_roster_symbol_column_missing")
    else:
        warnings.append("full_a_roster_missing_local_archive_only")
    available = sorted(symbols)
    selected = available[:max_symbols] if max_symbols > 0 else available
    return {
        "symbols": selected, "data_scope": "full_a", "scope_label": "全 A 本地数据（历史成员证据未完备）",
        "scope_source": "full_a_roster_union_local_archive", "analysis_pool_count": len(available),
        "requested_count": len(available), "selected_count": len(selected), "max_symbols": max_symbols,
        "truncated": len(selected) < len(available),
        "truncation_reason": f"explicit_max_symbols={max_symbols}" if len(selected) < len(available) else "",
        "symbols_preview": selected[:5], "symbols_omitted_count": max(0, len(selected) - 5),
        "warnings": warnings, "blocked": False,
    }


def read_backtest_daily(
    symbol: str, daily_dir: Path, *, start_date: str = "", end_date: str = ""
) -> tuple[pd.DataFrame, str]:
    """Shared CLI/API history preparation with a validated original price basis."""
    from quantdesk.corporate_action_repair import repair_corporate_actions
    from quantdesk.price_basis import classify_frame_price_basis

    paths = [daily_dir / f"{symbol}.csv", daily_dir / f"{symbol}_qfq.csv"]
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        return pd.DataFrame(), "daily_file_missing"
    frame = read_csv_frame(path)
    warnings = [str(item) for item in frame.attrs.get("warnings", []) if item]
    if frame.empty:
        return frame, ";".join(warnings or ["daily_file_empty"])
    # Classify the original complete file before aliases, filtering or sorting.
    basis, reason = classify_frame_price_basis(frame)
    if reason:
        return pd.DataFrame(), ";".join([reason, *warnings])
    aliases = {"trade_date": "date", "datetime": "date", "日期": "date", "code": "symbol", "preclose": "prev_close", "change_pct": "pct_chg"}
    frame = frame.rename(columns={key: value for key, value in aliases.items() if key in frame.columns and value not in frame.columns}).copy()
    if "date" not in frame or "close" not in frame:
        return pd.DataFrame(), "daily_required_columns_missing"
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "prev_close", "volume", "amount", "pct_chg", "turnover"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    frame = frame[frame["close"] > 0].copy()
    # Do not invent an open/high/low from close. Missing execution evidence is
    # preserved for the simulator's existing no-fill rule.
    if end_date:
        frame = frame[frame["date"] <= pd.Timestamp(end_date)].copy()
    if "prev_close" not in frame:
        frame["prev_close"] = frame["close"].shift(1)
    if basis == "raw":
        frame, info = repair_corporate_actions(frame)
        if info.get("unverifiable"):
            return pd.DataFrame(), ";".join(["corporate_action_unverifiable", *warnings])
        if info.get("ca_bars"):
            frame["adjust"] = "qfq"
            if "price_basis" in frame:
                frame["price_basis"] = "qfq"
            frame["research_restatement"] = "reported_return_proxy"
            frame["prev_close"] = frame["close"].shift(1)
            warnings.append(f"corporate_action_repaired:{info['ca_bars']}:reported_return_research_proxy")
    if start_date:
        frame = frame[frame["date"] >= pd.Timestamp(start_date)].copy()
    frame.attrs["warnings"] = warnings
    frame.attrs["original_price_basis"] = basis
    return frame.reset_index(drop=True), ";".join(warnings)


def align_backtest_research_basis(histories: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Align identity factors only when an all-raw source batch was restated.

    A batch with no restatement keeps its actual raw basis. Original mixed
    source bases are never repaired or hidden by this operation.
    """
    originals = {frame.attrs.get("original_price_basis") for frame in histories.values()}
    restated = any("research_restatement" in frame.columns for frame in histories.values())
    if originals != {"raw"} or not restated:
        return histories
    result = {}
    for symbol, frame in histories.items():
        prepared = frame.copy()
        prepared["adjust"] = "qfq"
        if "price_basis" in prepared:
            prepared["price_basis"] = "qfq"
        prepared["research_restatement"] = "reported_return_proxy"
        result[symbol] = prepared
    return result


def validate_backtest_history_sources(histories: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Check original cross-symbol bases as well as the prepared research basis."""
    from quantdesk.price_basis import classify_frame_price_basis, validate_price_frame_set

    original = {}
    for symbol, frame in histories.items():
        basis = frame.attrs.get("original_price_basis")
        if not basis:
            basis, _reason = classify_frame_price_basis(frame)
        original[symbol] = pd.DataFrame({"adjust": [basis]})
    original_policy = validate_price_frame_set(original, consumer="backtest")
    if not original_policy.get("eligible"):
        return {**original_policy, "validation_phase": "original_source_files"}
    policy = validate_price_frame_set(histories, consumer="backtest")
    return {**policy, "original_source_policy": original_policy, "research_restatement_is_official": False}


def _safe_number(value: Any, default: float = np.nan) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return default


def benchmark_total_return(
    benchmark_series: pd.Series | None,
    *,
    start_date: Any | None = None,
    end_date: Any | None = None,
) -> float:
    """Return local benchmark total return, optionally aligned to a date window."""

    if benchmark_series is None:
        return np.nan
    series = pd.Series(benchmark_series).dropna()
    if (
        (start_date is not None or end_date is not None)
        and len(series)
        and not isinstance(series.index, pd.RangeIndex)
        and not pd.api.types.is_numeric_dtype(series.index)
    ):
        index_dates = pd.to_datetime(series.index, errors="coerce")
        if not pd.isna(index_dates).all():
            mask = pd.Series(True, index=series.index)
            if start_date is not None:
                start = pd.to_datetime(start_date, errors="coerce")
                if pd.notna(start):
                    mask &= index_dates >= start
            if end_date is not None:
                end = pd.to_datetime(end_date, errors="coerce")
                if pd.notna(end):
                    mask &= index_dates <= end
            series = series.loc[mask.to_numpy()]
    if len(series) < 2:
        return np.nan
    first = _safe_number(series.iloc[0])
    last = _safe_number(series.iloc[-1])
    if not math.isfinite(first) or first == 0 or not math.isfinite(last):
        return np.nan
    return float(last / first - 1)


def benchmark_window_from_equity_curve(equity_curve: pd.DataFrame) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Return the valid equity date window used for benchmark alignment."""

    if "date" not in equity_curve.columns or "equity" not in equity_curve.columns or equity_curve.empty:
        return None, None
    valid_equity = pd.to_numeric(equity_curve["equity"], errors="coerce").notna()
    dates = pd.to_datetime(equity_curve.loc[valid_equity, "date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    return pd.Timestamp(dates.min()), pd.Timestamp(dates.max())


def load_benchmark_series(path: str | Path | None) -> pd.Series:
    """Load a local benchmark CSV with date and close columns."""

    if not path:
        return pd.Series(dtype=float, name="benchmark_close")
    frame = read_csv_frame(path)
    required = {"date", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"benchmark csv missing columns: {', '.join(sorted(missing))}")
    frame = frame[["date", "close"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    frame = frame.drop_duplicates("date", keep="last")
    return pd.Series(frame["close"].to_numpy(dtype=float), index=frame["date"], name="benchmark_close")


def load_industry_map(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    frame = read_csv_frame(path)
    required = {"symbol", "industry"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"industry map csv missing columns: {', '.join(sorted(missing))}")
    result: dict[str, str] = {}
    for _, row in frame[["symbol", "industry"]].dropna().iterrows():
        symbol = normalize_symbol(row["symbol"])
        industry = str(row["industry"]).strip()
        if symbol and industry:
            result[symbol] = industry
    return result
