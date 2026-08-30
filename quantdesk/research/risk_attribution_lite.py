from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.config import load_config
from quantdesk.shared import now_text
from quantdesk.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LATEST_JSON = "results/risk_attribution_lite_latest.json"
DEFAULT_LATEST_REPORT = "reports/risk_attribution_lite_latest.md"
DISCLAIMER = "Research diagnostics only. Not investment advice. Attribution is explanatory, not a trading signal."
OBSERVATION_LITE_DISCLAIMER = (
    "Risk attribution lite is five-dimensional research diagnostics, not Brinson attribution, "
    "not causal proof, not investment advice, no broker, no real orders."
)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "multi_strategy_book_json": "results/multi_strategy_account_book_latest.json",
    "factor_health_json": "results/factor_health_latest.json",
    "combined_score_json": "results/combined_score_latest.json",
    "combined_score_backtest_json": "results/combined_score_backtest_latest.json",
    "sector_rotation_json": "results/sector_rotation_latest.json",
    "research_dashboard_json": "results/research_dashboard_latest.json",
    "pit_universe_json": "results/pit_universe_status_latest.json",
    "intraday_live_json": "results/intraday_live_workbench_latest.json",
    "paper_portfolio_json": "results/auto_paper_portfolio_latest.json",
    "use_paper_portfolio_fallback": True,
    "stale_hours": 48,
    "include_sub_account_attribution": True,
    "include_sector_exposure": True,
    "include_factor_support": True,
    "include_pit_data_risk": True,
    "include_intraday_context": True,
    "include_risk_flags": True,
    "max_preview_items": 10,
}


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        number = int(float(value))
    except Exception:
        return default
    return number


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
            pass
    return value


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d_%H%M%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


def _is_stale(generated_at: Any, stale_hours: float | None) -> bool:
    if stale_hours is None:
        return False
    dt = _parse_dt(generated_at)
    if dt is None:
        return False
    return datetime.now() - dt > timedelta(hours=max(0.0, float(stale_hours)))


def _dedupe(items: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if item))


def load_risk_attribution_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        loaded = load_config()
        config.update(_as_dict(loaded.get("risk_attribution_lite")))
    except Exception:
        pass
    if overrides:
        config.update({key: value for key, value in overrides.items() if value is not None and value != ""})
    return config


