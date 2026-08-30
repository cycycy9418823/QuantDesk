from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from quantdesk.platform_common import PROJECT_ROOT, base_payload, clean_number, finalize_outputs
from quantdesk.risk.position_inputs import load_paper_positions

DISCLAIMER = "Scenario estimates are research-only and depend on local data coverage."


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


def _load_daily_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            trade_date = raw.get("date") or raw.get("trade_date")
            close = _float(raw.get("close"))
            if trade_date and close is not None:
                rows.append({
                    "date": str(trade_date),
                    "close": close,
                    "open": _float(raw.get("open")),
                    "high": _float(raw.get("high")),
                    "low": _float(raw.get("low")),
                    "volume": _float(raw.get("volume") or raw.get("vol")),
                })
        rows.sort(key=lambda item: item["date"])
        return rows


def _window_return(rows: list[dict], start_date: str, end_date: str) -> tuple[float | None, float | None]:
    window = [row for row in rows if start_date <= str(row.get("date")) <= end_date and _float(row.get("close")) is not None]
    if len(window) < 2:
        return None, None
    start = _float(window[0].get("close"))
    end = _float(window[-1].get("close"))
    if start in (None, 0) or end is None:
        return None, None
    returns = [float(row["close"]) / start - 1 for row in window if row.get("close") is not None]
    return end / start - 1, min(returns) if returns else None


def load_default_scenario_definitions() -> list[dict]:
    return [
        {"scenario_id": "cn_2015_crash_1", "name": "2015 crash phase 1", "start_date": "2015-06-15", "end_date": "2015-07-08"},
        {"scenario_id": "cn_2015_crash_2", "name": "2015 crash phase 2", "start_date": "2015-08-18", "end_date": "2015-08-26"},
        {"scenario_id": "cn_2020_covid_shock", "name": "2020 covid shock", "start_date": "2020-02-03", "end_date": "2020-02-14"},
        {"scenario_id": "cn_2022_april_drawdown", "name": "2022 April drawdown", "start_date": "2022-04-01", "end_date": "2022-04-29"},
        {"scenario_id": "cn_2024_smallcap_liquidity_shock", "name": "2024 smallcap liquidity shock", "start_date": "2024-01-29", "end_date": "2024-02-08"},
    ]


def _synthetic_positions() -> list[dict]:
    return [
        {"symbol": "600000", "name": "Synthetic Bank", "sector": "financials", "weight": 0.45, "market_value": 45000, "beta": 0.9, "research_only": True},
        {"symbol": "300750", "name": "Synthetic Growth", "sector": "manufacturing", "weight": 0.35, "market_value": 35000, "beta": 1.25, "research_only": True},
        {"symbol": "000001", "name": "Synthetic Core", "sector": "financials", "weight": 0.20, "market_value": 20000, "beta": 1.0, "research_only": True},
    ]


def _synthetic_scenario_market_data(scenario: dict) -> dict:
    severity = {
        "cn_2015_crash_1": -0.26,
        "cn_2015_crash_2": -0.18,
        "cn_2020_covid_shock": -0.08,
        "cn_2022_april_drawdown": -0.12,
        "cn_2024_smallcap_liquidity_shock": -0.16,
    }.get(scenario.get("scenario_id"), -0.1)
    return {
        "scenario_id": scenario.get("scenario_id"),
        "index_return": severity,
        "index_max_drawdown": severity,
        "symbol_returns": {"600000": severity * 0.8, "300750": severity * 1.35, "000001": severity * 0.9},
        "sector_returns": {"financials": severity * 0.85, "manufacturing": severity * 1.2},
        "synthetic": True,
    }


def load_scenario_market_data(scenario: dict, *, config: dict | None = None) -> tuple[dict, list[str]]:
    cfg = dict(config or {})
    warnings: list[str] = []
    if cfg.get("synthetic_test_data"):
        return _synthetic_scenario_market_data(scenario), ["synthetic_scenario_market_data_used"]
    index_dir = _resolve(cfg.get("index_dir") or "data/index")
    candidates = []
    if index_dir:
        candidates = [index_dir / "000300.SH.csv", index_dir / "000905.SH.csv", index_dir / "000852.SH.csv"]
    index_return = None
    index_drawdown = None
    for candidate in candidates:
        if candidate.exists():
            try:
                index_return, index_drawdown = _window_return(_load_daily_csv(candidate), scenario["start_date"], scenario["end_date"])
            except Exception as exc:
                warnings.append(f"index_data_invalid:{candidate.name}:{type(exc).__name__}")
            if index_return is not None:
                break
    if index_return is None:
        warnings.append(f"scenario_index_data_missing:{scenario.get('scenario_id')}")
    return {
        "scenario_id": scenario.get("scenario_id"),
        "index_return": index_return,
        "index_max_drawdown": index_drawdown,
        "symbol_returns": {},
        "sector_returns": {},
        "synthetic": False,
    }, warnings


