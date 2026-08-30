"""Local daily-bar feature builder for QuantDesk.

The engine reads existing local CSV files only.  It does not download market
data, connect to brokers, place orders, or produce investment advice.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from quantdesk.corporate_action_repair import repair_corporate_actions
from quantdesk.csv_io import read_csv_frame
from quantdesk.price_basis import (
    PRICE_BASIS_UNKNOWN,
    classify_frame_price_basis,
    observed_price_basis_labels,
    validate_price_frame_set,
)
from quantdesk.shared import normalize_symbol, now_text, truthy
from quantdesk.utils import atomic_write_json, read_json_safe, safe_float
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
FEATURE_DIR = PROJECT_ROOT / "data" / "features"
UNIVERSE_DIR = PROJECT_ROOT / "data" / "universe"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
LOW_PRICE_UNIVERSE_CSV = UNIVERSE_DIR / "low_price_learning_symbols.csv"
A_SHARE_UNIVERSE_CSV = UNIVERSE_DIR / "a_share_symbols.csv"
SPOT_SNAPSHOT_CSV = CACHE_DIR / "spot_snapshot_latest.csv"
DAILY_FEATURE_LATEST_CSV = FEATURE_DIR / "daily_features_latest.csv"
DAILY_FEATURE_SUMMARY_JSON = FEATURE_DIR / "daily_features_summary.json"

DISCLAIMER = "Local daily features only; no network, no broker connection, no real orders, not investment advice."


def _atomic_write_feature_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS))
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


OUTPUT_COLUMNS: tuple[str, ...] = (
    "symbol",
    "name",
    "latest_date",
    "row_count",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "pct_chg",
    "volume",
    "amount",
    "turnover",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma120",
    "close_vs_ma20_pct",
    "close_vs_ma60_pct",
    "return_5_pct",
    "return_20_pct",
    "return_60_pct",
    "volatility_20_pct",
    "amount_ma5",
    "amount_ma20",
    "volume_ma5",
    "volume_ma20",
    "turnover_ma5",
    "turnover_ma20",
    "high_20",
    "low_20",
    "is_limit_up_like",
    "is_limit_down_like",
    "consecutive_limit_up",
    "is_new_high_60",
    "is_new_low_60",
    "is_new_low_180",
    "benchmark_symbol",
    "benchmark_return_20_pct",
    "relative_index_return_20_pct",
    "liquidity_status",
    "data_health_status",
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "mkt_cap_float",
    "feature_degraded_ohlc",
    "source",
    "generated_at",
)

DAILY_ALIASES = {
    "date": "date",
    "trade_date": "date",
    "datetime": "date",
    "symbol": "symbol",
    "code": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "preclose": "prev_close",
    "prev_close": "prev_close",
    "volume": "volume",
    "amount": "amount",
    "pctChg": "pct_chg",
    "pct_chg": "pct_chg",
    "change_pct": "pct_chg",
    "turn": "turnover",
    "turnover": "turnover",
    "peTTM": "pe_ttm",
    "pe_ttm": "pe_ttm",
    "pbMRQ": "pb_mrq",
    "pb_mrq": "pb_mrq",
    "psTTM": "ps_ttm",
    "ps_ttm": "ps_ttm",
    "mkt_cap_float": "mkt_cap_float",
}

def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _append_prefixed_warnings(
    target: list[str] | None,
    warnings: Iterable[object],
    *,
    prefix: str,
) -> None:
    if target is None:
        return
    for warning in warnings:
        text = str(warning or "").strip()
        if not text:
            continue
        value = f"{prefix}:{text}" if prefix else text
        if value not in target:
            target.append(value)


def _read_csv_rows(
    path: Path,
    *,
    csv_read_warnings: list[str] | None = None,
    warning_prefix: str = "",
) -> list[dict[str, str]]:
    if not path.exists():
        return []
    frame = read_csv_frame(path, dtype=str)
    _append_prefixed_warnings(
        csv_read_warnings,
        frame.attrs.get("warnings", []),
        prefix=warning_prefix,
    )
    if frame.empty:
        return []
    frame = frame.fillna("")
    return [
        {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _read_low_price_symbols(
    path: Path = LOW_PRICE_UNIVERSE_CSV,
    *,
    csv_read_warnings: list[str] | None = None,
) -> list[dict[str, str]]:
    rows = []
    for row in _read_csv_rows(path, csv_read_warnings=csv_read_warnings, warning_prefix="universe_csv"):
        if not truthy(row.get("included")):
            continue
        symbol = normalize_symbol(row.get("symbol") or row.get("code"))
        if symbol:
            rows.append({"symbol": symbol, "name": row.get("name") or symbol})
    return rows


def _read_all_daily_symbols(daily_dir: Path = DAILY_DIR) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(daily_dir.glob("*.csv")):
        symbol = normalize_symbol(path.stem)
        if symbol:
            rows.append({"symbol": symbol, "name": symbol})
    return rows


def _read_name_map(*, csv_read_warnings: list[str] | None = None) -> dict[str, str]:
    names: dict[str, str] = {}
    for path in (LOW_PRICE_UNIVERSE_CSV, A_SHARE_UNIVERSE_CSV, SPOT_SNAPSHOT_CSV):
        for row in _read_csv_rows(
            path,
            csv_read_warnings=csv_read_warnings,
            warning_prefix=f"name_map_csv:{path.name}",
        ):
            symbol = normalize_symbol(row.get("symbol") or row.get("code") or row.get("ts_code"))
            name = row.get("name") or row.get("stock_name") or row.get("display_name")
            if symbol and name and symbol not in names:
                names[symbol] = str(name).strip()
    return names

def _read_daily_frame(
    symbol: str,
    daily_dir: Path = DAILY_DIR,
    *,
    csv_read_warnings: list[str] | None = None,
) -> pd.DataFrame:
    path = daily_dir / f"{normalize_symbol(symbol)}.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = read_csv_frame(path)
    _append_prefixed_warnings(
        csv_read_warnings,
        frame.attrs.get("warnings", []),
        prefix=f"daily_csv:{normalize_symbol(symbol)}",
    )
    if frame.empty:
        return pd.DataFrame()
    renamed = frame.rename(columns={col: DAILY_ALIASES.get(str(col), str(col)) for col in frame.columns}).copy()
    renamed = renamed.loc[:, ~renamed.columns.duplicated()]
    observed_basis, basis_reason = classify_frame_price_basis(renamed)
    if "date" not in renamed.columns or "close" not in renamed.columns:
        empty = pd.DataFrame()
        empty.attrs["observed_price_basis"] = observed_basis
        empty.attrs["price_basis_reason"] = basis_reason
        return empty
    renamed["date"] = pd.to_datetime(renamed["date"], errors="coerce")
    for column in (
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "volume",
        "amount",
        "pct_chg",
        "turnover",
        "pe_ttm",
        "pb_mrq",
        "ps_ttm",
        "mkt_cap_float",
    ):
        if column in renamed.columns:
            renamed[column] = pd.to_numeric(renamed[column], errors="coerce")
    # Round 21: never fabricate K-line fields. Missing open/high/low stay NA
    # and the symbol's feature row is labeled degraded instead.
    missing_ohlc = [column for column in ("open", "high", "low") if column not in renamed.columns]
    for column in missing_ohlc:
        renamed[column] = float("nan")
    renamed.attrs["missing_ohlc_columns"] = missing_ohlc
    if "prev_close" not in renamed.columns:
        renamed["prev_close"] = renamed["close"].shift(1)
    if "pct_chg" not in renamed.columns:
        renamed["pct_chg"] = (renamed["close"] / renamed["prev_close"] - 1.0) * 100.0
    for column in ("volume", "amount", "turnover", "pe_ttm", "pb_mrq", "ps_ttm", "mkt_cap_float"):
        if column not in renamed.columns:
            renamed[column] = pd.NA
    renamed = renamed.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    renamed = renamed[renamed["close"] > 0].reset_index(drop=True)
    # Every daily file is adjust=raw, so unadjusted split/dividend cliffs are
    # present in the price columns. This function feeds ma5/ma10/ma20/ma60/ma120,
    # close_vs_ma*_pct, return_*_pct and volatility_20_pct -- all trailing, so one
    # cliff corrupts them for the whole lookback. The repair is anchored to the
    # newest bar, so the latest quoted price is untouched; only history is
    # restated. prev_close is re-derived to stay on the same scale as close, which
    # also stops a cliff on the newest bar being misread as a limit-down move.
    # pct_chg is left alone: it is the provider's ground truth and drives the
    # limit-up/limit-down flags.
    repaired, ca_info = repair_corporate_actions(renamed)
    if not ca_info.get("unverifiable") and ca_info.get("ca_bars"):
        renamed = repaired
        renamed["prev_close"] = renamed["close"].shift(1)
        _append_prefixed_warnings(
            csv_read_warnings,
            [f"corporate_actions_repaired:{ca_info['ca_bars']}"],
            prefix=f"daily_csv:{normalize_symbol(symbol)}",
        )
    elif ca_info.get("unverifiable"):
        _append_prefixed_warnings(
            csv_read_warnings,
            [f"unverifiable_corporate_action:{ca_info.get('reason') or ''}"],
            prefix=f"daily_csv:{normalize_symbol(symbol)}",
        )
    renamed.attrs["observed_price_basis"] = observed_basis
    renamed.attrs["price_basis_reason"] = basis_reason
    return renamed


def _pct_return(series: pd.Series, days: int) -> float | None:
    if len(series) <= days:
        return None
    start = safe_float(series.iloc[-days - 1], None)
    end = safe_float(series.iloc[-1], None)
    if start is None or end is None or start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _bool_text(value: bool) -> str:
    return "true" if bool(value) else "false"


def _round_or_blank(value: Any, digits: int = 4) -> float | str:
    number = safe_float(value, None)
    if number is None or math.isnan(number) or math.isinf(number):
        return ""
    return round(float(number), digits)


def _limit_threshold(symbol: str, name: str) -> float:
    upper_name = str(name or "").upper()
    if "ST" in upper_name:
        return 4.8
    if symbol.startswith(("300", "301", "688", "689")):
        return 19.0
    return 9.5


def _consecutive_limit_up(frame: pd.DataFrame, threshold: float) -> int:
    count = 0
    for _, row in frame.tail(20).iloc[::-1].iterrows():
        pct = safe_float(row.get("pct_chg"), None)
        if pct is None or pct < threshold:
            break
        count += 1
    return count


def _liquidity_status(amount_ma20: Any) -> str:
    amount = safe_float(amount_ma20, None)
    if amount is None:
        return "unknown"
    if amount >= 30_000_000:
        return "ok"
    if amount >= 10_000_000:
        return "low"
    return "very_low"


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def _benchmark_return_20(*, csv_read_warnings: list[str] | None = None) -> tuple[str, float | None]:
    candidates = (
        PROJECT_ROOT / "data" / "index" / "sh_000001.csv",
        PROJECT_ROOT / "data" / "index_data" / "sh_000001.csv",
        PROJECT_ROOT / "data" / "benchmark" / "sh_000001.csv",
        PROJECT_ROOT / "data" / "index" / "000001.csv",
    )
    path = _first_existing(candidates)
    if path is None:
        return "", None
    frame = read_csv_frame(path)
    _append_prefixed_warnings(
        csv_read_warnings,
        frame.attrs.get("warnings", []),
        prefix=f"benchmark_csv:{path.name}",
    )
    if frame.empty:
        return path.stem, None
    frame = frame.rename(columns={col: DAILY_ALIASES.get(str(col), str(col)) for col in frame.columns})
    if "date" not in frame.columns or "close" not in frame.columns:
        return path.stem, None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    return path.stem, _pct_return(frame["close"], 20)


def _feature_input_paths(
    source_rows: list[dict[str, str]],
    *,
    daily_dir: Path,
    low_price_universe_path: Path,
) -> list[Path]:
    paths = [
        Path(__file__),
        low_price_universe_path,
        A_SHARE_UNIVERSE_CSV,
        SPOT_SNAPSHOT_CSV,
        PROJECT_ROOT / "data" / "index" / "sh_000001.csv",
        PROJECT_ROOT / "data" / "index_data" / "sh_000001.csv",
        PROJECT_ROOT / "data" / "benchmark" / "sh_000001.csv",
        PROJECT_ROOT / "data" / "index" / "000001.csv",
    ]
    paths.extend(
        daily_dir / f"{normalize_symbol(item.get('symbol'))}.csv"
        for item in source_rows
        if normalize_symbol(item.get("symbol"))
    )
    return sorted(dict.fromkeys(Path(path) for path in paths), key=lambda path: str(path).lower())


def _feature_input_snapshot(paths: list[Path]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            entries.append({"path": _display_path(path), "exists": False, "size": 0, "mtime_ns": 0})
        except OSError as exc:
            entries.append(
                {
                    "path": _display_path(path),
                    "exists": False,
                    "size": 0,
                    "mtime_ns": 0,
                    "stat_error": type(exc).__name__,
                }
            )
        else:
            entries.append(
                {
                    "path": _display_path(path),
                    "exists": True,
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                }
            )
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "path_count": len(entries),
        "entries": entries,
    }


def _feature_cache_reuse_status(
    *,
    source_rows: list[dict[str, str]],
    universe_source: str,
    only_low_price: bool,
    output_path: Path,
    summary_path: Path,
    input_snapshot: dict[str, Any],
) -> dict[str, Any]:
    reason_codes: list[str] = []
    summary = read_json_safe(summary_path, {})
    if not output_path.exists():
        reason_codes.append("feature_output_missing")
    if not summary_path.exists() or not summary:
        reason_codes.append("feature_summary_missing")
    if summary:
        if not bool(summary.get("ok")):
            reason_codes.append("feature_summary_not_ok")
        if bool(summary.get("only_low_price", True)) != bool(only_low_price):
            reason_codes.append("feature_scope_mismatch")
        if str(summary.get("universe_source") or "") != universe_source:
            reason_codes.append("feature_universe_source_mismatch")
        if int(summary.get("requested_count") or 0) != len(source_rows):
            reason_codes.append("feature_requested_count_mismatch")

    output_rows = _read_csv_rows(output_path) if output_path.exists() else []
    if summary and len(output_rows) != int(summary.get("feature_count") or 0):
        reason_codes.append("feature_output_row_count_mismatch")

    stored_digest = str(summary.get("feature_input_snapshot_digest") or "") if summary else ""
    snapshot_stable = bool(summary.get("feature_input_snapshot_stable")) if summary else False
    validation_mode = "snapshot_digest"
    if stored_digest:
        if not snapshot_stable:
            reason_codes.append("feature_input_snapshot_was_unstable")
        if stored_digest != str(input_snapshot.get("digest") or ""):
            reason_codes.append("feature_input_snapshot_changed")
    elif output_path.exists() and summary_path.exists():
        validation_mode = "legacy_mtime_guard"
        cutoff_ns = min(output_path.stat().st_mtime_ns, summary_path.stat().st_mtime_ns)
        if any(
            bool(entry.get("exists")) and int(entry.get("mtime_ns") or 0) > cutoff_ns
            for entry in input_snapshot.get("entries") or []
        ):
            reason_codes.append("feature_input_newer_than_output")
    else:
        validation_mode = "missing_artifact"

    return {
        "reusable": not reason_codes,
        "validation_mode": validation_mode,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "input_snapshot_digest": input_snapshot.get("digest", ""),
        "input_path_count": input_snapshot.get("path_count", 0),
    }


def _feature_row(symbol: str, name: str, frame: pd.DataFrame, generated_at: str, benchmark_symbol: str, benchmark_return_20: float | None) -> dict[str, Any]:
    latest = frame.iloc[-1]
    close = safe_float(latest.get("close"), None)
    prev_close = safe_float(latest.get("prev_close"), None)
    pct_chg = safe_float(latest.get("pct_chg"), None)
    threshold = _limit_threshold(symbol, name)
    close_series = frame["close"]
    amount_series = pd.to_numeric(frame.get("amount"), errors="coerce")
    volume_series = pd.to_numeric(frame.get("volume"), errors="coerce")
    turnover_series = pd.to_numeric(frame.get("turnover"), errors="coerce")
    ma5 = close_series.rolling(5, min_periods=1).mean().iloc[-1]
    ma10 = close_series.rolling(10, min_periods=1).mean().iloc[-1]
    ma20 = close_series.rolling(20, min_periods=1).mean().iloc[-1]
    ma60 = close_series.rolling(60, min_periods=1).mean().iloc[-1]
    ma120 = close_series.rolling(120, min_periods=1).mean().iloc[-1]
    return_20 = _pct_return(close_series, 20)
    relative_20 = None
    if return_20 is not None and benchmark_return_20 is not None:
        relative_20 = return_20 - benchmark_return_20
    amount_ma20 = amount_series.rolling(20, min_periods=1).mean().iloc[-1]
    high_20 = frame["high"].rolling(20, min_periods=1).max().iloc[-1]
    low_20 = frame["low"].rolling(20, min_periods=1).min().iloc[-1]
    high_60 = frame["high"].rolling(60, min_periods=1).max().iloc[-1]
    low_60 = frame["low"].rolling(60, min_periods=1).min().iloc[-1]
    low_180 = frame["low"].rolling(180, min_periods=1).min().iloc[-1]
    pct_for_limit = pct_chg
    if pct_for_limit is None and prev_close and close:
        pct_for_limit = (close / prev_close - 1.0) * 100.0
    is_limit_up = pct_for_limit is not None and pct_for_limit >= threshold
    is_limit_down = pct_for_limit is not None and pct_for_limit <= -threshold
    data_health = "ok" if len(frame) >= 60 else "short_history"
    row = {
        "symbol": symbol,
        "name": name or symbol,
        "latest_date": pd.Timestamp(latest.get("date")).strftime("%Y-%m-%d"),
        "row_count": int(len(frame)),
        "open": _round_or_blank(latest.get("open")),
        "high": _round_or_blank(latest.get("high")),
        "low": _round_or_blank(latest.get("low")),
        "close": _round_or_blank(close),
        "prev_close": _round_or_blank(prev_close),
        "pct_chg": _round_or_blank(pct_for_limit),
        "volume": _round_or_blank(latest.get("volume"), 2),
        "amount": _round_or_blank(latest.get("amount"), 2),
        "turnover": _round_or_blank(latest.get("turnover")),
        "ma5": _round_or_blank(ma5),
        "ma10": _round_or_blank(ma10),
        "ma20": _round_or_blank(ma20),
        "ma60": _round_or_blank(ma60),
        "ma120": _round_or_blank(ma120),
        "close_vs_ma20_pct": _round_or_blank((close / ma20 - 1.0) * 100.0 if close and ma20 else None),
        "close_vs_ma60_pct": _round_or_blank((close / ma60 - 1.0) * 100.0 if close and ma60 else None),
        "return_5_pct": _round_or_blank(_pct_return(close_series, 5)),
        "return_20_pct": _round_or_blank(return_20),
        "return_60_pct": _round_or_blank(_pct_return(close_series, 60)),
        "volatility_20_pct": _round_or_blank(close_series.pct_change(fill_method=None).rolling(20, min_periods=2).std().iloc[-1] * 100.0),
        "amount_ma5": _round_or_blank(amount_series.rolling(5, min_periods=1).mean().iloc[-1], 2),
        "amount_ma20": _round_or_blank(amount_ma20, 2),
        "volume_ma5": _round_or_blank(volume_series.rolling(5, min_periods=1).mean().iloc[-1], 2),
        "volume_ma20": _round_or_blank(volume_series.rolling(20, min_periods=1).mean().iloc[-1], 2),
        "turnover_ma5": _round_or_blank(turnover_series.rolling(5, min_periods=1).mean().iloc[-1]),
        "turnover_ma20": _round_or_blank(turnover_series.rolling(20, min_periods=1).mean().iloc[-1]),
        "high_20": _round_or_blank(high_20),
        "low_20": _round_or_blank(low_20),
        "is_limit_up_like": _bool_text(is_limit_up),
        "is_limit_down_like": _bool_text(is_limit_down),
        "consecutive_limit_up": _consecutive_limit_up(frame, threshold),
        "is_new_high_60": _bool_text(close is not None and close >= safe_float(high_60, close)),
        "is_new_low_60": _bool_text(close is not None and close <= safe_float(low_60, close)),
        "is_new_low_180": _bool_text(close is not None and close <= safe_float(low_180, close)),
        "benchmark_symbol": benchmark_symbol,
        "benchmark_return_20_pct": _round_or_blank(benchmark_return_20),
        "relative_index_return_20_pct": _round_or_blank(relative_20),
        "liquidity_status": _liquidity_status(amount_ma20),
        "data_health_status": data_health,
        "pe_ttm": _round_or_blank(latest.get("pe_ttm")),
        "pb_mrq": _round_or_blank(latest.get("pb_mrq")),
        "ps_ttm": _round_or_blank(latest.get("ps_ttm")),
        "mkt_cap_float": _round_or_blank(latest.get("mkt_cap_float"), 2),
        "feature_degraded_ohlc": ",".join(frame.attrs.get("missing_ohlc_columns") or []),
        "source": "local_daily_feature_engine",
        "generated_at": generated_at,
    }
    return {column: row.get(column, "") for column in OUTPUT_COLUMNS}


def build_daily_features(
    *,
    only_low_price: bool = True,
    daily_dir: Path = DAILY_DIR,
    low_price_universe_path: Path = LOW_PRICE_UNIVERSE_CSV,
    output_path: Path = DAILY_FEATURE_LATEST_CSV,
    summary_path: Path = DAILY_FEATURE_SUMMARY_JSON,
    limit: int = 0,
    symbols: str | list[str] | tuple[str, ...] | None = None,
    reuse_if_fresh: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build the local daily feature table.

    ``only_low_price=True`` processes only ``included=true`` rows in the
    low-price learning universe.  Passing ``False`` is an explicit local
    full-cache scan; it still does not touch the network.

    ``as_of`` (Round 21) truncates every frame to rows dated <= as_of BEFORE
    the last-row feature computation, so historical replays cannot leak
    future rows into "latest" features.
    """

    generated_at = now_text()
    warnings: list[str] = []
    explicit_symbols = []
    if isinstance(symbols, str):
        explicit_symbols = [normalize_symbol(item) for item in symbols.split(",") if normalize_symbol(item)]
    elif symbols:
        explicit_symbols = [normalize_symbol(item) for item in symbols if normalize_symbol(item)]
    if explicit_symbols:
        name_map = _read_name_map(csv_read_warnings=warnings)
        source_rows = [{"symbol": symbol, "name": name_map.get(symbol, symbol)} for symbol in dict.fromkeys(explicit_symbols)]
        universe_source = "specified_symbols"
    elif only_low_price:
        source_rows = _read_low_price_symbols(low_price_universe_path, csv_read_warnings=warnings)
        universe_source = "low_price_learning_symbols"
        if not source_rows:
            payload = {
                "ok": False,
                "message": "low_price_learning_symbols.csv missing or has no included=true symbols; daily features were not generated.",
                "only_low_price": True,
                "output_path": _display_path(output_path),
                "warnings": list(warnings),
                **observed_price_basis_labels(
                    PRICE_BASIS_UNKNOWN,
                    reason_codes=["no_price_frames"],
                ),
                "accepted_price_basis": PRICE_BASIS_UNKNOWN,
                "accepted_basis_counts": {},
                "excluded_reason_counts": {},
                "reason_codes": ["no_price_frames"],
                "per_symbol": {},
                "disclaimer": DISCLAIMER,
            }
            return payload
    else:
        source_rows = _read_all_daily_symbols(daily_dir)
        universe_source = "local_daily_cache_all"
    if limit and limit > 0:
        source_rows = source_rows[: int(limit)]
    input_paths = _feature_input_paths(
        source_rows,
        daily_dir=daily_dir,
        low_price_universe_path=low_price_universe_path,
    )
    input_snapshot_before = _feature_input_snapshot(input_paths)
    cache_validation: dict[str, Any] = {
        "reusable": False,
        "validation_mode": "disabled",
        "reason_codes": ["feature_cache_reuse_not_requested"],
    }
    if reuse_if_fresh:
        cache_validation = _feature_cache_reuse_status(
            source_rows=source_rows,
            universe_source=universe_source,
            only_low_price=only_low_price,
            output_path=output_path,
            summary_path=summary_path,
            input_snapshot=input_snapshot_before,
        )
        if cache_validation.get("reusable"):
            cached_summary = read_json_safe(summary_path, {})
            return {
                **cached_summary,
                "ok": True,
                "cache_reused": True,
                "cache_validation": cache_validation,
                "message": "Daily feature inputs are unchanged; reused the existing local feature artifact.",
                "skipped": list(cached_summary.get("skipped") or [])[:20],
            }
    name_map = _read_name_map(csv_read_warnings=warnings)
    benchmark_symbol, benchmark_return = _benchmark_return_20(csv_read_warnings=warnings)
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    accepted_frames: dict[str, pd.DataFrame] = {}
    accepted_names: dict[str, str] = {}
    per_symbol: dict[str, dict[str, Any]] = {}
    feature_latest_dates: dict[str, str] = {}
    excluded_reason_counts: dict[str, int] = {}
    for item in source_rows:
        symbol = normalize_symbol(item.get("symbol"))
        if not symbol:
            continue
        name = item.get("name") or name_map.get(symbol) or symbol
        frame = _read_daily_frame(symbol, daily_dir, csv_read_warnings=warnings)
        if as_of and not frame.empty and "date" in frame.columns:
            as_of_ts = pd.to_datetime(str(as_of), errors="coerce")
            if pd.notna(as_of_ts):
                attrs = dict(frame.attrs)
                frame = frame[frame["date"] <= as_of_ts].reset_index(drop=True)
                frame.attrs.update(attrs)
        basis = str(frame.attrs.get("observed_price_basis") or PRICE_BASIS_UNKNOWN)
        basis_reason = str(frame.attrs.get("price_basis_reason") or "")
        if frame.empty:
            reason = basis_reason or "missing_or_invalid_daily"
            skipped.append({"symbol": symbol, "name": name, "reason": reason})
            per_symbol[symbol] = {
                "price_basis": basis,
                "reason_code": reason,
                "eligible": False,
            }
            excluded_reason_counts[reason] = excluded_reason_counts.get(reason, 0) + 1
            continue
        if basis_reason:
            skipped.append({"symbol": symbol, "name": name, "reason": basis_reason})
            per_symbol[symbol] = {
                "price_basis": basis,
                "reason_code": basis_reason,
                "eligible": False,
            }
            excluded_reason_counts[basis_reason] = excluded_reason_counts.get(basis_reason, 0) + 1
            continue
        # The set-level guard needs only adjustment provenance.  Retaining the
        # full history for every symbol would make an explicit full-cache scan
        # needlessly hold the entire market in memory after feature calculation.
        accepted_frames[symbol] = frame.loc[:, ["adjust"]].drop_duplicates().reset_index(drop=True)
        accepted_names[symbol] = str(name)
        feature_row = _feature_row(symbol, name, frame, generated_at, benchmark_symbol, benchmark_return)
        rows.append(feature_row)
        feature_latest_dates[symbol] = str(feature_row.get("latest_date") or "")[:10]

    frame_set_validation = validate_price_frame_set(accepted_frames, consumer="feature")
    accepted_basis_counts = dict(frame_set_validation.get("basis_counts") or {})
    for symbol, evidence in (frame_set_validation.get("per_symbol") or {}).items():
        per_symbol[str(symbol)] = {
            **dict(evidence),
            "latest_date": feature_latest_dates.get(str(symbol), ""),
        }

    frame_set_reason_codes = [str(item) for item in (frame_set_validation.get("reason_codes") or []) if item]
    cross_basis_blocked = "cross_symbol_price_basis_mismatch" in frame_set_reason_codes
    if cross_basis_blocked:
        cross_reason = "cross_symbol_price_basis_mismatch"
        for symbol in accepted_frames:
            skipped.append({
                "symbol": symbol,
                "name": accepted_names.get(symbol, symbol),
                "reason": cross_reason,
            })
        excluded_reason_counts[cross_reason] = int(
            (frame_set_validation.get("reason_counts") or {}).get(cross_reason)
            or len(accepted_frames)
        )

    reason_codes = list(dict.fromkeys([*excluded_reason_counts, *frame_set_reason_codes]))
    price_block_reasons = {
        "mixed_price_basis",
        "unknown_price_basis",
        "cross_symbol_price_basis_mismatch",
        "consumer_price_basis_not_allowed",
        "unknown_price_consumer",
    }
    hard_price_block = cross_basis_blocked or (
        not rows and any(reason in price_block_reasons for reason in reason_codes)
    )
    accepted_price_basis = str(
        frame_set_validation.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN
    )
    run_price_basis = (
        PRICE_BASIS_UNKNOWN
        if skipped or hard_price_block
        else accepted_price_basis
    )
    price_labels = observed_price_basis_labels(run_price_basis, reason_codes=reason_codes)
    price_policy_validation = {
        **frame_set_validation,
        "run_status": "blocked" if hard_price_block else ("partial" if skipped else "passed"),
        "accepted_basis_counts": dict(sorted(accepted_basis_counts.items())),
        "excluded_reason_counts": dict(sorted(excluded_reason_counts.items())),
        "per_symbol": per_symbol,
    }

    if hard_price_block:
        return {
            "ok": False,
            "status": "blocked",
            "message": "price policy validation blocked daily feature output before write.",
            "schema_version": API_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "generated_at": generated_at,
            **price_labels,
            "accepted_price_basis": accepted_price_basis,
            "accepted_basis_counts": dict(sorted(accepted_basis_counts.items())),
            "excluded_reason_counts": dict(sorted(excluded_reason_counts.items())),
            "reason_codes": reason_codes,
            "per_symbol": per_symbol,
            "price_policy_validation": price_policy_validation,
            "only_low_price": bool(only_low_price),
            "universe_source": universe_source,
            "as_of": str(as_of or ""),
            "requested_count": len(source_rows),
            "feature_count": 0,
            "candidate_feature_count": len(rows),
            "skipped_count": len(skipped),
            "latest_date": "",
            "output_path": _display_path(output_path),
            "summary_path": _display_path(summary_path),
            "warnings": list(dict.fromkeys([*warnings, *reason_codes])),
            "skipped": skipped[:20],
            "disclaimer": DISCLAIMER,
            "cache_reused": False,
            "cache_validation": cache_validation,
        }

    latest_dates = [
        str(row.get("latest_date") or "")
        for row in rows
        if row.get("latest_date")
    ]
    input_snapshot_after = _feature_input_snapshot(input_paths)
    input_snapshot_stable = (
        input_snapshot_before.get("digest")
        == input_snapshot_after.get("digest")
    )
    summary_base = {
        "schema_version": API_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "generated_at": generated_at,
        **price_labels,
        "accepted_price_basis": accepted_price_basis,
        "accepted_basis_counts": dict(sorted(accepted_basis_counts.items())),
        "excluded_reason_counts": dict(sorted(excluded_reason_counts.items())),
        "reason_codes": reason_codes,
        "per_symbol": per_symbol,
        "price_policy_validation": price_policy_validation,
        "only_low_price": bool(only_low_price),
        "universe_source": universe_source,
        "as_of": str(as_of or ""),
        "requested_count": len(source_rows),
        "skipped_count": len(skipped),
        "latest_date": max(latest_dates) if latest_dates else "",
        "output_path": _display_path(output_path),
        "summary_path": _display_path(summary_path),
        "warnings": list(dict.fromkeys([*warnings, *reason_codes])),
        "feature_input_snapshot_digest": input_snapshot_after.get("digest", ""),
        "feature_input_snapshot_stable": input_snapshot_stable,
        "feature_input_path_count": input_snapshot_after.get("path_count", 0),
        "cache_reused": False,
        "cache_validation": cache_validation,
        "disclaimer": DISCLAIMER,
    }
    if not input_snapshot_stable:
        existing_hash = _sha256_file(output_path) if output_path.exists() else ""
        summary = {
            **summary_base,
            "ok": False,
            "status": "blocked",
            "feature_count": 0,
            "output_row_count": 0,
            "output_sha256": existing_hash,
            "warnings": list(
                dict.fromkeys(
                    [
                        *summary_base["warnings"],
                        "feature_input_snapshot_changed_before_publish",
                    ]
                )
            ),
            "skipped": skipped[:100],
        }
        atomic_write_json(summary_path, summary)
        return {**summary, "skipped": skipped[:20]}
    _atomic_write_feature_csv(output_path, rows)
    output_sha256 = _sha256_file(output_path)
    summary = {
        **summary_base,
        "ok": True,
        "status": "partial" if skipped else ("passed" if rows else "warning"),
        "feature_count": len(rows),
        "output_row_count": len(rows),
        "output_sha256": output_sha256,
        "skipped": skipped[:100],
    }
    atomic_write_json(summary_path, summary)
    return {**summary, "skipped": skipped[:20]}



