from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.research.sample_provenance import research_sample_context
from quantdesk.paper.market_rules import round_notional_down_to_lot_quantity
from quantdesk.paper.trade_costs import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_STAMP_TAX_RATE,
    TradeCostConfig,
    compute_trade_costs,
)
from quantdesk.shared import safe_float_zero as safe_float
from quantdesk.shared import truthy

RESULT_JSON = ROOT / "results" / "strategy_screening_funnel_latest.json"
REPORT_MD = ROOT / "reports" / "strategy_screening_funnel_latest.md"
DISCLAIMER = "Research-only fast screening. No broker, no real orders, no automatic trading, not investment advice."
RANDOM_SEED = 20260528
FEATURE_CACHE_VERSION = "strategy_screening_features_csv_v2"


@dataclass(frozen=True)
class Hypothesis:
    strategy: str
    family: str
    top_n: int = 20


HYPOTHESES: list[Hypothesis] = [
    Hypothesis("equal_weight_random", "baseline"),
    Hypothesis("equal_weight_low_turnover", "baseline"),
    Hypothesis("reversal_20_top20", "reversal"),
    Hypothesis("reversal_40_top20", "reversal"),
    Hypothesis("reversal_60_top5", "reversal", 5),
    Hypothesis("reversal_60_top10", "reversal", 10),
    Hypothesis("reversal_60_top20", "reversal"),
    Hypothesis("reversal_120_top20", "reversal"),
    Hypothesis("rsi_reversal", "reversal"),
    Hypothesis("bollinger_reversal", "reversal"),
    Hypothesis("crash_rebound", "reversal"),
    Hypothesis("momentum_20_top20", "momentum"),
    Hypothesis("momentum_60_top20", "momentum"),
    Hypothesis("momentum_120_top20", "momentum"),
    Hypothesis("momentum_250_top20", "momentum"),
    Hypothesis("ma_breakout", "trend"),
    Hypothesis("breakout_120", "trend"),
    Hypothesis("low_volatility_20", "low_vol"),
    Hypothesis("low_volatility_60", "low_vol"),
    Hypothesis("low_volatility_120", "low_vol"),
    Hypothesis("low_drawdown_120", "low_vol"),
    Hypothesis("stable_uptrend", "low_vol"),
    Hypothesis("volume_surge", "volume"),
    Hypothesis("volume_dry_rebound", "volume"),
    Hypothesis("liquidity_quality", "volume"),
    Hypothesis("reversal_60_lowvol", "combo"),
    Hypothesis("reversal_60_liquidity", "combo"),
    Hypothesis("reversal_60_low_drawdown", "combo"),
    Hypothesis("trend_lowvol", "combo"),
    Hypothesis("breakout_volume", "combo"),
]

STRATEGY_ALIASES: dict[str, str] = {
    "ma_trend": "ma_breakout",
}

MIMO_RETEST_HYPOTHESES: list[Hypothesis] = [
    Hypothesis("reversal_120_top5_no_filter", "reversal", 5),
    Hypothesis("reversal_120_top10_no_filter", "reversal", 10),
    Hypothesis("reversal_120_top20_no_filter", "reversal", 20),
    Hypothesis("reversal_120_top5_px_le_10", "reversal", 5),
    Hypothesis("reversal_120_top10_px_le_10", "reversal", 10),
    Hypothesis("reversal_120_top20_px_le_10", "reversal", 20),
    Hypothesis("reversal_120_top5_px_le_15", "reversal", 5),
    Hypothesis("reversal_120_top10_px_le_15", "reversal", 10),
    Hypothesis("reversal_120_top20_px_le_15", "reversal", 20),
    Hypothesis("reversal_120_top8_px_le_15", "reversal", 8),
    Hypothesis("reversal_120_top3_px_le_15", "reversal", 3),
    Hypothesis("reversal_60_top5_px_le_15", "reversal", 5),
    Hypothesis("reversal_60_top10_px_le_15", "reversal", 10),
    Hypothesis("reversal_60_top20_px_le_15", "reversal", 20),
    Hypothesis("rev120_xvol20_xc3_xr70", "reversal", 10),
    Hypothesis("volume_dry_rebound", "volume"),
    Hypothesis("rsi_reversal", "reversal"),
    Hypothesis("reversal_60_low_drawdown", "combo"),
    Hypothesis("liquidity_quality", "volume"),
    Hypothesis("equal_weight_random", "baseline"),
    Hypothesis("momentum_20_top20", "momentum"),
]


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            pass
    return value


def stable_hash(value: Any) -> str:
    text = json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def positive_int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def selected_daily_paths(data_root: Path, max_symbols: int = 0) -> tuple[list[Path], int]:
    paths = sorted(data_root.glob("*.csv"))
    limit = positive_int(max_symbols, 0)
    return (paths[:limit] if limit else paths), len(paths)


