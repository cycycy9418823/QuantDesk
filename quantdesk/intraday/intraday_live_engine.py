from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantdesk.config import load_config
from quantdesk.intraday_live_snapshot import build_partial_daily_row, write_partial_daily_snapshot, write_spot_snapshot
from quantdesk.intraday_score import score_intraday_snapshot
from quantdesk.shared import now_text
from quantdesk.strategy_conditions import evaluate_conditions
from quantdesk.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = "Intraday data is provisional. Research-only observation. Not investment advice. No real orders."
DEFAULT_LATEST_JSON = "results/intraday_live_workbench_latest.json"
DEFAULT_LATEST_REPORT = "reports/intraday_live_workbench_latest.md"
DEFAULT_ACCOUNT_STATE_PATH = "state/intraday_live_account.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "network": False,
    "watchlist": [],
    "account_state_path": DEFAULT_ACCOUNT_STATE_PATH,
    "partial_daily_dir": "data/intraday/partial_daily",
    "spot_snapshot_dir": "data/intraday/spot_snapshots",
    "output_dir": "results",
    "report_dir": "reports",
    "spot_refresh_seconds": 60,
    "minute_bar_refresh_seconds": 300,
    "loop_max_iterations": 0,
    "include_intraday_score": True,
    "include_virtual_positions": True,
    "include_condition_plan_status": True,
    "include_sector_heat": True,
    "forbid_partial_in_backtest": True,
    "paper_watchlist_json": "state/paper_watchlist.json",
    "research_only": True,
}


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