def daily_feature_status(
    *,
    output_path: Path = DAILY_FEATURE_LATEST_CSV,
    summary_path: Path = DAILY_FEATURE_SUMMARY_JSON,
) -> dict[str, Any]:
    summary = read_json_safe(summary_path, {})
    exists = output_path.exists()
    row_count = 0
    preview_latest = ""
    if exists:
        rows = _read_csv_rows(output_path)
        row_count = len(rows)
        dates = [row.get("latest_date", "") for row in rows if row.get("latest_date")]
        preview_latest = max(dates) if dates else ""
    return {
        "ok": True,
        "exists": exists,
        "path": _display_path(output_path),
        "summary_path": _display_path(summary_path),
        "feature_count": row_count,
        "latest_date": summary.get("latest_date") or preview_latest,
        "generated_at": summary.get("generated_at") or "",
        "only_low_price": summary.get("only_low_price", True),
        "universe_source": summary.get("universe_source", ""),
        "skipped_count": summary.get("skipped_count", 0),
        "price_basis": summary.get("price_basis", PRICE_BASIS_UNKNOWN),
        "price_basis_label": summary.get("price_basis_label", ""),
        "accepted_price_basis": summary.get(
            "accepted_price_basis", PRICE_BASIS_UNKNOWN
        ),
        "accepted_basis_counts": summary.get("accepted_basis_counts", {}),
        "excluded_reason_counts": summary.get("excluded_reason_counts", {}),
        "reason_codes": summary.get("reason_codes", []),
        "price_policy_validation": summary.get("price_policy_validation", {}),
        "disclaimer": DISCLAIMER,
    }


