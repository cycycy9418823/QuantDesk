from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from quantdesk.platform_common import PROJECT_ROOT, base_payload, clean_number, finalize_outputs
from quantdesk.risk.position_inputs import load_paper_positions

STYLE_FACTORS = ["size", "value", "growth", "momentum", "volatility", "liquidity", "beta", "leverage", "profitability", "industry"]
NUMERIC_STYLE_FACTORS = [factor for factor in STYLE_FACTORS if factor != "industry"]
DISCLAIMER = "Style attribution is a local-data research estimate, not a commercial Barra model."


def _resolve(path: str | Path | None) -> Path | None:
    if not path:
        return None
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle)), []
    except Exception as exc:
        return [], [f"csv_invalid:{path.name}:{type(exc).__name__}"]


def _synthetic_positions() -> list[dict]:
    return [
        {"symbol": "600000", "name": "Synthetic Bank", "weight": 0.4, "market_value": 40000, "research_only": True},
        {"symbol": "300750", "name": "Synthetic Growth", "weight": 0.35, "market_value": 35000, "research_only": True},
        {"symbol": "000001", "name": "Synthetic Core", "weight": 0.25, "market_value": 25000, "research_only": True},
    ]


def _synthetic_exposures() -> list[dict]:
    rows = []
    values = {
        "600000": [0.6, 0.9, -0.2, -0.1, 0.1, 0.4, 0.8, 0.2, 0.5, "bank"],
        "300750": [0.2, -0.3, 1.1, 0.7, 0.6, 0.3, 1.2, 0.1, 0.8, "manufacturing"],
        "000001": [0.5, 0.7, 0.0, 0.1, 0.2, 0.5, 1.0, 0.3, 0.4, "bank"],
    }
    for symbol, vals in values.items():
        row = {"symbol": symbol, "date": "2024-12-31", "source": "synthetic"}
        for factor, value in zip(STYLE_FACTORS, vals):
            row[factor] = value
        rows.append(row)
    return rows


def _synthetic_factor_returns() -> list[dict]:
    row = {"date": "2024-12-31", "source": "synthetic"}
    for idx, factor in enumerate(NUMERIC_STYLE_FACTORS):
        row[factor] = round((idx - 3) * 0.001, 6)
    row["industry"] = 0.001
    return [row]


def load_style_exposures(path: str | Path | None = None, config: dict | None = None) -> tuple[list[dict], list[str]]:
    cfg = dict(config or {})
    if cfg.get("synthetic_test_data"):
        return _synthetic_exposures(), ["synthetic_style_exposures_used"]
    target = _resolve(path or cfg.get("exposures_csv") or "data/factors/style_exposures.csv")
    if not target or not target.exists():
        return [], ["style_exposures_csv_missing"]
    rows, warnings = _read_csv(target)
    out: list[dict[str, Any]] = []
    for raw in rows:
        symbol = raw.get("symbol")
        if not symbol:
            continue
        row = {"symbol": str(symbol), "date": raw.get("date") or "", "industry": raw.get("industry") or "", "source": raw.get("source") or "csv"}
        for factor in NUMERIC_STYLE_FACTORS:
            row[factor] = _float(raw.get(factor))
        out.append(row)
    return out, warnings


def load_factor_returns(path: str | Path | None = None, config: dict | None = None) -> tuple[list[dict], list[str]]:
    cfg = dict(config or {})
    if cfg.get("synthetic_test_data"):
        return _synthetic_factor_returns(), ["synthetic_factor_returns_used"]
    target = _resolve(path or cfg.get("factor_returns_csv") or "data/factors/style_factor_returns.csv")
    if not target or not target.exists():
        return [], ["style_factor_returns_csv_missing"]
    rows, warnings = _read_csv(target)
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = {"date": raw.get("date") or "", "source": raw.get("source") or "csv"}
        for factor in STYLE_FACTORS:
            row[factor] = _float(raw.get(factor))
        out.append(row)
    return out, warnings