def _read_json_file(path: str | Path) -> dict[str, Any]:
    target = _project_path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _display_path(path: str | Path) -> str:
    target = _project_path(path)
    try:
        return str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _daily_exit_evidence_row(daily: dict[str, Any], condition: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Build a daily fallback row for one exit condition.

    Without minute bars, stop-style ``close <= line`` checks should use the
    complete daily low if available, while take-profit-style ``close >= line``
    checks should use the complete daily high. The original close is retained
    for all other checks. This is evidence for paper review only, not a fill
    price or an executable intraday trigger.
    """

    row = dict(daily)
    field = str(condition.get("field") or "")
    op = str(condition.get("op") or "")
    if field not in {"close", "last_price", "latest_price"}:
        return row, "daily_close"
    close = _safe_float(daily.get("close"), None)
    low = _safe_float(daily.get("low"), None)
    high = _safe_float(daily.get("high"), None)
    if op in {"<", "<="} and low is not None:
        row[field] = min(value for value in (low, close) if value is not None)
        row["daily_fallback_price_field"] = "low"
        return row, "daily_low_for_stop_review"
    if op in {">", ">="} and high is not None:
        row[field] = max(value for value in (high, close) if value is not None)
        row["daily_fallback_price_field"] = "high"
        return row, "daily_high_for_take_profit_review"
    if close is not None:
        row[field] = close
    row["daily_fallback_price_field"] = "close"
    return row, "daily_close"


def _evaluate_daily_exit_conditions(daily: dict[str, Any], exit_conditions: list[Any]) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    evidence_sources: list[str] = []
    for raw_condition in exit_conditions:
        if not isinstance(raw_condition, dict):
            continue
        evidence_row, evidence_source = _daily_exit_evidence_row(daily, raw_condition)
        result = evaluate_conditions(evidence_row, [raw_condition], mode="any")
        evidence_sources.append(evidence_source)
        for key, target in (("matched", matched), ("failed", failed), ("missing", missing)):
            for item in result.get(key) or []:
                enriched = dict(item)
                enriched["daily_fallback_evidence"] = evidence_source
                target.append(enriched)
        results.extend(result.get("condition_results") or [])
    return {
        "triggered": bool(matched),
        "matched": matched,
        "failed": failed,
        "missing": missing,
        "condition_results": results,
        "evidence_sources": list(dict.fromkeys(evidence_sources)),
    }


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


def load_intraday_live_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        loaded = load_config()
        config.update(_as_dict(loaded.get("intraday_live_workbench")))
    except Exception:
        pass
    if overrides:
        config.update({key: value for key, value in overrides.items() if value is not None})
    config["network"] = _safe_bool(config.get("network"), False)
    config["research_only"] = True
    config["watchlist"] = [str(item).strip() for item in _as_list(config.get("watchlist")) if str(item).strip()]
    return config


def build_empty_intraday_live_account_state(*, currency: str = "CNY") -> dict[str, Any]:
    timestamp = now_text()
    return {
        "cash": 0.0,
        "positions": [],
        "currency": str(currency or "CNY"),
        "research_only": True,
        "no_broker": True,
        "no_real_orders": True,
        "created_by": "qd live --init",
        "created_at": timestamp,
        "updated_at": timestamp,
        "schema_version": APP_VERSION.split("-", 1)[0],
        "disclaimer": "Intraday virtual account state is research-only. No broker connection. No real orders.",
    }


def init_intraday_live_account_state(
    path: str | Path = DEFAULT_ACCOUNT_STATE_PATH,
    *,
    force: bool = False,
    currency: str = "CNY",
) -> dict[str, Any]:
    target = _project_path(path)
    warnings: list[str] = []
    if target.exists() and not force:
        warnings.append(f"intraday_live_account_state_exists:{_display_path(target)}")
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        return _json_safe({
            "ok": True,
            "status": "warning",
            "task_type": "intraday_live_account_init",
            "generated_at": now_text(),
            "app_version": APP_VERSION,
            "network": False,
            "path": _display_path(target),
            "created": False,
            "overwritten": False,
            "live_account_initialized": True,
            "account_state": _as_dict(existing),
            "warnings": warnings,
            "errors": [],
            "output_paths": {"account_state": _display_path(target)},
            "disclaimer": "Intraday virtual account state is research-only. No broker connection. No real orders.",
        })
    target.parent.mkdir(parents=True, exist_ok=True)
    state = build_empty_intraday_live_account_state(currency=currency)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return _json_safe({
        "ok": True,
        "status": "passed",
        "task_type": "intraday_live_account_init",
        "generated_at": now_text(),
        "app_version": APP_VERSION,
        "network": False,
        "path": _display_path(target),
        "created": True,
        "overwritten": bool(force),
        "live_account_initialized": True,
        "account_state": state,
        "warnings": [],
        "errors": [],
        "output_paths": {"account_state": _display_path(target)},
        "disclaimer": "Intraday virtual account state is research-only. No broker connection. No real orders.",
    })


def load_intraday_live_account_state(
    path: str | Path = DEFAULT_ACCOUNT_STATE_PATH,
) -> tuple[dict[str, Any] | None, list[str]]:
    target = _project_path(path)
    if not target.exists():
        return None, [f"missing_intraday_live_account_state:{_display_path(target)}; suggested_next_action: python scripts/qd.py live --init"]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, [f"invalid_intraday_live_account_state:{exc}"]
    return _as_dict(payload), []


def _market_session(now: datetime | None = None) -> str:
    current = now or datetime.now()
    hhmm = current.hour * 100 + current.minute
    if hhmm < 915:
        return "pre_open"
    if 930 <= hhmm <= 1130 or 1300 <= hhmm <= 1500:
        return "open"
    if 1130 < hhmm < 1300:
        return "lunch_break"
    if hhmm > 1500:
        return "closed"
    return "unknown"


def refresh_spot_snapshot(config: dict[str, Any], *, network: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    watchlist = [str(item) for item in _as_list(config.get("watchlist")) if str(item)]
    synthetic_quotes = _as_list(config.get("synthetic_quotes"))
    if synthetic_quotes:
        return [dict(item) for item in synthetic_quotes if isinstance(item, dict)], warnings
    if not watchlist:
        warnings.append("empty_watchlist: generated empty intraday live snapshot")
        return [], warnings
    if not network:
        warnings.append("network_disabled: realtime quote refresh skipped; use local/synthetic quotes or --network true")
        return [], warnings
    warnings.append("realtime_quote_provider_unavailable: live provider integration is graceful fallback only")
    return [], warnings


def build_intraday_virtual_account_snapshot(account_state: dict[str, Any] | None, quotes: list[dict[str, Any]]) -> dict[str, Any]:
    if not account_state:
        return {"available": False, "equity": None, "unrealized_pnl": None, "positions": [], "warnings": ["missing_intraday_live_account_state"]}
    quote_map = {str(row.get("symbol")): row for row in quotes}
    cash = _safe_float(account_state.get("cash"), 0.0) or 0.0
    positions: list[dict[str, Any]] = []
    market_value = 0.0
    pnl = 0.0
    warnings: list[str] = []
    for raw in _as_list(account_state.get("positions")):
        row = _as_dict(raw)
        symbol = str(row.get("symbol") or "")
        quantity = _safe_float(row.get("quantity"), 0.0) or 0.0
        cost_basis = _safe_float(row.get("cost_basis"), 0.0) or 0.0
        quote = quote_map.get(symbol, {})
        last_price = _safe_float(quote.get("last_price"), None)
        if last_price is None:
            warnings.append(f"missing_quote_for_position:{symbol}")
            last_price = cost_basis
        value = quantity * (last_price or 0.0)
        item_pnl = value - quantity * cost_basis
        market_value += value
        pnl += item_pnl
        positions.append({"symbol": symbol, "quantity": quantity, "last_price": last_price, "market_value": round(value, 4), "unrealized_pnl": round(item_pnl, 4)})
    return {"available": True, "cash": cash, "market_value": round(market_value, 4), "equity": round(cash + market_value, 4), "unrealized_pnl": round(pnl, 4), "positions": positions, "warnings": warnings}


def _daily_condition_plans(config: dict[str, Any]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for key in ("condition_plans", "daily_condition_plans", "daily_degraded_plans"):
        value = config.get(key)
        if isinstance(value, list):
            plans.extend(dict(item) for item in value if isinstance(item, dict))
    paper_plans = config.get("paper_exit_condition_plans")
    if isinstance(paper_plans, list):
        plans.extend(dict(item) for item in paper_plans if isinstance(item, dict))
    if not plans and config.get("paper_watchlist_json"):
        watchlist = _read_json_file(config.get("paper_watchlist_json") or "")
        for item in watchlist.get("items") or []:
            if not isinstance(item, dict):
                continue
            plan = item.get("paper_exit_plan")
            if not isinstance(plan, dict) or not plan.get("paper_exit_candidate"):
                continue
            condition_plan = plan.get("condition_plan")
            if isinstance(condition_plan, dict):
                plans.append(dict(condition_plan))
    return plans


def build_daily_degraded_condition_review(config: dict[str, Any]) -> dict[str, Any]:
    """Review condition plans against complete daily evidence only.

    This is not an intraday trigger, not a fill model, and not an order route.
    It exists so missing minute bars still produce a useful learning review
    instead of making the condition panel look silently broken.
    """
    plans = _daily_condition_plans(config)
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for plan in plans:
        symbol = str(plan.get("symbol") or plan.get("code") or "")
        daily = _as_dict(plan.get("latest_daily") or plan.get("daily") or plan.get("daily_context") or plan)
        entry_conditions = _as_list(plan.get("entry_conditions"))
        exit_conditions = _as_list(plan.get("exit_conditions"))
        entry_result = evaluate_conditions(daily, entry_conditions) if entry_conditions else {"all_matched": False, "matched": [], "failed": [], "missing": []}
        exit_result = _evaluate_daily_exit_conditions(daily, exit_conditions) if exit_conditions else {"triggered": False, "matched": [], "failed": [], "missing": [], "evidence_sources": []}
        attention = bool(entry_result.get("all_matched") or exit_result.get("triggered"))
        items.append(
            {
                "symbol": symbol,
                "name": plan.get("name") or "",
                "strategy": plan.get("strategy") or plan.get("strategy_id") or "",
                "entry_daily_match": bool(entry_result.get("all_matched")),
                "exit_daily_match": bool(exit_result.get("triggered")),
                "entry_matched_count": len(entry_result.get("matched") or []),
                "entry_missing_count": len(entry_result.get("missing") or []),
                "exit_matched_count": len(exit_result.get("matched") or []),
                "exit_missing_count": len(exit_result.get("missing") or []),
                "matched_conditions": list((entry_result.get("matched") or []) + (exit_result.get("matched") or [])),
                "missing_conditions": list((entry_result.get("missing") or []) + (exit_result.get("missing") or [])),
                "daily_fallback_evidence": list(exit_result.get("evidence_sources") or []),
                "review_status": "daily_condition_attention" if attention else "daily_condition_not_matched",
                "triggered": attention,
                "trigger_source": "daily_degraded_review" if attention else "",
                "execution_allowed": False,
                "fill_model": "none_daily_review_only",
                "research_only": True,
                "paper_only": True,
                "no_broker": True,
                "no_real_orders": True,
            }
        )
    if not items:
        warnings.append("no_daily_condition_plans: daily degraded condition review has no local plans")
    return {
        "available": bool(items),
        "items": items,
        "item_count": len(items),
        "attention_count": sum(1 for item in items if item.get("review_status") == "daily_condition_attention"),
        "execution_allowed": False,
        "fill_model": "none_daily_review_only",
        "warnings": warnings,
        "message": "Minute bars are unavailable; this is a complete-daily review only, not an intraday trigger or fill.",
        "disclaimer": "Daily degraded review is research-only. No broker route. No real orders.",
    }


def build_condition_plan_status(config: dict[str, Any], minute_data: dict[str, Any] | None = None) -> dict[str, Any]:
    warnings = []
    if minute_data is None:
        warnings.append("minute_data_unavailable: condition plan trigger preview skipped")
        daily_review = build_daily_degraded_condition_review(config)
        warnings.extend(daily_review.get("warnings") or [])
        attention_items = [
            item for item in daily_review.get("items") or []
            if isinstance(item, dict) and item.get("review_status") == "daily_condition_attention"
        ]
        return {
            "available": False,
            "triggered_count": int(daily_review.get("attention_count") or 0),
            "items": attention_items,
            "warnings": warnings,
            "degraded_to_daily_review": bool(daily_review.get("available")),
            "daily_degraded_review": daily_review,
            "execution_allowed": False,
            "fill_model": "none_minute_data",
            "trigger_source": "daily_degraded_review" if attention_items else "",
            "disclaimer": "Condition status is a preview only; no orders are generated.",
        }
    return {"available": bool(minute_data), "triggered_count": 0, "items": [], "warnings": warnings, "disclaimer": "Condition status is a preview only; no orders are generated."}


def build_sector_heat_preview(quotes: list[dict[str, Any]], sector_map: dict[str, Any] | None = None) -> dict[str, Any]:
    if not sector_map:
        return {"available": False, "items": [], "warnings": ["sector_heat_preview_unavailable: sector heat preview skipped"]}
    buckets: dict[str, list[float]] = {}
    for quote in quotes:
        symbol = str(quote.get("symbol") or "")
        sector = str(sector_map.get(symbol) or "unknown")
        buckets.setdefault(sector, []).append(float(_safe_float(quote.get("change_pct"), 0.0) or 0.0))
    items = [{"sector": key, "avg_change_pct": round(sum(values) / len(values), 4), "symbol_count": len(values)} for key, values in buckets.items()]
    return {"available": True, "items": sorted(items, key=lambda item: item["avg_change_pct"], reverse=True), "warnings": []}


def _load_account_state(path: str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    return load_intraday_live_account_state(path)


def build_intraday_live_workbench(config: dict[str, Any] | None = None, *, once: bool = True, network: bool = False) -> dict[str, Any]:
    cfg = load_intraday_live_config(config)
    cfg["network"] = bool(network)
    generated_at = now_text()
    trade_date = datetime.now().strftime("%Y-%m-%d")
    snapshot_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    warnings: list[str] = []
    errors: list[str] = []
    quotes, quote_warnings = refresh_spot_snapshot(cfg, network=network)
    warnings.extend(quote_warnings)
    partial_rows = [build_partial_daily_row(quote, trade_date=trade_date, snapshot_time=snapshot_time) for quote in quotes]
    partial_result = write_partial_daily_snapshot(partial_rows, trade_date, Path(str(cfg.get("partial_daily_dir") or DEFAULT_CONFIG["partial_daily_dir"])) / f"{trade_date}.csv")
    if partial_result.get("warnings"):
        warnings.extend(str(item) for item in partial_result.get("warnings", []))
    if partial_result.get("errors"):
        errors.extend(str(item) for item in partial_result.get("errors", []))
    spot_result = write_spot_snapshot(quotes, trade_date, snapshot_time, Path(str(cfg.get("spot_snapshot_dir") or DEFAULT_CONFIG["spot_snapshot_dir"])) / trade_date / f"{snapshot_time.replace('-', '').replace(':', '').replace(' ', '_')}.csv")
    score_result = score_intraday_snapshot(partial_rows, config=cfg) if _safe_bool(cfg.get("include_intraday_score"), True) else {"scores": [], "warnings": []}
    warnings.extend(str(item) for item in score_result.get("warnings", []) if item)
    account_state, account_warnings = _load_account_state(str(cfg.get("account_state_path") or DEFAULT_CONFIG["account_state_path"]))
    warnings.extend(account_warnings)
    live_account_initialized = account_state is not None
    virtual_account = build_intraday_virtual_account_snapshot(account_state, quotes) if _safe_bool(cfg.get("include_virtual_positions"), True) else {"available": False, "equity": None, "unrealized_pnl": None, "positions": []}
    warnings.extend(str(item) for item in virtual_account.get("warnings", []) if item)
    condition_status = build_condition_plan_status(cfg) if _safe_bool(cfg.get("include_condition_plan_status"), True) else {"available": False, "triggered_count": 0, "items": []}
    warnings.extend(str(item) for item in condition_status.get("warnings", []) if item)
    sector_heat = build_sector_heat_preview(quotes, _as_dict(cfg.get("sector_map"))) if _safe_bool(cfg.get("include_sector_heat"), True) else {"available": False, "items": []}
    warnings.extend(str(item) for item in sector_heat.get("warnings", []) if item)
    status = "failed" if errors else ("warning" if warnings else "passed")
    payload = {
        "ok": not errors,
        "status": status,
        "task_type": "intraday_live_workbench",
        "generated_at": generated_at,
        "app_version": APP_VERSION,
        "network": bool(network),
        "trade_date": trade_date,
        "snapshot_time": snapshot_time,
        "market_session": _market_session(),
        "watchlist_count": len(_as_list(cfg.get("watchlist"))),
        "quote_count": len(quotes),
        "partial_daily": {"path": partial_result.get("path"), "row_count": partial_result.get("row_count", 0), "is_complete": False},
        "spot_snapshot": {"path": spot_result.get("path"), "row_count": spot_result.get("row_count", 0)},
        "intraday_scores": score_result.get("scores", []),
        "virtual_account_snapshot": virtual_account,
        "live_account_initialized": live_account_initialized,
        "condition_plan_status": condition_status,
        "sector_heat": sector_heat,
        "risk_flags": ["partial_daily_snapshot_provisional"],
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
        "errors": list(dict.fromkeys(str(item) for item in errors if item)),
        "output_paths": {},
        "suggested_next_action": "Run python scripts/qd.py live --init to create a research-only intraday virtual account state." if not live_account_initialized else "",
        "disclaimer": DISCLAIMER,
    }
    return _json_safe(payload)


def render_intraday_live_workbench_markdown(payload: dict[str, Any]) -> str:
    scores = list(payload.get("intraday_scores") or [])
    sector_items = list(_as_dict(payload.get("sector_heat")).get("items") or [])
    condition = _as_dict(payload.get("condition_plan_status"))
    account = _as_dict(payload.get("virtual_account_snapshot"))
    lines = [
        "# Intraday Live Workbench",
        "",
        f"- Status: {payload.get('status')}",
        f"- Snapshot time: {payload.get('snapshot_time')}",
        f"- Market session: {payload.get('market_session')}",
        f"- Watchlist count: {payload.get('watchlist_count')}",
        f"- Quote count: {payload.get('quote_count')}",
        f"- Partial daily rows: {_as_dict(payload.get('partial_daily')).get('row_count', 0)}",
        "",
        "## Intraday Scores",
        "",
        "| Symbol | Score | Basis | Provisional |",
        "| --- | ---: | --- | --- |",
    ]
    if scores:
        for row in scores[:20]:
            lines.append(f"| {row.get('symbol') or '-'} | {row.get('score')} | {row.get('score_basis')} | {str(bool(row.get('is_provisional'))).lower()} |")
    else:
        lines.append("| - | - | partial_daily_snapshot | true |")
    lines += [
        "",
        "## Virtual Account Snapshot",
        "",
        f"- Available: {str(bool(account.get('available'))).lower()}",
        f"- Equity: {account.get('equity')}",
        f"- Unrealized PnL: {account.get('unrealized_pnl')}",
        "",
        "## Condition Plan Status",
        "",
        f"- Available: {str(bool(condition.get('available'))).lower()}",
        f"- Triggered count: {condition.get('triggered_count', 0)}",
        "",
        "## Sector Heat",
        "",
    ]
    if sector_items:
        for item in sector_items[:10]:
            lines.append(f"- {item.get('sector')}: avg_change_pct={item.get('avg_change_pct')}, symbols={item.get('symbol_count')}")
    else:
        lines.append("- Sector heat unavailable.")
    lines += [
        "",
        "## Warnings",
        "",
    ]
    for warning in list(payload.get("warnings") or [])[:20]:
        lines.append(f"- {warning}")
    if not payload.get("warnings"):
        lines.append("- None")
    lines += ["", "## Output Paths", ""]
    for key, value in _as_dict(payload.get("output_paths")).items():
        lines.append(f"- {key}: {value}")
    lines += ["", "## Disclaimer", "", str(payload.get("disclaimer") or DISCLAIMER)]
    return "\n".join(lines) + "\n"


def write_intraday_live_workbench_outputs(
    payload: dict[str, Any],
    *,
    output_dir: str | Path = "results",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = _project_path(output_dir)
    rep_dir = _project_path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    latest_json = out_dir / "intraday_live_workbench_latest.json"
    timestamp_json = out_dir / f"intraday_live_workbench_{ts}.json"
    latest_report = rep_dir / "intraday_live_workbench_latest.md"
    timestamp_report = rep_dir / f"intraday_live_workbench_{ts}.md"
    output_paths = {
        "latest_json": _display_path(latest_json),
        "timestamp_json": _display_path(timestamp_json),
        "latest_report": _display_path(latest_report),
        "timestamp_report": _display_path(timestamp_report),
    }
    enriched = dict(payload)
    enriched["output_paths"] = output_paths
    latest_json.write_text(json.dumps(enriched, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    timestamp_json.write_text(json.dumps(enriched, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report = render_intraday_live_workbench_markdown(enriched)
    latest_report.write_text(report, encoding="utf-8")
    timestamp_report.write_text(report, encoding="utf-8")
    return enriched


def load_intraday_live_latest_snapshot(
    path: str | Path = DEFAULT_LATEST_JSON,
    *,
    stale_minutes: int = 10,
) -> dict[str, Any]:
    target = _project_path(path)
    if not target.exists():
        return {
            "available": False,
            "status": "missing",
            "warnings": [f"missing_intraday_live_workbench_latest:{_display_path(target)}"],
            "live_account_initialized": _project_path(DEFAULT_ACCOUNT_STATE_PATH).exists(),
            "latest_json": _display_path(target),
            "latest_report": DEFAULT_LATEST_REPORT,
            "disclaimer": DISCLAIMER,
        }
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "status": "failed", "warnings": [f"invalid_intraday_live_latest:{exc}"], "latest_json": _display_path(target), "disclaimer": DISCLAIMER}
    generated = str(payload.get("generated_at") or payload.get("snapshot_time") or "")
    warnings = [str(item) for item in payload.get("warnings", []) if item]
    stale = False
    try:
        dt = datetime.fromisoformat(generated.replace("Z", ""))
        stale = datetime.now() - dt > timedelta(minutes=stale_minutes)
    except Exception:
        warnings.append("intraday_live_snapshot_time_unparseable")
    if stale:
        warnings.append(f"intraday_live_snapshot_stale_over_{stale_minutes}m")
    return {
        "available": True,
        "status": "warning" if warnings else str(payload.get("status") or "passed"),
        "snapshot_time": payload.get("snapshot_time") or generated,
        "market_session": payload.get("market_session") or "",
        "watchlist_count": int(payload.get("watchlist_count") or 0),
        "quote_count": int(payload.get("quote_count") or 0),
        "intraday_score_count": len(list(payload.get("intraday_scores") or [])),
        "triggered_condition_count": int(_as_dict(payload.get("condition_plan_status")).get("triggered_count") or 0),
        "sector_heat_count": len(list(_as_dict(payload.get("sector_heat")).get("items") or [])),
        "warnings": list(dict.fromkeys(warnings)),
        "live_account_initialized": bool(payload.get("live_account_initialized", _project_path(DEFAULT_ACCOUNT_STATE_PATH).exists())),
        "risk_flags": list(payload.get("risk_flags") or [])[:5],
        "latest_json": _display_path(target),
        "latest_report": _as_dict(payload.get("output_paths")).get("latest_report") or DEFAULT_LATEST_REPORT,
        "disclaimer": payload.get("disclaimer") or DISCLAIMER,
    }


def render_daily_intraday_live_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        "## Intraday Live Workbench Snapshot",
        "",
        f"- Status: {snapshot.get('status') or 'missing'}",
        f"- Snapshot time: {snapshot.get('snapshot_time') or '-'}",
        f"- Market session: {snapshot.get('market_session') or '-'}",
        f"- Watchlist count: {snapshot.get('watchlist_count', 0)}",
        f"- Quote count: {snapshot.get('quote_count', 0)}",
        f"- Intraday score count: {snapshot.get('intraday_score_count', 0)}",
        f"- Triggered condition count: {snapshot.get('triggered_condition_count', 0)}",
        f"- Sector heat count: {snapshot.get('sector_heat_count', 0)}",
        f"- Latest JSON: {snapshot.get('latest_json') or '-'}",
        f"- Latest report: {snapshot.get('latest_report') or '-'}",
        "",
        "Warnings / risk flags:",
    ]
    for item in list(snapshot.get("warnings") or [])[:5]:
        lines.append(f"- {item}")
    for item in list(snapshot.get("risk_flags") or [])[:5]:
        lines.append(f"- {item}")
    if not snapshot.get("warnings") and not snapshot.get("risk_flags"):
        lines.append("- None")
    lines += ["", str(snapshot.get("disclaimer") or DISCLAIMER)]
    return "\n".join(lines)