def daily_feature_preview(limit: int = 20, *, output_path: Path = DAILY_FEATURE_LATEST_CSV) -> dict[str, Any]:
    rows = _read_csv_rows(output_path)[: max(0, int(limit or 20))]
    return {
        "ok": True,
        "exists": output_path.exists(),
        "path": _display_path(output_path),
        "limit": limit,
        "rows": rows,
        "disclaimer": DISCLAIMER,
    }


def load_daily_feature_map(output_path: Path = DAILY_FEATURE_LATEST_CSV) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _read_csv_rows(output_path):
        symbol = normalize_symbol(row.get("symbol"))
        if symbol:
            result[symbol] = row
    return result


FEATURE_MERGE_KEYS: tuple[str, ...] = (
    "return_5_pct",
    "return_20_pct",
    "return_60_pct",
    "volatility_20_pct",
    "turnover",
    "turnover_ma5",
    "turnover_ma20",
    "amount_ma5",
    "amount_ma20",
    "volume_ma5",
    "volume_ma20",
    "is_limit_up_like",
    "is_limit_down_like",
    "consecutive_limit_up",
    "is_new_high_60",
    "is_new_low_60",
    "is_new_low_180",
    "benchmark_symbol",
    "benchmark_return_20_pct",
    "relative_index_return_20_pct",
    "liquidity_status",
    "data_health_status",
)


def apply_daily_features_to_item(item: dict[str, Any], feature_row: dict[str, Any] | None) -> dict[str, Any]:
    if not feature_row:
        item["daily_feature_available"] = False
        return item
    for key in FEATURE_MERGE_KEYS:
        value = feature_row.get(key)
        if value not in (None, ""):
            item[key] = value
    item["daily_feature_available"] = True
    item["daily_feature_latest_date"] = feature_row.get("latest_date") or ""
    item["daily_feature_generated_at"] = feature_row.get("generated_at") or ""
    item["feature_source"] = "daily_features_latest"
    return item