def _symbol_daily_path(symbol: str, cfg: dict) -> Path:
    daily_dir = _resolve(cfg.get("daily_dir") or "data/daily/forward_adjusted") or (PROJECT_ROOT / "data/daily/forward_adjusted")
    for name in (f"{symbol}.csv", f"{symbol}.SH.csv", f"{symbol}.SZ.csv"):
        candidate = daily_dir / name
        if candidate.exists():
            return candidate
    return daily_dir / f"{symbol}.csv"


def estimate_position_scenario_return(position: dict, scenario_market_data: dict, *, config: dict | None = None) -> dict:
    cfg = dict(config or {})
    symbol = str(position.get("symbol") or "")
    sector = str(position.get("sector") or "")
    scenario_id = scenario_market_data.get("scenario_id")
    scenario = next((item for item in load_default_scenario_definitions() if item["scenario_id"] == scenario_id), None)
    warnings: list[str] = []
    source = "missing"
    scenario_return = None
    max_drawdown = None
    if symbol and cfg.get("synthetic_test_data"):
        scenario_return = (scenario_market_data.get("symbol_returns") or {}).get(symbol)
        max_drawdown = scenario_return
        source = "symbol_history"
    elif symbol and scenario:
        path = _symbol_daily_path(symbol, cfg)
        if path.exists():
            try:
                scenario_return, max_drawdown = _window_return(_load_daily_csv(path), scenario["start_date"], scenario["end_date"])
                if scenario_return is not None:
                    source = "symbol_history"
            except Exception as exc:
                warnings.append(f"symbol_history_invalid:{symbol}:{type(exc).__name__}")
    if scenario_return is None and sector:
        sector_return = (scenario_market_data.get("sector_returns") or {}).get(sector)
        if sector_return is not None:
            beta = clean_number(position.get("sector_beta"), 1.0)
            scenario_return = sector_return * beta
            max_drawdown = scenario_return
            source = "sector_proxy"
    if scenario_return is None and scenario_market_data.get("index_return") is not None:
        beta = clean_number(position.get("beta"), 1.0)
        scenario_return = float(scenario_market_data["index_return"]) * beta
        max_drawdown = scenario_market_data.get("index_max_drawdown")
        source = "index_beta_proxy"
    if scenario_return is None:
        warnings.append(f"scenario_data_missing_for_symbol:{symbol}")
    return {
        "symbol": symbol,
        "name": position.get("name") or "",
        "sector": sector,
        "weight": clean_number(position.get("weight"), 0.0),
        "market_value": position.get("market_value"),
        "scenario_return": round(scenario_return, 6) if isinstance(scenario_return, (int, float)) else None,
        "max_drawdown": round(max_drawdown, 6) if isinstance(max_drawdown, (int, float)) else None,
        "source": source,
        "warnings": warnings,
        "research_only": True,
    }