def _normalize_weights(positions: list[dict]) -> list[dict]:
    rows = [dict(pos) for pos in positions]
    if any(clean_number(row.get("weight"), 0.0) for row in rows):
        return rows
    total_mv = sum(clean_number(row.get("market_value"), 0.0) for row in rows)
    if total_mv > 0:
        for row in rows:
            row["weight"] = clean_number(row.get("market_value"), 0.0) / total_mv
    elif rows:
        equal = 1 / len(rows)
        for row in rows:
            row["weight"] = equal
    return rows


def compute_portfolio_style_exposure(
    positions: list[dict],
    exposures: list[dict],
    *,
    style_factors: list[str] | None = None,
) -> dict:
    factors = style_factors or STYLE_FACTORS
    exposure_by_symbol = {str(row.get("symbol")): row for row in exposures}
    rows = _normalize_weights(positions)
    portfolio_exposure = {factor: 0.0 for factor in factors if factor != "industry"}
    industry_weights: dict[str, float] = {}
    missing: list[str] = []
    covered_weight = 0.0
    for pos in rows:
        symbol = str(pos.get("symbol") or "")
        weight = clean_number(pos.get("weight"), 0.0)
        exp = exposure_by_symbol.get(symbol)
        if not exp:
            missing.append(symbol)
            continue
        covered_weight += weight
        for factor in portfolio_exposure:
            portfolio_exposure[factor] += weight * clean_number(exp.get(factor), 0.0)
        industry = str(exp.get("industry") or pos.get("sector") or "unknown")
        industry_weights[industry] = industry_weights.get(industry, 0.0) + weight
    return {
        "portfolio_style_exposure": {key: round(value, 6) for key, value in portfolio_exposure.items()},
        "industry_exposure_summary": [
            {"industry": key, "weight": round(value, 6)} for key, value in sorted(industry_weights.items(), key=lambda item: abs(item[1]), reverse=True)
        ],
        "coverage": {
            "exposure_coverage_ratio": round(min(1.0, covered_weight), 6),
            "missing_exposure_count": len(missing),
            "missing_symbols": missing,
        },
    }


def estimate_style_factor_returns(
    returns_rows: list[dict],
    exposure_rows: list[dict],
    *,
    style_factors: list[str],
) -> dict:
    by_symbol = {str(row.get("symbol")): row for row in exposure_rows}
    x_rows = []
    y_rows = []
    for row in returns_rows:
        exp = by_symbol.get(str(row.get("symbol")))
        ret = _float(row.get("return") or row.get("period_return"))
        if not exp or ret is None:
            continue
        x_rows.append([clean_number(exp.get(factor), 0.0) for factor in style_factors])
        y_rows.append(ret)
    if len(x_rows) < len(style_factors):
        return {"factor_returns": {}, "warnings": ["insufficient_cross_section_for_lstsq"]}
    if len(style_factors) == 1:
        denom = sum(row[0] * row[0] for row in x_rows)
        if not denom:
            return {"factor_returns": {}, "warnings": ["singular_exposure_matrix"]}
        beta = sum(row[0] * y for row, y in zip(x_rows, y_rows)) / denom
        return {"factor_returns": {style_factors[0]: round(beta, 6)}, "warnings": []}
    # Pure-Python fallback keeps this module independent of optional numerical
    # stacks. Multi-factor estimation is intentionally conservative unless the
    # caller supplies factor_returns directly.
    return {"factor_returns": {}, "warnings": ["multi_factor_lstsq_requires_optional_numpy_or_supplied_factor_returns"]}


