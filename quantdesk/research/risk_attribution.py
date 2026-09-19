from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from quantdesk.research.factor_attribution import factor_attribution
from quantdesk.shared import now_text
from quantdesk.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = "Research attribution only. Not investment advice. No broker. No real orders."


def _project_path(path: str | Path | None) -> Path | None:
    if path in (None, ""):
        return None
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return value
    return value


def _records(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if hasattr(payload, "to_dict"):
        try:
            rows = payload.to_dict(orient="records")
            return [dict(row) for row in rows]
        except Exception:
            pass
    if isinstance(payload, dict):
        if "rows" in payload and isinstance(payload["rows"], list):
            return [dict(row) for row in payload["rows"] if isinstance(row, dict)]
        if all(isinstance(value, dict) for value in payload.values()):
            return [dict(value, symbol=str(key)) for key, value in payload.items()]
        return [dict(payload)]
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    return []


def _read_table(path: str | Path | None) -> tuple[list[dict[str, Any]], list[str]]:
    target = _project_path(path)
    if target is None:
        return [], []
    if not target.exists():
        return [], [f"missing_input:{_display_path(target)}"]
    try:
        if target.suffix.lower() == ".json":
            raw = json.loads(target.read_text(encoding="utf-8"))
            return _records(raw), []
        with target.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)], []
    except Exception as exc:
        return [], [f"invalid_input:{_display_path(target)}:{exc}"]


def _industry_map_from_any(industry_map: Any) -> dict[str, str]:
    if isinstance(industry_map, dict):
        return {str(key): str(value.get("industry") if isinstance(value, dict) else value) for key, value in industry_map.items()}
    mapping: dict[str, str] = {}
    for row in _records(industry_map):
        symbol = str(row.get("symbol") or row.get("code") or "").strip()
        industry = str(row.get("industry") or row.get("sector") or "").strip()
        if symbol and industry:
            mapping[symbol] = industry
    return mapping