def feature_cache_fingerprint(data_root: Path, min_history_days: int, max_symbols: int = 0) -> tuple[str, dict[str, Any]]:
    paths, available = selected_daily_paths(data_root, max_symbols)
    entries: list[dict[str, Any]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append({"name": path.name, "size": stat.st_size, "sha256": _file_sha256(path)})
    meta = {
        "cache_version": FEATURE_CACHE_VERSION,
        "data_root": str(data_root.resolve()).replace("\\", "/"),
        "daily_csv_available": available,
        "scanned_csv": len(paths),
        "max_symbols": positive_int(max_symbols, 0),
        "min_history_days": int(min_history_days),
        "entries": entries,
    }
    return stable_hash(meta), meta


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _feature_cache_directory(args: argparse.Namespace) -> Path:
    output = _resolve_project_path(getattr(args, "output_dir", None), "results")
    configured = getattr(args, "cache_dir", None)
    if not configured:
        return output / "cache"
    path = Path(str(configured))
    return path if path.is_absolute() else output / path


def _read_feature_cache(path: Path, fingerprint: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata.get("schema") != FEATURE_CACHE_VERSION or metadata.get("fingerprint") != fingerprint:
        raise ValueError("feature_cache_schema_or_input_mismatch")
    digest = metadata.get("csv_sha256", "")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("feature_cache_invalid_digest")
    csv_path = path.with_name(f"{path.stem}_{digest}.csv")
    if _file_sha256(csv_path) != digest:
        raise ValueError("feature_cache_content_mismatch")
    dtypes = metadata.get("dtypes")
    columns = metadata.get("columns")
    allowed = {"float32", "float64", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "bool"}
    if not isinstance(dtypes, dict) or not isinstance(columns, list) or set(columns) != set(dtypes):
        raise ValueError("feature_cache_invalid_columns")
    if dtypes.get("date") not in {"datetime64[ns]", "datetime64[us]"} or dtypes.get("symbol") not in {"object", "str", "string"}:
        raise ValueError("feature_cache_invalid_identity_columns")
    if any(kind not in allowed for name, kind in dtypes.items() if name not in {"date", "symbol"}):
        raise ValueError("feature_cache_unsupported_dtype")
    frame = pd.read_csv(csv_path, dtype={k: v for k, v in dtypes.items() if k != "date"}, float_precision="round_trip")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").astype(dtypes["date"])
    if list(frame.columns) != columns or len(frame) != metadata.get("rows") or frame[["date", "symbol"]].isna().any().any():
        raise ValueError("feature_cache_shape_or_identity_mismatch")
    summary = metadata.get("data_quality_summary")
    if not isinstance(summary, dict):
        raise ValueError("feature_cache_invalid_data_quality")
    return frame, summary


def _write_feature_cache(path: Path, fingerprint: str, frame: pd.DataFrame, summary: dict[str, Any]) -> None:
    """Publish the receipt last; truncated writes never become a usable cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary_csv = Path(handle.name)
            pending.append(temporary_csv)
            frame.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        digest = _file_sha256(temporary_csv)
        csv_path = path.with_name(f"{path.stem}_{digest}.csv")
        os.replace(temporary_csv, csv_path)
        metadata = {"schema": FEATURE_CACHE_VERSION, "fingerprint": fingerprint,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "csv_sha256": digest,
                    "rows": len(frame), "columns": list(frame.columns),
                    "dtypes": {column: str(dtype) for column, dtype in frame.dtypes.items()},
                    "data_quality_summary": summary}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary_json = Path(handle.name)
            pending.append(temporary_json)
            json.dump(metadata, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_json, path)
    finally:
        for item in pending:
            item.unlink(missing_ok=True)


def _resolve_project_path(value: Any, default: str) -> Path:
    path = Path(str(value or default))
    return path if path.is_absolute() else ROOT / path


def max_drawdown(values: list[float] | pd.Series) -> float:
    series = pd.Series(values, dtype="float64").dropna()
    if series.empty:
        return 0.0
    return float((series / series.cummax() - 1.0).min())


def annualized_return(equity: list[float], dates: list[Any]) -> float:
    if len(equity) < 2 or len(dates) < 2 or equity[0] <= 0:
        return 0.0
    days = max(1, (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days)
    total = equity[-1] / equity[0] - 1.0
    if 1.0 + total <= 0:
        return -1.0
    return float((1.0 + total) ** (365.25 / days) - 1.0)


def sharpe_ratio(returns: list[float]) -> float | None:
    series = pd.Series(returns, dtype="float64").dropna()
    if len(series) < 2:
        return 0.0
    std = float(series.std(ddof=0))
    if std < 1e-12:
        return None
    return float(series.mean() / std * math.sqrt(252))


def sortino_ratio(returns: list[float]) -> float | None:
    from quantdesk.backtest_metrics import downside_sortino

    return downside_sortino(returns)


def yearly_returns_from_equity(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    out: dict[str, float] = {}
    for year, group in frame.groupby(frame["date"].dt.year.astype(str)):
        vals = pd.to_numeric(group["equity"], errors="coerce").dropna()
        if len(vals) >= 2 and vals.iloc[0] > 0:
            out[str(year)] = float(vals.iloc[-1] / vals.iloc[0] - 1.0)
    return out


def monthly_returns_from_equity(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    out: dict[str, float] = {}
    for month, group in frame.groupby(frame["date"].dt.to_period("M").astype(str)):
        vals = pd.to_numeric(group["equity"], errors="coerce").dropna()
        if len(vals) >= 2 and vals.iloc[0] > 0:
            out[str(month)] = float(vals.iloc[-1] / vals.iloc[0] - 1.0)
    return out


def metrics_without_year(equity_rows: list[dict[str, Any]], excluded_year: int = 2024) -> dict[str, Any]:
    yearly = yearly_returns_from_equity(equity_rows)
    kept_yearly = {year: ret for year, ret in yearly.items() if str(year) != str(excluded_year)}
    monthly = monthly_returns_from_equity(equity_rows)
    kept_monthly = {month: ret for month, ret in monthly.items() if not str(month).startswith(f"{excluded_year}-")}
    if len(kept_yearly) < 2:
        return {
            f"annualized_without_{excluded_year}": None,
            f"sharpe_without_{excluded_year}": None,
            f"decision_without_{excluded_year}": "insufficient_data",
        }
    compounded = 1.0
    for ret in kept_yearly.values():
        compounded *= 1.0 + safe_float(ret)
    ann = compounded ** (1.0 / len(kept_yearly)) - 1.0 if compounded > 0 else -1.0
    month_series = pd.Series(list(kept_monthly.values()), dtype="float64").dropna()
    if len(month_series) >= 2 and float(month_series.std(ddof=0)) >= 1e-12:
        shp = float(month_series.mean() / month_series.std(ddof=0) * math.sqrt(12))
    else:
        shp = None
    if ann >= 0.08 and safe_float(shp, -999) >= 0.55:
        decision = "still_effective"
    elif ann >= 0.05 or safe_float(shp, -999) >= 0.35:
        decision = "weakened_but_watchable"
    else:
        decision = "not_effective_without_2024"
    return {
        f"annualized_without_{excluded_year}": ann,
        f"sharpe_without_{excluded_year}": shp,
        f"decision_without_{excluded_year}": decision,
    }


def selection_concentration(selected_by_rebalance: list[dict[str, Any]]) -> dict[str, Any]:
    symbols: list[str] = []
    for item in selected_by_rebalance:
        symbols.extend([str(x) for x in item.get("symbols") or []])
    total_slots = len(symbols)
    if total_slots <= 0:
        return {
            "unique_symbol_count": 0,
            "max_single_symbol_slot_share": 0.0,
            "top_symbols": [],
            "over_concentrated": True,
        }
    counts: dict[str, int] = {}
    for sym in symbols:
        counts[sym] = counts.get(sym, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    max_share = top[0][1] / total_slots if top else 0.0
    return {
        "unique_symbol_count": len(counts),
        "max_single_symbol_slot_share": float(max_share),
        "top_symbols": [{"symbol": sym, "selection_count": count, "slot_share": count / total_slots} for sym, count in top],
        "over_concentrated": bool(max_share > 0.20 or len(counts) < 20),
    }


def delist_risk_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    slots = sum(int(e.get("selected_count") or 0) for e in events)
    price_hits = sum(int(e.get("price_lt_3_count") or 0) for e in events)
    ret_hits = sum(int(e.get("ret120_lt_minus_70_count") or 0) for e in events)
    either_hits = sum(int(e.get("either_risk_count") or 0) for e in events)
    return {
        "selected_slot_count": slots,
        "price_lt_3_count": price_hits,
        "ret120_lt_minus_70_count": ret_hits,
        "either_risk_count": either_hits,
        "price_lt_3_ratio": price_hits / slots if slots else None,
        "ret120_lt_minus_70_ratio": ret_hits / slots if slots else None,
        "delist_risk_hit_ratio": either_hits / slots if slots else None,
        "risk_proxy": "selected signal rows with close < 3 or ret_120d < -70%",
    }


def load_daily_data(data_root: Path, min_history_days: int, max_symbols: int = 0) -> tuple[pd.DataFrame, dict[str, Any]]:
    total = 0
    skipped: dict[str, int] = {}
    csv_read_warnings: list[str] = []
    frames: list[pd.DataFrame] = []
    paths, available_csv = selected_daily_paths(data_root, max_symbols)
    for path in paths:
        total += 1
        df_raw = read_csv_frame(path)
        read_warnings = [str(item) for item in df_raw.attrs.get("warnings", []) if item]
        csv_read_warnings.extend(read_warnings)
        if df_raw.empty and any("csv_read_failed" in item for item in read_warnings):
            skipped["read_error"] = skipped.get("read_error", 0) + 1
            continue
        cols = list(df_raw.columns)
        if any(any(ord(ch) > 127 for ch in col) for col in cols):
            skipped["chinese_or_non_ascii_schema"] = skipped.get("chinese_or_non_ascii_schema", 0) + 1
            continue
        date_col = "date" if "date" in cols else ("trade_date" if "trade_date" in cols else "")
        if not date_col:
            skipped["missing_date"] = skipped.get("missing_date", 0) + 1
            continue
        if "close" not in cols:
            skipped["missing_close"] = skipped.get("missing_close", 0) + 1
            continue
        usecols = [c for c in [date_col, "symbol", "open", "high", "low", "close", "volume", "vol", "amount"] if c in cols]
        df = df_raw[usecols].copy()
        if len(df) < min_history_days:
            skipped["too_few_rows"] = skipped.get("too_few_rows", 0) + 1
            continue
        df = df.rename(columns={date_col: "date", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        if df["date"].isna().all():
            skipped["invalid_date"] = skipped.get("invalid_date", 0) + 1
            continue
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        if df["close"].isna().all():
            skipped["invalid_close"] = skipped.get("invalid_close", 0) + 1
            continue
        if df["date"].duplicated().any():
            before = len(df)
            df = df.sort_values("date").drop_duplicates("date", keep="last")
            if len(df) < before * 0.95:
                skipped["duplicate_dates_unrepairable"] = skipped.get("duplicate_dates_unrepairable", 0) + 1
                continue
        for col in ["open", "high", "low", "volume", "amount"]:
            if col not in df.columns:
                df[col] = pd.NA
            df[col] = pd.to_numeric(df[col], errors="coerce")
        symbol = str(df["symbol"].dropna().iloc[0]) if "symbol" in df.columns and not df["symbol"].dropna().empty else path.stem
        df["symbol"] = symbol.zfill(6)
        df = df.dropna(subset=["date", "close"])
        df = df[df["close"] > 0]
        if len(df) < min_history_days:
            skipped["too_few_valid_rows"] = skipped.get("too_few_valid_rows", 0) + 1
            continue
        frames.append(df[["date", "symbol", "open", "high", "low", "close", "volume", "amount"]])
    panel = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]) if frames else pd.DataFrame()
    summary = {
        "total_csv": total,
        "daily_csv_available": available_csv,
        "scanned_csv": total,
        "max_symbols": positive_int(max_symbols, 0),
        "truncated_by_max_symbols": bool(max_symbols and available_csv > total),
        "usable_csv": int(panel["symbol"].nunique()) if not panel.empty else 0,
        "skipped_csv": total - (int(panel["symbol"].nunique()) if not panel.empty else 0),
        "skipped_reasons": skipped,
        "csv_read_warnings": list(dict.fromkeys(csv_read_warnings)),
        "min_date": str(panel["date"].min().date()) if not panel.empty else "",
        "max_date": str(panel["date"].max().date()) if not panel.empty else "",
        "survivorship_bias_risk": "high",
        "pit_quality": "none",
        "historical_st_available": False,
        "notes": [
            "This screening runner intentionally does not use formal PIT.",
            "This is for fast strategy screening only.",
            "Do not treat these results as production-ready.",
        ],
    }
    return panel.reset_index(drop=True), summary


def compute_factors(panel: pd.DataFrame) -> pd.DataFrame:
    frame = panel.copy().sort_values(["symbol", "date"])
    group = frame.groupby("symbol", group_keys=False)
    frame["daily_return"] = group["close"].transform(lambda values: values.ffill().pct_change(fill_method=None))
    for n in [5, 20, 40, 60, 120, 250]:
        frame[f"ret_{n}d"] = group["close"].transform(
            lambda values, period=n: values.ffill().pct_change(period, fill_method=None)
        )
    for n in [20, 60, 120]:
        frame[f"volatility_{n}d"] = group["daily_return"].rolling(n, min_periods=max(5, n // 2)).std().reset_index(level=0, drop=True)
    delta = group["close"].diff()
    gain = delta.clip(lower=0).groupby(frame["symbol"]).rolling(14, min_periods=7).mean().reset_index(level=0, drop=True)
    loss = (-delta.clip(upper=0)).groupby(frame["symbol"]).rolling(14, min_periods=7).mean().reset_index(level=0, drop=True)
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)
    frame["rsi_14"] = pd.to_numeric(rsi, errors="coerce").clip(0, 100)
    for n in [20, 60, 120]:
        frame[f"ma{n}"] = group["close"].rolling(n, min_periods=max(5, n // 2)).mean().reset_index(level=0, drop=True)
    frame["rolling_high_120"] = group["close"].rolling(120, min_periods=60).max().reset_index(level=0, drop=True)
    frame["bollinger_mid_20"] = frame["ma20"]
    frame["bollinger_std_20"] = group["close"].rolling(20, min_periods=10).std().reset_index(level=0, drop=True)
    frame["bollinger_upper_20_2"] = frame["bollinger_mid_20"] + frame["bollinger_std_20"] * 2
    frame["bollinger_lower_20_2"] = frame["bollinger_mid_20"] - frame["bollinger_std_20"] * 2
    for n in [5, 20, 60]:
        frame[f"volume_{n}d_avg"] = group["volume"].rolling(n, min_periods=max(3, n // 2)).mean().reset_index(level=0, drop=True)
    frame["avg_amount_20d"] = group["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    rolling_high = group["close"].rolling(120, min_periods=60).max().reset_index(level=0, drop=True)
    frame["max_drawdown_120d"] = frame["close"] / rolling_high - 1.0
    frame["history_days"] = group.cumcount() + 1
    return frame


def load_or_build_features(args: argparse.Namespace, data_root: Path) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    max_symbols = positive_int(getattr(args, "max_symbols", 0), 0)
    min_history_days = int(getattr(args, "min_history_days", 180) or 180)
    cache_enabled = truthy(getattr(args, "feature_cache", "false"))
    cache_dir = _feature_cache_directory(args)
    fingerprint, meta = feature_cache_fingerprint(data_root, min_history_days, max_symbols) if cache_enabled else ("", {})
    cache_path = cache_dir / f"strategy_screening_features_{fingerprint[:16]}.json"
    cache_info: dict[str, Any] = {
        "enabled": cache_enabled,
        "status": "disabled",
        "cache_key": fingerprint[:16],
        "path": str(cache_path.resolve()).replace("\\", "/"),
        "meta": {k: v for k, v in meta.items() if k != "entries"},
    }
    if cache_enabled and cache_path.exists():
        try:
            features, dq = _read_feature_cache(cache_path, fingerprint)
            cache_info["status"] = "hit"
            return features, dq, cache_info
        except Exception as exc:  # noqa: BLE001 - cache is an optimization only
            cache_info["status"] = "read_failed"
            cache_info["read_status"] = "read_failed_rebuilt_from_daily"
            cache_info["error"] = f"{type(exc).__name__}: {exc}"

    panel, dq = load_daily_data(data_root, min_history_days, max_symbols=max_symbols)
    if panel.empty:
        cache_info["status"] = "miss_empty" if cache_enabled else "disabled"
        return pd.DataFrame(), dq, cache_info
    features = compute_factors(panel)
    if cache_enabled:
        try:
            after, _ = feature_cache_fingerprint(data_root, min_history_days, max_symbols)
            if after != fingerprint:
                raise ValueError("daily_inputs_changed_during_feature_build_cache_not_written")
            _write_feature_cache(cache_path, fingerprint, features, dq)
            cache_info["status"] = "miss_written"
        except Exception as exc:  # noqa: BLE001 - screening can continue without cache writes
            cache_info["status"] = "write_failed"
            cache_info["error"] = f"{type(exc).__name__}: {exc}"
    return features, dq, cache_info


def rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").rank(pct=True, ascending=ascending)


def price_limit_for_strategy(strategy: str) -> float | None:
    if "_px_le_10" in strategy:
        return 10.0
    if "_px_le_15" in strategy:
        return 15.0
    return None


def score_candidates(cross: pd.DataFrame, hyp: Hypothesis, rng: random.Random, prior: set[str]) -> tuple[pd.DataFrame, list[str]]:
    c = cross.copy()
    s = hyp.strategy
    warnings: list[str] = []
    alias_target = STRATEGY_ALIASES.get(s)
    if alias_target:
        warnings.append(f"strategy_alias:{s}->{alias_target}")
        s = alias_target
    price_limit = price_limit_for_strategy(s)
    if price_limit is not None:
        c = c[pd.to_numeric(c["close"], errors="coerce") <= price_limit]
        warnings.append(f"survivorship_bias_risk_high_price_filter_px_le_{int(price_limit)}")
    if s == "equal_weight_random":
        c["score"] = [rng.random() for _ in range(len(c))]
    elif s == "equal_weight_low_turnover":
        c["score"] = c["symbol"].astype(str).map(lambda x: 1.0 if x in prior else rng.random() * 0.01)
    elif s.startswith("reversal_20"):
        c = c.dropna(subset=["ret_20d"]); c["score"] = rank(c["ret_20d"], ascending=False)
    elif s.startswith("reversal_40"):
        c = c.dropna(subset=["ret_40d"]); c["score"] = rank(c["ret_40d"], ascending=False)
    elif s.startswith("reversal_60_top"):
        c = c.dropna(subset=["ret_60d"]); c["score"] = rank(c["ret_60d"], ascending=False)
    elif s == "rev120_xvol20_xc3_xr70":
        c = c.dropna(subset=["close", "ret_120d", "volatility_20d"])
        c = c[(c["close"] <= 15.0) & (c["close"] >= 3.0) & (c["ret_120d"] >= -0.70)]
        if not c.empty:
            vol_cutoff = c["volatility_20d"].quantile(0.80)
            c = c[c["volatility_20d"] <= vol_cutoff]
        c["score"] = rank(c["ret_120d"], ascending=False)
        warnings.append("survivorship_bias_pressure_filter_px_ge_3_ret120_ge_minus_70_exclude_xvol20")
    elif s.startswith("reversal_120"):
        c = c.dropna(subset=["ret_120d"]); c["score"] = rank(c["ret_120d"], ascending=False)
    elif s == "rsi_reversal":
        c = c.dropna(subset=["rsi_14"]); c = c[c["rsi_14"] < 35]; c["score"] = rank(c["rsi_14"], ascending=True)
    elif s == "bollinger_reversal":
        c = c.dropna(subset=["close", "bollinger_lower_20_2"]); c = c[c["close"] < c["bollinger_lower_20_2"]]; c["score"] = rank((c["bollinger_lower_20_2"] - c["close"]) / c["close"], ascending=True)
    elif s == "crash_rebound":
        c = c.dropna(subset=["ret_60d", "ret_5d"]); c = c[(c["ret_60d"] < -0.20) & (c["ret_5d"] > 0)]; c["score"] = rank(c["ret_60d"], True) + rank(c["ret_5d"], True)
    elif s.startswith("momentum_20"):
        c = c.dropna(subset=["ret_20d"]); c["score"] = rank(c["ret_20d"], True)
    elif s.startswith("momentum_60"):
        c = c.dropna(subset=["ret_60d"]); c["score"] = rank(c["ret_60d"], True)
    elif s.startswith("momentum_120"):
        c = c.dropna(subset=["ret_120d"]); c["score"] = rank(c["ret_120d"], True)
    elif s.startswith("momentum_250"):
        c = c.dropna(subset=["ret_250d"]); c["score"] = rank(c["ret_250d"], True)
    elif s == "ma_breakout":
        c = c.dropna(subset=["close", "ma20", "volume_5d_avg", "volume_20d_avg", "ret_60d"])
        c = c[
            (c["close"] > c["ma20"])
            & (c["volume_20d_avg"] > 0)
            & (c["volume_5d_avg"] > c["volume_20d_avg"] * 1.5)
        ]
        c["score"] = rank(c["ret_60d"], True) + rank(c["volume_5d_avg"] / c["volume_20d_avg"], True)
    elif s == "breakout_120":
        c = c.dropna(subset=["close", "rolling_high_120", "ret_60d"]); c = c[c["close"] >= c["rolling_high_120"] * 0.98]; c["score"] = rank(c["ret_60d"], True)
    elif s == "low_volatility_20":
        c = c.dropna(subset=["volatility_20d"]); c["score"] = rank(c["volatility_20d"], ascending=False)
    elif s == "low_volatility_60":
        c = c.dropna(subset=["volatility_60d"]); c["score"] = rank(c["volatility_60d"], ascending=False)
    elif s == "low_volatility_120":
        c = c.dropna(subset=["volatility_120d"]); c["score"] = rank(c["volatility_120d"], ascending=False)
    elif s == "low_drawdown_120":
        c = c.dropna(subset=["max_drawdown_120d"]); c["score"] = rank(c["max_drawdown_120d"], True)
    elif s == "stable_uptrend":
        c = c.dropna(subset=["ret_120d", "volatility_60d"]); c = c[c["ret_120d"] > 0]; c["score"] = rank(c["ret_120d"], True) - rank(c["volatility_60d"], True)
    elif s == "volume_surge":
        c = c.dropna(subset=["volume_5d_avg", "volume_20d_avg", "ret_5d"]); c = c[(c["volume_5d_avg"] > c["volume_20d_avg"] * 1.5) & (c["ret_5d"] > 0)]; c["score"] = rank(c["volume_5d_avg"] / c["volume_20d_avg"], True) + rank(c["ret_5d"], True)
    elif s == "volume_dry_rebound":
        c = c.dropna(subset=["volume_20d_avg", "volume_60d_avg", "ret_5d", "volatility_20d"]); c = c[(c["volume_20d_avg"] < c["volume_60d_avg"]) & (c["ret_5d"] > 0)]; c["score"] = rank(c["ret_5d"], True) - rank(c["volatility_20d"], True)
    elif s == "liquidity_quality":
        c = c.dropna(subset=["avg_amount_20d", "volatility_60d"]); c["score"] = rank(c["avg_amount_20d"], True) - rank(c["volatility_60d"], True)
    elif s == "reversal_60_lowvol":
        c = c.dropna(subset=["ret_60d", "volatility_60d"]); c["score"] = -0.7 * rank(c["ret_60d"], True) - 0.3 * rank(c["volatility_60d"], True)
    elif s == "reversal_60_liquidity":
        c = c.dropna(subset=["ret_60d", "avg_amount_20d"]); c["score"] = -0.7 * rank(c["ret_60d"], True) + 0.3 * rank(c["avg_amount_20d"], True)
    elif s == "reversal_60_low_drawdown":
        c = c.dropna(subset=["ret_60d", "max_drawdown_120d"]); c["score"] = -0.6 * rank(c["ret_60d"], True) - 0.4 * rank(c["max_drawdown_120d"], True)
    elif s == "trend_lowvol":
        c = c.dropna(subset=["ret_120d", "ma20", "ma60", "volatility_60d"]); c = c[(c["ret_120d"] > 0) & (c["ma20"] > c["ma60"])]; c["score"] = rank(c["ret_120d"], True) - rank(c["volatility_60d"], True)
    elif s == "breakout_volume":
        c = c.dropna(subset=["close", "rolling_high_120", "volume_5d_avg", "volume_20d_avg", "ret_60d"]); c = c[(c["close"] >= c["rolling_high_120"] * 0.98) & (c["volume_5d_avg"] > c["volume_20d_avg"])]; c["score"] = rank(c["ret_60d"], True) + rank(c["volume_5d_avg"] / c["volume_20d_avg"], True)
    else:
        warnings.append("unknown_strategy")
        c = c.iloc[0:0].copy()
    c = c.dropna(subset=["score"]) if "score" in c.columns else c.iloc[0:0]
    return c.sort_values("score", ascending=False), warnings


def monthly_signal_dates(dates: list[pd.Timestamp]) -> list[pd.Timestamp]:
    frame = pd.DataFrame({"date": sorted(pd.to_datetime(dates))})
    frame["month"] = frame["date"].dt.to_period("M")
    return list(frame.groupby("month")["date"].max())


def screening_trade_cost(
    side: str,
    *,
    gross: float,
    commission_rate: float,
    stamp_tax_rate: float,
    slippage_bps: float,
) -> float:
    costs = compute_trade_costs(
        side=side,
        gross=gross,
        config=TradeCostConfig(
            commission_rate=max(float(commission_rate or 0.0), 0.0),
            stamp_tax_rate=max(float(stamp_tax_rate or 0.0), 0.0),
            slippage_bps=max(float(slippage_bps or 0.0), 0.0),
            apply_min_commission=max(float(commission_rate or 0.0), 0.0) > 0,
        ),
    )
    return float(costs["total_fee"])


def run_strategy(
    features: pd.DataFrame,
    close_wide: pd.DataFrame,
    open_wide: pd.DataFrame,
    hyp: Hypothesis,
    *,
    initial_cash: float,
    top_n_default: int,
    lot_size: int,
    commission_rate: float,
    stamp_tax_rate: float,
    slippage_bps: float,
    min_history_days: int,
    min_avg_amount_20d: float,
) -> dict[str, Any]:
    rng = random.Random(RANDOM_SEED)
    warnings: list[str] = []
    dates = list(close_wide.index)
    signals = monthly_signal_dates(dates)
    cash = initial_cash
    holdings: dict[str, int] = {}
    equity_rows: list[dict[str, Any]] = []
    selected_by_rebalance: list[dict[str, Any]] = []
    delist_risk_events: list[dict[str, Any]] = []
    turnovers: list[float] = []
    invested_ratios: list[float] = []
    unfilled = 0
    trade_count = 0
    cost_total = 0.0
    prior_set: set[str] = set()
    top_n = hyp.top_n or top_n_default
    by_date = {d: g for d, g in features.groupby("date")}
    for idx, signal_date in enumerate(signals[:-1]):
        execution_candidates = [d for d in dates if d > signal_date]
        if not execution_candidates:
            continue
        execution_date = execution_candidates[0]
        next_execution_candidates = [d for d in dates if d > signals[idx + 1]]
        end_date = next_execution_candidates[0] if next_execution_candidates else dates[-1]
        if not signal_date < execution_date:
            warnings.append("signal_date_not_before_execution_date")
            continue
        cross = by_date.get(signal_date)
        if cross is None or cross.empty:
            continue
        cross = cross[(cross["history_days"] >= min_history_days) & (cross["avg_amount_20d"] >= min_avg_amount_20d)]
        cross = cross[pd.to_numeric(cross["volume"], errors="coerce").fillna(0) > 0]
        scored, score_warnings = score_candidates(cross, hyp, rng, prior_set)
        warnings.extend(score_warnings)
        selected_frame = scored.head(top_n).copy()
        selected = selected_frame["symbol"].astype(str).tolist()
        if selected:
            close_vals = pd.to_numeric(selected_frame["close"], errors="coerce")
            ret_vals = pd.to_numeric(selected_frame["ret_120d"], errors="coerce")
            price_risk = close_vals < 3.0
            ret_risk = ret_vals < -0.70
            delist_risk_events.append({
                "signal_date": str(signal_date.date()),
                "selected_count": len(selected),
                "price_lt_3_count": int(price_risk.sum()),
                "ret120_lt_minus_70_count": int(ret_risk.sum()),
                "either_risk_count": int((price_risk | ret_risk).sum()),
            })
        selected_by_rebalance.append({"signal_date": str(signal_date.date()), "execution_date": str(execution_date.date()), "symbols": selected})
        if not selected:
            continue
        # Liquidate at execution open if available; otherwise close.
        if holdings:
            for sym, shares in list(holdings.items()):
                price = safe_float(open_wide.at[execution_date, sym] if sym in open_wide.columns else 0.0)
                if price <= 0:
                    price = safe_float(close_wide.at[execution_date, sym] if sym in close_wide.columns else 0.0)
                    warnings.append("sell_open_missing_used_close")
                if price > 0:
                    proceeds = shares * price
                    cost = screening_trade_cost(
                        "sell",
                        gross=proceeds,
                        commission_rate=commission_rate,
                        stamp_tax_rate=stamp_tax_rate,
                        slippage_bps=slippage_bps,
                    )
                    cash += proceeds - cost
                    cost_total += cost
                    trade_count += 1
            holdings = {}
        target_cash = cash / max(1, len(selected))
        for sym in selected:
            price = safe_float(open_wide.at[execution_date, sym] if sym in open_wide.columns else 0.0)
            if price <= 0:
                price = safe_float(close_wide.at[execution_date, sym] if sym in close_wide.columns else 0.0)
                warnings.append("buy_open_missing_used_close")
            if price <= 0:
                continue
            shares = round_notional_down_to_lot_quantity(target_cash, price, lot_size=lot_size)
            if shares < lot_size or shares * price > cash:
                unfilled += 1
                continue
            value = shares * price
            cost = screening_trade_cost(
                "buy",
                gross=value,
                commission_rate=commission_rate,
                stamp_tax_rate=stamp_tax_rate,
                slippage_bps=slippage_bps,
            )
            cash -= value + cost
            cost_total += cost
            holdings[sym] = shares
            trade_count += 1
        prior_set = set(holdings)
        period_dates = [d for d in dates if execution_date <= d < end_date]
        start_value = cash + sum(sh * safe_float(close_wide.at[execution_date, sym] if sym in close_wide.columns else 0) for sym, sh in holdings.items())
        prev_value = None
        for d in period_dates:
            value = cash
            for sym, shares in holdings.items():
                px = safe_float(close_wide.at[d, sym] if sym in close_wide.columns else 0.0)
                if px <= 0:
                    warnings.append("missing_close_in_holding")
                    continue
                value += shares * px
            equity_rows.append({"date": str(d.date()), "equity": value})
            prev_value = value
        if start_value > 0 and equity_rows:
            invested = max(0.0, (equity_rows[-1]["equity"] - cash) / equity_rows[-1]["equity"])
            invested_ratios.append(min(1.0, invested))
            if prior_set:
                turnovers.append(1.0)
    if not equity_rows:
        return invalid_result(hyp, "empty_equity_curve", warnings, selected_by_rebalance)
    equity_values = [float(r["equity"]) for r in equity_rows]
    equity_dates = [r["date"] for r in equity_rows]
    daily_rets = pd.Series(equity_values).ffill().pct_change(fill_method=None).dropna().tolist()
    yearly = yearly_returns_from_equity(equity_rows)
    monthly = monthly_returns_from_equity(equity_rows)
    ann_net = annualized_return(equity_values, equity_dates)
    gross_equity_end = equity_values[-1] + cost_total
    ann_gross = annualized_return([equity_values[0], gross_equity_end], [equity_dates[0], equity_dates[-1]])
    dd = max_drawdown(equity_values)
    shp = sharpe_ratio(daily_rets)
    sortino = sortino_ratio(daily_rets)
    calmar = ann_net / abs(dd) if dd < 0 else None
    pos_month = sum(1 for v in monthly.values() if v > 0)
    neg_month = sum(1 for v in monthly.values() if v < 0)
    pos_year = sum(1 for v in yearly.values() if v > 0)
    neg_year = sum(1 for v in yearly.values() if v < 0)
    if shp is not None and abs(shp) > 10:
        warnings.append("suspicious_sharpe_ratio")
    result = {
        "strategy": hyp.strategy,
        "family": hyp.family,
        "status": "unclassified",
        "annualized_return_net": ann_net,
        "annualized_return_gross": ann_gross,
        "final_nav": equity_values[-1] / initial_cash,
        "max_drawdown": dd,
        "sharpe": shp,
        "sortino": sortino,
        "calmar": calmar,
        "turnover_annualized": float(pd.Series(turnovers).mean() * 12.0) if turnovers else 0.0,
        "trade_count": trade_count,
        "win_rate_monthly": pos_month / max(1, pos_month + neg_month),
        "positive_month_count": pos_month,
        "negative_month_count": neg_month,
        "positive_year_count": pos_year,
        "negative_year_count": neg_year,
        "best_year": max(yearly, key=yearly.get) if yearly else "",
        "worst_year": min(yearly, key=yearly.get) if yearly else "",
        "yearly_returns": yearly,
        "return_2024": yearly.get("2024"),
        **metrics_without_year(equity_rows, 2024),
        "monthly_returns": monthly,
        "invested_ratio_avg": float(pd.Series(invested_ratios).mean()) if invested_ratios else 0.0,
        "unfilled_due_to_lot_size_count": unfilled,
        "cost_impact": cost_total / initial_cash,
        "selected_symbols_hash": stable_hash(selected_by_rebalance),
        "equity_curve_hash": stable_hash(equity_rows),
        "selected_symbols_by_rebalance_sample": selected_by_rebalance[:6],
        "latest_selected_symbols": latest_selected_symbols(selected_by_rebalance),
        "latest_selection_context": latest_selection_context(selected_by_rebalance),
        "selection_concentration": selection_concentration(selected_by_rebalance),
        "delist_risk_summary": delist_risk_summary(delist_risk_events),
        "delist_risk_events_sample": delist_risk_events[:12],
        "factor_coverage_summary": {"rebalance_count": len(selected_by_rebalance), "non_empty_selection_count": sum(1 for x in selected_by_rebalance if x["symbols"])},
        "warnings": list(dict.fromkeys(warnings))[:50],
        "reject_reasons": [],
        "watch_reasons": [],
        "candidate_reasons": [],
        "_equity_rows": equity_rows,
    }
    return result


def invalid_result(hyp: Hypothesis, reason: str, warnings: list[str], selected: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "strategy": hyp.strategy,
        "family": hyp.family,
        "status": "invalid",
        "annualized_return_net": None,
        "annualized_return_gross": None,
        "final_nav": None,
        "max_drawdown": None,
        "sharpe": None,
        "sortino": None,
        "calmar": None,
        "benchmark_return": None,
        "benchmark_annualized_return": None,
        "excess_return": None,
        "information_ratio": None,
        "turnover_annualized": 0.0,
        "trade_count": 0,
        "win_rate_monthly": 0.0,
        "positive_month_count": 0,
        "negative_month_count": 0,
        "positive_year_count": 0,
        "negative_year_count": 0,
        "best_year": "",
        "worst_year": "",
        "yearly_returns": {},
        "return_2024": None,
        "annualized_without_2024": None,
        "sharpe_without_2024": None,
        "decision_without_2024": "invalid",
        "monthly_returns": {},
        "invested_ratio_avg": 0.0,
        "unfilled_due_to_lot_size_count": 0,
        "cost_impact": 0.0,
        "selected_symbols_hash": stable_hash(selected),
        "equity_curve_hash": "",
        "selected_symbols_by_rebalance_sample": selected[:6],
        "latest_selected_symbols": latest_selected_symbols(selected),
        "latest_selection_context": latest_selection_context(selected),
        "selection_concentration": selection_concentration(selected),
        "delist_risk_summary": delist_risk_summary([]),
        "delist_risk_events_sample": [],
        "factor_coverage_summary": {"rebalance_count": len(selected), "non_empty_selection_count": 0},
        "warnings": list(dict.fromkeys(warnings)),
        "reject_reasons": [],
        "watch_reasons": [],
        "candidate_reasons": [],
        "invalid_reasons": [reason],
    }


def load_benchmark(symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    candidates = [
        ROOT / "data" / "daily" / f"{symbol}.csv",
        ROOT / "data" / "daily" / "000852.csv",
        ROOT / "data" / "daily" / "000300.csv",
        ROOT / "data" / "index" / f"{symbol}.csv",
        ROOT / "data" / "index" / "000852.csv",
        ROOT / "data" / "index" / "000300.csv",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            df = read_csv_frame(path)
            csv_read_warnings = [str(item) for item in df.attrs.get("warnings", []) if item]
            date_col = "date" if "date" in df.columns else ("trade_date" if "trade_date" in df.columns else "")
            if not date_col or "close" not in df.columns:
                continue
            df = df.rename(columns={date_col: "date"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df.dropna(subset=["date", "close"]).sort_values("date")
            if len(df) >= 2:
                return df[["date", "close"]], {"benchmark_symbol": path.stem, "benchmark_available": True, "warnings": csv_read_warnings}
        except Exception:
            continue
    return pd.DataFrame(), {"benchmark_symbol": symbol, "benchmark_available": False, "warnings": ["benchmark_missing"]}


def benchmark_metrics(benchmark: pd.DataFrame, equity_dates: list[str]) -> dict[str, Any]:
    if benchmark.empty or not equity_dates:
        return {"benchmark_return": None, "benchmark_annualized_return": None, "benchmark_yearly_returns": {}, "benchmark_daily_returns": []}
    start = pd.Timestamp(equity_dates[0])
    end = pd.Timestamp(equity_dates[-1])
    b = benchmark[(benchmark["date"] >= start) & (benchmark["date"] <= end)].copy()
    if len(b) < 2:
        return {"benchmark_return": None, "benchmark_annualized_return": None, "benchmark_yearly_returns": {}, "benchmark_daily_returns": []}
    total = float(b["close"].iloc[-1] / b["close"].iloc[0] - 1.0)
    ann = annualized_return([float(b["close"].iloc[0]), float(b["close"].iloc[-1])], [b["date"].iloc[0], b["date"].iloc[-1]])
    rows = [{"date": str(r.date.date()), "equity": float(r.close)} for r in b.itertuples()]
    return {
        "benchmark_return": total,
        "benchmark_annualized_return": ann,
        "benchmark_yearly_returns": yearly_returns_from_equity(rows),
        "benchmark_daily_returns": b["close"].ffill().pct_change(fill_method=None).dropna().tolist(),
    }


def classify_results(results: list[dict[str, Any]], benchmark_available: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[list[str]]]:
    equity_groups: dict[str, list[str]] = {}
    for r in results:
        if r.get("equity_curve_hash"):
            equity_groups.setdefault(str(r["equity_curve_hash"]), []).append(r["strategy"])
    duplicated = [v for v in equity_groups.values() if len(v) > 1]
    duplicate_names = {name for group in duplicated for name in group}
    candidate, watch, rejected, invalid = [], [], [], []
    for r in results:
        if r.get("status") == "invalid":
            invalid.append(r); continue
        ann = safe_float(r.get("annualized_return_net"))
        shp = safe_float(r.get("sharpe"), -999)
        dd = safe_float(r.get("max_drawdown"))
        excess = r.get("excess_return")
        invested = safe_float(r.get("invested_ratio_avg"))
        years = r.get("yearly_returns") or {}
        pos_year = int(r.get("positive_year_count") or 0)
        reasons: list[str] = []
        if r["strategy"] in duplicate_names:
            reasons.append("duplicated_equity_curve")
        if r.get("trade_count", 0) < 3:
            r["invalid_reasons"] = ["too_few_trades"]
            r["status"] = "invalid"; invalid.append(r); continue
        if r["strategy"].startswith("reversal_120") and r["strategy"] != "rev120_xvol20_xc3_xr70":
            r["survivorship_bias_classification"] = "survivorship_bias_severe"
            r.setdefault("warnings", []).append("survivorship_bias_severe_not_candidate")
        if r["strategy"] == "volume_dry_rebound":
            r["status"] = "watch"
            r["disputed_status"] = "disputed_needs_redefinition_before_candidate"
            r["watch_reasons"] = ["disputed_needs_redefinition_before_candidate"]
            r["required_improvements"] = ["redefine_signal", "rerun_delist_risk_pressure_test", "benchmark_against_rev120_xvol20_xc3_xr70"]
            watch.append(r)
            continue
        candidate_ok = (
            ann >= 0.08 and shp >= 0.55 and dd > -0.40 and
            (not benchmark_available or excess is None or excess > 0) and
            r["strategy"] not in duplicate_names and invested >= 0.70 and
            pos_year >= max(1, math.ceil(len(years) * 0.5))
        )
        if r["strategy"] == "rev120_xvol20_xc3_xr70":
            risk_ratio = (r.get("delist_risk_summary") or {}).get("delist_risk_hit_ratio")
            candidate_ok = (
                ann >= 0.12 and shp >= 0.55 and risk_ratio == 0 and
                (not benchmark_available or excess is None or excess > 0) and invested >= 0.70
            )
        if r["strategy"].startswith("reversal_120") and r["strategy"] != "rev120_xvol20_xc3_xr70":
            candidate_ok = False
        if candidate_ok:
            r["status"] = "candidate"
            r["candidate_reasons"] = ["passed_return_sharpe_drawdown_excess_investment_and_year_stability_filters"]
            candidate.append(r)
        elif ann >= 0.08 or 0.40 <= shp < 0.55 or (excess is not None and excess > 0.05):
            r["status"] = "watch"
            wr = []
            if dd <= -0.40: wr.append("reduce_drawdown")
            if shp < 0.55: wr.append("improve_sharpe")
            if invested < 0.70: wr.append("improve_capital_deployment")
            if r["strategy"] in duplicate_names: wr.append("resolve_duplicate_equity_curve")
            r["watch_reasons"] = wr or ["potential_signal_needs_risk_filter"]
            r["required_improvements"] = suggested_improvements(r)
            watch.append(r)
        else:
            r["status"] = "rejected"
            rr = []
            if ann < 0.05: rr.append("annualized_return_net_below_5pct")
            if shp < 0.35: rr.append("sharpe_below_0_35")
            if dd <= -0.60: rr.append("max_drawdown_at_or_below_minus_60pct")
            if excess is not None and excess < 0: rr.append("underperformed_benchmark")
            if r["strategy"] in duplicate_names: rr.append("duplicated_equity_curve_no_independent_value")
            r["reject_reasons"] = rr or ["weak_screening_result"]
            rejected.append(r)
    return candidate, watch, rejected, invalid, duplicated


def suggested_improvements(row: dict[str, Any]) -> list[str]:
    s = row.get("strategy", "")
    if "reversal_60" in s:
        return ["test_top_n_10_20_30", "test_lookback_40_60_90", "add_volatility_exclusion", "add_liquidity_filter_50m", "test_benchmark_ma120_timing"]
    if "low_volatility" in s:
        return ["test_top_n_20_30_50", "combine_with_positive_ret_120d", "test_monthly_vs_biweekly"]
    if "breakout" in s:
        return ["test_breakout_window_60_120_250", "test_volume_surge_threshold_1_2_1_5_2_0"]
    return ["parameter_refinement", "risk_filter_test", "benchmark_robustness_check"]


def family_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for family, group in pd.DataFrame([{k: r.get(k) for k in ["family", "strategy", "status", "annualized_return_net", "sharpe", "max_drawdown"]} for r in results]).groupby("family"):
        records = group.to_dict("records")
        valid = [r for r in records if r.get("annualized_return_net") is not None]
        best = max(valid, key=lambda r: safe_float(r.get("annualized_return_net"), -999), default={})
        out[str(family)] = {
            "best_strategy": best.get("strategy", ""),
            "avg_return": float(pd.to_numeric(group["annualized_return_net"], errors="coerce").mean()) if valid else None,
            "avg_sharpe": float(pd.to_numeric(group["sharpe"], errors="coerce").mean()) if valid else None,
            "avg_drawdown": float(pd.to_numeric(group["max_drawdown"], errors="coerce").mean()) if valid else None,
            "candidate_count": int((group["status"] == "candidate").sum()),
            "rejected_count": int((group["status"] == "rejected").sum()),
        }
    return out


def latest_selection_context(selected_by_rebalance: list[dict[str, Any]]) -> dict[str, Any]:
    for item in reversed(selected_by_rebalance or []):
        symbols = [str(symbol) for symbol in (item.get("symbols") or []) if str(symbol).strip()]
        if symbols:
            return {
                "signal_date": item.get("signal_date") or "",
                "execution_date": item.get("execution_date") or "",
                "symbols": list(dict.fromkeys(symbols)),
                "symbol_count": len(list(dict.fromkeys(symbols))),
            }
    return {"signal_date": "", "execution_date": "", "symbols": [], "symbol_count": 0}


def latest_selected_symbols(selected_by_rebalance: list[dict[str, Any]]) -> list[str]:
    return list(latest_selection_context(selected_by_rebalance).get("symbols") or [])


def _handoff_source_rows(
    candidate: list[dict[str, Any]],
    watch: list[dict[str, Any]],
    leaderboard: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    if candidate:
        return "candidate_pool", candidate
    if watch:
        return "watchlist", watch
    return "leaderboard", [row for row in leaderboard if row.get("latest_selected_symbols")]


def build_backtest_handoff(
    *,
    args: argparse.Namespace,
    data_root: Path,
    candidate: list[dict[str, Any]],
    watch: list[dict[str, Any]],
    leaderboard: list[dict[str, Any]],
) -> dict[str, Any]:
    source, rows = _handoff_source_rows(candidate, watch, leaderboard)
    selected = next((row for row in rows if row.get("latest_selected_symbols")), {})
    symbols = [str(symbol) for symbol in (selected.get("latest_selected_symbols") or []) if str(symbol).strip()]
    symbols = list(dict.fromkeys(symbols))[:50]
    strategy = str(selected.get("strategy") or "")
    ready = bool(symbols and strategy)
    symbol_arg = ",".join(symbols)
    command = [
        "python",
        "scripts/qd.py",
        "backtest",
        "--scope",
        "specified_symbols",
        "--symbols",
        symbol_arg,
        "--strategy",
        strategy,
        "--mode",
        "portfolio",
        "--network",
        "false",
        "--json",
        "true",
        "--summary",
        "true",
    ] if ready else []
    return {
        "contract_version": "backtest_handoff_v1",
        "status": "ready" if ready else "unavailable",
        "source": source,
        "strategy": strategy,
        "family": selected.get("family") or "",
        "symbols": symbols,
        "symbol_count": len(symbols),
        "selection_context": selected.get("latest_selection_context") or {},
        "data_root": str(data_root.resolve()).replace("\\", "/"),
        "backtest_scope": "specified_symbols",
        "backtest_mode": "portfolio",
        "command": command,
        "command_text": " ".join(command),
        "reason": "Use the latest non-empty screening selection as the bounded local backtest universe." if ready else "No non-empty screening selection is available.",
        "research_only": True,
        "paper_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "not_investment_advice": True,
        "not_position_advice": True,
    }


def run_screening(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    data_root = Path(args.data_root)
    if not data_root.is_absolute():
        data_root = ROOT / data_root
    features, dq, cache_info = load_or_build_features(args, data_root)
    start = str(getattr(args, "start_date", "") or "")
    end = str(getattr(args, "end_date", "") or "")
    if start and end and pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError("screening_start_date_after_end_date")
    if not features.empty:
        if start:
            features = features[features["date"] >= pd.Timestamp(start)]
        if end:
            features = features[features["date"] <= pd.Timestamp(end)]
    sample_context = research_sample_context({"sample_role": "in_sample", "sample_window": {
        "requested_start": start, "requested_end": end,
        "observed_start": str(features["date"].min().date()) if not features.empty else "",
        "observed_end": str(features["date"].max().date()) if not features.empty else "",
    }})
    warnings = [sample_context["sample_limitation"]]
    if features.empty:
        return {
            "ok": False,
            "status": "failed",
            "task_type": "strategy_screening_funnel",
            **sample_context,
            "data_quality_summary": dq,
            "performance": {
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "feature_cache": cache_info,
            },
            "warnings": [*warnings, "no_usable_csv_in_requested_window"],
        }
    close_wide = features.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
    open_wide = features.pivot(index="date", columns="symbol", values="open").sort_index()
    bench_df, bench_info = load_benchmark(args.benchmark)
    warnings.extend(bench_info.get("warnings") or [])
    hypothesis_pool = MIMO_RETEST_HYPOTHESES if truthy(getattr(args, "mimo_retest", "false")) else HYPOTHESES
    hyps = hypothesis_pool[: int(args.max_strategies)] if int(args.max_strategies or 0) > 0 else hypothesis_pool
    results: list[dict[str, Any]] = []
    for hyp in hyps:
        r = run_strategy(
            features,
            close_wide,
            open_wide,
            hyp,
            initial_cash=float(args.initial_cash),
            top_n_default=int(args.top_n_default),
            lot_size=int(args.lot_size),
            commission_rate=float(args.commission_rate),
            stamp_tax_rate=float(args.stamp_tax_rate),
            slippage_bps=float(args.slippage_bps),
            min_history_days=int(args.min_history_days),
            min_avg_amount_20d=float(args.min_avg_amount_20d),
        )
        bm = benchmark_metrics(bench_df, [x["date"] for x in r.get("_equity_rows", [])])
        r["benchmark_symbol"] = bench_info.get("benchmark_symbol")
        r["benchmark_available"] = bench_info.get("benchmark_available")
        r["benchmark_return"] = bm.get("benchmark_return")
        r["benchmark_annualized_return"] = bm.get("benchmark_annualized_return")
        r["benchmark_yearly_returns"] = bm.get("benchmark_yearly_returns")
        r["excess_return"] = None if bm.get("benchmark_annualized_return") is None or r.get("annualized_return_net") is None else safe_float(r.get("annualized_return_net")) - safe_float(bm.get("benchmark_annualized_return"))
        bench_rets = bm.get("benchmark_daily_returns") or []
        if r.get("_equity_rows") and bench_rets:
            strat_rets = pd.Series([x["equity"] for x in r["_equity_rows"]]).ffill().pct_change(fill_method=None).dropna()
            diff = strat_rets.iloc[-min(len(strat_rets), len(bench_rets)):].reset_index(drop=True) - pd.Series(bench_rets[-min(len(strat_rets), len(bench_rets)):]).reset_index(drop=True)
            r["information_ratio"] = None if diff.std(ddof=0) < 1e-12 else float(diff.mean() / diff.std(ddof=0) * math.sqrt(252))
        else:
            r["information_ratio"] = None
        r.pop("_equity_rows", None)
        r.update(sample_context)
        results.append(r)
    candidate, watch, rejected, invalid, duplicated = classify_results(results, bool(bench_info.get("benchmark_available")))
    if len([g for g in duplicated if len(g) >= 5]) > 0:
        decision = "screening_failed"
        status = "failed"
        warnings.append("strategy_scoring_degenerated")
    elif candidate:
        decision = "continue_with_candidates"
        status = "passed"
    elif watch:
        decision = "refine_watchlist"
        status = "warning"
    elif dq["usable_csv"] < 100 or not bench_info.get("benchmark_available"):
        decision = "data_quality_blocks_conclusion"
        status = "warning"
    else:
        decision = "switch_factor_direction"
        status = "warning"
    leaderboard = sorted(results, key=lambda r: safe_float(r.get("annualized_return_net"), -999), reverse=True)
    task_type = "strategy_screening_funnel_mimo_retest" if truthy(getattr(args, "mimo_retest", "false")) else "strategy_screening_funnel"
    payload = {
        "ok": status != "failed",
        "status": status,
        "task_type": task_type,
        **sample_context,
        "data_quality_summary": dq,
        "performance": {
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "feature_cache": cache_info,
            "max_symbols": positive_int(getattr(args, "max_symbols", 0), 0),
            "strategies_tested": len(results),
        },
        "benchmark_summary": {
            **bench_info,
            **benchmark_metrics(bench_df, [sample_context["sample_window"]["observed_start"], sample_context["sample_window"]["observed_end"]]),
        },
        "experiment_setup": {
            "initial_cash": float(args.initial_cash),
            "rebalance": "monthly",
            "top_n_default": int(args.top_n_default),
            "lot_size": int(args.lot_size),
            "commission_rate": float(args.commission_rate),
            "stamp_tax_rate": float(args.stamp_tax_rate),
            "slippage_bps": float(args.slippage_bps),
            "min_history_days": int(args.min_history_days),
            "min_avg_amount_20d": float(args.min_avg_amount_20d),
            "price_filter": "none",
            "random_seed": RANDOM_SEED,
            "hypothesis_mode": "mimo_retest" if truthy(getattr(args, "mimo_retest", "false")) else "default_screening",
            "max_symbols": positive_int(getattr(args, "max_symbols", 0), 0),
            "feature_cache_enabled": truthy(getattr(args, "feature_cache", "false")),
        },
        "strategy_results": results,
        "leaderboard": leaderboard,
        "backtest_handoff": build_backtest_handoff(
            args=args,
            data_root=data_root,
            candidate=candidate,
            watch=watch,
            leaderboard=leaderboard,
        ),
        "candidate_pool": [{"strategy": r["strategy"], "reason": "; ".join(r.get("candidate_reasons") or []), "next_actions": suggested_improvements(r), "metrics": compact_metrics(r)} for r in candidate],
        "watchlist": [{"strategy": r["strategy"], "watch_reasons": r.get("watch_reasons"), "required_improvements": r.get("required_improvements") or suggested_improvements(r), "metrics": compact_metrics(r)} for r in watch],
        "rejected_pool": [{"strategy": r["strategy"], "status": "rejected", "reason": "; ".join(r.get("reject_reasons") or []), "metrics": compact_metrics(r)} for r in rejected],
        "invalid_pool": [{"strategy": r["strategy"], "reason": "; ".join(r.get("invalid_reasons") or [])} for r in invalid],
        "duplicated_strategy_groups": duplicated,
        "family_summary": family_summary(results),
        "mimo_retest_checks": mimo_retest_checks(results) if truthy(getattr(args, "mimo_retest", "false")) else {},
        "decision": decision,
        "next_recommended_actions": next_actions(decision),
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }
    return json_safe(payload)


def compact_metrics(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r.get(k) for k in ["annualized_return_net", "max_drawdown", "sharpe", "excess_return", "final_nav", "turnover_annualized", "invested_ratio_avg"]}
    out["delist_risk_hit_ratio"] = (r.get("delist_risk_summary") or {}).get("delist_risk_hit_ratio")
    out["survivorship_bias_classification"] = r.get("survivorship_bias_classification")
    return out


def mimo_retest_checks(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {str(r.get("strategy")): r for r in results}
    strong_names = [
        "volume_dry_rebound",
        "reversal_120_top5_px_le_10",
        "reversal_120_top10_px_le_15",
        "reversal_120_top20_px_le_15",
        "rsi_reversal",
    ]
    direct = []
    for name in strong_names:
        r = by_name.get(name)
        if r:
            direct.append({
                "strategy": name,
                "status": r.get("status"),
                **compact_metrics(r),
                "positive_year_count": r.get("positive_year_count"),
                "worst_year": r.get("worst_year"),
                "return_2024": r.get("return_2024"),
                "annualized_without_2024": r.get("annualized_without_2024"),
                "sharpe_without_2024": r.get("sharpe_without_2024"),
                "decision_without_2024": r.get("decision_without_2024"),
            })
    volume = by_name.get("volume_dry_rebound")
    rev_low_price = [
        r for r in results
        if str(r.get("strategy")).startswith("reversal_120") and "_px_le_" in str(r.get("strategy"))
    ]
    better_than_volume = []
    if volume:
        base_ret = safe_float(volume.get("annualized_return_net"), -999)
        base_sharpe = safe_float(volume.get("sharpe"), -999)
        for r in rev_low_price:
            if safe_float(r.get("annualized_return_net"), -999) > base_ret and safe_float(r.get("sharpe"), -999) > base_sharpe:
                better_than_volume.append(r.get("strategy"))
    return {
        "direct_comparison": direct,
        "rev120_low_price_strategies_better_than_volume_dry_rebound_by_return_and_sharpe": better_than_volume,
        "top5_concentration": {
            name: (by_name.get(name) or {}).get("selection_concentration")
            for name in [
                "reversal_120_top5_no_filter",
                "reversal_120_top5_px_le_10",
                "reversal_120_top5_px_le_15",
            ]
            if name in by_name
        },
        "dependency_2024": [
            {
                "strategy": r.get("strategy"),
                "return_2024": r.get("return_2024"),
                "annualized_without_2024": r.get("annualized_without_2024"),
                "sharpe_without_2024": r.get("sharpe_without_2024"),
                "decision_without_2024": r.get("decision_without_2024"),
            }
            for r in sorted(results, key=lambda x: safe_float(x.get("annualized_return_net"), -999), reverse=True)[:10]
        ],
        "low_price_filter_bias_note": "px<=10 and px<=15 filters are fast-screening filters on survivorship-biased local daily CSV only; survivorship_bias_risk remains high.",
    }


def next_actions(decision: str) -> list[str]:
    return {
        "continue_with_candidates": ["refine_candidates"],
        "refine_watchlist": ["refine_watchlist"],
        "switch_factor_direction": ["switch_factor_direction"],
        "data_quality_blocks_conclusion": ["fix_data_quality"],
        "screening_failed": ["fix_screening_runner"],
    }.get(decision, ["fix_screening_runner"])


def render_markdown(payload: dict[str, Any]) -> str:
    dq = payload.get("data_quality_summary") or {}
    bm = payload.get("benchmark_summary") or {}
    lines = [
        "# Strategy Screening Funnel Report",
        "",
        DISCLAIMER,
        "",
        str(payload.get("sample_limitation") or ""),
        f"- sample role: `{payload.get('sample_role') or 'unspecified'}`; out-of-sample: `{payload.get('out_of_sample_validation') or 'not_established'}`",
        f"- sample window: `{payload.get('sample_window') or {}}`",
        "",
        "## 1. Executive Summary",
        f"- total strategies tested: `{len(payload.get('strategy_results') or [])}`",
        f"- candidate count: `{len(payload.get('candidate_pool') or [])}`",
        f"- watchlist count: `{len(payload.get('watchlist') or [])}`",
        f"- rejected count: `{len(payload.get('rejected_pool') or [])}`",
        f"- invalid count: `{len(payload.get('invalid_pool') or [])}`",
        f"- best strategy: `{(payload.get('leaderboard') or [{}])[0].get('strategy')}`",
        f"- decision: `{payload.get('decision')}`",
        "",
        "## 2. Data Quality",
        f"- total_csv: `{dq.get('total_csv')}`",
        f"- usable_csv: `{dq.get('usable_csv')}`",
        f"- skipped_csv: `{dq.get('skipped_csv')}`",
        f"- skipped reasons: `{dq.get('skipped_reasons')}`",
        f"- survivorship_bias_risk: `{dq.get('survivorship_bias_risk')}`",
        f"- PIT quality: `{dq.get('pit_quality')}`",
        "",
        "## 3. Benchmark",
        f"- benchmark used: `{bm.get('benchmark_symbol')}`",
        f"- benchmark annualized return: `{bm.get('benchmark_annualized_return')}`",
        f"- benchmark yearly returns: `{bm.get('benchmark_yearly_returns')}`",
        "",
        "## 4. Leaderboard",
        "| rank | strategy | family | status | annualized | sharpe | max_drawdown | excess_return | turnover | invested_ratio |",
        "|--:|:--|:--|:--|--:|--:|--:|--:|--:|--:|",
    ]
    for i, r in enumerate((payload.get("leaderboard") or [])[:30], 1):
        lines.append(f"| {i} | {r.get('strategy')} | {r.get('family')} | {r.get('status')} | {fmt(r.get('annualized_return_net'))} | {fmt(r.get('sharpe'))} | {fmt(r.get('max_drawdown'))} | {fmt(r.get('excess_return'))} | {fmt(r.get('turnover_annualized'))} | {fmt(r.get('invested_ratio_avg'))} |")
    for title, key in [("Candidate Pool", "candidate_pool"), ("Watchlist", "watchlist"), ("Rejected Pool", "rejected_pool"), ("Invalid Pool", "invalid_pool")]:
        lines.extend(["", f"## {title}", ""])
        items = payload.get(key) or []
        lines.extend([f"- `{x.get('strategy')}` {x.get('reason') or x.get('watch_reasons') or x.get('required_improvements')}" for x in items] or ["- none"])
    lines.extend(["", "## 9. Family Summary", ""])
    for family, summary in (payload.get("family_summary") or {}).items():
        lines.append(f"- {family}: best={summary.get('best_strategy')} avg_return={summary.get('avg_return')} avg_sharpe={summary.get('avg_sharpe')} candidate_count={summary.get('candidate_count')} rejected_count={summary.get('rejected_count')}")
    lines.extend(["", "## 10. Distinctness Check", f"- duplicated_strategy_groups: `{payload.get('duplicated_strategy_groups')}`", "", "## 11. Next Steps"])
    lines.extend([f"- {x}" for x in payload.get("next_recommended_actions") or []])
    handoff = payload.get("backtest_handoff") or {}
    lines.extend(
        [
            "",
            "## 12. Backtest Handoff",
            f"- status: `{handoff.get('status')}`",
            f"- source: `{handoff.get('source')}`",
            f"- strategy: `{handoff.get('strategy')}`",
            f"- symbol_count: `{handoff.get('symbol_count')}`",
            f"- command: `{handoff.get('command_text') or ''}`",
        ]
    )
    lines.extend(["", "## 13. Limitations", "- no formal PIT", "- survivorship bias high", "- no historical ST", "- benchmark limitations", "- research only", "- not investment advice"])
    return "\n".join(lines) + "\n"


def fmt(v: Any) -> str:
    if v is None:
        return ""
    return f"{safe_float(v):.4f}"


def display_output_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


def write_outputs(payload: dict[str, Any], output_dir: Path, report_dir: Path) -> dict[str, Any]:
    if payload.get("task_type") == "strategy_screening_funnel_mimo_retest":
        result = output_dir / "strategy_screening_funnel_mimo_retest_latest.json"
        report = report_dir / "strategy_screening_funnel_mimo_retest_latest.md"
    else:
        result = output_dir / "strategy_screening_funnel_latest.json"
        report = report_dir / "strategy_screening_funnel_latest.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["output_paths"] = {"json": display_output_path(result), "markdown": display_output_path(report)}
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report.write_text(render_markdown(payload), encoding="utf-8")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run simple strategy-first screening funnel on local daily CSV.")
    p.add_argument("--data-root", default="data/daily")
    p.add_argument("--initial-cash", type=float, default=100000)
    p.add_argument("--top-n-default", type=int, default=20)
    p.add_argument("--benchmark", default="000905")
    p.add_argument("--start-date", default="")
    p.add_argument("--end-date", default="")
    p.add_argument("--min-avg-amount-20d", type=float, default=20000000)
    p.add_argument("--min-history-days", type=int, default=180)
    p.add_argument("--lot-size", type=int, default=100)
    p.add_argument("--commission-rate", type=float, default=DEFAULT_COMMISSION_RATE)
    p.add_argument("--stamp-tax-rate", type=float, default=DEFAULT_STAMP_TAX_RATE)
    p.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    p.add_argument("--output-dir", default="results")
    p.add_argument("--report-dir", default="reports")
    p.add_argument("--max-strategies", type=int, default=0)
    p.add_argument("--max-symbols", type=int, default=0, help="Limit local daily CSV symbols before loading. 0 keeps full local universe.")
    p.add_argument("--feature-cache", default="false", help="true/false; cache computed factor panel under --cache-dir.")
    p.add_argument("--cache-dir", default=None, help="Default: <output-dir>/cache. Relative paths are under output-dir; absolute paths explicitly override it. CSV + SHA receipt only; legacy pickle caches are ignored.")
    p.add_argument("--mimo-retest", default="false")
    p.add_argument("--json", default="false")
    p.add_argument("--summary", nargs="?", const="true", default="false")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = write_outputs(run_screening(args), ROOT / args.output_dir, ROOT / args.report_dir)
    if truthy(args.summary):
        best = (payload.get("leaderboard") or [{}])[0]
        summary = {
            **research_sample_context(payload),
            "ok": payload.get("ok"),
            "status": payload.get("status"),
            "task_type": payload.get("task_type"),
            "total_csv": (payload.get("data_quality_summary") or {}).get("total_csv"),
            "usable_csv": (payload.get("data_quality_summary") or {}).get("usable_csv"),
            "skipped_csv": (payload.get("data_quality_summary") or {}).get("skipped_csv"),
            "benchmark_used": (payload.get("benchmark_summary") or {}).get("benchmark_symbol"),
            "benchmark_available": (payload.get("benchmark_summary") or {}).get("benchmark_available"),
            "strategies_tested": len(payload.get("strategy_results") or []),
            "max_symbols": (payload.get("performance") or {}).get("max_symbols"),
            "elapsed_seconds": (payload.get("performance") or {}).get("elapsed_seconds"),
            "feature_cache_status": ((payload.get("performance") or {}).get("feature_cache") or {}).get("status"),
            "candidate_count": len(payload.get("candidate_pool") or []),
            "watchlist_count": len(payload.get("watchlist") or []),
            "rejected_count": len(payload.get("rejected_pool") or []),
            "invalid_count": len(payload.get("invalid_pool") or []),
            "best_strategy": best.get("strategy"),
            "best_annualized_return_net": best.get("annualized_return_net"),
            "best_max_drawdown": best.get("max_drawdown"),
            "best_sharpe": best.get("sharpe"),
            "best_excess_return": best.get("excess_return"),
            "decision": payload.get("decision"),
            "output_paths": payload.get("output_paths"),
            "warnings": payload.get("warnings"),
        }
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    elif truthy(args.json):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    else:
        print(render_markdown(payload))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
