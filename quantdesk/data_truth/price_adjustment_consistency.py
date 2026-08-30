from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_chunks, read_csv_frame


def build_implied_adjustment_from_local_qfq(daily_dir: Path, symbols: list[str], max_rows_per_symbol: int = 200) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows: list[pd.DataFrame] = []
    missing_qfq: list[str] = []
    read_errors: dict[str, str] = {}
    for symbol in symbols:
        raw_path = daily_dir / f"{symbol}.csv"
        qfq_path = daily_dir / f"{symbol}_qfq.parquet"
        if not raw_path.exists() or not qfq_path.exists():
            missing_qfq.append(symbol)
            continue
        try:
            raw = read_csv_frame(raw_path, dtype={"symbol": str})
            qfq = pd.read_parquet(qfq_path)
            raw_cols = {c.lower(): c for c in raw.columns}
            qfq_cols = {c.lower(): c for c in qfq.columns}
            raw_date = raw_cols.get("date") or raw_cols.get("trade_date")
            qfq_date = qfq_cols.get("date") or qfq_cols.get("trade_date")
            raw_close = raw_cols.get("close")
            qfq_close = qfq_cols.get("close") or qfq_cols.get("qfq_close")
            if not raw_date or not qfq_date or not raw_close or not qfq_close:
                read_errors[symbol] = "missing date/close columns"
                continue
            left = raw[[raw_date, raw_close]].rename(columns={raw_date: "trade_date", raw_close: "raw_close"})
            right = qfq[[qfq_date, qfq_close]].rename(columns={qfq_date: "trade_date", qfq_close: "qfq_close"})
            merged = left.merge(right, on="trade_date", how="inner")
            merged["raw_close"] = pd.to_numeric(merged["raw_close"], errors="coerce")
            merged["qfq_close"] = pd.to_numeric(merged["qfq_close"], errors="coerce")
            merged = merged.dropna(subset=["raw_close", "qfq_close"])
            merged = merged[merged["raw_close"] > 0].copy()
            merged["symbol"] = symbol
            merged["implied_adj_factor"] = merged["qfq_close"] / merged["raw_close"]
            merged["source"] = "local_raw_csv_plus_qfq_parquet"
            merged["provider"] = "local_existing"
            merged["proxy_type"] = "implied_adjustment_factor_proxy"
            merged["usable_as_formal_adj_factor"] = False
            rows.append(merged.tail(max_rows_per_symbol))
        except Exception as exc:
            read_errors[symbol] = f"{type(exc).__name__}: {exc}"
    if rows:
        out = pd.concat(rows, ignore_index=True)
        out = out[["symbol", "trade_date", "raw_close", "qfq_close", "implied_adj_factor", "source", "provider", "proxy_type", "usable_as_formal_adj_factor"]]
    else:
        out = pd.DataFrame(columns=["symbol", "trade_date", "raw_close", "qfq_close", "implied_adj_factor", "source", "provider", "proxy_type", "usable_as_formal_adj_factor"])
    summary = {
        "symbols_requested": len(symbols),
        "symbols_with_implied_factor": int(out["symbol"].nunique()) if not out.empty else 0,
        "rows": int(len(out)),
        "missing_qfq_symbols": missing_qfq[:50],
        "read_errors": read_errors,
        "usable_as_formal_adj_factor": False,
    }
    if not out.empty:
        pct = out.sort_values(["symbol", "trade_date"]).groupby("symbol")["implied_adj_factor"].transform(
            lambda values: values.ffill().pct_change(fill_method=None)
        ).abs()
        summary["extreme_jump_count"] = int((pct > 0.5).sum())
        summary["duplicate_symbol_date_count"] = int(out.duplicated(["symbol", "trade_date"]).sum())
    return out, summary