def _table_to_symbol_values(rows: list[dict[str, Any]], value_keys: tuple[str, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("code") or "").strip()
        if not symbol:
            continue
        for key in value_keys:
            if key in row:
                out[symbol] = _safe_float(row.get(key))
                break
    return out


def _normalize_weights(weights: dict[str, float], warnings: list[str], label: str) -> dict[str, float]:
    total = sum(weights.values())
    if not weights or abs(total) <= 1e-12:
        warnings.append(f"{label}_weights_empty_or_zero")
        return dict(weights)
    if abs(total - 1.0) > 1e-8:
        warnings.append(f"{label}_weight_normalization:sum={round(total, 10)}")
        return {symbol: weight / total for symbol, weight in weights.items()}
    return dict(weights)


def _nested_periods(payload: Any) -> dict[str, dict[str, float]]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for period, values in payload.items():
        if isinstance(values, dict):
            flat = {
                str(symbol): _safe_float(value)
                for symbol, value in values.items()
                if not isinstance(value, (dict, list))
            }
            if flat:
                out[str(period)] = flat
    return out


def _rows_by_period(rows: list[dict[str, Any]], value_keys: tuple[str, ...]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        period = str(row.get("date") or row.get("period") or "single")
        symbol = str(row.get("symbol") or row.get("code") or "").strip()
        if not symbol:
            continue
        for key in value_keys:
            if key in row:
                out.setdefault(period, {})[symbol] = _safe_float(row.get(key))
                break
    return out


def _group_by_industry(
    returns: dict[str, float],
    weights: dict[str, float],
    industries: dict[str, str],
) -> dict[str, dict[str, float]]:
    buckets: dict[str, dict[str, float]] = {}
    for symbol, weight in weights.items():
        industry = industries.get(symbol) or "unknown"
        row = buckets.setdefault(industry, {"weight": 0.0, "weighted_return": 0.0})
        ret = returns.get(symbol, 0.0)
        row["weight"] += weight
        row["weighted_return"] += weight * ret
    for row in buckets.values():
        row["return"] = row["weighted_return"] / row["weight"] if abs(row["weight"]) > 1e-12 else 0.0
    return buckets


def _normalize_periods(portfolio_returns: Any, portfolio_weights: Any, benchmark_weights: Any) -> list[tuple[str, dict[str, float], dict[str, float], dict[str, float]]]:
    nested_returns = _nested_periods(portfolio_returns)
    nested_pw = _nested_periods(portfolio_weights)
    nested_bw = _nested_periods(benchmark_weights)
    if nested_returns and nested_pw and nested_bw:
        periods = sorted(set(nested_returns) & set(nested_pw) & set(nested_bw))
        return [(period, nested_returns[period], nested_pw[period], nested_bw[period]) for period in periods]
    if isinstance(portfolio_returns, dict) and isinstance(portfolio_weights, dict) and isinstance(benchmark_weights, dict):
        returns = {str(key): _safe_float(value) for key, value in portfolio_returns.items() if not isinstance(value, (dict, list))}
        p_weights = {str(key): _safe_float(value) for key, value in portfolio_weights.items() if not isinstance(value, (dict, list))}
        b_weights = {str(key): _safe_float(value) for key, value in benchmark_weights.items() if not isinstance(value, (dict, list))}
        if returns and p_weights and b_weights:
            return [("single", returns, p_weights, b_weights)]
    p_rows = _records(portfolio_returns)
    pw_rows = _records(portfolio_weights)
    bw_rows = _records(benchmark_weights)
    if p_rows and pw_rows and bw_rows:
        returns_by_period = _rows_by_period(p_rows, ("return", "portfolio_return", "ret"))
        pw_by_period = _rows_by_period(pw_rows, ("weight", "portfolio_weight"))
        bw_by_period = _rows_by_period(bw_rows, ("weight", "benchmark_weight"))
        common = sorted(set(returns_by_period) & set(pw_by_period) & set(bw_by_period))
        if common:
            return [(period, returns_by_period[period], pw_by_period[period], bw_by_period[period]) for period in common]
    return []


def brinson_attribution(
    portfolio_returns: Any,
    portfolio_weights: Any,
    benchmark_weights: Any,
    industry_map: Any,
    *,
    method: str = "brinson_fachler",
) -> dict[str, Any]:
    warnings: list[str] = []
    if method != "brinson_fachler":
        return {"available": False, "method": method, "warnings": [f"unsupported_brinson_method:{method}"]}
    industries = _industry_map_from_any(industry_map)
    if not industries:
        warnings.append("missing_industry_map")
    periods = _normalize_periods(portfolio_returns, portfolio_weights, benchmark_weights)
    if not periods:
        return {
            "available": False,
            "method": method,
            "allocation_effect": 0.0,
            "selection_effect": 0.0,
            "interaction_effect": 0.0,
            "total_explained_excess_return": 0.0,
            "residual_or_rounding_error": 0.0,
            "by_industry": [],
            "warnings": warnings + ["missing_portfolio_or_benchmark_inputs"],
        }
    totals = {"allocation_effect": 0.0, "selection_effect": 0.0, "interaction_effect": 0.0}
    by_industry_acc: dict[str, dict[str, float]] = {}
    portfolio_total = 0.0
    benchmark_total = 0.0
    for _period, returns, p_weights, b_weights in periods:
        if not b_weights:
            warnings.append("missing_benchmark_weights")
        p_weights = _normalize_weights(p_weights, warnings, "portfolio")
        b_weights = _normalize_weights(b_weights, warnings, "benchmark")
        missing_industry_symbols = sorted((set(p_weights) | set(b_weights)) - set(industries))
        if missing_industry_symbols:
            warnings.append("missing_industry_map_symbols:" + ",".join(missing_industry_symbols[:5]))
        p_groups = _group_by_industry(returns, p_weights, industries)
        b_groups = _group_by_industry(returns, b_weights, industries)
        rb_total = sum(row["weighted_return"] for row in b_groups.values())
        rp_total = sum(row["weighted_return"] for row in p_groups.values())
        portfolio_total += rp_total
        benchmark_total += rb_total
        for industry in sorted(set(p_groups) | set(b_groups)):
            pg = p_groups.get(industry, {"weight": 0.0, "return": 0.0})
            bg = b_groups.get(industry, {"weight": 0.0, "return": 0.0})
            wp = pg["weight"]
            wb = bg["weight"]
            rp = pg["return"]
            rb = bg["return"]
            allocation = (wp - wb) * (rb - rb_total)
            selection = wb * (rp - rb)
            interaction = (wp - wb) * (rp - rb)
            acc = by_industry_acc.setdefault(industry, {
                "industry": industry,
                "portfolio_weight": 0.0,
                "benchmark_weight": 0.0,
                "portfolio_return": 0.0,
                "benchmark_return": 0.0,
                "allocation_effect": 0.0,
                "selection_effect": 0.0,
                "interaction_effect": 0.0,
            })
            acc["portfolio_weight"] += wp
            acc["benchmark_weight"] += wb
            acc["portfolio_return"] += rp
            acc["benchmark_return"] += rb
            acc["allocation_effect"] += allocation
            acc["selection_effect"] += selection
            acc["interaction_effect"] += interaction
            totals["allocation_effect"] += allocation
            totals["selection_effect"] += selection
            totals["interaction_effect"] += interaction
    period_count = max(1, len(periods))
    explained = totals["allocation_effect"] + totals["selection_effect"] + totals["interaction_effect"]
    excess = portfolio_total - benchmark_total
    by_industry = []
    for row in by_industry_acc.values():
        item = dict(row)
        for key in ("portfolio_weight", "benchmark_weight", "portfolio_return", "benchmark_return"):
            item[key] = item[key] / period_count
        item["total_effect"] = item["allocation_effect"] + item["selection_effect"] + item["interaction_effect"]
        by_industry.append({key: round(value, 10) if isinstance(value, float) else value for key, value in item.items()})
    return _json_safe({
        "available": True,
        "method": method,
        "period_count": period_count,
        "normalized_weights": any("weight_normalization" in item for item in warnings),
        "allocation_effect": round(totals["allocation_effect"], 10),
        "selection_effect": round(totals["selection_effect"], 10),
        "interaction_effect": round(totals["interaction_effect"], 10),
        "total_explained_excess_return": round(explained, 10),
        "residual_or_rounding_error": round(excess - explained, 10),
        "by_industry": sorted(by_industry, key=lambda item: abs(float(item.get("total_effect") or 0.0)), reverse=True),
        "warnings": list(dict.fromkeys(warnings)),
    })


def summarize_brinson_attribution(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "allocation_effect": result.get("allocation_effect", 0.0),
        "selection_effect": result.get("selection_effect", 0.0),
        "interaction_effect": result.get("interaction_effect", 0.0),
        "total_explained_excess_return": result.get("total_explained_excess_return", 0.0),
        "residual_or_rounding_error": result.get("residual_or_rounding_error", 0.0),
        "industry_count": len(result.get("by_industry") or []),
    }


def synthetic_attribution_inputs() -> dict[str, Any]:
    return {
        "portfolio_returns": {"AAA": 0.10, "BBB": 0.02, "CCC": 0.04, "DDD": 0.01},
        "portfolio_weights": {"AAA": 0.40, "BBB": 0.20, "CCC": 0.30, "DDD": 0.10},
        "benchmark_weights": {"AAA": 0.25, "BBB": 0.25, "CCC": 0.25, "DDD": 0.25},
        "industry_map": {"AAA": "Tech", "BBB": "Tech", "CCC": "Finance", "DDD": "Finance"},
        "factor_returns": {"value": [0.01, 0.02, -0.01, 0.015], "momentum": [0.02, -0.01, 0.03, 0.01]},
        "portfolio_excess_returns": [0.021, 0.006, 0.013, 0.012],
    }


def load_attribution_inputs(
    portfolio_path: str | Path | None = None,
    benchmark_path: str | Path | None = None,
    industry_map_path: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    p_rows, p_warnings = _read_table(portfolio_path)
    b_rows, b_warnings = _read_table(benchmark_path)
    i_rows, i_warnings = _read_table(industry_map_path)
    warnings.extend(p_warnings + b_warnings + i_warnings)
    if not p_rows or not b_rows or not i_rows:
        warnings.append("insufficient_external_inputs")
        return {}, list(dict.fromkeys(warnings))
    return {
        "portfolio_returns": p_rows,
        "portfolio_weights": p_rows,
        "benchmark_weights": b_rows,
        "industry_map": i_rows,
        "period": {"start_date": start_date or "", "end_date": end_date or ""},
    }, list(dict.fromkeys(warnings))


def build_risk_attribution_report(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    warnings: list[str] = []
    synthetic = bool(cfg.get("synthetic_test_data", False))
    inputs: dict[str, Any]
    if synthetic:
        inputs = synthetic_attribution_inputs()
    else:
        inputs, load_warnings = load_attribution_inputs(
            cfg.get("portfolio_returns") or cfg.get("portfolio_path"),
            cfg.get("benchmark_weights") or cfg.get("benchmark_path"),
            cfg.get("industry_map"),
            cfg.get("start_date"),
            cfg.get("end_date"),
        )
        warnings.extend(load_warnings)
    brinson = brinson_attribution(
        inputs.get("portfolio_returns"),
        inputs.get("portfolio_weights"),
        inputs.get("benchmark_weights"),
        inputs.get("industry_map"),
    )
    warnings.extend(brinson.get("warnings") or [])
    factor = factor_attribution(
        inputs.get("portfolio_excess_returns") or [],
        inputs.get("factor_exposures"),
        inputs.get("factor_returns"),
    )
    warnings.extend(factor.get("warnings") or [])
    portfolio_return = 0.0
    benchmark_return = 0.0
    if brinson.get("available"):
        explained = _safe_float(brinson.get("total_explained_excess_return"))
        residual = _safe_float(brinson.get("residual_or_rounding_error"))
        excess = explained + residual
        benchmark_return = sum(_safe_float(row.get("benchmark_weight")) * _safe_float(row.get("benchmark_return")) for row in brinson.get("by_industry") or [])
        portfolio_return = benchmark_return + excess
    status = "warning" if warnings or not brinson.get("available") else "passed"
    payload = {
        "ok": True,
        "status": status,
        "task_type": "risk_attribution",
        "attribution_mode": "brinson_fachler",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "network": False,
        "synthetic_test_data": synthetic,
        "period": {"start_date": cfg.get("start_date") or "", "end_date": cfg.get("end_date") or ""},
        "inputs": {
            "portfolio_returns": str(cfg.get("portfolio_returns") or cfg.get("portfolio_path") or ("synthetic" if synthetic else "")),
            "benchmark_weights": str(cfg.get("benchmark_weights") or cfg.get("benchmark_path") or ("synthetic" if synthetic else "")),
            "industry_map": str(cfg.get("industry_map") or ("synthetic" if synthetic else "")),
        },
        "input_status": {
            "portfolio": "synthetic" if synthetic else ("available" if cfg.get("portfolio_returns") or cfg.get("portfolio_path") else "missing"),
            "benchmark": "synthetic" if synthetic else ("available" if cfg.get("benchmark_weights") or cfg.get("benchmark_path") else "missing"),
            "industry_map": "synthetic" if synthetic else ("available" if cfg.get("industry_map") else "missing"),
        },
        "portfolio_return": round(portfolio_return, 10),
        "benchmark_return": round(benchmark_return, 10),
        "excess_return": round(portfolio_return - benchmark_return, 10),
        "brinson": brinson,
        "factor_attribution": factor,
        "risk_flags": ["synthetic_test_data_not_market_data"] if synthetic else [],
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "errors": [],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(payload)


def render_risk_attribution_markdown(payload: dict[str, Any]) -> str:
    brinson = payload.get("brinson") or {}
    factor = payload.get("factor_attribution") or {}
    lines = [
        "# Risk Attribution",
        "",
        "Research attribution only. Brinson and factor outputs are explanatory diagnostics, not trading signals.",
        "",
        "## Status",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Mode: `{payload.get('attribution_mode')}`",
        f"- Synthetic test data: `{str(bool(payload.get('synthetic_test_data'))).lower()}`",
        f"- Generated at: {payload.get('generated_at')}",
        "",
        "## Inputs",
        "",
    ]
    for key, value in (payload.get("inputs") or {}).items():
        lines.append(f"- {key}: `{value or '-'}`")
    lines += [
        "",
        "## Portfolio vs benchmark return",
        "",
        f"- Portfolio return: {payload.get('portfolio_return')}",
        f"- Benchmark return: {payload.get('benchmark_return')}",
        f"- Excess return: {payload.get('excess_return')}",
        "",
        "## Brinson attribution",
        "",
        f"- Allocation effect: {brinson.get('allocation_effect')}",
        f"- Selection effect: {brinson.get('selection_effect')}",
        f"- Interaction effect: {brinson.get('interaction_effect')}",
        f"- Total explained excess return: {brinson.get('total_explained_excess_return')}",
        f"- Residual or rounding error: {brinson.get('residual_or_rounding_error')}",
        "",
        "## Industry breakdown",
        "",
        "| Industry | Portfolio W | Benchmark W | Portfolio R | Benchmark R | Allocation | Selection | Interaction | Total |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in brinson.get("by_industry") or []:
        lines.append(
            f"| {row.get('industry')} | {row.get('portfolio_weight')} | {row.get('benchmark_weight')} | "
            f"{row.get('portfolio_return')} | {row.get('benchmark_return')} | {row.get('allocation_effect')} | "
            f"{row.get('selection_effect')} | {row.get('interaction_effect')} | {row.get('total_effect')} |"
        )
    lines += ["", "## Factor attribution", ""]
    if factor.get("available"):
        lines.append("| Factor | Beta | Contribution | Contribution % |")
        lines.append("|---|---:|---:|---:|")
        for row in factor.get("factors") or []:
            lines.append(f"| {row.get('factor')} | {row.get('beta')} | {row.get('contribution')} | {row.get('contribution_pct')} |")
        lines.append(f"- Residual alpha: {factor.get('residual_alpha')}")
    else:
        lines.append("- unavailable")
    for title, key in (("Warnings", "warnings"), ("Risk flags", "risk_flags")):
        lines += ["", f"## {title}", ""]
        items = payload.get(key) or []
        lines.extend([f"- {item}" for item in items] or ["- none"])
    lines += ["", "## Disclaimer", "", str(payload.get("disclaimer") or DISCLAIMER)]
    return "\n".join(lines) + "\n"


def write_risk_attribution_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    out_dir = _project_path(output_dir) or PROJECT_ROOT / "results"
    rep_dir = _project_path(report_dir) or PROJECT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_json = out_dir / "risk_attribution_latest.json"
    timestamp_json = out_dir / f"risk_attribution_{ts}.json"
    latest_report = rep_dir / "risk_attribution_latest.md"
    timestamp_report = rep_dir / f"risk_attribution_{ts}.md"
    output_paths = {
        "latest_json": _display_path(latest_json),
        "timestamp_json": _display_path(timestamp_json),
        "latest_report": _display_path(latest_report),
        "timestamp_report": _display_path(timestamp_report),
    }
    enriched = dict(payload)
    enriched["output_paths"] = output_paths
    text = json.dumps(_json_safe(enriched), ensure_ascii=False, indent=2, allow_nan=False)
    latest_json.write_text(text, encoding="utf-8")
    timestamp_json.write_text(text, encoding="utf-8")
    report = render_risk_attribution_markdown(enriched)
    latest_report.write_text(report, encoding="utf-8")
    timestamp_report.write_text(report, encoding="utf-8")
    return _json_safe(enriched)