def compute_style_attribution(
    positions: list[dict],
    exposures: list[dict],
    factor_returns: list[dict] | None = None,
    *,
    config: dict | None = None,
) -> dict:
    cfg = dict(config or {})
    factors = cfg.get("style_factors") or STYLE_FACTORS
    exposure_payload = compute_portfolio_style_exposure(positions, exposures, style_factors=factors)
    portfolio_exposure = exposure_payload["portfolio_style_exposure"]
    latest_returns = dict((factor_returns or [{}])[-1]) if factor_returns else {}
    returns = {factor: clean_number(latest_returns.get(factor), 0.0) for factor in portfolio_exposure}
    contributions = {factor: round(portfolio_exposure.get(factor, 0.0) * returns.get(factor, 0.0), 6) for factor in portfolio_exposure}
    portfolio_return = cfg.get("portfolio_return")
    residual_alpha = None
    if isinstance(portfolio_return, (int, float)):
        residual_alpha = round(float(portfolio_return) - sum(contributions.values()), 6)
    dominant = sorted(
        [{"factor": key, "exposure": value} for key, value in portfolio_exposure.items()],
        key=lambda item: abs(item["exposure"]),
        reverse=True,
    )[:5]
    ranking = sorted(
        [{"factor": key, "contribution": value} for key, value in contributions.items()],
        key=lambda item: abs(item["contribution"]),
        reverse=True,
    )
    warnings = []
    if exposure_payload["coverage"]["missing_exposure_count"]:
        warnings.append("missing_symbol_style_exposures")
    if not factor_returns:
        warnings.append("style_factor_returns_missing_contributions_zero")
    if residual_alpha is None:
        warnings.append("portfolio_return_missing_residual_alpha_unavailable")
    return {
        **exposure_payload,
        "factor_returns": returns,
        "style_contributions": contributions,
        "residual_alpha": residual_alpha,
        "dominant_style_tilts": dominant,
        "style_contribution_ranking": ranking,
        "warnings": warnings,
    }


def _load_positions(cfg: dict) -> tuple[list[dict], list[str]]:
    if cfg.get("synthetic_test_data"):
        return _synthetic_positions(), ["synthetic_positions_used"]
    positions_json = _resolve(cfg.get("positions_json"))
    if positions_json and positions_json.exists():
        try:
            data = json.loads(positions_json.read_text(encoding="utf-8-sig"))
            rows = data.get("positions") if isinstance(data, dict) else data
            return list(rows or []), []
        except Exception as exc:
            return [], [f"positions_json_invalid:{type(exc).__name__}"]
    payload = load_paper_positions(cfg)
    return list(payload.get("positions") or []), list(payload.get("warnings") or [])


def build_style_attribution_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    positions, warnings = _load_positions(cfg)
    exposures, exposure_warnings = load_style_exposures(cfg.get("exposures_csv"), cfg)
    factor_returns, return_warnings = load_factor_returns(cfg.get("factor_returns_csv"), cfg)
    warnings.extend(exposure_warnings)
    warnings.extend(return_warnings)
    attribution = compute_style_attribution(positions, exposures, factor_returns, config=cfg) if positions else {
        "coverage": {"exposure_coverage_ratio": 0.0, "missing_exposure_count": 0},
        "portfolio_style_exposure": {},
        "factor_returns": {},
        "style_contributions": {},
        "residual_alpha": None,
        "dominant_style_tilts": [],
        "style_contribution_ranking": [],
        "industry_exposure_summary": [],
        "warnings": ["positions_missing"],
    }
    warnings.extend(attribution.get("warnings") or [])
    status = "warning" if warnings else "passed"
    payload = base_payload("style_attribution", status=status, warnings=list(dict.fromkeys(warnings)))
    payload.update({
        "disclaimer": DISCLAIMER,
        "synthetic_test_data": bool(cfg.get("synthetic_test_data")),
        "style_factors": STYLE_FACTORS,
        "factor_count": len(STYLE_FACTORS),
        "position_count": len(positions),
        **{key: value for key, value in attribution.items() if key != "warnings"},
        "items": attribution.get("style_contribution_ranking", []),
    })
    return finalize_outputs(payload, "style_attribution", render_style_attribution_markdown(payload))


def render_style_attribution_markdown(payload: dict) -> str:
    lines = [
        "# Style Attribution",
        "",
        f"- status: `{payload.get('status')}`",
        f"- exposure_coverage_ratio: `{(payload.get('coverage') or {}).get('exposure_coverage_ratio')}`",
        "",
        "| factor | exposure | contribution |",
        "| --- | ---: | ---: |",
    ]
    exposures = payload.get("portfolio_style_exposure") or {}
    contrib = payload.get("style_contributions") or {}
    for factor in NUMERIC_STYLE_FACTORS:
        lines.append(f"| {factor} | {exposures.get(factor)} | {contrib.get(factor)} |")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])[:30]]])
    lines.extend(["", payload.get("disclaimer") or DISCLAIMER])
    return "\n".join(lines)
