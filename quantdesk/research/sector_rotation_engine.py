from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.factor_evaluation import PIT_UNIVERSE_WARNING
from quantdesk.price_basis import (
    PRICE_BASIS_UNKNOWN,
    PRICE_DATA_CONTRACT_VERSION,
    price_basis_labels,
    validate_price_frame_set,
)
from quantdesk.shared import now_text, write_json
from quantdesk.version import API_SCHEMA_VERSION, APP_VERSION, PACKAGE_VERSION

LOG = logging.getLogger(__name__)

DISCLAIMER = (
    "Research diagnostics only. Not investment advice. Sector rotation does not "
    "change default screen, backtest, daily workflow, trading signals, scoring "
    "weights, broker boundaries, or real orders."
)
SECTOR_ASSUMPTION = "Sector rotation is an observation layer only and does not generate trading advice."
BENCHMARK_ASSUMPTION = "Benchmark defaults to equal_weight_universe when no explicit benchmark index is available."


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _prepare_price_panel(price_panel: pd.DataFrame) -> pd.DataFrame:
    if price_panel is None or price_panel.empty:
        return pd.DataFrame()
    frame = price_panel.copy()
    rename = {"trade_date": "date", "code": "symbol", "ts_code": "symbol"}
    frame = frame.rename(columns={column: rename.get(str(column), str(column)) for column in frame.columns})
    if not {"date", "symbol", "close"}.issubset(frame.columns):
        return pd.DataFrame()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if "volume" in frame.columns:
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame.dropna(subset=["date", "symbol", "close"])
    frame = frame[frame["close"] > 0]
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def load_sector_map(config: dict[str, Any] | None = None) -> dict[str, str]:
    """Load symbol -> sector_name from the existing theme/sector map if present."""

    config = config or {}
    path_value = config.get("sector_map_path") or config.get("theme_map_path")
    try:
        if path_value:
            path = Path(path_value)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[2] / path
            if path.exists():
                frame = read_csv_frame(path, dtype=str)
                for warning in frame.attrs.get("warnings", []):
                    LOG.warning("sector_map_csv_warning: path=%s warning=%s", path, warning)
            else:
                return {}
        else:
            from quantdesk.theme_sector_engine import load_theme_map

            rows = load_theme_map()
            frame = pd.DataFrame(list(rows.values())) if isinstance(rows, dict) else pd.DataFrame(rows)
        if frame.empty or "symbol" not in frame.columns:
            return {}
        sector_columns = [column for column in ("sector", "industry", "primary_theme", "theme", "theme_tags") if column in frame.columns]
        if not sector_columns:
            return {}
        mapping: dict[str, str] = {}
        for row in frame.to_dict(orient="records"):
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            sector = ""
            for column in sector_columns:
                value = str(row.get(column) or "").strip()
                if value and value.lower() not in {"nan", "none"}:
                    sector = value.split(";")[0].split(",")[0].strip()
                    break
            if sector:
                mapping[symbol] = sector
        return mapping
    except Exception:
        return {}


def attach_sector_to_price_panel(
    price_panel: pd.DataFrame,
    sector_map: dict[str, str] | None,
    *,
    symbol_col: str = "symbol",
) -> pd.DataFrame:
    frame = price_panel.copy() if price_panel is not None else pd.DataFrame()
    warnings: list[str] = []
    if frame.empty:
        frame.attrs["warnings"] = ["no_daily_data: price panel is empty"]
        return frame
    if "sector_name" in frame.columns:
        frame["sector_name"] = frame["sector_name"].fillna("Unknown").astype(str)
        frame.attrs["warnings"] = []
        return frame
    for candidate in ("sector", "industry", "theme", "primary_theme"):
        if candidate in frame.columns:
            frame["sector_name"] = frame[candidate].fillna("Unknown").astype(str)
            frame.attrs["warnings"] = []
            return frame
    mapping = sector_map or {}
    if not mapping:
        warnings.append("missing_sector_map: all symbols grouped as Unknown")
        frame["sector_name"] = "Unknown"
    elif symbol_col not in frame.columns:
        warnings.append(f"sector_symbol_column_missing:{symbol_col}")
        frame["sector_name"] = "Unknown"
    else:
        frame["sector_name"] = frame[symbol_col].astype(str).map(mapping).fillna("Unknown").astype(str)
        missing_ratio = _safe_float((frame["sector_name"] == "Unknown").mean())
        if missing_ratio > 0:
            warnings.append(f"sector_map_partial_coverage:{missing_ratio:.2%}_unknown")
    frame.attrs["warnings"] = warnings
    return frame