def load_json_payload(
    path: str | Path,
    *,
    expected_task_type: str | None = None,
    stale_hours: float | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    target = _project_path(path)
    meta = {
        "path": _display_path(target),
        "status": "missing",
        "warnings": [f"missing_input_json:{_display_path(target)}"],
        "generated_at": "",
        "stale": False,
    }
    if not target.exists():
        return None, meta
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        meta["status"] = "invalid"
        meta["warnings"] = [f"invalid_input_json:{_display_path(target)}:{exc}"]
        return None, meta
    if not isinstance(payload, dict):
        meta["status"] = "invalid"
        meta["warnings"] = [f"invalid_input_payload:{_display_path(target)}"]
        return None, meta
    warnings: list[str] = []
    if expected_task_type and payload.get("task_type") != expected_task_type:
        warnings.append(f"task_type_mismatch:{expected_task_type}:{payload.get('task_type') or 'missing'}")
        meta["status"] = "task_type_mismatch"
    else:
        meta["status"] = "available"
    generated_at = str(payload.get("generated_at") or "")
    stale = _is_stale(generated_at, stale_hours)
    if stale:
        warnings.append(f"stale_input:{_display_path(target)}:{generated_at}")
        meta["status"] = "stale" if meta["status"] == "available" else meta["status"]
    meta.update({"warnings": warnings, "generated_at": generated_at, "stale": stale})
    return payload, meta


def _book_position_count(book: dict[str, Any] | None) -> int:
    total = 0
    for sleeve in _as_list(_as_dict(book).get("sub_accounts")):
        total += len(_as_list(_as_dict(sleeve).get("positions")))
    return total


def _paper_position_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = _as_dict(payload)
    candidates = [
        raw.get("positions"),
        _as_dict(raw.get("portfolio")).get("positions"),
        _as_dict(raw.get("account")).get("positions"),
    ]
    for value in candidates:
        if isinstance(value, dict):
            return [_as_dict(item) for item in value.values()]
        if isinstance(value, list):
            return [_as_dict(item) for item in value]
    return []


def _strategy_sub_account_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "board" in text or "tactical" in text or "momentum" in text:
        return "tactical"
    if "swing" in text:
        return "swing"
    return "long_term"


def _normalize_paper_position(row: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or row.get("code") or "").strip()
    if not symbol:
        return None
    quantity = _safe_float(row.get("quantity") or row.get("shares"), 0.0) or 0.0
    last_price = _safe_float(
        row.get("last_price")
        or row.get("latest_price")
        or row.get("current_price")
        or row.get("close")
        or row.get("entry_price"),
        None,
    )
    market_value = _safe_float(row.get("market_value") or row.get("current_value"), None)
    if market_value is None and last_price is not None:
        market_value = quantity * last_price
    return _json_safe({
        "symbol": symbol,
        "name": row.get("name") or "",
        "quantity": float(quantity),
        "last_price": last_price,
        "cost_basis": _safe_float(row.get("cost_basis") or row.get("entry_price"), None),
        "market_value": float(market_value or 0.0),
        "unrealized_pnl": _safe_float(row.get("unrealized_pnl"), None),
        "sector": row.get("sector") or "",
        "industry": row.get("industry") or "",
        "strategy_type": row.get("strategy_type") or row.get("strategy") or "",
        "source": "auto_paper_portfolio",
        "research_only": True,
    })


def _load_paper_portfolio_positions(path: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    target = _project_path(path)
    if not target.exists():
        return [], [f"paper_portfolio_fallback_missing:{_display_path(target)}"]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [], [f"paper_portfolio_fallback_invalid:{_display_path(target)}:{exc}"]
    positions: list[dict[str, Any]] = []
    for raw in _paper_position_rows(payload if isinstance(payload, dict) else {}):
        normalized = _normalize_paper_position(raw)
        if normalized:
            positions.append(normalized)
    if not positions:
        return [], [f"paper_portfolio_fallback_empty:{_display_path(target)}"]
    return positions, []


def _attach_paper_portfolio_fallback(
    account_book: dict[str, Any] | None,
    *,
    path: str | Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    book = _as_dict(account_book)
    if not book or _book_position_count(book) > 0:
        return account_book, {"used": False, "position_count": 0, "warnings": []}
    positions, load_warnings = _load_paper_portfolio_positions(path)
    if not positions:
        return account_book, {"used": False, "position_count": 0, "warnings": load_warnings}
    sub_accounts = [_as_dict(row) for row in _as_list(book.get("sub_accounts"))]
    if not sub_accounts:
        sub_accounts = [
            {"name": "long_term", "weight": 1.0, "cash": _safe_float(book.get("cash"), 0.0) or 0.0, "positions": []}
        ]
    by_name = {str(row.get("name") or f"sub_{idx}"): dict(row) for idx, row in enumerate(sub_accounts)}
    fallback_name = next(iter(by_name))
    for position in positions:
        target = _strategy_sub_account_name(position.get("strategy_type"))
        if target not in by_name:
            target = fallback_name
        account = by_name[target]
        account.setdefault("positions", [])
        account["positions"] = _as_list(account.get("positions")) + [position]
        by_name[target] = account
    enriched_accounts: list[dict[str, Any]] = []
    for original in sub_accounts:
        name = str(original.get("name") or "")
        account = by_name.get(name, dict(original))
        market_value = sum(_safe_float(pos.get("market_value"), 0.0) or 0.0 for pos in _as_list(account.get("positions")))
        cash = _safe_float(account.get("cash"), 0.0) or 0.0
        account["market_value"] = round(market_value, 4)
        account["equity"] = round(cash + market_value, 4)
        account["position_source"] = "auto_paper_portfolio_fallback" if account.get("positions") else account.get("position_source", "")
        enriched_accounts.append(account)
    enriched = dict(book)
    enriched["sub_accounts"] = _json_safe(enriched_accounts)
    enriched["market_value"] = round(sum(_safe_float(row.get("market_value"), 0.0) or 0.0 for row in enriched_accounts), 4)
    enriched["equity"] = round(sum(_safe_float(row.get("equity"), 0.0) or 0.0 for row in enriched_accounts), 4)
    warnings = _dedupe(load_warnings + [f"paper_portfolio_positions_fallback_used:{len(positions)}"])
    return enriched, {
        "used": True,
        "position_count": len(positions),
        "source": _display_path(path),
        "warnings": warnings,
    }


def compute_sub_account_contribution(account_book: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    book = _as_dict(account_book)
    if not book:
        return {"available": False, "sub_account_count": 0, "contributions": []}, ["missing_multi_strategy_book"]
    sub_accounts = [_as_dict(item) for item in _as_list(book.get("sub_accounts"))]
    portfolio_initial = _safe_float(book.get("initial_cash"), 0.0) or 0.0
    portfolio_equity = _safe_float(book.get("equity"), None)
    if portfolio_equity is None:
        portfolio_equity = sum((_safe_float(row.get("equity"), 0.0) or 0.0) for row in sub_accounts)
        warnings.append("portfolio_equity_missing: summed sub_account equity")
    portfolio_pnl = portfolio_equity - portfolio_initial
    if portfolio_initial <= 0:
        warnings.append("portfolio_initial_cash_non_positive: contribution pct unavailable")
    contributions: list[dict[str, Any]] = []
    for row in sub_accounts:
        initial = _safe_float(row.get("initial_cash"), None)
        equity = _safe_float(row.get("equity"), None)
        if initial is None:
            initial = portfolio_initial * (_safe_float(row.get("weight"), 0.0) or 0.0)
        if equity is None:
            equity = initial
            warnings.append(f"sub_account_equity_missing:{row.get('name') or 'unknown'}")
        pnl = (equity or 0.0) - (initial or 0.0)
        contribution_pct = (pnl / portfolio_initial * 100.0) if portfolio_initial > 0 else None
        equity_share = (equity / portfolio_equity) if portfolio_equity else None
        contributions.append({
            "name": row.get("name") or "",
            "weight": _safe_float(row.get("weight"), 0.0) or 0.0,
            "initial_cash": round(initial or 0.0, 4),
            "equity": round(equity or 0.0, 4),
            "pnl": round(pnl, 4),
            "pnl_contribution_pct": round(contribution_pct, 6) if contribution_pct is not None else None,
            "equity_share": round(equity_share, 6) if equity_share is not None else None,
            "position_count": len(_as_list(row.get("positions"))),
            "warning_count": len(_as_list(row.get("warnings"))),
        })
    return _json_safe({
        "available": bool(sub_accounts),
        "sub_account_count": len(sub_accounts),
        "portfolio_equity": round(portfolio_equity or 0.0, 4),
        "portfolio_initial_cash": round(portfolio_initial, 4),
        "portfolio_pnl": round(portfolio_pnl, 4),
        "contributions": contributions,
    }), warnings


def compute_sector_exposure(account_book: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    book = _as_dict(account_book)
    positions: list[dict[str, Any]] = []
    for sleeve in _as_list(book.get("sub_accounts")):
        for raw in _as_list(_as_dict(sleeve).get("positions")):
            row = _as_dict(raw)
            row["_sub_account"] = _as_dict(sleeve).get("name") or ""
            positions.append(row)
    if not positions:
        return {"available": False, "coverage": 0.0, "total_market_value": 0.0, "sector_exposure": [], "missing_sector_count": 0, "warnings": ["no_positions_available"]}, ["no_positions_available"]
    buckets: dict[str, dict[str, Any]] = {}
    total_value = 0.0
    covered_value = 0.0
    missing_sector_count = 0
    for row in positions:
        sector = str(row.get("sector") or row.get("industry") or "").strip()
        if not sector:
            sector = "unknown"
            missing_sector_count += 1
        value = _safe_float(row.get("market_value"), None)
        if value is None:
            quantity = _safe_float(row.get("quantity"), None)
            last_price = _safe_float(row.get("last_price"), None)
            if quantity is not None and last_price is not None:
                value = quantity * last_price
                warnings.append(f"market_value_estimated:{row.get('symbol') or 'unknown'}")
            else:
                value = 0.0
                warnings.append(f"market_value_missing:{row.get('symbol') or 'unknown'}")
        total_value += value
        if sector != "unknown":
            covered_value += value
        bucket = buckets.setdefault(sector, {"sector": sector, "market_value": 0.0, "symbols": set()})
        bucket["market_value"] += value
        if row.get("symbol"):
            bucket["symbols"].add(str(row.get("symbol")))
    items = []
    for sector, bucket in buckets.items():
        value = float(bucket["market_value"])
        items.append({
            "sector": sector,
            "market_value": round(value, 4),
            "weight": round(value / total_value, 6) if total_value > 0 else 0.0,
            "symbol_count": len(bucket["symbols"]),
        })
    items.sort(key=lambda item: item["market_value"], reverse=True)
    coverage = covered_value / total_value if total_value > 0 else 0.0
    if missing_sector_count:
        warnings.append(f"missing_sector_count:{missing_sector_count}")
    return _json_safe({
        "available": True,
        "coverage": round(coverage, 6),
        "total_market_value": round(total_value, 4),
        "sector_exposure": items,
        "missing_sector_count": missing_sector_count,
        "warnings": _dedupe(warnings),
    }), _dedupe(warnings)


def _extract_factor_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = _as_dict(payload)
    candidates = [
        payload.get("factors"),
        payload.get("factor_results"),
        payload.get("factor_health"),
        _as_dict(payload.get("factor_health_summary")).get("factors"),
        _as_dict(payload.get("summary")).get("factors"),
    ]
    for item in candidates:
        if isinstance(item, list):
            return [_as_dict(row) for row in item]
        if isinstance(item, dict):
            return [_as_dict({"factor": key, **_as_dict(value)}) for key, value in item.items()]
    return []


def compute_factor_support_attribution(
    factor_health: dict[str, Any] | None,
    combined_score: dict[str, Any] | None,
    combined_score_backtest: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    rows = _extract_factor_rows(factor_health)
    if not rows and factor_health:
        summary = _as_dict(factor_health.get("factor_health_summary") or factor_health.get("summary"))
        usable = _safe_int(summary.get("usable_factor_count") or summary.get("usable_count"), 0)
        weak = _safe_int(summary.get("weak_factor_count") or summary.get("weak_count"), 0)
    else:
        usable = 0
        weak = 0
    supported: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("factor") or row.get("name") or row.get("factor_name") or "")
        rank_ic = _safe_float(row.get("rank_ic") or row.get("mean_rank_ic"), None)
        icir = _safe_float(row.get("icir") or row.get("rank_icir") or row.get("ic_ir"), None)
        status = str(row.get("status") or ("usable" if (rank_ic or 0) > 0 else "weak"))
        if "usable" in status.lower():
            usable += 1
        elif status:
            weak += 1
        supported.append({"factor": name, "rank_ic": rank_ic, "icir": icir, "status": status})
    supported = sorted(supported, key=lambda item: abs(_safe_float(item.get("rank_ic"), 0.0) or 0.0), reverse=True)[:10]
    if not factor_health:
        warnings.append("missing_factor_health")
    if not combined_score:
        warnings.append("missing_combined_score")
    if not combined_score_backtest:
        warnings.append("missing_combined_score_backtest")
    metrics = _as_dict(_as_dict(combined_score_backtest).get("metrics") or _as_dict(combined_score_backtest).get("performance") or _as_dict(combined_score_backtest).get("summary"))
    backtest_sharpe = _safe_float(metrics.get("sharpe") or metrics.get("sharpe_ratio"), None)
    backtest_max_drawdown = _safe_float(metrics.get("max_drawdown"), None)
    backtest_return = _safe_float(metrics.get("total_return") or metrics.get("annual_return"), None)
    return _json_safe({
        "available": bool(factor_health or combined_score or combined_score_backtest),
        "usable_factor_count": usable,
        "weak_factor_count": weak,
        "top_supported_factors": supported,
        "combined_score_available": bool(combined_score),
        "backtest_sharpe": backtest_sharpe,
        "backtest_max_drawdown": backtest_max_drawdown,
        "backtest_return": backtest_return,
        "warnings": _dedupe(warnings),
        "disclaimer": "Factor support, not causal proof.",
    }), _dedupe(warnings)


def compute_data_risk_attribution(
    research_dashboard: dict[str, Any] | None,
    pit_universe: dict[str, Any] | None,
    input_status: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    flags: list[str] = []
    pit_payload = _as_dict(pit_universe)
    research_payload = _as_dict(research_dashboard)
    pit_enabled = bool(pit_payload.get("pit_universe") or pit_payload.get("available") or pit_payload.get("ok"))
    if not pit_enabled:
        flags.extend(["pit_universe_unavailable", "survivorship_bias_not_corrected"])
        warnings.append("pit_universe_unavailable: survivorship bias may remain")
    if not research_payload:
        warnings.append("missing_research_dashboard")
    stale_inputs = []
    missing_inputs = []
    for name, meta in _as_dict(input_status).items():
        meta = _as_dict(meta)
        if meta.get("status") == "missing":
            missing_inputs.append(name)
        if meta.get("stale"):
            stale_inputs.append(name)
    return {
        "pit_universe": pit_enabled,
        "survivorship_bias_warning": not pit_enabled,
        "stale_inputs": stale_inputs,
        "missing_inputs": missing_inputs,
        "data_risk_flags": _dedupe(flags),
        "warnings": _dedupe(warnings),
    }, _dedupe(warnings)


def compute_intraday_context_attribution(intraday_live: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    payload = _as_dict(intraday_live)
    if not payload:
        return {"available": False, "is_provisional": True, "top_intraday_scores": [], "warnings": ["missing_intraday_live_context"]}, ["missing_intraday_live_context"]
    scores = [_as_dict(item) for item in _as_list(payload.get("intraday_scores"))]
    scores.sort(key=lambda item: _safe_float(item.get("score"), 0.0) or 0.0, reverse=True)
    condition = _as_dict(payload.get("condition_plan_status"))
    sector = _as_dict(payload.get("sector_heat"))
    return _json_safe({
        "available": True,
        "snapshot_time": payload.get("snapshot_time") or payload.get("generated_at") or "",
        "market_session": payload.get("market_session") or "unknown",
        "watchlist_count": _safe_int(payload.get("watchlist_count"), 0),
        "quote_count": _safe_int(payload.get("quote_count"), 0),
        "intraday_score_count": len(scores),
        "top_intraday_scores": scores[:10],
        "triggered_condition_count": _safe_int(condition.get("triggered_count"), 0),
        "sector_heat_count": len(_as_list(sector.get("items"))),
        "is_provisional": True,
        "warnings": _dedupe(_as_list(payload.get("warnings"))[:5]),
    }), _dedupe(_as_list(payload.get("warnings"))[:5])


def build_risk_attribution_flags(
    *,
    input_meta: dict[str, Any],
    sub_account: dict[str, Any],
    sector: dict[str, Any],
    factor: dict[str, Any],
    data_risk: dict[str, Any],
    intraday: dict[str, Any],
) -> list[str]:
    flags: list[str] = ["research_only_not_investment_advice"]
    if _as_dict(input_meta.get("multi_strategy_book")).get("status") in {"missing", "invalid", "task_type_mismatch"}:
        flags.append("missing_multi_strategy_book")
    if _as_dict(input_meta.get("multi_strategy_book")).get("stale"):
        flags.append("stale_multi_strategy_book")
    if not _as_list(sub_account.get("contributions")):
        flags.append("no_positions_available")
    if not sector.get("available"):
        flags.append("missing_sector_map")
    if (_safe_float(sector.get("coverage"), 0.0) or 0.0) < 0.5:
        flags.append("low_sector_coverage")
    if not factor.get("available"):
        flags.append("missing_factor_health")
    if not factor.get("backtest_sharpe") and "missing_combined_score_backtest" in _as_list(factor.get("warnings")):
        flags.append("missing_combined_score_backtest")
    flags.extend(_as_list(data_risk.get("data_risk_flags")))
    if intraday.get("available"):
        flags.append("intraday_context_provisional")
    for row in _as_list(sub_account.get("contributions")):
        if (_safe_float(_as_dict(row).get("equity_share"), 0.0) or 0.0) > 0.8:
            flags.append("single_sub_account_dominance")
    return _dedupe(flags)


def build_risk_attribution_report(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_risk_attribution_config(config)
    stale_hours = _safe_float(cfg.get("stale_hours"), 48.0)
    inputs = {
        "multi_strategy_book": ("multi_strategy_book_json", "multi_strategy_account_book"),
        "factor_health": ("factor_health_json", None),
        "combined_score": ("combined_score_json", None),
        "combined_score_backtest": ("combined_score_backtest_json", "combined_score_backtest"),
        "sector_rotation": ("sector_rotation_json", "sector_rotation"),
        "research_dashboard": ("research_dashboard_json", "factor_research_dashboard"),
        "pit_universe": ("pit_universe_json", None),
        "intraday_live": ("intraday_live_json", "intraday_live_workbench"),
    }
    payloads: dict[str, dict[str, Any] | None] = {}
    metas: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for name, (config_key, task_type) in inputs.items():
        payload, meta = load_json_payload(str(cfg.get(config_key) or DEFAULT_CONFIG[config_key]), expected_task_type=task_type, stale_hours=stale_hours)
        payloads[name] = payload
        metas[name] = meta
        warnings.extend(_as_list(meta.get("warnings")))
    paper_fallback: dict[str, Any] = {"used": False, "position_count": 0, "warnings": []}
    if bool(cfg.get("use_paper_portfolio_fallback", True)):
        fallback_book, paper_fallback = _attach_paper_portfolio_fallback(
            payloads["multi_strategy_book"],
            path=str(cfg.get("paper_portfolio_json") or DEFAULT_CONFIG["paper_portfolio_json"]),
        )
        if paper_fallback.get("used"):
            payloads["multi_strategy_book"] = fallback_book
        warnings.extend(_as_list(paper_fallback.get("warnings")))
    sub_attr, sub_warnings = compute_sub_account_contribution(payloads["multi_strategy_book"]) if cfg.get("include_sub_account_attribution", True) else ({}, [])
    sector_attr, sector_warnings = compute_sector_exposure(payloads["multi_strategy_book"]) if cfg.get("include_sector_exposure", True) else ({}, [])
    factor_attr, factor_warnings = compute_factor_support_attribution(payloads["factor_health"], payloads["combined_score"], payloads["combined_score_backtest"]) if cfg.get("include_factor_support", True) else ({}, [])
    data_attr, data_warnings = compute_data_risk_attribution(payloads["research_dashboard"], payloads["pit_universe"], metas) if cfg.get("include_pit_data_risk", True) else ({}, [])
    intraday_attr, intraday_warnings = compute_intraday_context_attribution(payloads["intraday_live"]) if cfg.get("include_intraday_context", True) else ({}, [])
    warnings.extend(sub_warnings + sector_warnings + factor_warnings + data_warnings + intraday_warnings)
    flags = build_risk_attribution_flags(input_meta=metas, sub_account=sub_attr, sector=sector_attr, factor=factor_attr, data_risk=data_attr, intraday=intraday_attr)
    errors: list[str] = []
    status = "failed" if errors else ("warning" if warnings or flags else "passed")
    payload = {
        "ok": not errors,
        "status": status,
        "task_type": "risk_attribution_lite",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "network": False,
        "portfolio_name": _as_dict(payloads.get("multi_strategy_book")).get("portfolio_name") or "",
        "inputs": {name: _display_path(str(cfg.get(config_key) or DEFAULT_CONFIG[config_key])) for name, (config_key, _task) in inputs.items()},
        "input_status": {name: meta.get("status") for name, meta in metas.items()},
        "input_metadata": metas,
        "paper_portfolio_fallback": paper_fallback,
        "sub_account_attribution": sub_attr,
        "sector_exposure": sector_attr,
        "factor_support_attribution": factor_attr,
        "data_risk_attribution": data_attr,
        "intraday_context_attribution": intraday_attr,
        "risk_flags": flags,
        "warnings": _dedupe(warnings),
        "errors": errors,
        "assumptions": [
            "Risk attribution lite is explanatory and research-only.",
            "Factor support is not causal proof.",
            "Intraday context is provisional and never treated as a formal daily signal.",
        ],
        "output_paths": {},
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(payload)


def mark_observation_lite_payload(payload: dict[str, Any], *, compatibility_alias: bool = False) -> dict[str, Any]:
    """Mark the lite diagnostics payload without routing through a rename wrapper."""

    marked = dict(payload)
    marked["mode"] = "observation_lite"
    marked["compatibility_alias"] = bool(compatibility_alias)
    if compatibility_alias:
        marked["alias_for"] = "risk_attribution_lite"
    marked["disclaimer"] = OBSERVATION_LITE_DISCLAIMER
    marked["warnings"] = _dedupe([
        *[str(item) for item in marked.get("warnings") or []],
        "risk_attribution_lite_is_not_brinson_attribution",
    ])
    marked["assumptions"] = _dedupe([
        "Risk attribution lite is five-dimensional diagnostics only.",
        "Use qd attribution for true Brinson allocation, selection, and interaction attribution.",
        *[str(item) for item in marked.get("assumptions") or []],
    ])
    return _json_safe(marked)


def render_risk_attribution_markdown(payload: dict[str, Any]) -> str:
    sub = _as_dict(payload.get("sub_account_attribution"))
    sector = _as_dict(payload.get("sector_exposure"))
    factor = _as_dict(payload.get("factor_support_attribution"))
    data = _as_dict(payload.get("data_risk_attribution"))
    intraday = _as_dict(payload.get("intraday_context_attribution"))
    lines = [
        "# Risk Attribution Lite",
        "",
        DISCLAIMER,
        "",
        "Attribution is explanatory, not causal proof. Intraday context is provisional.",
        "",
        "## Status",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Portfolio: `{payload.get('portfolio_name') or '-'}`",
        f"- Generated at: {payload.get('generated_at')}",
        "",
        "## Input Status",
        "",
    ]
    for name, status in _as_dict(payload.get("input_status")).items():
        lines.append(f"- {name}: `{status}`")
    lines += ["", "## Portfolio Summary", ""]
    lines.append(f"- Portfolio equity: {sub.get('portfolio_equity')}")
    lines.append(f"- Portfolio PnL: {sub.get('portfolio_pnl')}")
    lines += ["", "## Sub-account Contribution", ""]
    contributions = _as_list(sub.get("contributions"))
    if contributions:
        lines += ["| Name | Weight | Equity | PnL | PnL Contribution % |", "|---|---:|---:|---:|---:|"]
        for row in contributions[:10]:
            item = _as_dict(row)
            lines.append(f"| {item.get('name')} | {item.get('weight')} | {item.get('equity')} | {item.get('pnl')} | {item.get('pnl_contribution_pct')} |")
    else:
        lines.append("- unavailable")
    lines += ["", "## Sector Exposure", ""]
    exposures = _as_list(sector.get("sector_exposure"))
    if exposures:
        lines += ["| Sector | Market Value | Weight | Symbols |", "|---|---:|---:|---:|"]
        for row in exposures[:10]:
            item = _as_dict(row)
            lines.append(f"| {item.get('sector')} | {item.get('market_value')} | {item.get('weight')} | {item.get('symbol_count')} |")
    else:
        lines.append("- unavailable")
    lines += ["", "## Factor Support", ""]
    lines.append(f"- Usable factors: {factor.get('usable_factor_count', 0)}")
    lines.append(f"- Weak factors: {factor.get('weak_factor_count', 0)}")
    lines.append(f"- Backtest Sharpe: {factor.get('backtest_sharpe')}")
    lines.append(f"- Backtest max drawdown: {factor.get('backtest_max_drawdown')}")
    lines += ["", "## PIT/Data Risk", ""]
    lines.append(f"- PIT universe: {str(bool(data.get('pit_universe'))).lower()}")
    lines.append(f"- Survivorship bias warning: {str(bool(data.get('survivorship_bias_warning'))).lower()}")
    lines += ["", "## Intraday Context", ""]
    lines.append(f"- Available: {str(bool(intraday.get('available'))).lower()}")
    lines.append(f"- Snapshot time: {intraday.get('snapshot_time') or '-'}")
    lines.append(f"- Market session: {intraday.get('market_session') or '-'}")
    lines.append(f"- Intraday score count: {intraday.get('intraday_score_count', 0)}")
    lines.append("- Intraday scores are provisional and are not formal daily scores.")
    for title, key in (("Risk Flags", "risk_flags"), ("Warnings", "warnings"), ("Suggested Research Actions", "assumptions")):
        lines += ["", f"## {title}", ""]
        items = _as_list(payload.get(key))
        if items:
            lines.extend(f"- {item}" for item in items[:10])
        else:
            lines.append("- none")
    lines += ["", "## Output Paths", ""]
    for key, value in _as_dict(payload.get("output_paths")).items():
        lines.append(f"- {key}: {value}")
    lines += ["", "## Disclaimer", "", str(payload.get("disclaimer") or DISCLAIMER)]
    return "\n".join(lines) + "\n"


def write_risk_attribution_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    out_dir = _project_path(output_dir)
    rep_dir = _project_path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_json = out_dir / "risk_attribution_lite_latest.json"
    timestamp_json = out_dir / f"risk_attribution_lite_{ts}.json"
    latest_report = rep_dir / "risk_attribution_lite_latest.md"
    timestamp_report = rep_dir / f"risk_attribution_lite_{ts}.md"
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
    try:
        from quantdesk.experiment_tracker import log_experiment_non_blocking

        sub = _as_dict(enriched.get("sub_account_attribution"))
        factor = _as_dict(enriched.get("factor_support_attribution"))
        tracking = log_experiment_non_blocking(
            "risk_attribution_lite",
            {"portfolio_name": enriched.get("portfolio_name"), "input_status": enriched.get("input_status")},
            {
                "sub_account_count": sub.get("sub_account_count"),
                "portfolio_pnl": sub.get("portfolio_pnl"),
                "usable_factor_count": factor.get("usable_factor_count"),
                "weak_factor_count": factor.get("weak_factor_count"),
                "risk_flag_count": len(_as_list(enriched.get("risk_flags"))),
            },
            output_paths=output_paths,
            tags=["auto_logged"],
        )
        if tracking.get("experiment_id"):
            enriched["experiment_id"] = tracking.get("experiment_id")
        if tracking.get("warning") and not tracking.get("ok"):
            enriched.setdefault("warnings", []).append(tracking["warning"])
    except Exception as exc:
        enriched.setdefault("warnings", []).append(f"experiment_tracker_failed:{exc}")
    if enriched.get("experiment_id") or enriched.get("warnings") != payload.get("warnings"):
        text = json.dumps(_json_safe(enriched), ensure_ascii=False, indent=2, allow_nan=False)
        latest_json.write_text(text, encoding="utf-8")
        timestamp_json.write_text(text, encoding="utf-8")
    return _json_safe(enriched)


def load_risk_attribution_snapshot(
    path: str | Path = DEFAULT_LATEST_JSON,
    *,
    stale_hours: int = 48,
) -> dict[str, Any]:
    payload, meta = load_json_payload(path, expected_task_type="risk_attribution_lite", stale_hours=stale_hours)
    warnings = _as_list(meta.get("warnings"))
    if not payload:
        return {
            "enabled": True,
            "status": meta.get("status") or "missing",
            "portfolio_name": "",
            "input_status": {},
            "top_contributors": [],
            "top_sector_exposures": [],
            "usable_factor_count": 0,
            "weak_factor_count": 0,
            "pit_universe": False,
            "intraday_context_available": False,
            "warning_count": len(warnings),
            "warnings": warnings,
            "risk_flags_preview": [],
            "latest_json": _display_path(path),
            "latest_report": DEFAULT_LATEST_REPORT,
            "stale": bool(meta.get("stale")),
            "disclaimer": DISCLAIMER,
        }
    sub = _as_dict(payload.get("sub_account_attribution"))
    sector = _as_dict(payload.get("sector_exposure"))
    factor = _as_dict(payload.get("factor_support_attribution"))
    data = _as_dict(payload.get("data_risk_attribution"))
    intraday = _as_dict(payload.get("intraday_context_attribution"))
    output_paths = _as_dict(payload.get("output_paths"))
    warnings.extend(_as_list(payload.get("warnings"))[:5])
    return _json_safe({
        "enabled": True,
        "status": "stale" if meta.get("stale") else payload.get("status") or "warning",
        "portfolio_name": payload.get("portfolio_name") or "",
        "input_status": _as_dict(payload.get("input_status")),
        "top_contributors": _as_list(sub.get("contributions"))[:5],
        "top_sector_exposures": _as_list(sector.get("sector_exposure"))[:5],
        "usable_factor_count": _safe_int(factor.get("usable_factor_count"), 0),
        "weak_factor_count": _safe_int(factor.get("weak_factor_count"), 0),
        "pit_universe": bool(data.get("pit_universe")),
        "intraday_context_available": bool(intraday.get("available")),
        "warning_count": len(_dedupe(warnings)),
        "warnings": _dedupe(warnings),
        "risk_flags_preview": _as_list(payload.get("risk_flags"))[:5],
        "latest_json": str(output_paths.get("latest_json") or _display_path(path)).replace("\\", "/"),
        "latest_report": str(output_paths.get("latest_report") or DEFAULT_LATEST_REPORT).replace("\\", "/"),
        "stale": bool(meta.get("stale")),
        "disclaimer": payload.get("disclaimer") or DISCLAIMER,
    })


def render_daily_risk_attribution_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    snapshot = _as_dict(snapshot)
    lines = [
        "## Risk Attribution Snapshot",
        "",
        DISCLAIMER,
        "",
        f"- Status: `{snapshot.get('status') or 'missing'}`",
        f"- Portfolio: `{snapshot.get('portfolio_name') or '-'}`",
        f"- Latest report: {snapshot.get('latest_report') or DEFAULT_LATEST_REPORT}",
        f"- Latest JSON: {snapshot.get('latest_json') or DEFAULT_LATEST_JSON}",
        "",
        "### Input Status",
        "",
    ]
    input_status = _as_dict(snapshot.get("input_status"))
    if input_status:
        lines.extend(f"- {key}: `{value}`" for key, value in input_status.items())
    else:
        lines.append("- unavailable")
    lines += ["", "### Top Contributors", ""]
    contributors = _as_list(snapshot.get("top_contributors"))
    lines.extend(f"- {_as_dict(item).get('name')}: pnl={_as_dict(item).get('pnl')}" for item in contributors[:5]) if contributors else lines.append("- unavailable")
    lines += ["", "### Top Sector Exposures", ""]
    sectors = _as_list(snapshot.get("top_sector_exposures"))
    lines.extend(f"- {_as_dict(item).get('sector')}: weight={_as_dict(item).get('weight')}" for item in sectors[:5]) if sectors else lines.append("- unavailable")
    lines += ["", "### Factor / PIT / Intraday", ""]
    lines.append(f"- Usable factors: {snapshot.get('usable_factor_count', 0)}")
    lines.append(f"- Weak factors: {snapshot.get('weak_factor_count', 0)}")
    lines.append(f"- PIT universe: {str(bool(snapshot.get('pit_universe'))).lower()}")
    lines.append(f"- Intraday context available: {str(bool(snapshot.get('intraday_context_available'))).lower()}")
    for title, key in (("Risk Flags", "risk_flags_preview"), ("Warnings", "warnings")):
        lines += ["", f"### {title}", ""]
        items = _as_list(snapshot.get(key))
        lines.extend(f"- {item}" for item in items[:5]) if items else lines.append("- none")
    lines.append("")
    return "\n".join(lines)
