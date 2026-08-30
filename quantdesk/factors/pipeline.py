from __future__ import annotations

import numpy as np
import pandas as pd


def winsorize_series(s: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    clean = pd.to_numeric(s, errors="coerce")
    if clean.dropna().empty:
        return clean
    lo, hi = clean.quantile(lower), clean.quantile(upper)
    return clean.clip(lo, hi)


def cross_sectional_zscore(s: pd.Series) -> pd.Series:
    clean = pd.to_numeric(s, errors="coerce")
    std = clean.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return pd.Series(0.0, index=s.index).where(clean.notna(), np.nan)
    return (clean - clean.mean()) / std


def neutralize_by_industry(factor: pd.Series, industry: pd.Series) -> pd.Series:
    aligned = pd.DataFrame({"factor": pd.to_numeric(factor, errors="coerce"), "industry": industry}, index=factor.index)
    valid = aligned["factor"].notna() & aligned["industry"].notna()
    residual = pd.Series(np.nan, index=factor.index, dtype=float)
    if valid.sum() < 2:
        return factor.astype(float)
    dummies = pd.get_dummies(aligned.loc[valid, "industry"], drop_first=True, dtype=float)
    X = np.column_stack([np.ones(len(dummies)), dummies.to_numpy()])
    y = aligned.loc[valid, "factor"].to_numpy(dtype=float)
    try:
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        residual.loc[valid] = y - (X @ beta)
    except np.linalg.LinAlgError:
        residual.loc[valid] = y
    return residual


def standardize_factors(
    factor_df: pd.DataFrame,
    industry_map: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    required = {"symbol", "date"}
    missing = required - set(factor_df.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    result = factor_df.copy()
    log = {"warnings": [], "factors_processed": []}
    factor_cols = [c for c in result.columns if c not in {"symbol", "date"} and pd.api.types.is_numeric_dtype(result[c])]
    for col in factor_cols:
        result[col] = result.groupby("date", group_keys=False)[col].transform(winsorize_series)
        result[col] = result.groupby("date", group_keys=False)[col].transform(cross_sectional_zscore)
        if industry_map:
            result["_industry"] = result["symbol"].map(industry_map)
            neutralized = pd.Series(np.nan, index=result.index, dtype=float)
            for _date, group in result.groupby("date", sort=False):
                neutralized.loc[group.index] = neutralize_by_industry(group[col], group["_industry"])
            result[col] = neutralized
        result[col] = pd.to_numeric(result[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        log["factors_processed"].append(col)
    if "_industry" in result.columns:
        result = result.drop(columns=["_industry"])
    if not factor_cols:
        log["warnings"].append("no_numeric_factor_columns")
    return result, log


def compute_rank_ic(factor_df: pd.DataFrame, forward_return_col: str = "fwd_ret_1m") -> pd.DataFrame:
    required = {"symbol", "date", forward_return_col}
    missing = required - set(factor_df.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    factor_cols = [
        c for c in factor_df.columns
        if c not in {"symbol", "date", forward_return_col} and pd.api.types.is_numeric_dtype(factor_df[c])
    ]
    records: list[dict[str, object]] = []
    for date, group in factor_df.groupby("date"):
        record: dict[str, object] = {"date": date}
        fwd = pd.to_numeric(group[forward_return_col], errors="coerce")
        for col in factor_cols:
            factor = pd.to_numeric(group[col], errors="coerce")
            valid = factor.notna() & fwd.notna()
            if valid.sum() < 3 or factor[valid].nunique() <= 1 or fwd[valid].nunique() <= 1:
                record[col] = np.nan
            else:
                record[col] = round(float(factor[valid].rank().corr(fwd[valid].rank())), 6)
        records.append(record)
    return pd.DataFrame(records)


def ic_summary(ic_df: pd.DataFrame) -> dict:
    summary: dict[str, dict[str, float | int]] = {}
    for col in ic_df.columns:
        if col == "date":
            continue
        series = pd.to_numeric(ic_df[col], errors="coerce").dropna()
        if len(series) < 2:
            continue
        mean_ic = float(series.mean())
        std_ic = float(series.std(ddof=0))
        ir = mean_ic / std_ic if std_ic > 0 else 0.0
        t_stat = mean_ic / (std_ic / np.sqrt(len(series))) if std_ic > 0 else 0.0
        summary[col] = {
            "mean_ic": round(mean_ic, 6),
            "std_ic": round(std_ic, 6),
            "ir": round(ir, 4),
            "t_stat": round(t_stat, 4),
            "hit_rate": round(float((series > 0).mean()), 4),
            "periods": int(len(series)),
        }
    return summary