def build_sector_return_panel(
    price_panel: pd.DataFrame,
    sector_col: str = "sector_name",
    date_col: str = "date",
    symbol_col: str = "symbol",
    price_col: str = "close",
    windows: tuple[int, ...] = (5, 20, 60),
) -> pd.DataFrame:
    warnings: list[str] = []
    frame = _prepare_price_panel(price_panel)
    if frame.empty:
        out = pd.DataFrame()
        out.attrs["warnings"] = ["no_daily_data: price panel is empty or missing date/symbol/close"]
        return out
    if sector_col not in frame.columns and sector_col in price_panel.columns:
        frame[sector_col] = price_panel[sector_col].values
    if sector_col not in frame.columns:
        frame[sector_col] = "Unknown"
        warnings.append("missing_sector_map: sector_name column missing")
    frame[sector_col] = frame[sector_col].fillna("Unknown").astype(str)
    frame = frame.sort_values([symbol_col, date_col])
    frame["stock_return_1d"] = frame.groupby(symbol_col)[price_col].transform(
        lambda values: values.ffill().pct_change(fill_method=None)
    )
    sector_daily = (
        frame.dropna(subset=["stock_return_1d"])
        .groupby([date_col, sector_col], as_index=False)
        .agg(sector_return_1d=("stock_return_1d", "mean"), sector_symbol_count=(symbol_col, "nunique"))
    )
    if sector_daily.empty:
        sector_daily.attrs["warnings"] = ["insufficient_data: no sector daily returns"]
        return sector_daily
    sector_daily = sector_daily.sort_values([sector_col, date_col]).reset_index(drop=True)
    for window in windows:
        if len(frame[date_col].drop_duplicates()) <= window:
            warnings.append(f"insufficient_data: fewer than {window + 1} dates for sector_return_{window}d")
        sector_daily[f"sector_return_{window}d"] = (
            sector_daily.groupby(sector_col)["sector_return_1d"]
            .transform(lambda values, w=window: (1.0 + values).rolling(w, min_periods=1).apply(math.prod, raw=True) - 1.0)
        )
    sector_daily.attrs["warnings"] = list(dict.fromkeys(warnings))
    return sector_daily


def compute_benchmark_return(price_panel: pd.DataFrame, method: str = "equal_weight_universe") -> pd.DataFrame:
    warnings: list[str] = []
    frame = _prepare_price_panel(price_panel)
    if frame.empty or method == "unavailable":
        out = pd.DataFrame(columns=["date", "benchmark_return_1d"])
        out.attrs["warnings"] = ["benchmark_unavailable: no usable price panel"]
        out.attrs["status"] = "unavailable"
        return out
    if method not in {"equal_weight_universe", "benchmark_index"}:
        warnings.append(f"benchmark_method_unsupported:{method}; fallback equal_weight_universe")
    frame["stock_return_1d"] = frame.groupby("symbol")["close"].transform(
        lambda values: values.ffill().pct_change(fill_method=None)
    )
    benchmark = (
        frame.dropna(subset=["stock_return_1d"])
        .groupby("date", as_index=False)
        .agg(benchmark_return_1d=("stock_return_1d", "mean"))
        .sort_values("date")
        .reset_index(drop=True)
    )
    if benchmark.empty:
        warnings.append("benchmark_unavailable: insufficient returns")
        benchmark.attrs["status"] = "unavailable"
    else:
        warnings.append("benchmark_fallback_equal_weight_universe")
        benchmark.attrs["status"] = "fallback"
    benchmark.attrs["warnings"] = list(dict.fromkeys(warnings))
    benchmark.attrs["method"] = "equal_weight_universe"
    return benchmark


