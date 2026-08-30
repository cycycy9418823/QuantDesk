from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from quantdesk.csv_io import read_csv_frame
from quantdesk.factors.pipeline import compute_rank_ic, ic_summary, standardize_factors
from quantdesk.shared import now_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "features" / "daily_features_latest.csv"
DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "results" / "factor_ic_report_latest.json"
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "results" / "factor_ic_report_latest.csv"
DEFAULT_REPORT_MD = PROJECT_ROOT / "reports" / "factor_ic_report_latest.md"
PROHIBITED_USAGE = ["live_trading", "broker_connection", "auto_execution", "buy_sell_recommendation"]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _load_factor_input(path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    target = _project_path(path)
    if not target.exists():
        return pd.DataFrame(), [f"missing_input:{target}"]
    df = read_csv_frame(target)
    warnings.extend(str(item) for item in df.attrs.get("warnings", []) if item)
    if "date" not in df.columns:
        if "latest_date" in df.columns:
            df = df.rename(columns={"latest_date": "date"})
            warnings.append("date_derived_from_latest_date")
        else:
            warnings.append("missing_date_column")
    if "symbol" not in df.columns:
        warnings.append("missing_symbol_column")
    return df, warnings


def build_factor_ic_report(
    input_csv: str | Path = DEFAULT_INPUT,
    *,
    forward_return_col: str = "fwd_ret_1m",
    industry_map: dict[str, str] | None = None,
) -> tuple[dict[str, Any], pd.DataFrame]:
    df, warnings = _load_factor_input(input_csv)
    if df.empty:
        payload = _base_payload("failed", input_csv, warnings)
        return payload, pd.DataFrame()
    required = {"symbol", "date", forward_return_col}
    missing = sorted(required - set(df.columns))
    if missing:
        warnings.append(f"missing_required_columns:{','.join(missing)}")
        payload = _base_payload("warning", input_csv, warnings)
        payload.update({"row_count": int(len(df)), "ic_summary": {}, "factor_count": 0})
        return payload, pd.DataFrame()
    factor_df = df.copy()
    numeric_cols = [
        col for col in factor_df.columns
        if col not in {"symbol", "date"} and pd.api.types.is_numeric_dtype(factor_df[col])
    ]
    factor_df = factor_df[["symbol", "date", *numeric_cols]].copy()
    standardized, log = standardize_factors(factor_df, industry_map=industry_map)
    ic_df = compute_rank_ic(standardized, forward_return_col=forward_return_col)
    summary = ic_summary(ic_df)
    status = "passed" if summary else "warning"
    if not summary:
        warnings.append("no_factor_ic_summary")
    payload = _base_payload(status, input_csv, warnings)
    payload.update(
        {
            "row_count": int(len(df)),
            "factor_count": len([c for c in numeric_cols if c != forward_return_col]),
            "forward_return_col": forward_return_col,
            "standardization_log": log,
            "ic_summary": summary,
            "ic_period_count": int(len(ic_df)),
        }
    )
    return payload, ic_df


def _base_payload(status: str, source: str | Path, warnings: list[str]) -> dict[str, Any]:
    return {
        "task_id": "factor_ic_report",
        "status": status,
        "research_only": True,
        "source_files": [str(_project_path(source))],
        "warnings": list(dict.fromkeys(warnings)),
        "allowed_usage": ["factor_research", "paper_research", "human_review"],
        "prohibited_usage": PROHIBITED_USAGE,
        "generated_at": now_text(),
        "disclaimer": "Research-only factor diagnostics. No broker, no live trading, no buy/sell recommendation.",
    }


def render_factor_ic_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Factor IC Report",
        "",
        "Research-only factor diagnostics. This report does not provide trading advice.",
        "",
        f"- status: `{payload.get('status')}`",
        f"- row_count: `{payload.get('row_count', 0)}`",
        f"- factor_count: `{payload.get('factor_count', 0)}`",
        f"- ic_period_count: `{payload.get('ic_period_count', 0)}`",
        "",
        "## IC Summary",
    ]
    summary = payload.get("ic_summary") if isinstance(payload.get("ic_summary"), dict) else {}
    if summary:
        for factor, stats in summary.items():
            lines.append(
                f"- `{factor}`: mean_ic={stats.get('mean_ic')}, t_stat={stats.get('t_stat')}, hit_rate={stats.get('hit_rate')}"
            )
    else:
        lines.append("- No valid IC summary.")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", *(f"- {item}" for item in payload.get("warnings", []))])
    lines.extend(["", "## Prohibited Usage", *(f"- {item}" for item in payload.get("prohibited_usage", []))])
    return "\n".join(lines) + "\n"


def write_factor_ic_report(
    payload: dict[str, Any],
    ic_df: pd.DataFrame,
    *,
    output_json: str | Path = DEFAULT_OUTPUT_JSON,
    output_csv: str | Path = DEFAULT_OUTPUT_CSV,
    report_md: str | Path = DEFAULT_REPORT_MD,
) -> dict[str, str]:
    json_path = _project_path(output_json)
    csv_path = _project_path(output_csv)
    md_path = _project_path(report_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    ic_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(render_factor_ic_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "csv_path": str(csv_path), "report_path": str(md_path)}