def estimate_portfolio_scenario_loss(positions: list[dict], scenario: dict, scenario_market_data: dict, *, config: dict | None = None) -> dict:
    cfg = dict(config or {})
    position_results = [estimate_position_scenario_return(pos, scenario_market_data, config=cfg) for pos in positions]
    total_mv = sum(clean_number(pos.get("market_value"), 0.0) for pos in positions)
    if not any(clean_number(pos.get("weight"), 0.0) for pos in positions) and total_mv > 0:
        for pos, result in zip(positions, position_results):
            result["weight"] = clean_number(pos.get("market_value"), 0.0) / total_mv
    estimated_return = sum(clean_number(item.get("weight"), 0.0) * float(item["scenario_return"]) for item in position_results if item.get("scenario_return") is not None)
    estimated_loss_amount = total_mv * estimated_return if total_mv > 0 else None
    sector_map: dict[str, float] = {}
    for item in position_results:
        if item.get("scenario_return") is None:
            continue
        sector = item.get("sector") or "unknown"
        sector_map[sector] = sector_map.get(sector, 0.0) + clean_number(item.get("weight"), 0.0) * float(item["scenario_return"])
    contributors = sorted(
        [item for item in position_results if item.get("scenario_return") is not None],
        key=lambda item: clean_number(item.get("weight"), 0.0) * float(item.get("scenario_return") or 0.0),
    )[:5]
    coverage = {
        "symbol_history_count": sum(1 for item in position_results if item.get("source") == "symbol_history"),
        "sector_proxy_count": sum(1 for item in position_results if item.get("source") == "sector_proxy"),
        "index_proxy_count": sum(1 for item in position_results if item.get("source") == "index_beta_proxy"),
        "missing_count": sum(1 for item in position_results if item.get("source") == "missing"),
    }
    warnings = [warning for item in position_results for warning in item.get("warnings", [])]
    return {
        **scenario,
        "estimated_portfolio_return": round(estimated_return, 6),
        "estimated_loss_amount": round(estimated_loss_amount, 2) if estimated_loss_amount is not None else None,
        "max_drawdown_estimate": min((item.get("max_drawdown") for item in position_results if isinstance(item.get("max_drawdown"), (int, float))), default=None),
        "top_loss_contributors": contributors,
        "sector_breakdown": [{"sector": key, "estimated_return_contribution": round(value, 6)} for key, value in sorted(sector_map.items())],
        "data_coverage": coverage,
        "warnings": warnings,
    }


def _load_positions(cfg: dict) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    if cfg.get("synthetic_test_data"):
        return _synthetic_positions(), ["synthetic_positions_used"]
    positions_json = _resolve(cfg.get("positions_json"))
    if positions_json and positions_json.exists():
        try:
            data = json.loads(positions_json.read_text(encoding="utf-8-sig"))
            rows = data.get("positions") if isinstance(data, dict) else data
            return list(rows or []), warnings
        except Exception as exc:
            return [], [f"positions_json_invalid:{type(exc).__name__}"]
    payload = load_paper_positions(cfg)
    warnings.extend(payload.get("warnings") or [])
    return list(payload.get("positions") or []), warnings


def build_stress_scenarios_report(config: dict | None = None) -> dict:
    cfg = dict(config or {})
    positions, warnings = _load_positions(cfg)
    scenarios = load_default_scenario_definitions()
    if cfg.get("scenario"):
        scenarios = [item for item in scenarios if item["scenario_id"] == cfg.get("scenario")]
    results: list[dict] = []
    for scenario in scenarios:
        market_data, market_warnings = load_scenario_market_data(scenario, config=cfg)
        warnings.extend(market_warnings)
        results.append(estimate_portfolio_scenario_loss(positions, scenario, market_data, config=cfg))
    worst = min(results, key=lambda item: clean_number(item.get("estimated_portfolio_return"), 0.0), default={})
    if not positions:
        warnings.append("positions_missing")
    status = "warning" if warnings else "passed"
    payload = base_payload("stress_test", status=status, warnings=list(dict.fromkeys(warnings)))
    payload.update({
        "disclaimer": DISCLAIMER,
        "synthetic_test_data": bool(cfg.get("synthetic_test_data")),
        "position_count": len(positions),
        "scenario_count": len(results),
        "scenarios": results,
        "items": results,
        "worst_scenario": worst,
    })
    return finalize_outputs(payload, "stress_scenarios", render_stress_scenarios_markdown(payload))


def render_stress_scenarios_markdown(payload: dict) -> str:
    lines = [
        "# Stress Scenarios",
        "",
        f"- status: `{payload.get('status')}`",
        f"- position_count: `{payload.get('position_count')}`",
        "",
        "| scenario | estimated_return | data_missing |",
        "| --- | ---: | ---: |",
    ]
    for item in payload.get("scenarios", []):
        missing = (item.get("data_coverage") or {}).get("missing_count")
        lines.append(f"| {item.get('scenario_id')} | {item.get('estimated_portfolio_return')} | {missing} |")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", *[f"- {item}" for item in payload.get("warnings", [])[:30]]])
    lines.extend(["", payload.get("disclaimer") or DISCLAIMER])
    return "\n".join(lines)
