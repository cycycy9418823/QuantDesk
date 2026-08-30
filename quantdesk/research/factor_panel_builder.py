from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.factors.ashare_factors import compute_ashare_factors
from quantdesk.factors.liquidity import compute_liquidity_factors
from quantdesk.factors.momentum import compute_momentum_factors
from quantdesk.factors.volatility import compute_volatility_factors
from quantdesk.price_basis import (
    PRICE_BASIS_UNKNOWN,
    PRICE_DATA_CONTRACT_VERSION,
    price_basis_labels,
    validate_price_frame_set,
)
from quantdesk.shared import normalize_symbol, now_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DAILY_DIR = PROJECT_ROOT / "data" / "daily"
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "results" / "factor_panel_latest.csv"
DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "results" / "factor_panel_latest.json"
DEFAULT_REPORT_MD = PROJECT_ROOT / "reports" / "factor_panel_latest.md"
PROHIBITED_USAGE = ["live_trading", "broker_connection", "auto_execution", "buy_sell_recommendation"]


DAILY_ALIASES = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover",
}


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _read_daily_csv(path: Path) -> pd.DataFrame:
    df = read_csv_frame(path)
    return _normalize_daily_frame(df, path)


def _normalize_daily_frame(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    warnings = list(df.attrs.get("warnings") or [])
    if df.empty:
        out = pd.DataFrame()
        out.attrs["warnings"] = warnings
        return out
    df = df.rename(columns={col: DAILY_ALIASES.get(str(col), str(col)) for col in df.columns})
    if "symbol" not in df.columns:
        df["symbol"] = normalize_symbol(path.stem)
    else:
        df["symbol"] = df["symbol"].map(normalize_symbol)
    if "date" not in df.columns or "close" not in df.columns:
        out = pd.DataFrame()
        out.attrs["warnings"] = warnings + [f"daily_csv_missing_required_columns:{path}"]
        return out
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["open", "high", "low"]:
        if col not in df.columns:
            df[col] = df["close"]
    if "volume" not in df.columns:
        df["volume"] = pd.NA
    if "amount" not in df.columns:
        df["amount"] = pd.NA
    out = df.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates(["symbol", "date"], keep="last")
    out.attrs["warnings"] = warnings
    return out


def _sample_dates(dates: pd.Series, frequency: str, max_dates: int) -> list[pd.Timestamp]:
    unique = pd.Series(pd.to_datetime(dates.dropna().unique())).sort_values()
    if unique.empty:
        return []
    freq = str(frequency or "M").upper()
    if freq in {"D", "DAILY"}:
        sampled = unique
    else:
        sampled = unique.groupby(unique.dt.to_period("M")).max()
    if max_dates > 0:
        sampled = sampled.tail(max_dates)
    return [pd.Timestamp(x) for x in sampled.tolist()]


def build_factor_panel(
    daily_dir: str | Path = DEFAULT_DAILY_DIR,
    *,
    symbols: list[str] | None = None,
    limit_symbols: int = 200,
    frequency: str = "M",
    max_dates: int = 36,
    forward_days: int = 20,
) -> tuple[dict[str, Any], pd.DataFrame]:
    root = _project_path(daily_dir)
    warnings: list[str] = []
    if not root.exists():
        return _base_payload("failed", root, [f"missing_daily_dir:{root}"]), pd.DataFrame()
    if symbols:
        paths = [root / f"{normalize_symbol(symbol)}.csv" for symbol in symbols]
    else:
        paths = sorted(root.glob("*.csv"))
        if limit_symbols > 0:
            paths = paths[:limit_symbols]
    raw_frames = {normalize_symbol(path.stem): read_csv_frame(path) for path in paths}
    price_validation = validate_price_frame_set(raw_frames, consumer="feature")
    if not price_validation.get("eligible"):
        warnings.extend(str(item) for frame in raw_frames.values() for item in frame.attrs.get("warnings", []) if item)
        warnings.extend(str(item) for item in price_validation.get("reason_codes") or [])
        return _base_payload("blocked", root, warnings, price_validation=price_validation), pd.DataFrame()
    read_frames = [
        _normalize_daily_frame(raw_frames[normalize_symbol(path.stem)], path)
        for path in paths
    ]
    for frame in read_frames:
        warnings.extend(str(item) for item in frame.attrs.get("warnings", []) if item)
    frames = [frame for frame in read_frames if not frame.empty]
    if not frames:
        return _base_payload("failed", root, ["no_valid_daily_csv"]), pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    panel = compute_momentum_factors(panel)
    panel = compute_volatility_factors(panel)
    panel = compute_liquidity_factors(panel)
    panel = compute_ashare_factors(panel)
    panel["fwd_ret_1m"] = panel.groupby("symbol", group_keys=False)["close"].transform(
        lambda s: s.shift(-forward_days) / s - 1
    )
    sample_dates = set(_sample_dates(panel["date"], frequency, max_dates))
    sampled = panel[panel["date"].isin(sample_dates)].copy()
    factor_cols = [
        "momentum_5d",
        "momentum_20d",
        "momentum_60d",
        "reversal_5d",
        "realized_volatility_20d",
        "parkinson_volatility_20d",
        "intraday_range_ratio_20d",
        "turnover_proxy_20d",
        "amihud_illiquidity_20d",
        "volume_price_corr_20d",
        "limit_up_freq_20d",
        "limit_down_freq_20d",
        "turnover_decay_20d",
    ]
    output_cols = ["date", "symbol", "close", "adjust", "fwd_ret_1m", *factor_cols]
    sampled = sampled[[col for col in output_cols if col in sampled.columns]].replace([float("inf"), float("-inf")], pd.NA)
    sampled = sampled.dropna(subset=["fwd_ret_1m"])
    status = "passed" if not sampled.empty else "warning"
    if sampled.empty:
        warnings.append("empty_factor_panel_after_forward_return")
    payload = _base_payload(status, root, warnings, price_validation=price_validation)
    payload.update(
        {
            "symbol_count": int(sampled["symbol"].nunique()) if not sampled.empty else 0,
            "row_count": int(len(sampled)),
            "date_count": int(sampled["date"].nunique()) if not sampled.empty else 0,
            "date_start": str(sampled["date"].min().date()) if not sampled.empty else "",
            "date_end": str(sampled["date"].max().date()) if not sampled.empty else "",
            "factor_columns": [col for col in factor_cols if col in sampled.columns],
            "forward_return_col": "fwd_ret_1m",
            "frequency": frequency,
            "forward_days": int(forward_days),
            "limit_symbols": int(limit_symbols),
        }
    )
    sampled["date"] = pd.to_datetime(sampled["date"]).dt.strftime("%Y-%m-%d")
    return payload, sampled.reset_index(drop=True)


def _base_payload(
    status: str,
    source: str | Path,
    warnings: list[str],
    *,
    price_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Factor panel reads flat ``data/daily`` (raw canonical). Attach Option C
    # price-basis labels so consumers know these factors use raw prices and see
    # any divergent adjustment-factor alias warning. This appends metadata only;
    # it does not change factor values or forward returns.
    validation = deepcopy(price_validation) if isinstance(price_validation, dict) else {
        "contract_version": PRICE_DATA_CONTRACT_VERSION,
        "consumer": "feature",
        "status": "blocked",
        "eligible": False,
        "accepted_price_basis": PRICE_BASIS_UNKNOWN,
        "basis_counts": {},
        "reason_codes": ["price_policy_validation_not_run"],
    }
    accepted_basis = str(validation.get("accepted_price_basis") or PRICE_BASIS_UNKNOWN)
    labels = price_basis_labels(accepted_basis, extra_warnings=list(warnings))
    return {
        "task_id": "factor_panel_builder",
        "status": status,
        "research_only": True,
        "source_files": [str(source)],
        "warnings": list(dict.fromkeys(warnings)),
        "reason_codes": list(dict.fromkeys([*list(validation.get("reason_codes") or []), "pit_filter_not_applied"])),
        "degraded_reasons": ["pit_filter_not_applied"],
        "price_data_contract_version": PRICE_DATA_CONTRACT_VERSION,
        "price_policy_validation": validation,
        "evidence_eligible": validation.get("eligible") is True,
        "formal_factor_evidence_eligible": False,
        "price_basis": labels["price_basis"],
        "price_basis_label": labels["price_basis_label"],
        "adjustment_basis": labels["adjustment_basis"],
        "adjustment_warnings": labels["adjustment_warnings"],
        "allowed_usage": ["factor_research", "paper_research", "human_review"],
        "prohibited_usage": PROHIBITED_USAGE,
        "generated_at": now_text(),
        "disclaimer": "Research-only factor panel. No broker, no live trading, no buy/sell recommendation.",
    }


def render_factor_panel_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Factor Panel Builder",
        "",
        "Research-only local factor panel. This is not a trading signal.",
        "",
        f"- status: `{payload.get('status')}`",
        f"- row_count: `{payload.get('row_count', 0)}`",
        f"- symbol_count: `{payload.get('symbol_count', 0)}`",
        f"- date_count: `{payload.get('date_count', 0)}`",
        f"- date_range: `{payload.get('date_start', '')}` to `{payload.get('date_end', '')}`",
        f"- forward_return_col: `{payload.get('forward_return_col', '')}`",
        "",
        "## Factor Columns",
        *(f"- {col}" for col in payload.get("factor_columns", [])),
    ]
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", *(f"- {item}" for item in payload.get("warnings", []))])
    lines.extend(["", "## Prohibited Usage", *(f"- {item}" for item in payload.get("prohibited_usage", []))])
    return "\n".join(lines) + "\n"


def write_factor_panel(
    payload: dict[str, Any],
    panel: pd.DataFrame,
    *,
    output_csv: str | Path = DEFAULT_OUTPUT_CSV,
    output_json: str | Path = DEFAULT_OUTPUT_JSON,
    report_md: str | Path = DEFAULT_REPORT_MD,
) -> dict[str, str]:
    csv_path = _project_path(output_csv)
    json_path = _project_path(output_json)
    md_path = _project_path(report_md)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(csv_path, index=False, encoding="utf-8-sig")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_factor_panel_markdown(payload), encoding="utf-8")
    return {"csv_path": str(csv_path), "json_path": str(json_path), "report_path": str(md_path)}