def cross_validate_adjustment_factors(implied: pd.DataFrame, existing_path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    if implied.empty or not existing_path.exists():
        return pd.DataFrame(), {
            "conclusion": "no_overlap_to_validate",
            "matched_symbol_count": 0,
            "matched_symbol_date_count": 0,
        }
    implied_keys = implied[["symbol", "trade_date"]].copy()
    implied_keys["symbol"] = implied_keys["symbol"].astype(str).str.zfill(6)
    implied_keys["trade_date"] = implied_keys["trade_date"].astype(str)
    wanted_symbols = set(implied_keys["symbol"])
    wanted_dates = set(implied_keys["trade_date"])
    chunks: list[pd.DataFrame] = []
    try:
        for chunk in read_csv_chunks(
            existing_path,
            dtype={"symbol": str, "trade_date": str},
            chunksize=250_000,
        ):
            if not {"symbol", "trade_date", "adj_factor"}.issubset(chunk.columns):
                return pd.DataFrame(), {"conclusion": "existing_adjustment_factors_inconclusive", "matched_symbol_count": 0, "matched_symbol_date_count": 0}
            chunk["symbol"] = chunk["symbol"].astype(str).str.zfill(6)
            chunk["trade_date"] = chunk["trade_date"].astype(str)
            subset = chunk[chunk["symbol"].isin(wanted_symbols) & chunk["trade_date"].isin(wanted_dates)]
            if not subset.empty:
                chunks.append(subset[["symbol", "trade_date", "adj_factor"]].copy())
    except Exception:
        existing_fallback = read_csv_frame(existing_path, dtype={"symbol": str, "trade_date": str})
        if not {"symbol", "trade_date", "adj_factor"}.issubset(existing_fallback.columns):
            return pd.DataFrame(), {"conclusion": "existing_adjustment_factors_inconclusive", "matched_symbol_count": 0, "matched_symbol_date_count": 0}
        existing_fallback["symbol"] = existing_fallback["symbol"].astype(str).str.zfill(6)
        existing_fallback["trade_date"] = existing_fallback["trade_date"].astype(str)
        chunks = [existing_fallback[["symbol", "trade_date", "adj_factor"]]]
    existing = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=["symbol", "trade_date", "adj_factor"])
    if not {"symbol", "trade_date", "adj_factor"}.issubset(existing.columns):
        return pd.DataFrame(), {"conclusion": "existing_adjustment_factors_inconclusive", "matched_symbol_count": 0, "matched_symbol_date_count": 0}
    existing["adj_factor"] = pd.to_numeric(existing["adj_factor"], errors="coerce")
    implied = implied.copy()
    implied["symbol"] = implied["symbol"].astype(str).str.zfill(6)
    implied["trade_date"] = implied["trade_date"].astype(str)
    merged = implied.merge(existing[["symbol", "trade_date", "adj_factor"]], on=["symbol", "trade_date"], how="outer", indicator=True)
    matched = merged[merged["_merge"] == "both"].copy()
    if matched.empty:
        return merged, {
            "conclusion": "no_overlap_to_validate",
            "matched_symbol_count": 0,
            "matched_symbol_date_count": 0,
            "missing_existing_factor_count": int((merged["_merge"] == "left_only").sum()),
            "missing_implied_factor_count": int((merged["_merge"] == "right_only").sum()),
        }
    matched["abs_diff"] = (matched["implied_adj_factor"] - matched["adj_factor"]).abs()
    severe = matched[matched["abs_diff"] > 0.05]
    corr = matched["implied_adj_factor"].corr(matched["adj_factor"]) if len(matched) > 1 else None
    p95 = matched["abs_diff"].quantile(0.95)
    if len(severe) == 0 and matched["abs_diff"].median() <= 0.01:
        conclusion = "existing_adjustment_factors_validated_for_pilot"
    elif len(severe) <= max(1, len(matched) * 0.05):
        conclusion = "existing_adjustment_factors_partially_supported"
    else:
        conclusion = "existing_adjustment_factors_conflicting"
    summary = {
        "conclusion": conclusion,
        "matched_symbol_count": int(matched["symbol"].nunique()),
        "matched_symbol_date_count": int(len(matched)),
        "median_abs_diff": float(matched["abs_diff"].median()),
        "p95_abs_diff": float(p95),
        "max_abs_diff": float(matched["abs_diff"].max()),
        "correlation": None if pd.isna(corr) else float(corr),
        "mismatch_count": int((matched["abs_diff"] > 0.01).sum()),
        "severe_mismatch_count": int(len(severe)),
        "missing_existing_factor_count": int((merged["_merge"] == "left_only").sum()),
        "missing_implied_factor_count": int((merged["_merge"] == "right_only").sum()),
        "pilot_only_not_full_market": True,
    }
    return merged, summary


def pre_close_consistency_from_daily(daily_dir: Path, symbols: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows = []
    files_with_pre_close = 0
    for symbol in symbols:
        path = daily_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        df = read_csv_frame(path, dtype={"symbol": str})
        cols = {c.lower(): c for c in df.columns}
        date_col = cols.get("date") or cols.get("trade_date")
        close_col = cols.get("close")
        pre_col = cols.get("pre_close")
        if not date_col or not close_col:
            continue
        df = df.sort_values(date_col).copy()
        df["previous_close_proxy"] = pd.to_numeric(df[close_col], errors="coerce").shift(1)
        if pre_col:
            files_with_pre_close += 1
            diff = (pd.to_numeric(df[pre_col], errors="coerce") - df["previous_close_proxy"]).abs()
            rows.append(pd.DataFrame({"symbol": symbol, "trade_date": df[date_col], "pre_close": df[pre_col], "previous_close_proxy": df["previous_close_proxy"], "abs_diff": diff, "usable_as_formal_pre_close": True}))
        else:
            rows.append(pd.DataFrame({"symbol": symbol, "trade_date": df[date_col], "pre_close": None, "previous_close_proxy": df["previous_close_proxy"], "abs_diff": None, "usable_as_formal_pre_close": False}))
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["symbol", "trade_date", "pre_close", "previous_close_proxy", "abs_diff", "usable_as_formal_pre_close"])
    conclusion = "pre_close_formal_available" if files_with_pre_close else ("pre_close_proxy_available" if len(out) else "pre_close_missing_required_external_truth")
    if files_with_pre_close == 0:
        conclusion = "pre_close_missing_required_external_truth"
    return out, {
        "conclusion": conclusion,
        "files_checked": len(symbols),
        "files_with_pre_close": files_with_pre_close,
        "proxy_rows": int(len(out)),
        "pre_close_formal_materialized": False,
    }