def compute_relative_strength(
    sector_returns: pd.DataFrame,
    benchmark_returns: pd.DataFrame,
    windows: tuple[int, ...] = (5, 20, 60),
    strength_weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    warnings = list(sector_returns.attrs.get("warnings") or [])
    if sector_returns.empty:
        out = pd.DataFrame()
        out.attrs["warnings"] = warnings + ["insufficient_data: sector returns are empty"]
        return out
    frame = sector_returns.copy()
    bench = benchmark_returns.copy() if benchmark_returns is not None else pd.DataFrame()
    if "date" in bench.columns:
        bench = bench.sort_values("date")
        for window in windows:
            bench[f"benchmark_return_{window}d"] = (
                (1.0 + bench["benchmark_return_1d"])
                .rolling(window, min_periods=1)
                .apply(math.prod, raw=True)
                - 1.0
            )
        frame = frame.merge(bench[["date", *[f"benchmark_return_{w}d" for w in windows]]], on="date", how="left")
    else:
        for window in windows:
            frame[f"benchmark_return_{window}d"] = 0.0
        warnings.append("benchmark_unavailable: relative strength uses zero benchmark")
    weights = strength_weights or {"rs_5d": 0.3, "rs_20d": 0.5, "rs_60d": 0.2}
    score = pd.Series(0.0, index=frame.index)
    total_weight = 0.0
    for window in windows:
        sector_col = f"sector_return_{window}d"
        bench_col = f"benchmark_return_{window}d"
        rs_col = f"rs_{window}d"
        frame[rs_col] = pd.to_numeric(frame.get(sector_col, 0.0), errors="coerce").fillna(0.0) - pd.to_numeric(frame.get(bench_col, 0.0), errors="coerce").fillna(0.0)
        weight = _safe_float(weights.get(rs_col, 0.0))
        score = score + frame[rs_col].fillna(0.0) * weight
        total_weight += abs(weight)
    frame["composite_strength_score"] = score / total_weight if total_weight else score
    frame.attrs["warnings"] = list(dict.fromkeys(warnings + list(benchmark_returns.attrs.get("warnings") or [])))
    return frame


def compute_sector_rank_changes(
    sector_strength_panel: pd.DataFrame,
    lookback_dates: tuple[int, ...] = (5, 20),
) -> pd.DataFrame:
    if sector_strength_panel.empty:
        out = pd.DataFrame()
        out.attrs["warnings"] = ["insufficient_data: sector strength panel is empty"]
        return out
    frame = sector_strength_panel.copy().sort_values(["date", "sector_name"])
    frame["current_rank"] = frame.groupby("date")["composite_strength_score"].rank(ascending=False, method="min").astype(int)
    wide = frame.pivot(index="date", columns="sector_name", values="current_rank").sort_index()
    for lookback in lookback_dates:
        prior = wide.shift(lookback)
        changes: list[float] = []
        for row in frame.itertuples(index=False):
            old_rank = prior.get(row.sector_name)
            if old_rank is None:
                changes.append(0.0)
                continue
            value = old_rank.get(row.date)
            changes.append(_safe_float(value) - _safe_float(row.current_rank) if pd.notna(value) else 0.0)
        frame[f"rank_improvement_{lookback}d"] = changes
    frame.attrs["warnings"] = list(sector_strength_panel.attrs.get("warnings") or [])
    return frame


def compute_sector_breadth(
    price_panel: pd.DataFrame,
    sector_col: str = "sector_name",
    windows: tuple[int, ...] = (5, 20),
) -> pd.DataFrame:
    warnings: list[str] = []
    frame = _prepare_price_panel(price_panel)
    if frame.empty:
        out = pd.DataFrame()
        out.attrs["warnings"] = ["no_daily_data: price panel is empty"]
        return out
    if sector_col not in frame.columns and price_panel is not None and sector_col in price_panel.columns:
        frame[sector_col] = price_panel[sector_col].values
    if sector_col not in frame.columns:
        frame[sector_col] = "Unknown"
        warnings.append("missing_sector_map: sector_name column missing for breadth")
    frame = frame.sort_values(["symbol", "date"])
    frame["ret_1d"] = frame.groupby("symbol")["close"].transform(
        lambda values: values.ffill().pct_change(fill_method=None)
    )
    frame["ret_5d"] = frame.groupby("symbol")["close"].transform(
        lambda values: values.ffill().pct_change(5, fill_method=None)
    )
    frame["high_20d"] = frame.groupby("symbol")["close"].transform(lambda values: values.rolling(20, min_periods=1).max())
    frame["ma20"] = frame.groupby("symbol")["close"].transform(lambda values: values.rolling(20, min_periods=1).mean())
    frame["is_new_high_20d"] = frame["close"] >= frame["high_20d"]
    frame["above_ma20"] = frame["close"] >= frame["ma20"]
    breadth = (
        frame.groupby(["date", sector_col], as_index=False)
        .agg(
            up_ratio_1d=("ret_1d", lambda values: _safe_float((pd.to_numeric(values, errors="coerce") > 0).mean())),
            up_ratio_5d=("ret_5d", lambda values: _safe_float((pd.to_numeric(values, errors="coerce") > 0).mean())),
            new_high_ratio_20d=("is_new_high_20d", "mean"),
            above_ma20_ratio=("above_ma20", "mean"),
        )
        .rename(columns={sector_col: "sector_name"})
    )
    breadth.attrs["warnings"] = list(dict.fromkeys(warnings))
    return breadth


def compute_sector_heat_score(sector_panel: pd.DataFrame, weights: dict[str, float] | None = None) -> pd.DataFrame:
    if sector_panel.empty:
        out = pd.DataFrame()
        out.attrs["warnings"] = ["insufficient_data: sector panel is empty"]
        return out
    weights = weights or {"relative_strength": 0.5, "rank_improvement": 0.25, "breadth": 0.25}
    frame = sector_panel.copy()
    rs = pd.to_numeric(frame.get("composite_strength_score", 0.0), errors="coerce").fillna(0.0)
    rank_part = (
        pd.to_numeric(frame.get("rank_improvement_5d", 0.0), errors="coerce").fillna(0.0) * 0.6
        + pd.to_numeric(frame.get("rank_improvement_20d", 0.0), errors="coerce").fillna(0.0) * 0.4
    )
    breadth = (
        pd.to_numeric(frame.get("up_ratio_5d", 0.0), errors="coerce").fillna(0.0) * 0.4
        + pd.to_numeric(frame.get("above_ma20_ratio", 0.0), errors="coerce").fillna(0.0) * 0.4
        + pd.to_numeric(frame.get("new_high_ratio_20d", 0.0), errors="coerce").fillna(0.0) * 0.2
        - 0.5
    )
    frame["heat_score"] = (
        rs * _safe_float(weights.get("relative_strength", 0.5))
        + rank_part * 0.05 * _safe_float(weights.get("rank_improvement", 0.25))
        + breadth * _safe_float(weights.get("breadth", 0.25))
    )

    def status(value: Any) -> str:
        score = _safe_float(value)
        if score >= 0.06:
            return "hot"
        if score >= 0.015:
            return "improving"
        if score <= -0.06:
            return "cold"
        if score <= -0.015:
            return "weakening"
        return "neutral"

    frame["heat_status"] = frame["heat_score"].map(status)
    frame.attrs["warnings"] = list(sector_panel.attrs.get("warnings") or [])
    return frame


def run_sector_rotation(
    price_panel: pd.DataFrame,
    *,
    sector_map: dict[str, str] | None = None,
    windows: tuple[int, ...] = (5, 20, 60),
    benchmark_method: str = "equal_weight_universe",
    min_sector_symbols: int = 3,
    strength_weights: dict[str, float] | None = None,
    heat_score_weights: dict[str, float] | None = None,
    adjustment: str = "raw",
) -> dict[str, Any]:
    warnings: list[str] = []
    if price_panel is None or price_panel.empty:
        return _empty_payload(["no_daily_data: price panel is empty or missing date/symbol/close"], windows, benchmark_method, min_sector_symbols, strength_weights, heat_score_weights, adjustment=adjustment)
    price_validation = validate_price_frame_set(
        {"sector_rotation_panel": price_panel},
        consumer="feature",
    )
    if not price_validation.get("eligible"):
        payload = _empty_payload(
            [str(item) for item in price_validation.get("reason_codes") or ["price_policy_validation_failed"]],
            windows,
            benchmark_method,
            min_sector_symbols,
            strength_weights,
            heat_score_weights,
            adjustment=adjustment,
        )
        payload.update(
            {
                **price_basis_labels(PRICE_BASIS_UNKNOWN),
                "ok": False,
                "status": "blocked",
                "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
                "price_policy_validation": price_validation,
                "reason_codes": list(price_validation.get("reason_codes") or []),
                "evidence_eligible": False,
            }
        )
        return payload
    price = _prepare_price_panel(price_panel)
    if price.empty:
        return _empty_payload(["no_daily_data: price panel is empty or missing date/symbol/close"], windows, benchmark_method, min_sector_symbols, strength_weights, heat_score_weights, adjustment=adjustment)
    if "sector_name" in price_panel.columns:
        price["sector_name"] = price_panel["sector_name"].values
    else:
        attached = attach_sector_to_price_panel(price, sector_map)
        price = attached
        warnings.extend(attached.attrs.get("warnings") or [])
    returns = build_sector_return_panel(price, windows=windows)
    warnings.extend(returns.attrs.get("warnings") or [])
    benchmark = compute_benchmark_return(price, method=benchmark_method)
    warnings.extend(benchmark.attrs.get("warnings") or [])
    if returns.empty:
        return _empty_payload(warnings + ["insufficient_data: no sector returns"], windows, benchmark_method, min_sector_symbols, strength_weights, heat_score_weights, adjustment=adjustment)
    strength = compute_relative_strength(returns, benchmark, windows=windows, strength_weights=strength_weights)
    ranked = compute_sector_rank_changes(strength, lookback_dates=(5, 20))
    breadth = compute_sector_breadth(price, windows=(5, 20))
    warnings.extend(breadth.attrs.get("warnings") or [])
    merged = ranked.merge(breadth, on=["date", "sector_name"], how="left")
    heat = compute_sector_heat_score(merged, weights=heat_score_weights)
    warnings.extend(heat.attrs.get("warnings") or [])
    # Find the latest date with sufficient sector coverage (not just the absolute max date)
    if "date" in heat.columns and not heat.empty:
        date_sector_counts = heat.groupby("date")["sector_name"].nunique()
        total_sectors = int(heat["sector_name"].nunique())
        min_sectors_for_date = max(3, total_sectors // 2)
        valid_dates = date_sector_counts[date_sector_counts >= min_sectors_for_date]
        if not valid_dates.empty:
            latest_date = pd.Timestamp(valid_dates.index.max())
        else:
            latest_date = pd.Timestamp(heat["date"].max())
    else:
        latest_date = None
    latest = heat[heat["date"] == latest_date].copy() if latest_date is not None else pd.DataFrame()
    if min_sector_symbols > 1 and "sector_symbol_count" in latest.columns:
        before = len(latest)
        latest = latest[pd.to_numeric(latest["sector_symbol_count"], errors="coerce").fillna(0) >= int(min_sector_symbols)]
        if len(latest) < before:
            warnings.append(f"min_sector_symbols_filter_applied:{min_sector_symbols}")
    if latest.empty:
        return _empty_payload(warnings + ["insufficient_data: no latest sector rows"], windows, benchmark_method, min_sector_symbols, strength_weights, heat_score_weights, adjustment=adjustment)
    latest = latest.sort_values(["heat_score", "composite_strength_score"], ascending=False)
    rankings = [_sector_row(row, windows) for row in latest.to_dict(orient="records")]
    improving_pool = [
        item for item in rankings
        if _safe_float(item.get("rank_improvement_5d")) > 0 or str(item.get("heat_status")) in {"hot", "improving"}
    ]
    weakening_pool = [
        item for item in rankings
        if _safe_float(item.get("rank_improvement_5d")) < 0 or str(item.get("heat_status")) in {"weakening", "cold"}
    ]
    top_improving = sorted(improving_pool, key=lambda item: (_safe_float(item.get("rank_improvement_5d")), _safe_float(item.get("heat_score"))), reverse=True)[:10]
    weakening = sorted(weakening_pool, key=lambda item: (_safe_float(item.get("rank_improvement_5d")), _safe_float(item.get("heat_score"))))[:10]
    bench_latest = benchmark[benchmark["date"] <= latest_date].tail(1) if latest_date is not None and not benchmark.empty else pd.DataFrame()
    benchmark_payload = {
        "method": "equal_weight_universe",
        "status": benchmark.attrs.get("status") or "fallback",
        "return_5d": 0.0,
        "return_20d": 0.0,
        "return_60d": 0.0,
    }
    if not bench_latest.empty:
        temp = compute_relative_strength(
            pd.DataFrame([{"date": bench_latest.iloc[0]["date"], "sector_name": "_benchmark", **{f"sector_return_{w}d": 0.0 for w in windows}, "sector_return_1d": 0.0}]),
            benchmark,
            windows=windows,
        )
        row = temp[temp["date"] == bench_latest.iloc[0]["date"]].tail(1)
        for window in windows:
            col = f"benchmark_return_{window}d"
            benchmark_payload[f"return_{window}d"] = _safe_float(row[col].iloc[0]) if not row.empty and col in row.columns else 0.0
    universe = {
        "symbol_count": int(price["symbol"].nunique()),
        "sector_count": int(price["sector_name"].nunique()),
        "date_count": int(price["date"].nunique()),
        "sample_count": int(len(price)),
        "pit_universe": False,
    }
    status = "warning" if warnings else "passed"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "sector_rotation",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "local_daily",
        **price_basis_labels(str(price_validation.get("accepted_price_basis") or adjustment)),
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": price_validation,
        "target_date": latest_date.strftime("%Y-%m-%d") if latest_date is not None else "",
        "network": False,
        "config": {
            "windows": [int(item) for item in windows],
            "benchmark_method": benchmark_method,
            "min_sector_symbols": int(min_sector_symbols),
            "strength_weights": strength_weights or {"rs_5d": 0.3, "rs_20d": 0.5, "rs_60d": 0.2},
            "heat_score_weights": heat_score_weights or {"relative_strength": 0.5, "rank_improvement": 0.25, "breadth": 0.25},
        },
        "universe": universe,
        "benchmark": benchmark_payload,
        "sector_rankings": rankings,
        "top_improving_sectors": top_improving,
        "weakening_sectors": weakening,
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "reason_codes": ["static_sector_map_proxy_non_pit"],
        "degraded_reasons": ["static_sector_map_proxy_non_pit"],
        "evidence_eligible": False,
        "formal_factor_fit_eligible": False,
        "sector_membership": {
            "status": "proxy_non_pit",
            "used_for_formal_fit": False,
        },
        "changes_default_screen": False,
        "assumptions": [SECTOR_ASSUMPTION, BENCHMARK_ASSUMPTION, PIT_UNIVERSE_WARNING],
        "output_paths": {},
        "suggested_next_action": "Review sector rotation as market-structure diagnostics only; do not treat sector lists as trading advice.",
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(payload)


def _sector_row(row: dict[str, Any], windows: tuple[int, ...]) -> dict[str, Any]:
    heat_status = str(row.get("heat_status") or "neutral")
    heat_to_sector = {"hot": "leading", "improving": "strengthening", "neutral": "neutral", "weakening": "weakening", "cold": "weakening"}
    out = {
        "sector_name": str(row.get("sector_name") or "Unknown"),
        "symbol_count": int(_safe_float(row.get("sector_symbol_count"))),
        "current_rank": int(_safe_float(row.get("current_rank"))),
        "rank_improvement_5d": _safe_float(row.get("rank_improvement_5d")),
        "rank_improvement_20d": _safe_float(row.get("rank_improvement_20d")),
        "up_ratio_1d": _safe_float(row.get("up_ratio_1d")),
        "up_ratio_5d": _safe_float(row.get("up_ratio_5d")),
        "new_high_ratio_20d": _safe_float(row.get("new_high_ratio_20d")),
        "above_ma20_ratio": _safe_float(row.get("above_ma20_ratio")),
        "heat_score": _safe_float(row.get("heat_score")),
        "heat_status": heat_status,
        "sector_status": heat_to_sector.get(heat_status, "neutral"),
    }
    for window in windows:
        out[f"return_{window}d"] = _safe_float(row.get(f"sector_return_{window}d"))
        out[f"rs_{window}d"] = _safe_float(row.get(f"rs_{window}d"))
    for key in ("return_5d", "return_20d", "return_60d", "rs_5d", "rs_20d", "rs_60d"):
        out.setdefault(key, 0.0)
    return out


def _empty_payload(
    warnings: list[str],
    windows: tuple[int, ...],
    benchmark_method: str,
    min_sector_symbols: int,
    strength_weights: dict[str, float] | None,
    heat_score_weights: dict[str, float] | None,
    adjustment: str = "raw",
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "warning",
        "task_type": "sector_rotation",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "package_version": PACKAGE_VERSION,
        "schema_version": API_SCHEMA_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "data_source": "none",
        **price_basis_labels(adjustment),
        "target_date": "",
        "network": False,
        "config": {
            "windows": [int(item) for item in windows],
            "benchmark_method": benchmark_method,
            "min_sector_symbols": int(min_sector_symbols),
            "strength_weights": strength_weights or {"rs_5d": 0.3, "rs_20d": 0.5, "rs_60d": 0.2},
            "heat_score_weights": heat_score_weights or {"relative_strength": 0.5, "rank_improvement": 0.25, "breadth": 0.25},
        },
        "universe": {"symbol_count": 0, "sector_count": 0, "date_count": 0, "sample_count": 0, "pit_universe": False},
        "benchmark": {"method": benchmark_method, "status": "unavailable", "return_5d": 0.0, "return_20d": 0.0, "return_60d": 0.0},
        "sector_rankings": [],
        "top_improving_sectors": [],
        "weakening_sectors": [],
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "reason_codes": ["sector_rotation_evidence_unavailable"],
        "degraded_reasons": ["sector_rotation_evidence_unavailable"],
        "evidence_eligible": False,
        "formal_factor_fit_eligible": False,
        "sector_membership": {
            "status": "missing",
            "used_for_formal_fit": False,
        },
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": {
            "status": "blocked",
            "eligible": False,
            "reason_codes": ["price_policy_validation_not_run"],
        },
        "changes_default_screen": False,
        "assumptions": [SECTOR_ASSUMPTION, BENCHMARK_ASSUMPTION, PIT_UNIVERSE_WARNING],
        "output_paths": {},
        "suggested_next_action": "Import local daily data and a sector/theme map, then rerun sector-rotation.",
        "disclaimer": DISCLAIMER,
    }


def build_sector_rotation_report(payload: dict[str, Any]) -> str:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    benchmark = payload.get("benchmark") if isinstance(payload.get("benchmark"), dict) else {}
    rankings = payload.get("sector_rankings") if isinstance(payload.get("sector_rankings"), list) else []
    improving = payload.get("top_improving_sectors") if isinstance(payload.get("top_improving_sectors"), list) else []
    weakening = payload.get("weakening_sectors") if isinstance(payload.get("weakening_sectors"), list) else []
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    assumptions = payload.get("assumptions") if isinstance(payload.get("assumptions"), list) else []
    lines = [
        "# QuantDesk Sector Rotation Report",
        "",
        f"- generated_at: `{payload.get('generated_at') or '-'}`",
        f"- app_version: `{payload.get('app_version') or '-'}`",
        f"- data_source: `{payload.get('data_source') or '-'}`",
        f"- target_date: `{payload.get('target_date') or '-'}`",
        f"- benchmark_method: `{benchmark.get('method') or config.get('benchmark_method') or '-'}`",
        f"- benchmark_status: `{benchmark.get('status') or '-'}`",
        f"- symbols: `{universe.get('symbol_count', 0)}`",
        f"- sectors: `{universe.get('sector_count', 0)}`",
        f"- dates: `{universe.get('date_count', 0)}`",
        "",
        "## Top Sector Ranking",
        "",
        "| Rank | Sector | Heat | RS 5D | RS 20D | Rank +5D | Up Ratio 5D | Status |",
        "|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in rankings[:20]:
        lines.append(
            f"| {int(row.get('current_rank') or 0)} | {row.get('sector_name') or '-'} | "
            f"{_safe_float(row.get('heat_score')):.6f} | {_safe_float(row.get('rs_5d')):.4%} | "
            f"{_safe_float(row.get('rs_20d')):.4%} | {_safe_float(row.get('rank_improvement_5d')):.0f} | "
            f"{_safe_float(row.get('up_ratio_5d')):.2%} | {row.get('heat_status') or '-'} |"
        )
    if not rankings:
        lines.append("| 0 | none | 0 | 0 | 0 | 0 | 0 | neutral |")
    lines.extend(["", "## Top Improving Sectors", "", "| Sector | Rank +5D | Heat | Status |", "|---|---:|---:|---|"])
    for row in improving[:10]:
        lines.append(f"| {row.get('sector_name') or '-'} | {_safe_float(row.get('rank_improvement_5d')):.0f} | {_safe_float(row.get('heat_score')):.6f} | {row.get('heat_status') or '-'} |")
    if not improving:
        lines.append("| none | 0 | 0 | neutral |")
    lines.extend(["", "## Weakening Sectors", "", "| Sector | Rank +5D | Heat | Status |", "|---|---:|---:|---|"])
    for row in weakening[:10]:
        lines.append(f"| {row.get('sector_name') or '-'} | {_safe_float(row.get('rank_improvement_5d')):.0f} | {_safe_float(row.get('heat_score')):.6f} | {row.get('heat_status') or '-'} |")
    if not weakening:
        lines.append("| none | 0 | 0 | neutral |")
    lines.extend(["", "## Breadth Summary", "", "| Sector | Up 1D | Up 5D | New High 20D | Above MA20 |", "|---|---:|---:|---:|---:|"])
    for row in rankings[:20]:
        lines.append(
            f"| {row.get('sector_name') or '-'} | {_safe_float(row.get('up_ratio_1d')):.2%} | "
            f"{_safe_float(row.get('up_ratio_5d')):.2%} | {_safe_float(row.get('new_high_ratio_20d')):.2%} | "
            f"{_safe_float(row.get('above_ma20_ratio')):.2%} |"
        )
    lines.extend(["", "## Assumptions", ""])
    lines.extend([f"- {item}" for item in assumptions] or ["- none"])
    lines.extend(["", "## PIT Universe Limitation", "", PIT_UNIVERSE_WARNING, "", "## Warnings", ""])
    lines.extend([f"- {item}" for item in warnings] or ["- none"])
    lines.extend([
        "",
        "## Suggested Next Action",
        "",
        str(payload.get("suggested_next_action") or "-"),
        "",
        "## Disclaimer",
        "",
        str(payload.get("disclaimer") or DISCLAIMER),
    ])
    return "\n".join(lines) + "\n"


def write_sector_rotation_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    output_path = Path(output_dir)
    report_path = Path(report_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path.mkdir(parents=True, exist_ok=True)
    stamp = now_text().replace("-", "").replace(":", "").replace("T", "_").replace(" ", "_")
    latest_json = output_path / "sector_rotation_latest.json"
    timestamp_json = output_path / f"sector_rotation_{stamp}.json"
    report = report_path / f"sector_rotation_report_{stamp}.md"
    latest_report = report_path / "sector_rotation_report_latest.md"
    payload = _json_safe(dict(payload))
    payload["output_paths"] = {
        "latest_json": str(latest_json).replace("\\", "/"),
        "timestamp_json": str(timestamp_json).replace("\\", "/"),
        "report": str(report).replace("\\", "/"),
        "latest_report": str(latest_report).replace("\\", "/"),
    }
    write_json(latest_json, payload)
    write_json(timestamp_json, payload)
    report_text = build_sector_rotation_report(payload)
    report.write_text(report_text, encoding="utf-8")
    latest_report.write_text(report_text, encoding="utf-8")
    try:
        from quantdesk.experiment_tracker import log_experiment_non_blocking

        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        tracking = log_experiment_non_blocking(
            "sector_rotation",
            payload.get("config") if isinstance(payload.get("config"), dict) else {},
            {
                "sector_count": summary.get("sector_count") or len(payload.get("sector_rankings") or []),
                "hot_sector_count": summary.get("hot_sector_count"),
                "improving_sector_count": summary.get("improving_sector_count"),
            },
            output_paths=payload.get("output_paths") if isinstance(payload.get("output_paths"), dict) else {},
            tags=["auto_logged"],
        )
        if tracking.get("experiment_id"):
            payload["experiment_id"] = tracking.get("experiment_id")
        if tracking.get("warning") and not tracking.get("ok"):
            payload.setdefault("warnings", []).append(tracking["warning"])
    except Exception as exc:
        payload.setdefault("warnings", []).append(f"experiment_tracker_failed:{exc}")
    return payload
